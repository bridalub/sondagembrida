"""
Execução de uma pesquisa:

  descobrir -> identificar CNPJ -> cruzar com a carteira -> complementar dados -> evidências -> oportunidade

- Cada candidato é gravado em transação própria: interrupções NÃO apagam o que já foi salvo.
- Status final: CONCLUIDA (tudo rodou, sem cortes), PARCIAL (limite de consultas/tempo, ou fonte com erro),
  CANCELADA, FALHA. `completa=True` só quando nenhuma etapa foi cortada.
- Reaproveita resultados: candidatos pendentes de execuções anteriores da mesma pesquisa continuam
  sendo processados; sites já verificados dentro da validade não são lidos de novo.
"""
import traceback
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from prospeccao import conectores
from prospeccao.conectores.base import (
    ContextoExecucao, ExecucaoCancelada, ExecucaoPausada, FonteIndisponivel, OrcamentoEsgotado, PrazoEsgotado,
    ResultadoDescoberta,
)
from prospeccao.models import Candidato, Execucao, Fonte, LogExecucao, RegraEvidencia
from prospeccao.services import cruzamento as cz
from prospeccao.services import evidencias as ev
from prospeccao.services import identificacao as idf
from prospeccao.services import oportunidades as ops
from prospeccao.services.enriquecimento import ultima_coleta
from prospeccao.services.exclusoes import motivo_exclusao


class _Estado:
    def __init__(self):
        self.limites, self.erros = [], 0
        self.ids_execucao = []
        self.parar = False
        self.motivo_fim = ""
        self.externos = 0
        self.sync = None
        self.conferencia = None


def _log(execucao):
    def logar(mensagem, nivel="INFO", fonte=""):
        LogExecucao.objects.create(execucao=execucao, nivel=nivel, fonte=fonte, mensagem=str(mensagem)[:2000])
    return logar


def _salvar(execucao, **campos):
    for k, v in campos.items():
        setattr(execucao, k, v)
    campos["heartbeat_em"] = timezone.now()
    Execucao.objects.filter(pk=execucao.pk).update(**campos)


def _fonte(codigo):
    return Fonte.objects.filter(codigo=codigo, habilitada=True).first()


# ------------------------------------------------------------------ etapas

def _descobrir(execucao, ctx, est):
    pesquisa = execucao.pesquisa
    if execucao.gatilho == Execucao.Gatilho.CSV:
        fonte = Fonte.objects.get(codigo=conectores.CODIGO_CSV)
        resultados = [ResultadoDescoberta(**r) for r in execucao.parametros.get("resultados", [])]
        fontes_resultados = [(fonte, iter(resultados))]
        ctx.log(f"Importação CSV: {len(resultados)} linha(s) válidas (arquivo {execucao.parametros.get('arquivo')})",
                fonte=fonte.codigo)
    else:
        fontes_resultados = []
        for fonte in pesquisa.fontes.filter(habilitada=True, tipo=Fonte.Tipo.DESCOBERTA):
            if not fonte.credencial_configurada:
                ctx.log(f"Fonte {fonte.nome} sem credencial ({fonte.variavel_credencial}); ignorada", "AVISO", fonte.codigo)
                est.limites.append(f"{fonte.codigo}: credencial ausente")
                continue
            fontes_resultados.append((fonte, conectores.conector_descoberta(fonte).descobrir(pesquisa, ctx)))
        if not fontes_resultados:
            ctx.log("Nenhuma fonte de descoberta habilitada nesta pesquisa", "AVISO")
            est.limites.append("sem fonte de descoberta habilitada")

    resultados = novos = existentes = 0
    for fonte, gerador in fontes_resultados:
        try:
            for r in gerador:
                ctx.verificar()
                with transaction.atomic():
                    cand, criado = idf.registrar_resultado(r, fonte, execucao, pesquisa)
                    motivo = motivo_exclusao(pesquisa, nome=cand.nome, atividade=cand.atividade_fonte,
                                             cnpj=cand.cnpj_candidato)
                    if motivo and cand.status not in (Candidato.Status.DESCARTADO,):
                        cand.status, cand.motivo_exclusao = Candidato.Status.EXCLUIDO, motivo[:255]
                        cand.save(update_fields=["status", "motivo_exclusao"])
                    ev.aplicar_textos(RegraEvidencia.Alvo.NOME, [(cand.nome, cand.url_referencia, fonte.codigo)],
                                      candidato=cand, empresa=cand.empresa)
                    ev.aplicar_textos(RegraEvidencia.Alvo.ATIVIDADE_FONTE,
                                      [(cand.atividade_fonte, cand.url_referencia, fonte.codigo)],
                                      candidato=cand, empresa=cand.empresa)
                est.ids_execucao.append(cand.pk)
                resultados += 1
                novos += criado
                existentes += not criado
                if resultados == 1 or resultados % 10 == 0:
                    _salvar(execucao, resultados=resultados, candidatos_novos=novos, candidatos_existentes=existentes,
                            progresso={"etapa": "descoberta", "resultados": resultados})
        except (FonteIndisponivel, ValueError) as e:
            est.erros += 1
            est.limites.append(f"{fonte.codigo}: {e}")
            ctx.log(f"Descoberta interrompida na fonte {fonte.nome}: {e}", "ERRO", fonte.codigo)
        except OrcamentoEsgotado as e:
            if isinstance(e, PrazoEsgotado):
                raise
            est.limites.append(str(e))
            ctx.log(f"Limite atingido: {e}. Resultados obtidos até aqui foram mantidos.", "AVISO", fonte.codigo)
    _salvar(execucao, resultados=resultados, candidatos_novos=novos, candidatos_existentes=existentes)


def _data_ref_carga(carga):
    from datetime import date as _date
    if carga.data_arquivos:
        return carga.data_arquivos
    a, m = carga.competencia.split("-")
    return _date(int(a), int(m), 1)


def _verificar_cnpj(cand, cnpj, metodo, enr, ctx, url=""):
    """
    Confirma o CNPJ: primeiro na base Receita local (carga ativa, sem custo), depois na BrasilAPI.
    Vincula só se a correspondência for confiável (candidato da própria base Receita é o registro oficial).
    Retorna empresa|None.
    """
    from prospeccao.conectores.receita import consultar_local, referencia
    dados_local, carga = consultar_local(cnpj)
    if dados_local is not None:
        if cand.fonte.codigo == "receita_cnpj":
            cand.confianca = Candidato.Confianca.ALTA
            cand.detalhe_identificacao = f"[RECEITA_BASE] registro oficial – {referencia(carga)}"
        else:
            status, conf, det = idf.avaliar_correspondencia(cand, dados_local)
            cand.confianca, cand.detalhe_identificacao = conf, f"[{metodo}{' ' + url if url else ''}] {det} (base local)"
            if status != Candidato.Status.IDENTIFICADO:
                cand.status, cand.cnpj_candidato = Candidato.Status.CONFLITO, cnpj
                cand.save()
                return None
        cand.cnpj_candidato = cnpj
        cand.save()
        return idf.vincular(cand, cnpj, dados=dados_local, fonte_enriquecimento="receita_dados_abertos",
                            metodo="RECEITA_BASE" if cand.fonte.codigo == "receita_cnpj" else metodo,
                            data_referencia=_data_ref_carga(carga), rotulo_fonte=referencia(carga))
    if enr is None:
        return None
    dados = enr.consultar(cnpj, ctx)
    if dados is None:
        cand.status, cand.confianca = Candidato.Status.CONFLITO, Candidato.Confianca.BAIXA
        cand.detalhe_identificacao = f"CNPJ {cnpj} ({metodo}) não encontrado na Receita via BrasilAPI"
        cand.cnpj_candidato = cnpj
        cand.save()
        return None
    status, conf, det = idf.avaliar_correspondencia(cand, dados)
    cand.confianca, cand.detalhe_identificacao, cand.cnpj_candidato = conf, f"[{metodo}{' ' + url if url else ''}] {det}", cnpj
    if status != Candidato.Status.IDENTIFICADO:
        cand.status = Candidato.Status.CONFLITO
        cand.save()
        return None
    cand.save()
    return idf.vincular(cand, cnpj, dados=dados, fonte_enriquecimento=enr.codigo, metodo=metodo,
                        data_referencia=timezone.localdate(), rotulo_fonte="BrasilAPI (consulta)")


def _identificar(execucao, ctx, est):
    pesquisa = execucao.pesquisa
    enr_fonte = _fonte("brasilapi_cnpj") if pesquisa.enriquecer_por_cnpj else None
    enr = conectores.ENRIQUECIMENTO["brasilapi_cnpj"](enr_fonte) if enr_fonte else None
    site_fonte = _fonte("site_publico") if pesquisa.identificar_por_site else None
    site = conectores.IDENTIFICACAO["site_publico"](site_fonte) if site_fonte else None
    if not enr:
        ctx.log("Verificação de CNPJ desligada/indisponível: CNPJs encontrados ficam aguardando verificação", "AVISO")
    validade = timezone.now() - timedelta(days=pesquisa.validade_dados_dias)
    # esta execução primeiro, depois pendentes de execuções anteriores (reaproveitamento)
    base = Candidato.objects.filter(pesquisas=pesquisa, status__in=[Candidato.Status.PENDENTE,
                                                                    Candidato.Status.CNPJ_ENCONTRADO])
    ordem = {pk: i for i, pk in enumerate(est.ids_execucao)}
    candidatos = sorted(base.select_related("fonte"), key=lambda c: ordem.get(c.pk, 10 ** 9))
    indice, sites_lidos, identificados = None, 0, 0
    sites_esgotado = enr_esgotado = False
    for cand in candidatos:
        ctx.verificar()
        try:
            with transaction.atomic():
                if cand.status == Candidato.Status.CNPJ_ENCONTRADO and (cand.fonte.codigo == "receita_cnpj"
                                                                        or (enr and not enr_esgotado)):
                    if _verificar_cnpj(cand, cand.cnpj_candidato, cand.metodo_identificacao or "FONTE", enr, ctx):
                        identificados += 1
                    continue
                if (cand.status == Candidato.Status.PENDENTE and site and cand.site and not sites_esgotado
                        and sites_lidos < pesquisa.max_sites_execucao
                        and (not cand.site_verificado_em or cand.site_verificado_em < validade)):
                    sites_lidos += 1
                    res = site.analisar(cand.site, ctx)
                    cand.site_verificado_em = timezone.now()
                    cand.dados = {**cand.dados, "site_emails": dict(list(res.emails.items())[:10]),
                                  "site_telefones": dict(list(res.telefones.items())[:10]),
                                  "site_cnpjs": res.cnpjs, "site_erros": res.erros[:5]}
                    cand.save()
                    ev.aplicar_textos(RegraEvidencia.Alvo.TEXTO_SITE, [(t, u, "site_publico") for u, t in res.paginas],
                                      candidato=cand, empresa=cand.empresa)
                    if res.erros and not res.paginas:
                        ctx.log(f"Site de '{cand.nome}' não lido: {res.erros[0][:200]}", "AVISO", "site_publico")
                    cnpjs = list(res.cnpjs)
                    if len(cnpjs) == 1:
                        cand.cnpj_candidato, cand.metodo_identificacao = cnpjs[0], "SITE"
                        cand.status = Candidato.Status.CNPJ_ENCONTRADO
                        cand.save()
                        if enr and not enr_esgotado and _verificar_cnpj(cand, cnpjs[0], "SITE", enr, ctx, res.cnpjs[cnpjs[0]]):
                            identificados += 1
                        continue
                    if len(cnpjs) > 1:
                        cand.status = Candidato.Status.CONFLITO
                        cand.detalhe_identificacao = "Site publica mais de um CNPJ: " + ", ".join(cnpjs[:6])
                        cand.save()
                        continue
                if cand.status == Candidato.Status.PENDENTE:
                    indice = indice or idf.IndiceCarteira()
                    cand.sugestoes = indice.sugerir(cand)
                    if cand.sugestoes and not cand.detalhe_identificacao:
                        cand.detalhe_identificacao = "Possíveis correspondências na base – confirmar na revisão"
                    cand.save(update_fields=["sugestoes", "detalhe_identificacao"])
        except OrcamentoEsgotado as e:
            if isinstance(e, PrazoEsgotado):
                raise
            est.limites.append(str(e))
            ctx.log(f"Limite atingido: {e}", "AVISO")
            if "site_publico" in str(e):
                sites_esgotado = True
            else:
                enr_esgotado = True
            if sites_esgotado and enr_esgotado:
                break
        except FonteIndisponivel as e:
            est.erros += 1
            ctx.log(f"Fonte indisponível ao identificar '{cand.nome}': {e}", "ERRO")
            if "brasilapi" in str(e):
                enr_esgotado = True
                est.limites.append(f"verificação de CNPJ interrompida: {e}")
    if sites_lidos >= pesquisa.max_sites_execucao:
        restantes = base.filter(status=Candidato.Status.PENDENTE, site_verificado_em__isnull=True).exclude(site="").count()
        if restantes:
            est.limites.append(f"limite de sites por execução ({pesquisa.max_sites_execucao}); {restantes} site(s) ficam "
                               "para a próxima execução")
    _salvar(execucao, identificados=identificados)


def _cruzar(execucao, ctx, est):
    pesquisa = execucao.pesquisa
    cands = (Candidato.objects.filter(pesquisas=pesquisa, status=Candidato.Status.IDENTIFICADO, empresa__isnull=False)
             .filter(pk__in=est.ids_execucao) | Candidato.objects.filter(
        pesquisas=pesquisa, status=Candidato.Status.IDENTIFICADO, ultima_execucao=execucao)).distinct()
    novas = conhecidos = 0
    empresas_vistas = set()
    for cand in cands.select_related("empresa"):
        ctx.verificar()
        e = cand.empresa
        if e.pk in empresas_vistas:
            continue
        empresas_vistas.add(e.pk)
        with transaction.atomic():
            fonte_cnae = (e.dados_coletados.filter(campo="cnae").order_by("-coletado_em")
                          .values_list("fonte", flat=True).first())
            if not fonte_cnae:
                ctx.log(f"{e.nome}: sem dados cadastrais (Receita) ainda", "AVISO")
            cnaes = ([(e.cnae, e.atividade_principal)] if e.cnae else []) + \
                [(c.get("codigo"), c.get("descricao")) for c in (e.cnaes_secundarios or [])]
            ev.aplicar_cnaes(cnaes, empresa=e, fonte=fonte_cnae or "desconhecida")
            motivo = motivo_exclusao(pesquisa, nome=e.nome, atividade=e.atividade_principal, cnaes=[c for c, _ in cnaes],
                                     cnpj=e.cnpj or "")
            if motivo:
                cand.status, cand.motivo_exclusao = Candidato.Status.EXCLUIDO, motivo[:255]
                cand.save(update_fields=["status", "motivo_exclusao"])
                ctx.log(f"{e.nome}: {motivo}", fonte="exclusao")
                continue
            op, criada, sit = ops.avaliar(e, pesquisa)
            # "novas" = somente oportunidades EXTERNAS (CNPJ fora da carteira e dos leads BRIDA)
            novas += bool(criada and sit == cz.Situacao.NOVA_EXTERNA)
            if sit in (cz.Situacao.CLIENTE_CARTEIRA, cz.Situacao.BLOQUEADA, cz.Situacao.NAO_CONTATAR,
                       cz.Situacao.CADASTRO_SEM_COMPRA, cz.Situacao.LEAD_CONHECIDO):
                conhecidos += 1
    pendentes = Candidato.objects.filter(pesquisas=pesquisa, status__in=[
        Candidato.Status.PENDENTE, Candidato.Status.CNPJ_ENCONTRADO, Candidato.Status.CONFLITO]).count()
    _salvar(execucao, oportunidades_novas=novas, conhecidos_carteira=conhecidos, pendentes=pendentes)


def _complementar(execucao, ctx, est):
    """
    Para empresas EXTERNAS identificadas nesta execução: procura site/canais com os conectores disponíveis
    (site informado pela fonte ou domínio próprio do e-mail cadastral), confere se o site publica o MESMO
    CNPJ, registra fontes e datas, mantém conflitos para revisão e roda a IA (se configurada) só sobre o
    texto efetivamente coletado. Consultas ainda válidas não são repetidas.
    """
    from carteira.models import CanalEmpresa, DadoColetado, Empresa
    from carteira.services.normalizacao import dominio_de_email, dominio_de_url
    from prospeccao.models import CategoriaProduto, Evidencia, Oportunidade
    from prospeccao.services import ia
    from prospeccao.services.enriquecimento import registrar_canal, registrar_dado

    pesquisa = execucao.pesquisa
    site_fonte = _fonte("site_publico") if pesquisa.identificar_por_site else None
    if not site_fonte:
        ctx.log("Complementação por site desligada nesta pesquisa", "AVISO")
        return
    site = conectores.IDENTIFICACAO["site_publico"](site_fonte)
    analisador = ia.obter_analisador()
    ia_ok, ia_msg = ia.status()
    ctx.log(ia_msg, "INFO" if ia_ok else "AVISO", "ia")
    catalogo = list(CategoriaProduto.objects.filter(ativa=True).values_list("nome", flat=True))
    validade = timezone.now() - timedelta(days=pesquisa.validade_dados_dias)
    empresas = Empresa.objects.filter(
        candidatos__pk__in=est.ids_execucao, candidatos__status=Candidato.Status.IDENTIFICADO,
        oportunidade__tipo=Oportunidade.Tipo.NOVA_EXTERNA).distinct()
    feitos = analises = sem_dominio = 0
    for e in empresas:
        ctx.verificar()
        ultimo = DadoColetado.objects.filter(empresa=e, campo="complemento_site").first()
        if ultimo and ultimo.coletado_em >= validade:
            continue
        if e.candidatos.filter(site_verificado_em__gte=validade).exclude(site="").exists():
            continue  # site já lido na identificação (dentro da validade): não repete a consulta
        dominio = dominio_de_url(e.site) or dominio_de_email(e.email) or next(
            (dominio_de_email(c.valor) for c in e.canais.filter(tipo="EMAIL") if dominio_de_email(c.valor)), "")
        if not dominio:
            sem_dominio += 1
            registrar_dado(e, "complemento_site", "sem site conhecido e e-mail cadastral sem domínio próprio",
                           "site_publico")
            continue
        motivo_pulo = _dominio_suspeito(dominio)
        if motivo_pulo:
            registrar_dado(e, "complemento_site", f"NAO_RELACIONADO: domínio {dominio} {motivo_pulo}", "site_publico")
            continue
        if feitos >= pesquisa.max_sites_execucao:
            est.limites.append(f"limite de sites por execução na complementação ({pesquisa.max_sites_execucao})")
            break
        feitos += 1
        url = e.site or f"https://{dominio}"
        try:
            res = site.analisar(url, ctx)
        except OrcamentoEsgotado as ex:
            if isinstance(ex, PrazoEsgotado):
                raise
            est.limites.append(str(ex))
            break
        if not res.paginas:
            registrar_dado(e, "complemento_site", f"site não lido: {'; '.join(res.erros)[:400]}", "site_publico", url)
            continue
        cnpjs = set(res.cnpjs)
        if e.cnpj in cnpjs:
            verif, nota = CanalEmpresa.Verificacao.CONFIRMADO_FONTE, "site publica o mesmo CNPJ"
        elif any(c[:8] == e.cnpj[:8] for c in cnpjs):
            verif, nota = CanalEmpresa.Verificacao.CONFLITO, ("site publica CNPJ de outro estabelecimento do mesmo "
                                                              "grupo (matriz/filial) – não associado automaticamente")
        elif cnpjs:
            verif, nota = CanalEmpresa.Verificacao.CONFLITO, f"site publica outro CNPJ ({', '.join(sorted(cnpjs))[:60]})"
        elif _site_menciona_empresa(e, dominio, res.paginas):
            verif, nota = CanalEmpresa.Verificacao.ENCONTRADO, ("provável: domínio do e-mail cadastral e o site menciona "
                                                                "o nome da empresa (não publica CNPJ)")
        else:
            registrar_dado(e, "complemento_site", f"NAO_RELACIONADO: {url} não publica CNPJ nem menciona o nome da "
                           "empresa (provável contador/terceiro) – canais e textos NÃO associados", "site_publico",
                           res.paginas[0][0])
            continue
        registrar_canal(e, CanalEmpresa.Tipo.SITE, url, "site_publico", res.paginas[0][0], "COMERCIAL", verificacao=verif)
        registrar_dado(e, "complemento_site", f"{verif}: {nota}", "site_publico", res.paginas[0][0])
        if verif == CanalEmpresa.Verificacao.CONFLITO:
            ctx.log(f"{e.nome}: {nota} – mantido para revisão", "AVISO", "site_publico")
            continue
        if verif == CanalEmpresa.Verificacao.CONFIRMADO_FONTE and not e.site:
            e.site, e.dominio = url[:300], dominio
            e.save(update_fields=["site", "dominio", "atualizado_em"])
        for email, u in list(res.emails.items())[:6]:
            if dominio_de_email(email) == dominio:
                registrar_canal(e, CanalEmpresa.Tipo.EMAIL, email, "site_publico", u, verificacao=verif)
        for tel, u in list(res.telefones.items())[:4]:
            registrar_canal(e, CanalEmpresa.Tipo.TELEFONE, tel, "site_publico", u, "COMERCIAL", verificacao=verif)
        ev.aplicar_textos(RegraEvidencia.Alvo.TEXTO_SITE, [(t, u, "site_publico") for u, t in res.paginas], empresa=e)
        from prospeccao.services import escopo as esc
        pertinente, mot_esc = esc.pertinente_para_gasto(e)
        if analisador and not pertinente:
            ctx.log(f"{e.nome}: {mot_esc}", "INFO", "escopo")
        if analisador and pertinente:
            from prospeccao.models import UsoIA
            from prospeccao.services import orcamento as orcm
            orc = getattr(est, "orcamento", None) or orcm.Orcamento(pesquisa, execucao)
            est.orcamento = orc
            uso = UsoIA(tipo="ANALISE_SITE", provedor=analisador.nome, modelo=analisador.modelo, pesquisa=pesquisa,
                        empresa=e, versao_prompt="site-v1",
                        materiais=[{"url": u, "caracteres": len(t)} for u, t in res.paginas[:3]])
            texto = " ".join(t[:12_000] for _, t in res.paginas[:3])
            try:
                r, reserva, inicio = orcm.executar_ia(orc, analisador, uso, texto,
                                                      lambda: analisador.analisar(e, res.paginas, catalogo))
            except orcm.SemOrcamento as ex:
                ctx.log(f"IA não executada: {ex}", "AVISO", "ia")
                est.limites.append(f"pendência: {ex}")
                analisador, r = None, None
            except Exception as ex:  # falha da IA não interrompe as etapas independentes
                ctx.log(f"IA falhou para {e.nome}: {type(ex).__name__}: {str(ex)[:200]}", "AVISO", "ia")
                r = None
            else:
                uso.status = "RECUSA" if (r is None or r.recusou) else "OK"
                ia.registrar_uso(uso, getattr(r, "usage", None), inicio)
                orc.liquidar(reserva, uso.custo_estimado_usd, ia=True)
            if r is not None:
                analises += 1
                _gravar_ia(e, r, Evidencia, CategoriaProduto, Oportunidade)
        ops.avaliar(e, pesquisa)
    ctx.log(f"Complementação: {feitos} site(s) consultado(s), {sem_dominio} sem domínio próprio, "
            f"{analises} análise(s) de IA", fonte="site_publico")
    _salvar(execucao, complementados=feitos, ia_analises=analises)


_PADRAO_CONTADOR = __import__("re").compile(r"contab|contador|escritorio|assessoriacont|fiscal|tributar", __import__("re").I)


def _dominio_suspeito(dominio):
    """Domínio de contador ou compartilhado por várias empresas de grupos diferentes na base Receita."""
    if _PADRAO_CONTADOR.search(dominio):
        return "parece de escritório contábil"
    from receita.models import CargaReceita, EstabelecimentoReceita
    carga = CargaReceita.ativa_atual()
    if carga:
        raizes = (EstabelecimentoReceita.objects.filter(carga=carga, email__iendswith="@" + dominio)
                  .values("cnpj_basico").distinct()[:6].count())
        if raizes > 5:
            return "é usado no e-mail cadastral de mais de 5 empresas de grupos diferentes (provável contador/terceiro)"
    return ""


def _site_menciona_empresa(empresa, dominio, paginas):
    from carteira.services.normalizacao import normalizar_texto
    from prospeccao.services.identificacao import tokens_distintivos
    tokens = {t for t in tokens_distintivos(empresa.razao_social or empresa.nome) | tokens_distintivos(empresa.nome_fantasia)
              if len(t) >= 4}
    if not tokens:
        return False
    texto = normalizar_texto(" ".join(t[:50000] for _, t in paginas))
    dom = normalizar_texto(dominio.replace(".", " "))
    return any(f" {t} " in f" {texto} " or t in dom.replace(" ", "") for t in tokens)


def _gravar_ia(empresa, r, Evidencia, CategoriaProduto, Oportunidade):
    import hashlib
    cats = {c.nome: c for c in CategoriaProduto.objects.all()}
    for item in r.evidencias:
        chave = hashlib.sha256(f"ia|{empresa.pk}|{item['url']}|{item['trecho']}".encode()).hexdigest()
        evid, criada = Evidencia.objects.get_or_create(chave=chave, defaults=dict(
            empresa=empresa, tipo=item["tipo"], descricao=f"IA: {item['descricao']}"[:255],
            classificacao=Evidencia.Classificacao.HIPOTESE, fonte=f"ia:{r.modelo}"[:60], url=item["url"][:500],
            trecho=item["trecho"][:300], gerada_por="IA"))
        if criada:
            evid.aplicacoes.set([cats[a] for a in item["aplicacoes"] if a in cats])
    Oportunidade.objects.filter(empresa=empresa).update(
        resumo_ia=("(IA recusou a análise)" if r.recusou else f"{r.atividade} {r.explicacao}".strip())[:2000],
        perguntas_ia=r.perguntas, ia_modelo=r.modelo[:60], ia_em=timezone.now())


# ------------------------------------------------------------------ orquestração

def _custo_total(ctx, est):
    """Custo das fontes + o gasto MEDIDO de IA e busca web desta execução (UsoIA/ConsultaWeb via orçamento)."""
    from decimal import Decimal
    orc = getattr(est, "orcamento", None)
    return Decimal(str(ctx.custo or 0)) + Decimal(str(getattr(orc, "gasto", 0) or 0))


def executar(execucao_id):
    """Executa uma execução PENDENTE. Retorna a execução (ou None se outra instância já a pegou)."""
    pegou = Execucao.objects.filter(pk=execucao_id, status=Execucao.Status.PENDENTE).update(
        status=Execucao.Status.EXECUTANDO, iniciada_em=timezone.now(), heartbeat_em=timezone.now())
    if not pegou:
        return None
    execucao = Execucao.objects.select_related("pesquisa").get(pk=execucao_id)
    ctx = ContextoExecucao(execucao, prazo_segundos=execucao.pesquisa.max_duracao_segundos, logar=_log(execucao))
    est = _Estado()
    status, mensagem = Execucao.Status.CONCLUIDA, ""
    from prospeccao.services import prontidao
    conf = prontidao.conferir(execucao.pesquisa)
    imp, avisos_prep = conf["impedimentos"], conf["avisos"]
    if imp:  # requisito indispensável ausente: para AGORA, sem consultar nada
        for i in imp:
            ctx.log(f"Falta: {i['falta']}. Impede: {i['etapa']}. Como resolver: {i['como']}.", "ERRO", "preparacao")
        _salvar(execucao, status=Execucao.Status.BLOQUEADA, finalizada_em=timezone.now(), completa=False,
                mensagem=prontidao.texto_impedimentos(imp)[:2000], etapa="fim",
                limites_atingidos=[f"bloqueio: {i['falta']}" for i in imp])
        return execucao
    for a in avisos_prep:
        ctx.log(a, "AVISO", "preparacao")
    for i in conf["info"]:
        ctx.log(i, "INFO", "preparacao")
    _ok_sync, aviso_sync = cz.tentar_atualizar_carteira()
    atual, quando, horas = cz.cruzamento_atual()
    ctx.log(aviso_sync, "AVISO" if not _ok_sync else "INFO")
    try:
        if execucao.pesquisa.modo == "PLANEJADA":
            from prospeccao.services import meta as meta_srv
            from prospeccao.services import selecao
            est.sync = (bool(quando), quando, horas)
            est.aviso_carteira = aviso_sync
            est.conferencia = None
            est.sf_falhou = False

            def _lote(ids):
                if est.parar or not ids:
                    return
                selecao.investigar(execucao, ctx, est, somente=list(ids), recalcular=False)
                painel = meta_srv.atualizar(execucao, est)
                # com várias frentes, as cotas por frente encerram cada uma; a meta total não corta as seguintes
                # meta só encerra depois da 1ª consulta em TODAS as cidades (cobertura antes de aprofundar)
                if painel["novos_validados"] >= painel["meta"] and painel["meta"] and not getattr(est, "multi_frente", False)                         and not getattr(est, "cidades_pendentes", None):
                    est.parar = True
                    est.motivo_fim = "meta"
                    ctx.log(f"Meta de {painel['meta']} novos leads atingida", fonte="meta")

            _salvar(execucao, etapa="descoberta")
            selecao.selecionar(execucao, ctx, est, on_lote=_lote)
            if not est.parar:
                _salvar(execucao, etapa="investigacao")
                selecao.investigar(execucao, ctx, est)
            meta_srv.atualizar(execucao, est)
        else:
            etapas = (("descoberta", _descobrir), ("identificacao", _identificar), ("cruzamento", _cruzar),
                      ("complementacao", _complementar))
            for nome, etapa in etapas:
                _salvar(execucao, etapa=nome)
                etapa(execucao, ctx, est)
        pend = list(getattr(ctx, "pendencias", []) or [])
        est.limites += [p for p in pend if p not in est.limites]
        if est.limites or est.erros:
            status = Execucao.Status.PARCIAL
            if pend and not execucao.resultados:
                mensagem = "Nenhum resultado e a busca não foi efetivamente concluída: " + "; ".join(pend)[:500]
    except ExecucaoPausada:
        status, mensagem = Execucao.Status.PARCIAL, "Pausada pelo usuário. Resultados gravados foram mantidos."
        est.motivo_fim = "pausa"
    except ExecucaoCancelada:
        status, mensagem = Execucao.Status.CANCELADA, "Cancelada a pedido do usuário; dados já gravados foram mantidos."
    except OrcamentoEsgotado as e:
        status, mensagem = Execucao.Status.PARCIAL, f"Interrompida por limite: {e}. Dados já gravados foram mantidos."
        est.limites.append(str(e))
    except Exception as e:
        texto = str(e).lower()
        if any(p in texto for p in ("no space left", "could not extend file", "disk full", "espaço em disco")):
            status = Execucao.Status.INTERROMPIDA
            mensagem = ("Interrompida por falta de espaço em disco: o banco não conseguiu gravar. O que já foi gravado "
                        "foi mantido; libere espaço e execute de novo para retomar.")
        else:
            status = Execucao.Status.FALHA
            mensagem = f"{type(e).__name__}: {e}"
        try:
            ctx.log(traceback.format_exc()[-3000:], "ERRO")
        except Exception:
            pass
    finally:
        _salvar(execucao, status=status, finalizada_em=timezone.now(), mensagem=mensagem,
                completa=(status == Execucao.Status.CONCLUIDA), limites_atingidos=est.limites,
                erros=est.erros + (1 if status == Execucao.Status.FALHA else 0), consultas=ctx.consultas,
                custo=_custo_total(ctx, est), etapa="fim")
        if execucao.pesquisa.modo == "PLANEJADA":
            from prospeccao.services import meta as meta_srv
            meta_srv.definir_motivo(execucao, est, status)
            try:
                continuar_automaticamente(execucao, est, status)
            except Exception as e:  # a continuação nunca derruba o fechamento da execução
                ctx.log(f"Continuação automática não agendada: {type(e).__name__}: {e}", "AVISO", "continuacao")
    return execucao


MAX_CONTINUACOES = 6
# Motivos em que outra execução NÃO adianta (decisão do usuário, dinheiro ou franquia acabaram, ou a retomada
# por velocidade já tem agendamento próprio).
_SEM_CONTINUACAO = ("cancelamento", "pausa", "franquia", "sem_leitura", "orcamento", "velocidade")


def avaliar_continuacao(execucao, est, status):
    """Decide se a pesquisa continua sozinha. Retorna (continuar, motivo). Não grava.

    Continua quando a execução parou por limite interno (tempo, páginas, chamadas de IA, rodadas) com trabalho útil
    pendente E houve avanço nesta execução. Sem avanço, para: é um ciclo de consultas repetidas."""
    from prospeccao.models import ConsultaWeb, SelecaoPesquisa
    from prospeccao.services import descoberta_google as dg
    from prospeccao.services import meta as meta_srv
    pesquisa = execucao.pesquisa
    motivo = ((execucao.progresso or {}).get("motivo") or getattr(est, "motivo_fim", "") or "")
    if status not in (Execucao.Status.PARCIAL, Execucao.Status.CONCLUIDA):
        return False, f"execução {status.lower()}"
    limites = " ".join(execucao.limites_atingidos or []).lower()
    so_ia = motivo == "orcamento" and "teto financeiro" not in limites and "teto global" not in limites \
        and "busca web" not in limites
    if motivo in _SEM_CONTINUACAO and not so_ia:  # falta só de IA não impede a busca dirigida na franquia
        return False, f"encerrada por {meta_srv.MOTIVOS.get(motivo, motivo).lower()}"
    n = int((execucao.parametros or {}).get("continuacao") or 0)
    if n >= MAX_CONTINUACOES:
        return False, f"{n} continuações automáticas já feitas; o restante fica como pendência visível"
    painel = (execucao.progresso or {}).get("painel") or meta_srv.resumo(pesquisa, est)
    if painel.get("meta") and painel.get("leads_completos", 0) >= painel["meta"]:
        return False, "meta de leads completos atingida"
    pend = dg.pendentes_identificacao(pesquisa)
    if not pend:
        return False, "nenhuma empresa pendente de identificação ou contato"
    feitas = {dg.norm_consulta(q) for q in ConsultaWeb.objects.filter(
        pesquisa=pesquisa, finalidade="COMPLEMENTAR", status="OK").values_list("consulta", flat=True)}
    from prospeccao.services.lote_identificacao import montar_consultas
    com_trabalho = 0
    for sel in pend:
        e = sel.empresa
        if (e.cnpj or "").strip():
            com_trabalho += not (e.situacao_cadastral or "").strip()
            continue
        if any(dg.norm_consulta(q) not in feitas for q in montar_consultas(e)):
            com_trabalho += 1
    if not com_trabalho:
        return False, "as buscas dirigidas possíveis já foram feitas; o que falta fica para conferência"
    avancou = (ConsultaWeb.objects.filter(execucao=execucao, status="OK").exists()
               or SelecaoPesquisa.objects.filter(pesquisa=pesquisa, ultima_execucao=execucao).exists())
    if not avancou:
        return False, "esta execução não avançou (nenhuma consulta concluída); parada para não repetir o ciclo"
    return True, (f"{com_trabalho} empresa(s) ainda com identificação a fazer; parada desta execução: "
                  f"{meta_srv.MOTIVOS.get(motivo, motivo) or 'limite interno'}")


def continuar_automaticamente(execucao, est, status):
    """Salva o progresso e enfileira a próxima execução, sem comando manual. O worker em andamento a executa."""
    from prospeccao.services import fila
    from prospeccao.services.prontidao import em_teste
    continuar, motivo = avaliar_continuacao(execucao, est, status)
    progresso = dict(execucao.progresso or {})
    n = int((execucao.parametros or {}).get("continuacao") or 0)
    if continuar and em_teste() and not (execucao.parametros or {}).get("permitir_continuacao_em_teste"):
        continuar, motivo = False, "testes automatizados não enfileiram continuação"
    elif continuar and not em_teste():
        from prospeccao.services.lote_identificacao import franquia_permite
        if franquia_permite() is None:  # leitura da conta não gasta busca
            continuar, motivo = False, "franquia SerpApi sem saldo ou não legível; pendências ficam salvas"
    if continuar:
        nova, criada = fila.enfileirar(execucao.pesquisa, Execucao.Gatilho.AGENDADA, parametros={
            "retomada_de": execucao.pk, "continuacao": n + 1, "motivo": f"continuação automática: {motivo}"[:300]})
        texto = (f"execução {nova.pk} enfileirada automaticamente ({n + 1}/{MAX_CONTINUACOES}): {motivo}"
                 if criada else f"execução {nova.pk} já estava na fila")
        if criada:
            fila.disparar_worker()
    else:
        texto = f"não necessária: {motivo}"
    progresso["continuacao"] = texto
    Execucao.objects.filter(pk=execucao.pk).update(progresso=progresso)
    execucao.progresso = progresso
    LogExecucao.objects.create(execucao=execucao, nivel="INFO", fonte="continuacao",
                               mensagem=f"Continuação automática {texto}"[:4000])
    return continuar, texto
