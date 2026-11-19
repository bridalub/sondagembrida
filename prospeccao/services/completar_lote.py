"""Complementa um lote já descoberto. Não abre descoberta nova e não gasta busca Google nova."""
import os

import requests
from django.utils import timezone

from carteira.services.normalizacao import normalizar_texto
from prospeccao.conectores.base import ContextoExecucao
from prospeccao.conectores.busca_web import GoogleSerpApi
from prospeccao.conectores.site import SitePublico
from prospeccao.models import ConsultaWeb, Execucao, Fonte, SelecaoPesquisa
from prospeccao.services.descoberta_google import _abrivel, investigar_dirigido, registrar_contatos_item
from prospeccao.services.identificacao_cnpj import completar_identificacao
from prospeccao.services.orcamento import Orcamento


def empresas_do_lote(pesquisa):
    vistos, saida = set(), []
    for sel in (SelecaoPesquisa.objects.filter(pesquisa=pesquisa, empresa__isnull=False)
                .select_related("empresa").order_by("empresa_id")):
        if sel.empresa_id in vistos:
            continue
        vistos.add(sel.empresa_id)
        saida.append(sel)
    return saida


def _indice(selecoes):
    por_nome, por_place = {}, {}
    for sel in selecoes:
        chave = normalizar_texto(sel.empresa.nome)
        por_nome[chave] = None if chave in por_nome else sel.empresa
        for ref in sel.empresa.canais.values_list("referencia", flat=True):
            if "place_id:" in (ref or ""):
                por_place[ref.split("place_id:")[-1][:80]] = sel.empresa
    return por_nome, por_place


def recuperar_arquivo_pago(pesquisa, selecoes):
    """Relê o JSON já pago. Não soma o limite diário e não cria empresa."""
    rel = {"arquivos": 0, "whatsapp": 0, "erros": []}
    chave = (os.getenv("SERPAPI_API_KEY") or "").strip()
    if not chave:
        rel["erros"].append("arquivo pago não relido: chave ausente")
        return rel
    por_nome, por_place = _indice(selecoes)
    vistos = set()
    for cw in ConsultaWeb.objects.filter(pesquisa=pesquisa, provedor="google_serpapi", status="OK").order_by("pk"):
        acao = (cw.acoes or [{}])[0] if isinstance(cw.acoes, list) and cw.acoes else {}
        rid = str(acao.get("request_id") or "")
        if not rid or rid in vistos:
            continue
        vistos.add(rid)
        try:
            resp = requests.get(f"https://serpapi.com/searches/{rid}.json", params={"api_key": chave}, timeout=25)
            resp.raise_for_status()
            dados = resp.json()
        except Exception as exc:
            rel["erros"].append(f"{rid[:12]}: {type(exc).__name__}")
            continue
        rel["arquivos"] += 1
        for item in GoogleSerpApi._extrair(dados if isinstance(dados, dict) else {}, "local"):
            if not (item.get("whatsapp") or item.get("whatsapp_sem_numero")):
                continue
            empresa = por_place.get(item.get("place_id") or "") or por_nome.get(normalizar_texto(item.get("nome_citado")))
            if empresa is None:
                continue
            registrar_contatos_item(empresa, item, cw.consulta or "")
            rel["whatsapp"] += len(item.get("whatsapp") or []) + len(item.get("whatsapp_sem_numero") or [])
    return rel


def reler_sites(selecoes, ctx):
    fonte = Fonte.objects.filter(codigo="site_publico", habilitada=True).first()
    rel = {"lidos": 0, "sem_site": 0, "erros": []}
    if fonte is None:
        rel["erros"].append("leitor de site desligado")
        return rel
    leitor = SitePublico(fonte)
    from prospeccao.services import contatos as ct
    for sel in selecoes:
        empresa = sel.empresa
        urls = []
        for valor in empresa.canais.filter(tipo="SITE").values_list("valor", flat=True):
            if valor and _abrivel(valor) and valor not in urls:
                urls.append(valor)
        if not urls:
            rel["sem_site"] += 1
            continue
        resultado = {"site": None, "paginas": [], "tentativas": [], "revisar": [], "nomes_alt": []}
        leitura = {}
        try:
            ct._tentar_sites(empresa, leitor, ctx, [(u, "releitura do site já indicado") for u in urls[:2]],
                             resultado, leitura)
            rel["lidos"] += 1
        except Exception as exc:
            rel["erros"].append(f"{empresa.pk}: {type(exc).__name__}")
    return rel


def sintetizar(selecoes, pesquisa, execucao):
    """Uma síntese por empresa, sem busca Google nova."""
    orc = Orcamento(pesquisa, execucao)
    ctx = ContextoExecucao(execucao)
    fonte = Fonte.objects.filter(codigo="site_publico", habilitada=True).first()
    leitor = SitePublico(fonte) if fonte else None
    rel = {"ia": 0, "motivos": {}}
    for sel in selecoes:
        if sel.empresa.salesforce_id or sel.fila == SelecaoPesquisa.Fila.CONHECIDA:
            continue
        info = investigar_dirigido(
            sel.empresa, sel, pesquisa, None, leitor, orc, ctx, max_buscas=0, reler_sites=False)
        rel["ia"] += 1
        acao = ((info.get("passos") or [{}])[-1].get("acao") or "sem_acao")
        rel["motivos"][acao] = rel["motivos"].get(acao, 0) + 1
        ident = info.get("identificacao") or {}
        if ident.get("motivo"):
            rel["motivos"][ident.get("acao") or "identificacao"] = rel["motivos"].get(ident.get("acao") or "identificacao", 0)
    return rel


def identificar_sem_busca(selecoes):
    motivos = {}
    for sel in selecoes:
        if sel.empresa.salesforce_id:
            motivos["conhecida"] = motivos.get("conhecida", 0) + 1
            continue
        info = completar_identificacao(sel.empresa, sel, max_buscas=0)
        chave = info.get("acao") or "sem_acao"
        motivos[chave] = motivos.get(chave, 0) + 1
    return motivos


def foto(selecoes):
    linhas = []
    for sel in selecoes:
        e = sel.empresa
        wa = list(e.canais.filter(whatsapp_explicito=True).values_list("valor", "referencia"))
        links = list(e.dados_coletados.filter(campo="link_whatsapp_publicado").values_list("valor", flat=True))
        linhas.append({
            "id": e.pk, "nome": e.nome, "fila": sel.fila, "cnpj": e.cnpj or "",
            "situacao": e.situacao_cadastral or "", "salesforce": e.salesforce_id or "",
            "whatsapp": wa, "links": links,
            "motivo": (e.dados_coletados.filter(campo="motivo_encerramento_identificacao")
                       .order_by("-id").values_list("valor", flat=True).first() or ""),
        })
    return linhas


def conferir_publicacoes(urls):
    """Confere o leitor na publicação atual. Não grava cadastro de empresa."""
    fonte = Fonte.objects.filter(codigo="site_publico", habilitada=True).first()
    if fonte is None:
        return [{"url": u, "erro": "leitor desligado"} for u in urls]
    leitor = SitePublico(fonte)
    ctx = ContextoExecucao()
    saida = []
    for url in urls:
        try:
            res = leitor.analisar(url, ctx)
        except Exception as exc:
            saida.append({"url": url, "erro": f"{type(exc).__name__}: {exc}"[:180]})
            continue
        saida.append({
            "url": url,
            "paginas": [p[0] for p in res.paginas],
            "whatsapp": [{"numero": n, "url": u} for n, u in res.whatsapp.items()],
            "links_sem_numero": list(res.links_whatsapp),
            "cnpjs": list(res.cnpjs),
            "classes": [d.get("classe") for d in res.diagnostico],
            "erros": res.erros[:3],
        })
    return saida


def abrir_execucao(pesquisa):
    return Execucao.objects.create(
        pesquisa=pesquisa, status=Execucao.Status.EXECUTANDO, gatilho=Execucao.Gatilho.COMANDO,
        iniciada_em=timezone.now(),
        parametros={"complementacao_lote": {"origem_execucao": 3, "descoberta": False,
                                            "motivo": "Completar WhatsApp, CNPJ e situação do lote já encontrado"}})
