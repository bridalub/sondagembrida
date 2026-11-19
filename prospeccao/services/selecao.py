"""
Pesquisa PLANEJADA: seleção e investigação.

A quantidade desejada é o TOTAL de novos leads da pesquisa (frentes e municípios somados),
não uma cota por frente. Empresas já conhecidas ou repetidas não consomem essa meta.

ETAPA 1 – seleção inicial (base Receita local + busca web), em lotes:
  porta de entrada = CNAE principal OU secundário do plano OU termo de operação no nome.
  Cada lote é gravado e pode ser investigado antes do fim da varredura.
  Conhecidos (contas/leads BRIDA, inclusive bloqueadas) vão para a fila "Conhecida".
  Resultado sem CNPJ vira candidata pendente — não é descartado e não entra na meta.

ETAPA 2 – investigação (até a meta da pesquisa, dentro dos limites da execução):
  identifica o CNPJ na base local, cruza com a carteira em lote, procura site e canais.
"""
import re
import time

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from carteira.models import Abordagem, CanalEmpresa, Empresa, LeadSalesforce
from carteira.services.normalizacao import dominio_de_email, normalizar_texto
from prospeccao import conectores
from prospeccao.conectores.base import OrcamentoEsgotado, PrazoEsgotado
from prospeccao.models import Candidato, Evidencia, Fonte, Oportunidade, SelecaoPesquisa, UsoIA, linhas
from prospeccao.services import contatos as ct
from prospeccao.services import escopo as esc
from prospeccao.services import cruzamento as cz
from prospeccao.services import evidencias as ev
from prospeccao.services import oportunidades as ops
from prospeccao.services import planejamento

SITS = {"ATIVA": "02", "SUSPENSA": "03", "INAPTA": "04", "BAIXADA": "08", "NULA": "01"}
CONHECIDAS = (cz.Situacao.CLIENTE_CARTEIRA, cz.Situacao.CADASTRO_SEM_COMPRA, cz.Situacao.LEAD_CONHECIDO)
LOTE_PROGRESSO = 5
_DIST = re.compile(r"distribuid\w*\s+de\s+(lubrific|grax|filtro|oleo)", re.I)


# ------------------------------------------------------------------ etapa 1

def _conhecidos():
    sf = dict(Empresa.objects.filter(salesforce_id__isnull=False, cnpj__isnull=False).values_list("cnpj", "situacao_protheus"))
    leads = set(LeadSalesforce.objects.exclude(cnpj__isnull=True).values_list("cnpj", flat=True))
    return sf, leads


def pontuar(e, cfg, pesos):
    """(pontuação, motivos). e = dict de EstabelecimentoReceita."""
    codigos = {c["codigo"]: c["descricao"] for c in cfg.get("cnaes", [])}
    pts, mot = 0.0, []
    if e["cnae_principal"] in codigos:
        pts += pesos["cnae_principal"]
        mot.append(f"Atividade principal {e['cnae_principal']} – {codigos[e['cnae_principal']][:70]}")
    sec = [c for c in (e["cnaes_secundarios"] or "").split() if c in codigos]
    if sec:
        pts += pesos["cnae_secundario"]
        mot.append(f"Atividade secundária {sec[0]} – {codigos[sec[0]][:60]}" + (f" (+{len(sec) - 1})" if len(sec) > 1 else ""))
    nome = f" {e['razao_social']} {e['nome_fantasia']} ".upper()
    termos = [t for t in cfg.get("termos", []) if t and t in nome][:2]
    if termos:
        pts += pesos["termo_nome"] * len(termos)
        mot.append(f"Nome indica a operação ({', '.join(termos)})")
    if e["matriz"]:
        pts += pesos["matriz"]
        mot.append("Matriz")
    if e["porte"] == "DEMAIS":
        pts += pesos["porte_demais"]
        mot.append("Porte 'demais' (não ME/EPP)")
    elif e["porte"] == "EMPRESA DE PEQUENO PORTE":
        pts += pesos["porte_epp"]
    if e["natureza_codigo"] == "2135":
        pts += pesos["empresario_individual"]
        mot.append("Empresário individual/MEI (preferência menor, não exclui)")
    if dominio_de_email(e["email"]):
        pts += pesos["dominio_proprio"]
        mot.append("E-mail cadastral com domínio próprio")
    if e["telefone1"]:
        pts += pesos["telefone"]
    return round(pts, 2), mot


def _excluido(e, plano):
    exc = plano.conteudo.get("exclusoes_explicitas") or {}
    nome = f"{e['razao_social']} {e['nome_fantasia']}".upper()
    for t in exc.get("termos", []):
        if t and t.upper() in nome:
            return f"exclusão explícita do gestor: termo '{t}'"
    for c in exc.get("cnaes", []):
        c = "".join(ch for ch in c if ch.isdigit())
        if c and e["cnae_principal"].startswith(c):
            return f"exclusão explícita do gestor: CNAE {c}"
    return ""


def frente_e_revenda(frente):
    nome = f"{getattr(frente, 'nome', '')} {getattr(frente, 'descricao', '')}".lower()
    return "revenda" in nome or "distribui" in nome


def consulta_permitida(consulta, frente=None):
    """Consumidores da frente, não a própria prateleira de lubrificantes — salvo frente de revenda."""
    if frente is not None and frente_e_revenda(frente):
        return True
    return _DIST.search(consulta or "") is None


def consultas_descoberta(cfg, pesquisa, limite=None, frente=None):
    """Consultas da frente, repartidas entre os municípios, dentro do limite (total, não por cidade)."""
    limite = pesquisa.max_buscas_descoberta if limite is None else limite
    cidades = linhas(pesquisa.cidades) or [""]
    base = [q for q in cfg.get("consultas_descoberta", []) if q and "{" not in q and consulta_permitida(q, frente)]
    if not base:
        bruto = list(cfg.get("termos") or []) + list(cfg.get("sinais") or []) + list(cfg.get("atividades") or [])
        termos = []
        for t in bruto:
            t = (t or "").strip()
            if len(t) >= 4 and consulta_permitida(t, frente) and t.lower() not in termos:
                termos.append(t.lower())
        base = termos[:limite] or [f"{(cfg.get('frente') or 'empresa').split()[0].lower()}"]
    saida, i = [], 0
    teto = max(len(base) * max(len(cidades), 1), 1)
    while len(saida) < limite and i < teto:
        termo = re.sub(r"\s+", " ", base[i % len(base)]).strip()
        ja_cita = any(c and c.lower() in termo.lower() for c in cidades)
        if ja_cita:
            item = termo
        else:
            cidade = cidades[(i // len(base)) % len(cidades)]
            item = f"{termo} {cidade} {pesquisa.uf}".strip() if cidade else termo
        item = re.sub(r"\s+", " ", item)
        if item not in saida and consulta_permitida(item, frente):
            saida.append(item)
        i += 1
    return saida[:limite]


_CAMPOS_E = ["cnpj", "cnpj_basico", "razao_social", "nome_fantasia", "cnae_principal", "cnaes_secundarios", "matriz",
             "porte", "natureza_codigo", "email", "telefone1", "municipio_codigo", "situacao_codigo"]


def _nome_bate(e, nome):
    from carteira.services.normalizacao import normalizar_texto
    from prospeccao.services.identificacao import tokens_distintivos
    tokens = {t for t in tokens_distintivos(e["razao_social"]) | tokens_distintivos(e["nome_fantasia"]) if len(t) >= 4}
    n = f" {normalizar_texto(nome or '')} "
    return bool(tokens) and any(f" {t} " in n for t in tokens)


def _cnpjs_do_resultado(r, site, ctx):
    """[(cnpj, como)] a partir do resultado: CNPJ lido no próprio site (não diretório), CNPJ citado pela busca
    (pista) ou CNPJ no trecho do buscador. Nada é deduzido de domínio ou título."""
    import re as _re
    from carteira.services import cnpj as cnpj_srv
    out, lido = [], False
    if site is not None and not r.get("diretorio"):
        try:
            res = site.analisar(r["url"], ctx)
        except (PrazoEsgotado, OrcamentoEsgotado):
            raise
        except Exception:
            res = None
        if res is not None and res.paginas:
            lido = True
            if len(res.cnpjs) > 3:
                r["anotacao"] = "página com vários CNPJs (lista/diretório) – ignorada"
                return []
            out += [(c, f"site {r['dominio']} publica o CNPJ ({u[:120]})") for c, u in res.cnpjs.items()]
    for bruto in [r.get("cnpj_citado") or ""] + _re.findall(r"\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}", r.get("trecho") or ""):
        doc = cnpj_srv.analisar(bruto) if bruto else None
        if doc and doc.cnpj and doc.cnpj not in {c for c, _ in out}:
            origem = "diretório" if r.get("diretorio") else ("site (citado pela busca)" if not lido else "busca")
            out.append((doc.cnpj, f"CNPJ citado por {origem} {r['dominio']} – pista, identidade a confirmar"))
    return out


def _marcar_primeiro(execucao):
    progresso = dict(execucao.progresso or {})
    if progresso.get("primeiro_resultado_em"):
        return
    progresso["primeiro_resultado_em"] = timezone.now().isoformat()
    execucao.progresso = progresso
    type(execucao).objects.filter(pk=execucao.pk).update(progresso=progresso, heartbeat_em=timezone.now())


def _persistir_sem_cnpj(r, consulta, execucao, pesquisa, est):
    """Candidata relevante sem CNPJ. A chave é a URL (ou domínio+título), nunca só o nome."""
    import hashlib
    from prospeccao.conectores.base import ResultadoDescoberta
    from prospeccao.services import identificacao as idf
    url = (r.get("url") or "")[:500]
    titulo = (r.get("nome_citado") or r.get("titulo") or "")[:255]
    base_id = url or f"{r.get('dominio') or ''}|{titulo[:80]}"
    if not base_id.strip("|"):
        return None
    if getattr(est, "pendentes_web", 0) >= (execucao.pesquisa.limite_resultados or 200):
        return None
    fonte, _ = Fonte.objects.get_or_create(
        codigo="busca_web",
        defaults=dict(nome="Busca web (candidata sem CNPJ)", tipo=Fonte.Tipo.IMPORTACAO, habilitada=True,
                      intervalo_minimo_segundos=0, max_consultas_execucao=0,
                      observacoes="Candidatas da descoberta web ainda sem CNPJ. Não varre a internet sozinha."))
    chave = hashlib.sha256(base_id.encode()).hexdigest()[:40]
    tels = r.get("telefones") or []
    emails = r.get("emails") or []
    res = ResultadoDescoberta(
        id_externo=chave, nome=titulo or "Empresa sem nome na fonte", cidade=r.get("municipio_citado") or "",
        uf=execucao.pesquisa.uf, telefone=(tels[0] if tels else "")[:60], email=(emails[0] if emails else "")[:254],
        site=url[:300], atividade=(consulta or "")[:500], url_referencia=url,
        dados={"consulta": consulta, "hipotese_comercial": True, "sem_cnpj": True})
    cand, criado = idf.registrar_resultado(res, fonte, execucao, pesquisa)
    if criado:
        est.pendentes_web = getattr(est, "pendentes_web", 0) + 1
        _marcar_primeiro(execucao)
    return cand


def _ligar_candidato(cand, cnpj):
    """Associa o CNPJ já lido na fonte à candidata, sem apagar nome, contato ou URL."""
    if cand is None or not cnpj:
        return
    if cand.cnpj_candidato and cand.cnpj_candidato != cnpj:
        return
    from carteira.models import Empresa
    cand.cnpj_candidato = cnpj
    if not cand.metodo_identificacao:
        cand.metodo_identificacao = "BUSCA"
    if cand.status == Candidato.Status.PENDENTE:
        cand.status = Candidato.Status.CNPJ_ENCONTRADO
    from carteira.models import empresa_por_cnpj
    _e = empresa_por_cnpj(cnpj)
    empresa_id = _e.pk if _e else None
    if empresa_id and not cand.empresa_id:
        cand.empresa_id = empresa_id
        cand.status = Candidato.Status.IDENTIFICADO
    cand.save(update_fields=["cnpj_candidato", "metodo_identificacao", "status", "empresa"])


def descoberta_web(execucao, ctx, est, frente, cfg, plano, carga, municipios, sits, ja, pesos, site, busca, orc,
                   on_lote=None):
    """
    Busca web por atividade e localidade. Cada resultado vira candidata na hora, com o que a página trouxe
    (nome, local, site, telefone, e-mail). CNPJ não é requisito para aparecer. Se surgir com segurança, a
    seleção e o cruzamento correm em seguida. Fora do recorte mensal fica para consulta complementar.
    """
    from receita.models import EstabelecimentoReceita
    from prospeccao.services import orcamento as orcm
    pesquisa = execucao.pesquisa
    if busca is None:
        return 0
    novos, lidos, lote = 0, set(), []
    est.externos = getattr(est, "externos", 0)
    cidade = (linhas(pesquisa.cidades) or [""])[0]
    contexto = {"regiao": f"{pesquisa.cidades}/{pesquisa.uf}", "cidade": cidade, "uf": pesquisa.uf,
                "frente": frente.nome}

    def criar(cnpj, mot, e=None, fila=SelecaoPesquisa.Fila.AGUARDANDO):
        nonlocal novos
        if e is not None:
            pts, m2 = pontuar(e, cfg, pesos)
            exc = _excluido(e, plano)
            perfil, _ = esc.perfil_cnae(e.get("cnae_principal"))
            if perfil == "FORA" and not exc:
                exc = f"atividade {e.get('cnae_principal')} fora do escopo comercial (encontrada na descoberta web)"
            elif perfil == "REVISAR":
                m2 = m2 + ["possível concorrente/revenda de combustíveis ou lubrificantes – revisar (sem gasto pago)"]
        else:
            pts, m2, exc = 0, [], ""
        try:
            with transaction.atomic():
                sel = SelecaoPesquisa.objects.create(
                    pesquisa=pesquisa, frente=frente, cnpj=cnpj, plano=plano, pontuacao=pts, motivos_selecao=mot + m2,
                    primeira_execucao=execucao, ultima_execucao=execucao,
                    fila=SelecaoPesquisa.Fila.FORA if exc else fila,
                    aderencia=SelecaoPesquisa.Aderencia.FORA if exc else SelecaoPesquisa.Aderencia.INSUFICIENTE,
                    aderencia_motivos=[exc] if exc else [])
        except IntegrityError:
            ja.add(cnpj)
            return None
        if getattr(est, "ids_execucao", None) is None:
            est.ids_execucao = []
        est.ids_execucao.append(sel.pk)
        ja.add(cnpj)
        _marcar_primeiro(execucao)
        if fila != SelecaoPesquisa.Fila.CONHECIDA and fila != SelecaoPesquisa.Fila.REVISAR_IDENTIDADE:
            novos += 1
            est.externos += 1
            lote.append(sel.pk)
            if on_lote and len(lote) >= LOTE_PROGRESSO:
                on_lote(list(lote))
                lote.clear()
        elif fila == SelecaoPesquisa.Fila.REVISAR_IDENTIDADE:
            lote.append(sel.pk)
        return sel

    def ja_tem(cnpj):
        return cnpj in ja or SelecaoPesquisa.objects.filter(pesquisa=pesquisa, frente=frente, cnpj=cnpj).exists()

    limite = min(pesquisa.limite_resultados or 200, getattr(est, "teto_externos_frente", 10 ** 9))
    fila_q = list(consultas_descoberta(cfg, pesquisa, (execucao.parametros or {}).get("descoberta_por_frente"), frente))
    motivos_q, extra_q = {}, 0
    while fila_q:
        q = fila_q.pop(0)
        ctx.verificar()
        if getattr(est, "parar", False) or getattr(est, "externos", 0) >= limite:
            break
        resultados = orcm.buscar(busca, q, finalidade="COMPLEMENTAR" if q in motivos_q else "DESCOBERTA", orc=orc,
                                 ctx=ctx, contexto=contexto if getattr(busca, "lote", False) else None,
                                 motivo=motivos_q.get(q, ""))
        an = getattr(resultados, "analise", None) or {}
        if extra_q < 1 and an.get("proximas_consultas") and getattr(est, "externos", 0) < limite:
            q2 = an["proximas_consultas"][0]
            if q2 and q2 != q and q2 not in motivos_q:  # a IA escolheu a próxima consulta pelo que faltou
                fila_q.append(q2)
                motivos_q[q2] = (f"IA analisou o retorno de '{q[:70]}': falta "
                                 f"{', '.join(an.get('faltando') or ['empresas'])} – {an.get('justificativa', '')}")[:300]
                extra_q += 1
                ctx.log(f"[{frente.nome}] consulta complementar escolhida pela IA: {q2}", fonte="descoberta")
        reg = getattr(orc, "ultimo_registro", None)
        sf = getattr(est, "sf", {})
        leads = getattr(est, "leads", set())
        for r in resultados:
            if getattr(est, "parar", False) or est.externos >= limite:
                r["anotacao"] = "limite da execução ou meta atingida"
                continue
            if r.get("dominio") and r["dominio"] in lidos:
                r["anotacao"] = "domínio já visto nesta execução — consulta seguinte segue"
                continue
            if r.get("dominio"):
                lidos.add(r["dominio"])
            # A empresa aparece antes do CNPJ. A leitura do site, se houver, vem depois do registro.
            cand = _persistir_sem_cnpj(r, q, execucao, pesquisa, est)
            citacoes = _cnpjs_do_resultado(r, None, ctx)
            if not citacoes and site is not None and not r.get("diretorio") and r.get("url"):
                citacoes = _cnpjs_do_resultado(r, site, ctx)
            if not citacoes:
                r.setdefault("anotacao", "candidata encontrada; CNPJ ainda não identificado")
                continue
            for cnpj, como in citacoes:
                if ja_tem(cnpj):
                    r.setdefault("anotacao", f"CNPJ {cnpj} já considerado nesta pesquisa")
                    continue
                e = None
                if carga is not None:
                    e = EstabelecimentoReceita.objects.filter(carga=carga, cnpj=cnpj).values(*_CAMPOS_E).first()
                if e is not None and (e["municipio_codigo"] not in municipios or e["situacao_codigo"] not in sits):
                    sel = criar(cnpj, [f"Encontrada por busca web: '{q}' → {como}",
                                 "CNPJ fora do município ou da situação do plano — mantida para revisão, não descartada"],
                          e, fila=SelecaoPesquisa.Fila.REVISAR_IDENTIDADE)
                    if sel is not None:
                        _ligar_candidato(cand, cnpj)
                    # o site pode publicar só a unidade de outra cidade: estabelecimentos da MESMA RAIZ na região
                    # entram como possível correspondência (vínculo de grupo a conferir), nunca como confirmação
                    for irmao in irmaos_na_regiao(carga, cnpj, municipios, sits):
                        if ja_tem(irmao["cnpj"]):
                            continue
                        fila_i = SelecaoPesquisa.Fila.CONHECIDA if irmao["cnpj"] in sf or irmao["cnpj"] in leads                             else SelecaoPesquisa.Fila.AGUARDANDO
                        criar(irmao["cnpj"], [f"Encontrada por busca web: '{q}' → {como}",
                                              f"Possível correspondência: mesma raiz do CNPJ {cnpj} publicado na fonte "
                                              f"(unidade fora da região); este estabelecimento fica na região – "
                                              f"vínculo de grupo a conferir"], irmao, fila=fila_i)
                    continue
                if e is not None and "pista" in como and not _nome_bate(e, r.get("nome_citado") or r.get("titulo")):
                    r.setdefault("anotacao", f"CNPJ {cnpj} citado não corresponde ao nome — não associado")
                    continue
                if e is None:
                    mot = [f"Encontrada por busca web: '{q}' → {como}",
                           "Ausente do recorte mensal da Receita — consulta complementar pendente"]
                else:
                    mot = [f"Encontrada por busca web: '{q}' → {como}"]
                fila = SelecaoPesquisa.Fila.CONHECIDA if cnpj in sf or cnpj in leads else SelecaoPesquisa.Fila.AGUARDANDO
                sel = criar(cnpj, mot, e, fila=fila)
                if sel is not None:
                    _ligar_candidato(cand, cnpj)
            if not any(ja_tem(c) for c, _ in citacoes):
                pass
        if reg is not None and resultados:
            type(reg).objects.filter(pk=reg.pk).update(resultados=resultados)
    if lote and on_lote and not getattr(est, "parar", False):
        on_lote(list(lote))
        lote.clear()
    ctx.log(f"[{frente.nome}] descoberta web: {novos} empresa(s) nova(s) ({len(lidos)} domínio(s) avaliado(s))",
            fonte="descoberta_web")
    return novos


def selecionar(execucao, ctx, est, on_lote=None):
    from receita.importador import tabela
    from receita.models import CargaReceita, EstabelecimentoReceita
    from carteira.services.normalizacao import normalizar_texto
    pesquisa = execucao.pesquisa
    plano = planejamento.plano_para_execucao(pesquisa)
    type(execucao).objects.filter(pk=execucao.pk).update(plano=plano)
    execucao.plano = plano
    ctx.log(f"Plano v{plano.versao} ({plano.get_origem_display()}) aplicado", fonte="plano")
    params = execucao.parametros or {}
    so_descoberta = bool(params.get("somente_investigar") and params.get("descoberta_por_frente"))
    if params.get("somente_investigar") and not so_descoberta:
        ctx.log("Execução só de (re)investigação: nenhum candidato novo selecionado", fonte="selecao")
        return
    if so_descoberta:
        ctx.log(f"Execução de validação: sem seleção pela base; descoberta web limitada a "
                f"{params['descoberta_por_frente']} consulta(s) por frente", fonte="selecao")
    carga = CargaReceita.ativa_atual()
    if carga:
        cidades = {normalizar_texto(c) for c in linhas(pesquisa.cidades)}
        municipios = [c for c, d in tabela(carga.competencia, "MUNICIPIO").items() if normalizar_texto(d) in cidades]
        sits = [SITS[s.strip().upper()] for s in (pesquisa.situacoes_receita or "ATIVA").split(",") if s.strip().upper() in SITS]
    else:
        est.limites.append("sem carga da base Receita; descoberta por características segue e o CNPJ fica pendente")
        ctx.log("Base Receita indisponível: candidatas da web são gravadas mesmo sem CNPJ local", "AVISO", "receita")
        municipios, sits = [], []
    sf, leads = _conhecidos()
    est.sf, est.leads = sf, leads
    pesos = {**planejamento.PESOS_PADRAO, **(plano.conteudo.get("pesos") or {})}
    com_evidencia = set(Evidencia.objects.filter(ativa=True).exclude(classificacao="INSUFICIENTE")
                        .values_list("empresa__cnpj", flat=True))
    campos = ["cnpj", "cnpj_basico", "razao_social", "nome_fantasia", "cnae_principal", "cnaes_secundarios", "matriz",
              "porte", "natureza_codigo", "email", "telefone1"]
    from prospeccao.services import orcamento as orcm
    busca = _busca_web(pesquisa, ctx) if (params.get("descoberta_por_frente") or pesquisa.max_buscas_descoberta) \
        else None
    site_fonte = Fonte.objects.filter(codigo="site_publico", habilitada=True).first()
    site = conectores.IDENTIFICACAO["site_publico"](site_fonte) if site_fonte and busca else None
    if site is not None:
        site.MAX_PAGINAS = min(pesquisa.max_paginas_site, 3)
    est.orcamento = getattr(est, "orcamento", None) or orcm.Orcamento(pesquisa, execucao)
    est.orcamento.ctx = ctx
    est.externos = getattr(est, "externos", 0)
    frentes = pesquisa.frentes.all()
    if params.get("frentes_ids"):
        frentes = frentes.filter(pk__in=params["frentes_ids"])
    frentes = list(frentes)
    from prospeccao.services import descoberta_google as dg
    if dg.eh_google(busca):
        # descoberta comercial pelo Google (fonte principal); a Receita só complementa/identifica
        est.multi_frente = len(frentes) > 1
        dg.descobrir(execucao, ctx, est, frentes, plano, carga, set(municipios), set(sits), busca, site,
                     est.orcamento, on_lote, dg.cota_por_frente(pesquisa, len(frentes)))
        if not pesquisa.fontes.filter(codigo="receita_cnpj").exists():
            ctx.log("Varredura da base Receita desligada nesta pesquisa (fonte não marcada): descobertas só do Google",
                    fonte="selecao")
            return
    import math
    # meta dividida entre as frentes: nenhuma frente consome a meta inteira e deixa as outras sem rodar
    cota = math.ceil((pesquisa.quantidade_desejada or 10) / max(1, len(frentes)))
    for i_frente, frente in enumerate(frentes):
        est.multi_frente = len(frentes) > 1  # com várias frentes, quem encerra são as cotas
        ja_frente = (SelecaoPesquisa.objects.filter(pesquisa=pesquisa, frente=frente, empresa__isnull=False)
                     .exclude(fila__in=[SelecaoPesquisa.Fila.CONHECIDA, SelecaoPesquisa.Fila.FORA,
                                        SelecaoPesquisa.Fila.NAO_CONTATAR]).count()
                     if len(frentes) > 1 else 0)
        restante = cota - ja_frente if len(frentes) > 1 else 10 ** 9
        if restante <= 0:
            ctx.log(f"[{frente.nome}] já tem {ja_frente} lead(s) externo(s) (cota da frente: {cota}) – frente pulada",
                    fonte="selecao")
            continue
        est.teto_externos_frente = getattr(est, "externos", 0) + restante
        cfg = plano.conteudo.get("por_frente", {}).get(str(frente.pk))
        if not cfg:
            ctx.log(f"Frente '{frente.nome}' sem configuração no plano – ignorada", "AVISO", "plano")
            continue
        codigos = [c["codigo"] for c in cfg.get("cnaes", [])]
        filtro = Q(cnae_principal__in=codigos) if codigos else Q(pk__in=[])
        if codigos:
            filtro |= Q(cnaes_secundarios__regex=r"(^| )(" + "|".join(codigos) + r")( |$)")
        for t in cfg.get("termos", []):
            if len(t) >= 4:
                filtro |= Q(razao_social__icontains=t) | Q(nome_fantasia__icontains=t)
        ja = set(SelecaoPesquisa.objects.filter(pesquisa=pesquisa, frente=frente).values_list("cnpj", flat=True))
        try:
            if not getattr(est, "parar", False) and not dg.eh_google(busca):
                descoberta_web(execucao, ctx, est, frente, cfg, plano, carga, set(municipios), set(sits), ja, pesos,
                               site, busca, est.orcamento, on_lote=on_lote)
        except OrcamentoEsgotado as e_orc:
            est.limites.append(str(e_orc))
        if carga is None or so_descoberta or getattr(est, "parar", False):
            base = EstabelecimentoReceita.objects.none()
        else:
            base = EstabelecimentoReceita.objects.filter(
                carga=carga, uf=pesquisa.uf.upper(), municipio_codigo__in=municipios,
                situacao_codigo__in=sits).filter(filtro)
        universo, pontuados = 0, []
        for e in base.values(*campos).iterator(chunk_size=5000):
            universo += 1
            if e["cnpj"] in ja:
                continue
            pts, mot = pontuar(e, cfg, pesos)
            if e["cnpj"] in com_evidencia:
                pts += pesos["evidencia_existente"]
                mot.append("Já possui evidência armazenada")
            pontuados.append((pts, e, mot))
        pontuados.sort(key=lambda x: (-x[0], x[1]["cnpj"]))
        novos = conhecidos = 0
        lote, limite = [], pesquisa.limite_resultados or 200
        for pts, e, mot in pontuados:
            if getattr(est, "parar", False):
                break
            conhecido = e["cnpj"] in sf or e["cnpj"] in leads
            if not conhecido and (est.externos >= limite or est.externos >= est.teto_externos_frente):
                break
            if conhecido and conhecidos >= pesquisa.quantidade_desejada:
                continue
            exc = _excluido(e, plano)
            try:
                with transaction.atomic():
                    sel = SelecaoPesquisa.objects.create(
                        pesquisa=pesquisa, frente=frente, cnpj=e["cnpj"], plano=plano, pontuacao=pts, motivos_selecao=mot,
                        primeira_execucao=execucao, ultima_execucao=execucao,
                        fila=SelecaoPesquisa.Fila.FORA if exc else SelecaoPesquisa.Fila.AGUARDANDO,
                        aderencia=SelecaoPesquisa.Aderencia.FORA if exc else SelecaoPesquisa.Aderencia.INSUFICIENTE,
                        aderencia_motivos=[exc] if exc else [])
            except IntegrityError:
                ja.add(e["cnpj"])
                continue
            if getattr(est, "ids_execucao", None) is None:
                est.ids_execucao = []
            est.ids_execucao.append(sel.pk)
            _marcar_primeiro(execucao)
            if conhecido:
                conhecidos += 1
            else:
                novos += 1
                est.externos += 1
                lote.append(sel.pk)
            if on_lote and len(lote) >= LOTE_PROGRESSO:
                on_lote(list(lote))
                lote = []
                if getattr(est, "parar", False):
                    break
        if lote and on_lote and not getattr(est, "parar", False):
            on_lote(list(lote))
        ctx.log(f"[{frente.nome}] universo do plano: {universo:,} estabelecimentos; já considerados antes: {len(ja):,}; "
                f"selecionados agora: {novos} externos (maior pontuação) + {conhecidos} conhecidos", fonte="selecao")
        execucao.progresso = {**(execucao.progresso or {}), frente.nome: {"universo": universo, "novos": novos,
                                                                         "conhecidos": conhecidos}}
        type(execucao).objects.filter(pk=execucao.pk).update(progresso=execucao.progresso)
        if getattr(est, "parar", False):
            break


# ------------------------------------------------------------------ etapa 2

def _vincular(sel, execucao, ctx):
    from prospeccao import conectores
    from prospeccao.services.pipeline import _verificar_cnpj
    fonte = Fonte.objects.get(codigo="receita_cnpj")
    origem = (Candidato.objects.filter(cnpj_candidato=sel.cnpj).exclude(fonte=fonte)
              .exclude(nome="").order_by("-pk").first())
    cand, _ = Candidato.objects.get_or_create(fonte=fonte, id_externo=sel.cnpj, defaults=dict(
        nome=(origem.nome if origem else "") or sel.cnpj, cidade=(origem.cidade if origem else ""),
        uf=(origem.uf if origem else "") or execucao.pesquisa.uf,
        cnpj_candidato=sel.cnpj, cnpj_informado=sel.cnpj, status=Candidato.Status.CNPJ_ENCONTRADO,
        metodo_identificacao="FONTE", primeira_execucao=execucao))
    if origem is not None and (cand.nome == sel.cnpj or not cand.nome):
        cand.nome = origem.nome or cand.nome
        cand.cidade = cand.cidade or origem.cidade
        cand.uf = cand.uf or origem.uf
        cand.telefone = cand.telefone or origem.telefone
        cand.email = cand.email or origem.email
        cand.site = cand.site or origem.site
        cand.save()
    cand.pesquisas.add(execucao.pesquisa)
    enr = None
    if execucao.pesquisa.enriquecer_por_cnpj:
        enr_fonte = Fonte.objects.filter(codigo="brasilapi_cnpj", habilitada=True).first()
        if enr_fonte is not None:
            enr = conectores.ENRIQUECIMENTO["brasilapi_cnpj"](enr_fonte)
    if cand.status != Candidato.Status.IDENTIFICADO or not cand.empresa_id:
        try:
            _verificar_cnpj(cand, sel.cnpj, "BUSCA" if origem else "RECEITA_BASE", enr, ctx)
            cand.refresh_from_db()
            if not cand.empresa_id:
                nota = ("consulta complementar sem confirmação (CNPJ não encontrado ou dados divergentes)" if enr
                        else "consulta complementar desativada nesta pesquisa")
                cand.dados = {**(cand.dados or {}), "consulta_complementar": nota}
                cand.save(update_fields=["dados"])
        except (PrazoEsgotado, OrcamentoEsgotado):
            raise
        except Exception as exc:  # falha da consulta não derruba a pesquisa: CNPJ fica pendente, com o motivo
            from prospeccao.conectores.base import ExecucaoCancelada
            if isinstance(exc, ExecucaoCancelada):
                raise
            cand.refresh_from_db()
            cand.dados = {**(cand.dados or {}), "consulta_complementar": f"falhou: {type(exc).__name__}: {str(exc)[:120]}"}
            cand.save(update_fields=["dados"])
            ctx.log(f"Consulta complementar do CNPJ {sel.cnpj} falhou ({type(exc).__name__}); mantido como pendente",
                    "AVISO", "brasilapi_cnpj")
    if cand.empresa_id:
        agora = timezone.now()
        Candidato.objects.filter(cnpj_candidato=sel.cnpj, empresa__isnull=True).update(
            empresa_id=cand.empresa_id, status=Candidato.Status.IDENTIFICADO, vinculado_em=agora)
    return cand.empresa


def aderencia_com_google(sel, empresa, cfg, ia_res):
    """calcular_aderencia + regra do lead sem CNPJ: sem CNAE para conferir, vale a categoria do Google."""
    ader_ant, mot_ant = sel.aderencia, list(sel.aderencia_motivos or [])
    ader, mot = calcular_aderencia(sel, empresa, cfg, ia_res)
    if (not empresa.cnpj and ader_ant == SelecaoPesquisa.Aderencia.PROVAVEL
            and ader == SelecaoPesquisa.Aderencia.INSUFICIENTE and any("Google" in m for m in mot_ant)):
        return SelecaoPesquisa.Aderencia.PROVAVEL, mot_ant + [
            "CNPJ não identificado: atividade pela categoria do Google; confirmar na conversa"]
    return ader, mot


def calcular_aderencia(sel, empresa, cfg, ia_res):
    """(aderencia, motivos). Falta de dado nunca vira FORA."""
    mot = []
    if sel.aderencia == SelecaoPesquisa.Aderencia.FORA and sel.aderencia_motivos and "exclusão explícita" in sel.aderencia_motivos[0]:
        return SelecaoPesquisa.Aderencia.FORA, sel.aderencia_motivos
    codigos = {c["codigo"] for c in cfg.get("cnaes", [])}
    principal_ok = empresa.cnae in codigos
    site_confiavel = empresa.canais.filter(tipo="SITE", confianca_associacao__in=["ALTA", "MEDIA"]).exists()
    positivas = Evidencia.objects.filter(empresa=empresa, ativa=True, fonte="site_publico",
                                         classificacao=Evidencia.Classificacao.CONFIRMADO_FONTE,
                                         chave__startswith=f"frente:{sel.frente_id}:")
    negativas = Evidencia.objects.filter(empresa=empresa, ativa=True, fonte="site_publico",
                                         classificacao=Evidencia.Classificacao.INSUFICIENTE,
                                         chave__startswith=f"frente:{sel.frente_id}:")
    if ia_res:
        rel = ia_res.get("relacao")
        proprias = [e for e in ia_res.get("evidencias", []) if e.get("contexto") == "OPERACAO_PROPRIA"]
        mot.append(f"IA: {rel} – {ia_res.get('justificativa', '')[:200]}")
        if rel == "SUSTENTADA" and proprias:
            return SelecaoPesquisa.Aderencia.SUSTENTADA, mot + [f"IA citou {len(proprias)} trecho(s) de operação própria"]
        if rel == "FORA":
            return SelecaoPesquisa.Aderencia.FORA, mot
    if site_confiavel and positivas.exists():
        p = positivas.first()
        return SelecaoPesquisa.Aderencia.SUSTENTADA, mot + [f"Site da empresa descreve a operação: “{p.trecho[:140]}”"]
    if negativas.exists() and not principal_ok and not positivas.exists():
        n = negativas.first()
        return SelecaoPesquisa.Aderencia.FORA, mot + [
            f"O site trata do tema como serviço/produto ou para terceiros, não como operação própria: “{n.trecho[:140]}”"]
    if principal_ok:
        return SelecaoPesquisa.Aderencia.PROVAVEL, mot + [f"Atividade principal {empresa.cnae} relacionada à frente "
                                                          "(operação ainda não confirmada)"]
    return SelecaoPesquisa.Aderencia.INSUFICIENTE, mot + ["Entrou por atividade secundária/nome; falta evidência da operação"]


def irmaos_na_regiao(carga, cnpj, municipios, sits, limite=3):
    """Estabelecimentos da mesma raiz de CNPJ nos municípios e situações da pesquisa (base local, sem custo)."""
    from receita.models import EstabelecimentoReceita
    if carga is None or not cnpj or not municipios:
        return []
    return list(EstabelecimentoReceita.objects.filter(
        carga=carga, cnpj_basico=cnpj[:8], municipio_codigo__in=list(municipios), situacao_codigo__in=list(sits))
        .exclude(cnpj=cnpj).values(*_CAMPOS_E)[:limite])


def motivo_identidade_pendente(sel):
    """Por que o CNPJ ainda não virou empresa: fora do recorte de municípios, fora da base local ou consulta
    complementar sem resultado. Nunca afirma situação cadastral sem fonte."""
    from receita.models import CargaReceita, EstabelecimentoReceita
    from receita.importador import tabela
    carga = CargaReceita.ativa_atual()
    e = EstabelecimentoReceita.objects.filter(carga=carga, cnpj=sel.cnpj).values(
        "municipio_codigo", "situacao_codigo", "razao_social").first() if carga else None
    if e is not None:
        cidade = tabela(carga.competencia, "MUNICIPIO").get(e["municipio_codigo"], e["municipio_codigo"])
        return (f"CNPJ {sel.cnpj} existe na base local, mas é de estabelecimento em {cidade} "
                f"({e['razao_social'][:50]}) – fora do recorte desta pesquisa; mantido para revisão")
    cand = Candidato.objects.filter(cnpj_candidato=sel.cnpj).order_by("-pk").first()
    det = (cand.motivo_exclusao or (cand.dados or {}).get("consulta_complementar", "")) if cand else ""
    return (f"CNPJ {sel.cnpj} fora da base local; consulta complementar sem confirmação"
            + (f" ({str(det)[:120]})" if det else "") + " – pendente, não descartado")


FILAS_DISPONIVEIS = ("SELECIONADA", "CONTATO_CADASTRAL", "BUSCAR_CONTATO")


def reencontrada(empresa, pesquisa):
    """Empresa da prospecção que já existia antes desta pesquisa ou foi encontrada por outra pesquisa.
    É informação ("Encontrada anteriormente"), nunca bloqueio."""
    if empresa is None or empresa.salesforce_id:
        return False
    if empresa.criado_em and pesquisa.criada_em and empresa.criado_em < pesquisa.criada_em:
        return True
    return SelecaoPesquisa.objects.filter(empresa=empresa).exclude(pesquisa=pesquisa).exists()


def informativos(empresa, pesquisa):
    """Linhas informativas (não bloqueiam): encontrada antes e tentativas sem resposta."""
    info = []
    if reencontrada(empresa, pesquisa):
        outras = sorted(set(SelecaoPesquisa.objects.filter(empresa=empresa).exclude(pesquisa=pesquisa)
                            .values_list("pesquisa_id", flat=True)))
        info.append("Encontrada anteriormente" + (f" (pesquisa {', '.join(map(str, outras))})" if outras else "")
                      + " – mesmo cadastro, ainda sem contato efetivo")
    tent = Abordagem.objects.filter(empresa=empresa, houve_conversa=False).select_related("usuario")
    n = tent.count()
    if n:
        a = tent.order_by("-data_hora").first()
        info.append(f"Tentativa sem resposta ({n}): última em {timezone.localtime(a.data_hora):%d/%m/%Y} por "
                      f"{a.usuario} – {a.get_resultado_display()}; continua disponível")
    return info


def calcular_fila(sel, empresa, pesquisa, cache=None):
    """(fila, motivos). Disponíveis recebem as informações de 'encontrada antes' e de tentativas."""
    fila, motivos = _calcular_fila(sel, empresa, pesquisa, cache)
    if empresa is not None and fila in FILAS_DISPONIVEIS:
        motivos = list(motivos) + informativos(empresa, pesquisa)
        if not empresa.cnpj:
            motivos.append("CNPJ não identificado pelos dados disponíveis – identidade a conferir")
        if not Empresa.objects.filter(salesforce_id__isnull=False).exists():
            motivos.append("Cruzamento pendente: sem carteira Salesforce local – não confirmada como nova")
    return fila, motivos


def conta_salesforce_relacionada(empresa):
    """Conta do Salesforce dona de um CNPJ ligado a esta empresa da prospecção: o principal, um associado ou o
    citado na identificação (quando o CNPJ já era da conta e por isso não foi adotado). None se não houver."""
    if empresa is None or empresa.salesforce_id:
        return None
    from carteira.models import DadoColetado
    cnpjs = set(empresa.cnpjs_associados.values_list("cnpj", flat=True))
    cnpjs |= set(DadoColetado.objects.filter(empresa=empresa, campo="cnpj_candidato", fonte="cruzamento_salesforce")
                 .values_list("valor", flat=True))
    if empresa.cnpj:
        cnpjs.add(empresa.cnpj)
    cnpjs = {c for c in cnpjs if c and c.isdigit() and len(c) == 14}
    if not cnpjs:
        return None
    return (Empresa.objects.filter(cnpj__in=cnpjs, salesforce_id__isnull=False).exclude(pk=empresa.pk)
            .only("cnpj", "salesforce_id", "nome").first())


def _calcular_fila(sel, empresa, pesquisa, cache=None):
    """(fila, motivos) com precedência explícita. Decisão humana prevalece."""
    if empresa is None and sel.aderencia == SelecaoPesquisa.Aderencia.FORA and sel.aderencia_motivos:
        return SelecaoPesquisa.Fila.FORA, list(sel.aderencia_motivos)  # fora do escopo/região: não vira "identidade"
    if empresa is None and not sel.cnpj.isdigit():  # possível correspondência da web: mantém o motivo registrado
        return SelecaoPesquisa.Fila.REVISAR_IDENTIDADE, list(sel.fila_motivos or ["Possível correspondência; conferir"])
    if empresa is None:
        from carteira.models import Empresa
        conta = Empresa.objects.filter(cnpj=sel.cnpj, salesforce_id__isnull=False).only("salesforce_id").first()
        if conta is not None:
            return SelecaoPesquisa.Fila.CONHECIDA, [
                f"Conta {conta.salesforce_id} já está na base local do Salesforce"]
        return SelecaoPesquisa.Fila.REVISAR_IDENTIDADE, [motivo_identidade_pendente(sel)]
    sit, det, _ = cz.situacao_empresa(empresa, cache=cache)
    if sit in CONHECIDAS:
        return SelecaoPesquisa.Fila.CONHECIDA, [det]
    conta = conta_salesforce_relacionada(empresa)
    if conta is not None:  # o CNPJ desta empresa é de uma conta do Salesforce (cadastros não unidos)
        return SelecaoPesquisa.Fila.CONHECIDA, [
            f"CNPJ {conta.cnpj} é da conta {conta.salesforce_id} ({conta.nome[:60]}) no Salesforce"]
    if sit in (cz.Situacao.BLOQUEADA, cz.Situacao.NAO_CONTATAR):
        return SelecaoPesquisa.Fila.NAO_CONTATAR, [det]
    # SÓ contato efetivo (conversa registrada pela equipe) tira da primeira abordagem; tentativa sem resposta não.
    conversas = Abordagem.objects.filter(empresa=empresa, houve_conversa=True)
    if conversas.exists():
        ult = conversas.select_related("usuario").order_by("-data_hora").first()
        quem = f"{timezone.localtime(ult.data_hora):%d/%m/%Y} por {ult.usuario} ({ult.get_resultado_display()})"
        if Abordagem.objects.filter(empresa=empresa, data_retorno__isnull=False, retorno_concluido=False).exists():
            return SelecaoPesquisa.Fila.RETORNO, [f"Contato efetivo em {quem}; retorno agendado – em acompanhamento"]
        return SelecaoPesquisa.Fila.JA_ABORDADA, [f"Contato efetivo em {quem} – em acompanhamento"]
    # já encontrada antes NÃO bloqueia: segue a avaliação normal (ver informativos() abaixo)
    # município comprovado pela fonte (endereço da fonte ou cadastro do CNPJ): sem prova, localização pendente
    cids = {normalizar_texto(c) for c in linhas(pesquisa.cidades)}
    cid = normalizar_texto(empresa.cidade or "")
    if cids and cid not in cids:
        if not cid:
            return SelecaoPesquisa.Fila.LOCALIZACAO_PENDENTE, [
                "Localização pendente: a fonte não informa o município (nome, bairro ou DDD não comprovam); conferir"]
        return SelecaoPesquisa.Fila.FORA, [f"Fora da região da pesquisa: a fonte indica {empresa.cidade}"]
    ader = sel.aderencia_efetiva
    if sel.decisao_humana == "RETIRAR":
        return SelecaoPesquisa.Fila.FORA, [f"Retirada pelo gestor: {sel.decisao_motivo}"]
    if ader == SelecaoPesquisa.Aderencia.FORA and sel.decisao_humana != "INCLUIR":
        return SelecaoPesquisa.Fila.FORA, sel.aderencia_motivos[:2]
    classe = ops.classe_situacao(empresa)
    if classe == "PENDENTE" and not empresa.cnpj:
        pass  # lead da web sem CNPJ: aparece, com "identidade a conferir" (CNPJ não é exigido para apresentar)
    elif classe != "OK":
        return SelecaoPesquisa.Fila.REVISAR_IDENTIDADE, [f"Situação cadastral {empresa.situacao_cadastral or 'pendente'}"]
    if empresa.candidatos.filter(status="CONFLITO").exists():
        return SelecaoPesquisa.Fila.REVISAR_IDENTIDADE, ["Conflito de identificação"]
    exploratoria_ok = pesquisa.permitir_exploratoria and ader == SelecaoPesquisa.Aderencia.PROVAVEL
    if sel.decisao_humana != "INCLUIR" and not (ader == SelecaoPesquisa.Aderencia.SUSTENTADA or exploratoria_ok):
        return SelecaoPesquisa.Fila.INVESTIGAR, ["Aderência ainda não sustentada" + (
            "" if pesquisa.permitir_exploratoria else " (abordagem exploratória desativada nesta pesquisa)")]
    uteis = ct.canais_utilizaveis(empresa)
    if not uteis.exists():
        if ct.canais_terceiros(empresa).exists():
            return SelecaoPesquisa.Fila.BUSCAR_CONTATO, [
                "Só há contato de terceiro (possível contador) – empresa disponível; buscar contato direto"]
        return SelecaoPesquisa.Fila.BUSCAR_CONTATO, ["Nenhum telefone ou e-mail encontrado"]
    da_empresa = uteis.exclude(origem_canal=CanalEmpresa.Origem.CADASTRAL).filter(
        confianca_associacao__in=[CanalEmpresa.Confianca.ALTA, CanalEmpresa.Confianca.MEDIA])
    if not da_empresa.exists() and not uteis.filter(confirmacao_humana__in=["ATENDIDO", "CONFERIDO"]).exists():
        origens = sorted({"cadastro" if c.origem_canal == "CADASTRAL" else "diretório/busca" for c in uteis})
        return SelecaoPesquisa.Fila.CONTATO_CADASTRAL, [
            f"Contato encontrado em {' e '.join(origens)} – a conferir no primeiro contato"]
    return SelecaoPesquisa.Fila.SELECIONADA, [f"{sel.get_aderencia_display()}; contato encontrado em fonte da empresa"]


def _evidencias_frente(empresa, cfg, frente_id, paginas):
    """Sinais da frente nos textos do site, com contexto. Chave por frente (não duplica)."""
    import hashlib
    from prospeccao.services.evidencias import _padrao, _trecho, contexto_negativo, normalizar_mesmo_tamanho
    negativos = list(dict.fromkeys(ev.NEGATIVOS_PADRAO + [n for n in cfg.get("contextos_negativos", []) if n]))
    for url, texto in paginas:
        norm = normalizar_mesmo_tamanho(texto)
        for sinal in cfg.get("sinais", []):
            p = _padrao(sinal)
            if not p:
                continue
            melhor = None
            for m in p.finditer(norm):
                neg = contexto_negativo(norm, m.start(), m.end(), negativos)
                melhor = (m, neg) if (melhor is None or (melhor[1] and not neg)) else melhor
                if not neg:
                    break
            if not melhor:
                continue
            m, neg = melhor
            chave = "frente:%s:%s" % (frente_id, hashlib.sha256(f"{empresa.pk}|{sinal}|{url}".encode()).hexdigest()[:40])
            janela = norm[max(0, m.start() - 90):m.end() + 90]
            if neg:
                classe = Evidencia.Classificacao.INSUFICIENTE
                desc = f"Menção a '{sinal}' em contexto de '{neg}' (não comprova operação própria)"
            elif "AGREGAD" in janela:
                classe = Evidencia.Classificacao.HIPOTESE
                desc = ("Menção a veículos agregados: hipótese comercial, não comprova compra de lubrificantes "
                        "nem frota própria")
            else:
                classe = Evidencia.Classificacao.CONFIRMADO_FONTE
                desc = f"Site da empresa menciona '{sinal}' (frente {cfg.get('frente')})"
                if "OFICINA PROPRIA" in janela:
                    desc = "Oficina própria (manutenção da empresa, distinta de veículos agregados). " + desc
            Evidencia.objects.update_or_create(chave=chave, defaults=dict(
                empresa=empresa, tipo="ATIVIDADE", descricao=desc[:255], classificacao=classe, fonte="site_publico",
                url=url[:500], trecho=_trecho(texto, m.start(), m.end()), gerada_por="REGRA", ativa=True))


def _uso_reaproveitavel(empresa, pesquisa, paginas):
    """Análise de IA ainda válida para a mesma empresa e o mesmo material (mesmas URLs) – evita pagar de novo."""
    from datetime import timedelta
    from prospeccao.services import ia
    limite = timezone.now() - timedelta(days=pesquisa.validade_analise_ia_dias)
    urls = sorted(u for u, _ in paginas)
    for uso in UsoIA.objects.filter(tipo="INVESTIGACAO", empresa=empresa, status="OK", pesquisa=pesquisa,
                                    versao_prompt=ia.VERSAO_PROMPT_INVESTIGACAO, criado_em__gte=limite).order_by("-criado_em")[:5]:
        if sorted(m.get("url") for m in (uso.materiais or [])) == urls and uso.resultado:
            return uso
    return None


CONF_IA = {"ALTA": CanalEmpresa.Confianca.MEDIA, "MEDIA": CanalEmpresa.Confianca.MEDIA,
           "BAIXA": CanalEmpresa.Confianca.BAIXA}  # o programa limita: IA nunca atribui confiança ALTA sozinha


def _ia_investigar(empresa, cfg, pesquisa, paginas, execucao, orc=None, site_conf="", site_aplica=""):
    from prospeccao.models import CategoriaProduto
    from prospeccao.services import ia
    ok, msg = ia.status()
    if not ok:
        return None, "NAO_CONFIGURADA"
    if not paginas:
        return None, "SEM_MATERIAL"
    reuso = _uso_reaproveitavel(empresa, pesquisa, paginas)
    if reuso is not None:
        return reuso.resultado, "REAPROVEITADA"
    from prospeccao.services import orcamento as orcm
    orc = orc or orcm.Orcamento(pesquisa, execucao)
    an = ia.obter_analisador()
    an.max_tokens = pesquisa.ia_max_tokens_saida
    catalogo = list(CategoriaProduto.objects.filter(ativa=True).values_list("nome", flat=True))
    _, texto = ia.pedido_investigacao(empresa, cfg, pesquisa.objetivo, paginas, catalogo)
    uso = UsoIA(tipo="INVESTIGACAO", provedor=an.nome, modelo=an.modelo, versao_prompt=ia.VERSAO_PROMPT_INVESTIGACAO,
                pesquisa=pesquisa, plano=execucao.plano, empresa=empresa,
                materiais=[{"url": u, "caracteres": len(t)} for u, t in paginas])
    try:
        (res, usage, modelo, _), reserva, inicio = orcm.executar_ia(
            orc, an, uso, texto, lambda: an.analisar_operacao(empresa, cfg, pesquisa.objetivo, paginas, catalogo))
    except orcm.SemOrcamento:
        return None, "SEM_ORCAMENTO"
    except Exception:
        return None, "FALHA"
    uso.modelo, uso.status, uso.resultado = modelo, ("RECUSA" if res is None else "OK"), res or {}
    ia.registrar_uso(uso, usage, inicio)
    orc.liquidar(reserva, uso.custo_estimado_usd, ia=True)
    ia_descartados = []
    if res:
        import hashlib
        cats = {c.nome: c for c in CategoriaProduto.objects.all()}
        for item in res["evidencias"]:
            chave = hashlib.sha256(f"ia2|{empresa.pk}|{item['url']}|{item['trecho_exato']}".encode()).hexdigest()
            evid, criada = Evidencia.objects.get_or_create(chave=chave, defaults=dict(
                empresa=empresa, tipo=item["tipo"], fonte=f"ia:{modelo}"[:60], url=item["url"][:500],
                trecho=item["trecho_exato"][:300], gerada_por="IA",
                classificacao=Evidencia.Classificacao.HIPOTESE if item["contexto"] == "OPERACAO_PROPRIA"
                else Evidencia.Classificacao.INSUFICIENTE,
                descricao=f"IA [{item['contexto']}]: {item['descricao']}"[:255]))
            if criada:
                evid.aplicacoes.set([cats[a] for a in item["aplicacoes"] if a in cats])
        material = " ".join(t for _, t in paginas)
        mat_dig, mat_low = re.sub(r"\D", "", material), material.lower()
        ordem_c = ["CONFLITO", "BAIXA", "MEDIA", "ALTA"]
        for c in res["contatos"]:
            if c["tipo"] == "EMAIL":
                literal = (c["valor"] or "").strip().lower() in mat_low
            else:
                dig = re.sub(r"\D", "", c["valor"] or "")
                dig = dig[2:] if dig.startswith("55") and len(dig) > 11 else dig
                literal = len(dig) >= 10 and (dig in mat_dig or dig[2:] in mat_dig)
            if not literal:  # citado pela IA mas ausente do texto lido: descartado (não vira contato)
                ia_descartados.append(c.get("valor", "")[:60])
                continue
            conf = CONF_IA.get(c.get("confianca"), CanalEmpresa.Confianca.BAIXA)
            if site_conf in ordem_c and ordem_c.index(site_conf) < ordem_c.index(conf):
                conf = site_conf  # nunca acima da associação do próprio site
            ct.registrar(empresa, c["tipo"], c["valor"], fonte=f"ia:{modelo}"[:60], origem=CanalEmpresa.Origem.PUBLICACAO,
                         confianca=conf, url=c.get("url", "") or (paginas[0][0] if paginas else ""),
                         aplica_a=site_aplica or CanalEmpresa.AplicaA.EMPRESA,
                         evidencia=f"citado literalmente no site (leitura por IA): “{c.get('trecho_exato', '')[:120]}”",
                         departamento=c.get("departamento", ""),
                         whatsapp=c["tipo"] == "WHATSAPP" or bool(c.get("whatsapp_explicito")),
                         finalidade=c.get("finalidade") if c.get("finalidade") not in (None, "", "INCERTO") else None)
        Oportunidade.objects.filter(empresa=empresa).update(
            resumo_ia=res["justificativa"][:2000], perguntas_ia=res["perguntas_para_vendedor"], ia_modelo=modelo[:60],
            ia_em=timezone.now())
        if ia_descartados:
            uso.resultado = {**uso.resultado, "_contatos_descartados_nao_literais": ia_descartados}
            uso.save(update_fields=["resultado"])
    return res, uso.status


def _busca_web(pesquisa, ctx):
    from prospeccao.conectores.busca_web import obter_busca
    busca, msg = obter_busca(ctx)
    if busca is not None and getattr(busca, "lote", False):
        busca.max_acoes = max(1, pesquisa.max_buscas_web_empresa)
    ctx.log(msg, "INFO" if busca else "AVISO", getattr(busca, "codigo", "busca_web"))
    return busca


def investigar(execucao, ctx, est, somente=None, recalcular=True):
    from prospeccao.services import orcamento as orcm
    if getattr(est, "parar", False) and somente is None:
        ctx.log("Meta de novos leads já atingida; sem investigação adicional", fonte="meta")
        return
    pesquisa = execucao.pesquisa
    plano = execucao.plano or planejamento.plano_para_execucao(pesquisa)
    site_fonte = Fonte.objects.filter(codigo="site_publico", habilitada=True).first()
    site = conectores.IDENTIFICACAO["site_publico"](site_fonte) if site_fonte and pesquisa.identificar_por_site else None
    if site is not None:
        site.MAX_PAGINAS = pesquisa.max_paginas_site
    busca = _busca_web(pesquisa, ctx)
    orc = getattr(est, "orcamento", None) or orcm.Orcamento(pesquisa, execucao)
    orc.ctx = ctx
    est.orcamento = orc
    if not getattr(est, "cache_cz", None):
        est.cache_cz = cz.cache_cruzamento()

    regs_empresa = []

    def buscar_fn(qs, empresa, motivo=""):
        if getattr(busca, "lote", False):  # OpenAI: UMA chamada por empresa com as consultas sugeridas
            tel_cad = list(empresa.canais.filter(tipo="TELEFONE", origem_canal="CADASTRAL")
                           .values_list("valor", flat=True)[:2])
            contexto = {"cidade": empresa.cidade or "", "uf": empresa.uf or pesquisa.uf,
                        "empresa": {"cnpj": empresa.cnpj_formatado, "razao_social": empresa.razao_social or empresa.nome,
                                    "nome_fantasia": empresa.nome_fantasia or "", "municipio": empresa.cidade or "",
                                    "uf": empresa.uf or pesquisa.uf, "endereco": (empresa.endereco or "")[:150],
                                    "cep": empresa.cep or "", "telefones_cadastrais": tel_cad}}
            r = orcm.buscar(busca, list(qs), finalidade="COMPLEMENTAR" if motivo else "CONTATO", orc=orc, ctx=ctx,
                            empresa=empresa, contexto=contexto, motivo=motivo)
            regs_empresa.append(orc.ultimo_registro)
            return r
        res = []
        for q in qs:
            res += orcm.buscar(busca, q, finalidade="CONTATO", orc=orc, ctx=ctx, empresa=empresa)
            regs_empresa.append(orc.ultimo_registro)
        return res

    consultas = plano.conteudo.get("consultas_externas") or planejamento.CONSULTAS_PADRAO
    from prospeccao.services import ia
    ctx.log(ia.status()[1], "INFO" if ia.status()[0] else "AVISO", "ia")
    ids = (execucao.parametros or {}).get("investigar_ids")
    controle_ids = set((execucao.parametros or {}).get("controle_ids") or [])
    if somente is not None:
        pend = list(SelecaoPesquisa.objects.filter(
            pesquisa=pesquisa, pk__in=somente, fila=SelecaoPesquisa.Fila.AGUARDANDO).select_related("frente"))
    elif ids:  # validação controlada: SOMENTE estas seleções (as novas da descoberta ficam aguardando)
        pend = list(SelecaoPesquisa.objects.filter(pesquisa=pesquisa, pk__in=ids).select_related("frente"))
        est.ids_execucao = list(ids) + [i for i in est.ids_execucao if i not in ids]
    else:
        pend = list(SelecaoPesquisa.objects.filter(pesquisa=pesquisa, fila=SelecaoPesquisa.Fila.AGUARDANDO)
                    .select_related("frente"))
    ordem = {pk: i for i, pk in enumerate(est.ids_execucao)}
    pend.sort(key=lambda s: (ordem.get(s.pk, 10 ** 9), -s.pontuacao))
    feitos, ia_n, canais_novos = 0, 0, 0
    ia_status = {}
    por_frente = {}
    for sel in pend:
        ctx.verificar()
        regs_empresa.clear()
        if sel.pk not in ordem:  # pendente de execução anterior (retomada): cota própria por frente
            chave = ("antigo", sel.frente_id)
            if por_frente.get(chave, 0) >= pesquisa.quantidade_desejada:
                continue
            por_frente[chave] = por_frente.get(chave, 0) + 1
        cfg = plano.conteudo.get("por_frente", {}).get(str(sel.frente_id), {})
        try:
            # sem transação: registros de custo (UsoIA/ConsultaWeb) não podem ser desfeitos por falha posterior
            empresa = sel.empresa if (sel.empresa_id and not sel.cnpj.isdigit()) else _vincular(sel, execucao, ctx)
            sel.empresa = empresa
            if empresa is not None:
                ops.avaliar(empresa, pesquisa)
                sit = cz.situacao_empresa(empresa, cache=est.cache_cz)[0]
                paginas, site_conf, site_aplica = [], "", ""
                # reencontrada com contato já obtido: reaproveita os dados, sem novas consultas desnecessárias
                ja_nossa = (sit == cz.Situacao.NOVA_EXTERNA and reencontrada(empresa, pesquisa)
                            and ct.canais_utilizaveis(empresa).exists())
                if ja_nossa:
                    ctx.log(f"{empresa.nome}: encontrada anteriormente; dados e contatos reaproveitados, sem nova consulta",
                            fonte="investigacao")
                    if sel.aderencia != SelecaoPesquisa.Aderencia.FORA or not sel.aderencia_motivos:
                        ant = (SelecaoPesquisa.objects.filter(empresa=empresa).exclude(pesquisa=pesquisa)
                               .exclude(aderencia=SelecaoPesquisa.Aderencia.FORA).order_by("-investigada_em").first())
                        if ant is not None:
                            sel.aderencia = ant.aderencia_efetiva
                            sel.aderencia_motivos = list(ant.aderencia_motivos or [])[:3] + [
                                f"Avaliação reaproveitada da pesquisa {ant.pesquisa_id}"]
                        else:
                            sel.aderencia, sel.aderencia_motivos = calcular_aderencia(sel, empresa, cfg, None)
                if sit not in CONHECIDAS + (cz.Situacao.BLOQUEADA, cz.Situacao.NAO_CONTATAR) and not ja_nossa:
                    ia_adiada = False
                    por_frente[sel.frente_id] = por_frente.get(sel.frente_id, 0) + 1
                    pertinente, mot_esc = esc.pertinente_para_gasto(empresa)
                    if not pertinente:  # fora do escopo / revisão: só o caminho gratuito, nenhuma chamada paga
                        ctx.log(f"{empresa.nome}: {mot_esc}", "AVISO", "escopo")  # decisão de escopo, não limite
                    if site:
                        from prospeccao.services import descoberta_google as dg
                        if dg.eh_google(busca) and pertinente:
                            # busca complementar no Google só depois que TODAS as cidades tiveram a 1ª consulta:
                            # a complementação de uma empresa não pode impedir a descoberta nas demais cidades
                            pend_cid = getattr(est, "cidades_pendentes", None)
                            from prospeccao.services.prontidao import em_teste
                            reserva = em_teste() and getattr(est, "google_restante", 10 ** 6) <= len(linhas(pesquisa.cidades))
                            adiar = bool(pend_cid or reserva or getattr(est, "adiar_complemento_google", False))
                            # durante a descoberta: só o caminho gratuito (site indicado + fontes salvas), sem IA;
                            # a IA e a busca dirigida ficam para o lote de identificação, que vem logo em seguida
                            r = dg.investigar_dirigido(empresa, sel, pesquisa, busca, site, orc, ctx,
                                                       max_buscas=0 if adiar else pesquisa.max_buscas_web_empresa,
                                                       sem_ia=adiar)
                            if adiar:
                                ia_adiada = True
                                ctx.log(f"{empresa.nome}: salva; identificação dirigida no próximo lote ("
                                        + (f"cidades ainda não pesquisadas: {', '.join(sorted(pend_cid))}" if pend_cid
                                           else "a descoberta segue em paralelo; a empresa entra na fila de identificação")
                                        + ")", fonte="investigacao")
                        else:
                            r = ct.investigar_contatos(empresa, site, ctx,
                                                       buscar_fn=buscar_fn if (busca and pertinente) else None,
                                                       consultas=consultas, max_buscas=pesquisa.max_buscas_web_empresa,
                                                       forcar_busca=sel.pk in controle_ids)
                        ctx.log(f"{empresa.nome}: pesquisa web {r.get('busca_paga')}", fonte="investigacao")
                        paginas, canais_novos = r["paginas"], canais_novos + r["novos_canais"]
                        site_conf, site_aplica = r.get("confianca", ""), r.get("aplica_a", "")
                        _anotar_leitura(regs_empresa, r["leitura"])
                        if paginas:
                            ev.aplicar_textos("TEXTO_SITE", [(t, u, "site_publico") for u, t in paginas], empresa=empresa)
                            _evidencias_frente(empresa, cfg, sel.frente_id, paginas)
                    else:
                        ct.qualificar_cadastrais(empresa)
                    if pertinente and ia_adiada:
                        ia_res, ia_st = None, "ADIADA"  # IA reservada para identificação e contato
                    elif pertinente:
                        ia_res, ia_st = _ia_investigar(empresa, cfg, pesquisa, paginas, execucao, orc,
                                                       site_conf, site_aplica)
                    else:
                        ia_res, ia_st = None, "FORA_ESCOPO"
                    ia_status[ia_st] = ia_status.get(ia_st, 0) + 1
                    ia_n += ia_st in ("OK", "RECUSA")
                    if sel.aderencia != SelecaoPesquisa.Aderencia.FORA or not sel.aderencia_motivos:
                        sel.aderencia, sel.aderencia_motivos = aderencia_com_google(sel, empresa, cfg, ia_res)
                    ops.avaliar(empresa, pesquisa)
                    feitos += 1
            sel.fila, sel.fila_motivos = calcular_fila(sel, empresa, pesquisa, cache=getattr(est, "cache_cz", None))
            sel.investigada_em, sel.ultima_execucao = timezone.now(), execucao
            sel.save()
        except PrazoEsgotado:
            raise
        except OrcamentoEsgotado as e:
            est.limites.append(str(e))
            ctx.log(f"Limite atingido: {e}; seleções restantes continuam aguardando", "AVISO")
            break
        except Exception as exc:
            from django.db import InterfaceError, OperationalError
            from prospeccao.conectores.base import ExecucaoCancelada
            if isinstance(exc, (OperationalError, InterfaceError, ExecucaoCancelada)):
                raise
            est.erros += 1
            nota = f"Falha isolada ao investigar: {type(exc).__name__}: {exc}"[:300]
            ctx.log(f"{getattr(sel.empresa, 'nome', sel.pk)}: {nota}. As demais continuam.", "ERRO", "investigacao")
            motivos = list(sel.fila_motivos or [])
            if nota not in motivos:
                motivos.append(nota)
            sel.fila_motivos = motivos
            sel.ultima_execucao = execucao
            sel.save(update_fields=["fila_motivos", "ultima_execucao", "atualizada_em"])
    for motivo in sorted(getattr(orc, "motivos_bloqueio", [])):
        if f"pendência: {motivo}" not in est.limites:
            est.limites.append(f"pendência: {motivo}")
            ctx.log(f"Chamadas pagas não feitas: {motivo}", "AVISO", "orcamento")
    if recalcular:
        recalcular_filas(pesquisa)  # reflete decisões humanas, abordagens e sincronizações desde a última vez
        type(execucao).objects.filter(pk=execucao.pk).update(complementados=feitos, ia_analises=ia_n,
                                                            identificados=len(pend))
    ctx.log(f"Investigação: {feitos} empresa(s) externa(s) investigada(s), {canais_novos} canal(is) novo(s), "
            f"IA: {ia_status or 'nenhuma'}; gasto estimado US$ {orc.gasto:.4f}", fonte="investigacao")


def reprocessar_respostas_salvas(pesquisa, ctx=None):
    """Reaproveita respostas e páginas já salvas com o mesmo serviço do botão Buscar.

    Sem SerpApi e sem IA. Falha de uma empresa fica registrada; falha de banco interrompe.
    """
    from django.db import InterfaceError, OperationalError

    from prospeccao.conectores.base import ContextoExecucao
    from prospeccao.services import descoberta_google as dg

    ctx = ctx or ContextoExecucao()
    saida = []
    sels = SelecaoPesquisa.objects.filter(pesquisa=pesquisa, empresa__isnull=False).exclude(
        fila__in=[SelecaoPesquisa.Fila.CONHECIDA, SelecaoPesquisa.Fila.FORA,
                  SelecaoPesquisa.Fila.NAO_CONTATAR]).select_related("empresa")
    for sel in sels:
        try:
            r = dg.investigar_dirigido(
                sel.empresa, sel, pesquisa, None, None, None, ctx, max_buscas=0, sem_ia=True,
                permitir_consulta_complementar=False)
            ident = r.get("identificacao") or {}
            saida.append({"id": sel.empresa_id, "acao": ident.get("acao") or "",
                          "motivo": (ident.get("motivo") or "")[:180], "novos": r.get("novos_canais") or 0})
        except (OperationalError, InterfaceError):
            raise
        except Exception as exc:
            nota = f"Falha isolada no reprocessamento: {type(exc).__name__}: {exc}"[:300]
            motivos = list(sel.fila_motivos or [])
            if nota not in motivos:
                motivos.append(nota)
            sel.fila_motivos = motivos
            sel.save(update_fields=["fila_motivos", "atualizada_em"])
            saida.append({"id": sel.empresa_id, "acao": "falha", "motivo": nota, "novos": 0})
    return saida


def _anotar_leitura(regs, leitura):
    """Grava em cada resultado da ConsultaWeb o resultado da leitura do site e a associação com a empresa.
    Uma URL devolvida pela pesquisa NÃO confirma, por si só, que pertence à empresa."""
    from carteira.services.normalizacao import dominio_de_url
    for reg in regs:
        if reg is None or not reg.pk or not isinstance(reg.resultados, list):
            continue
        mudou = False
        for item in reg.resultados:
            if not isinstance(item, dict):
                continue
            d = item.get("dominio") or dominio_de_url(item.get("url", ""))
            novo = leitura.get(d) or "não lido (não entrou na lista de sites candidatos)"
            if item.get("leitura") != novo:
                item["leitura"], mudou = novo, True
        if mudou:
            type(reg).objects.filter(pk=reg.pk).update(resultados=reg.resultados)


def recalcular_filas(pesquisa):
    """Reavalia filas (ex.: após abordagem, decisão humana ou sincronização) sem nova coleta."""
    for sel in SelecaoPesquisa.objects.filter(pesquisa=pesquisa).exclude(fila=SelecaoPesquisa.Fila.AGUARDANDO) \
            .select_related("empresa"):
        sel.fila, sel.fila_motivos = calcular_fila(sel, sel.empresa, pesquisa)
        sel.save(update_fields=["fila", "fila_motivos", "atualizada_em"])
