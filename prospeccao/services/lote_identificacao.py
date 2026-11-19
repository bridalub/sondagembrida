"""Lote pontual de buscas SerpApi para identificação. Não altera o limite diário geral."""
import os
import re
from decimal import Decimal

import requests
from django.db import transaction
from django.db.models import Count, F, Sum
from django.utils import timezone

from prospeccao.conectores.busca_web import SERPAPI_URL, GoogleSerpApi, _sem_chave
from prospeccao.models import ConsultaWeb, Execucao, Fonte, LogExecucao, Oportunidade, SelecaoPesquisa, UsoFonte, UsoIA
from prospeccao.services.descoberta_google import norm_consulta

CHAVE = "lote_serpapi_identificacao"
_RUA = re.compile(r"^(rua|r\.|av\.|avenida|alameda|rodovia|estrada|travessa|praca|praça)\b", re.I)


def explicar_estouro_diario():
    """Por que o contador do dia passou do limite configurado, sem zerar o uso."""
    fonte = Fonte.objects.filter(codigo="google_serpapi").first()
    limite = fonte.max_consultas_dia if fonte else None
    hoje = timezone.localdate()
    uso = UsoFonte.objects.filter(fonte=fonte, dia=hoje).first() if fonte else None
    consultas = uso.consultas if uso else 0
    erros = uso.erros if uso else 0
    cw = ConsultaWeb.objects.filter(provedor="google_serpapi", criada_em__date=hoje)
    por_exec = list(cw.values("execucao_id", "pesquisa_id", "finalidade", "status").annotate(n=Count("id")))
    por_hora = list(
        cw.extra(select={"hora": "extract(hour from criada_em)"}).values("hora").annotate(n=Count("id")).order_by("hora"))
    gravadas = cw.count()
    texto = (
        f"Limite diário geral configurado agora em Fonte.google_serpapi.max_consultas_dia={limite}. "
        f"UsoFonte de {hoje.isoformat()} permanece em {consultas} consulta(s) e {erros} erro(s). Este registro não zera esse contador e não altera o limite. "
        f"ConsultaWeb gravadas hoje com provedor google_serpapi: {gravadas}. "
        f"Por execução, pesquisa, finalidade e status: {por_exec}. Por hora no relógio do banco: {por_hora}. "
    )
    if gravadas == consultas:
        texto += (
            "O contador e as consultas gravadas fecham um a um: não houve retentativa extra nem soma em dobro. "
            "A execução 22 da pesquisa 10 e as execuções 23 e 24 da pesquisa 11 estão nesse total. "
            "O cliente confere o limite antes de cada chamada, sem trava entre processos. "
            "As 28 foram gravadas como OK, então passaram com o limite que a fonte tinha na hora ou o campo foi reduzido a 20 depois; "
            "não há histórico desse campo. O código que cria a fonte usa SERPAPI_MAX_REQUISICOES_DIA, com padrão 48. "
            "O valor 20 fica como está e continua valendo para o fluxo geral."
        )
    else:
        texto += (
            "A diferença entre o contador e as consultas gravadas são tentativas HTTP que não viraram busca concluída. "
            "O limite geral não foi aumentado."
        )
    return texto


def abrir_lote(pesquisa, maximo, auditoria):
    """Cria a execução do lote. Não mexe em max_consultas_dia nem em UsoFonte."""
    fonte = Fonte.objects.filter(codigo="google_serpapi").first()
    limite = fonte.max_consultas_dia if fonte else None
    ex = Execucao.objects.create(
        pesquisa=pesquisa, status=Execucao.Status.PARCIAL, gatilho=Execucao.Gatilho.COMANDO,
        parametros={CHAVE: {
            "maximo": int(maximo), "usadas": 0,
            "motivo": "Autorização pontual para identificar pendências da pesquisa, na franquia incluída da SerpApi.",
            "limite_diario_geral_preservado": limite,
        }})
    LogExecucao.objects.create(execucao=ex, nivel="AVISO", fonte="lote_serpapi", mensagem=auditoria[:4000])
    return ex


def saldo_lote(execucao):
    lote = (execucao.parametros or {}).get(CHAVE) or {}
    return int(lote.get("usadas") or 0), int(lote.get("maximo") or 0)


def reservar_vaga(execucao):
    """Consome uma vaga do lote e soma UsoFonte. Não altera o limite diário."""
    with transaction.atomic():
        ex = Execucao.objects.select_for_update().get(pk=execucao.pk)
        lote = dict((ex.parametros or {}).get(CHAVE) or {})
        usadas, maximo = int(lote.get("usadas") or 0), int(lote.get("maximo") or 0)
        if usadas >= maximo:
            return False
        lote["usadas"] = usadas + 1
        params = dict(ex.parametros or {})
        params[CHAVE] = lote
        ex.parametros = params
        ex.save(update_fields=["parametros"])
        execucao.parametros = params
    fonte = Fonte.objects.filter(codigo="google_serpapi").first()
    if fonte is not None:
        uso, _ = UsoFonte.objects.get_or_create(fonte=fonte, dia=timezone.localdate())
        UsoFonte.objects.filter(pk=uso.pk).update(consultas=F("consultas") + 1)
    return True


def consulta_ja_feita(empresa, consulta):
    alvo = norm_consulta(consulta)
    for q in ConsultaWeb.objects.filter(
            empresa=empresa, provedor="google_serpapi", status="OK").values_list("consulta", flat=True):
        if norm_consulta(q) == alvo:
            return True
    return False


def nome_e_logradouro(nome):
    return bool(_RUA.match((nome or "").strip()))


def montar_consultas(empresa):
    """Variações sustentadas pelos dados já salvos. Não inventa termo."""
    cidade = (empresa.cidade or "").strip()
    uf = (empresa.uf or "").strip()
    local = " ".join(p for p in (cidade, uf) if p)
    nome = []
    sinais = []
    if empresa.nome and local:
        nome.append(f'"{empresa.nome}" "{local}" CNPJ')
    fone = empresa.canais.filter(tipo="TELEFONE").exclude(valor="").values_list("valor", flat=True).first()
    if fone and local:
        sinais.append(f'"{fone}" "{local}" CNPJ')
    site = empresa.canais.filter(tipo="SITE").exclude(valor="").values_list("valor", flat=True).first()
    if site and local:
        sinais.append(f'"{site}" "{local}" CNPJ')
    ordem = (sinais + nome) if nome_e_logradouro(empresa.nome) else (nome + sinais)
    unicas = []
    for q in ordem:
        if q not in unicas:
            unicas.append(q)
    return unicas


def titulo_comercial(titulo, nome_atual, endereco):
    t = (titulo or "").strip()
    if len(t) < 3 or nome_e_logradouro(t):
        return ""
    from carteira.services.normalizacao import normalizar_texto
    nt, nn, ne = normalizar_texto(t), normalizar_texto(nome_atual), normalizar_texto(endereco)
    if not nt or nt == nn or (ne and nt in ne):
        return ""
    return t[:255]


def franquia_permite():
    """None se a próxima busca sairia da franquia incluída. A leitura da conta não gasta busca nem o limite diário."""
    chave = (os.getenv("SERPAPI_API_KEY") or "").strip()
    if not chave:
        return None
    try:
        resp = requests.get("https://serpapi.com/account.json", params={"api_key": chave}, timeout=20)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    d = resp.json()
    conta = {k: d.get(k) for k in (
        "plan_name", "plan_id", "searches_per_month", "plan_searches_left", "extra_credits",
        "total_searches_left", "this_month_usage", "this_hour_searches", "account_rate_limit_per_hour",
        "account_status")}
    if (conta.get("plan_searches_left") or 0) <= 0:
        return None
    if conta.get("account_status") not in (None, "", "Active"):
        return None
    return conta


def buscar_na_franquia(consulta, execucao, empresa):
    """Uma busca orgânica. Custo de IA não é reservado: a franquia incluída não entra no teto em dólar."""
    if consulta_ja_feita(empresa, consulta):
        return "repetida", [], {}
    conta = franquia_permite()
    if conta is None:
        return "sem_franquia", [], {}
    usadas, maximo = saldo_lote(execucao)
    if usadas >= maximo:
        return "sem_lote", [], {}
    if not reservar_vaga(execucao):
        return "sem_lote", [], {}
    chave = (os.getenv("SERPAPI_API_KEY") or "").strip()
    params = {"api_key": chave, "engine": "google", "q": consulta, "hl": "pt-br", "gl": "br",
              "google_domain": "google.com.br", "num": 10}
    try:
        resp = requests.get(SERPAPI_URL, params=params, timeout=40)
        corpo = _sem_chave(resp.text[:240])
    except requests.RequestException as exc:
        ConsultaWeb.objects.create(
            pesquisa_id=execucao.pesquisa_id, execucao=execucao, empresa=empresa, finalidade="COMPLEMENTAR",
            provedor="google_serpapi", consulta=consulta[:300], status="FALHA", erro=_sem_chave(str(exc))[:500],
            custo_estimado_usd=Decimal("0"), analise={"lote": CHAVE, "franquia": "incluida"})
        return "falha", [], {}
    if resp.status_code != 200:
        ConsultaWeb.objects.create(
            pesquisa_id=execucao.pesquisa_id, execucao=execucao, empresa=empresa, finalidade="COMPLEMENTAR",
            provedor="google_serpapi", consulta=consulta[:300], status="FALHA", erro=f"HTTP {resp.status_code} {corpo}"[:500],
            custo_estimado_usd=Decimal("0"), analise={"lote": CHAVE, "franquia": "incluida"})
        baixo = corpo.lower()
        if "run out of searches" in baixo or "your account" in baixo:
            return "sem_franquia", [], {}
        return "falha", [], {}
    dados = resp.json()
    erro = str(dados.get("error") or "")
    if erro:
        ConsultaWeb.objects.create(
            pesquisa_id=execucao.pesquisa_id, execucao=execucao, empresa=empresa, finalidade="COMPLEMENTAR",
            provedor="google_serpapi", consulta=consulta[:300], status="FALHA", erro=_sem_chave(erro)[:500],
            custo_estimado_usd=Decimal("0"), analise={"lote": CHAVE, "franquia": "incluida"})
        baixo = erro.lower()
        if "run out" in baixo or "credit" in baixo or "plan" in baixo:
            return "sem_franquia", [], {}
        return "falha", [], {}
    meta = dados.get("search_metadata") or {}
    saida = GoogleSerpApi._extrair(dados, "organico")
    resultados = [r for r in saida if isinstance(r, dict)]
    ConsultaWeb.objects.create(
        pesquisa_id=execucao.pesquisa_id, execucao=execucao, empresa=empresa, finalidade="COMPLEMENTAR",
        provedor="google_serpapi", consulta=consulta[:300], status="OK", resultados=resultados,
        custo_estimado_usd=Decimal("0"), modelo="serpapi:google",
        acoes=[{"tipo": "google:organico", "consulta": consulta[:300], "request_id": meta.get("id", ""),
                "status": meta.get("status", ""), "lote": CHAVE}],
        analise={"lote": CHAVE, "franquia": "incluida", "request_id": meta.get("id", "")})
    return "ok", resultados, {"request_id": meta.get("id", ""), "status": meta.get("status", "")}


def exibir_conhecidas(pesquisa):
    """Abre ficha na área de conhecidas, sem voltar a ser prospecto novo."""
    from prospeccao.services import cruzamento as cz
    n = 0
    for sel in SelecaoPesquisa.objects.filter(
            pesquisa=pesquisa, fila=SelecaoPesquisa.Fila.CONHECIDA, empresa__isnull=False).select_related("empresa"):
        empresa = sel.empresa
        if not (empresa.salesforce_id or empresa.cnpj):
            continue
        sit, detalhe, _ = cz.situacao_empresa(empresa)
        tipo = (Oportunidade.Tipo.LEAD_CONHECIDO if sit == cz.Situacao.LEAD_CONHECIDO
                else Oportunidade.Tipo.CADASTRO_SEM_COMPRA)
        op = Oportunidade.objects.filter(empresa=empresa).first()
        if op is None:
            op = Oportunidade.objects.create(
                empresa=empresa, tipo=tipo, status=Oportunidade.Status.JA_CLIENTE,
                situacao_carteira=sit or "", cruzamento_detalhe=(detalhe or "")[:2000],
                cruzado_em=timezone.now(), cruzamento_base="SINCRONIZACAO", escopo="")
        elif op.tipo == Oportunidade.Tipo.NOVA_EXTERNA and op.status in (
                Oportunidade.Status.NOVA, Oportunidade.Status.LIBERADA):
            op.tipo = tipo
            op.status = Oportunidade.Status.JA_CLIENTE
            op.situacao_carteira = sit or op.situacao_carteira
            op.save(update_fields=["tipo", "status", "situacao_carteira", "atualizada_em"])
        op.pesquisas.add(pesquisa)
        n += 1
    return n


def custo_ia(pesquisa):
    agg = UsoIA.objects.filter(pesquisa=pesquisa).aggregate(n=Count("id"), usd=Sum("custo_estimado_usd"))
    return int(agg["n"] or 0), agg["usd"] or Decimal("0")
