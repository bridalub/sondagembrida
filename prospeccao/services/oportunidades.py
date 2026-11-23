"""
Oportunidades: uma por empresa (OneToOne), atualizada a cada execução sem perder decisões humanas.

Prioridade (regras explicadas em `prioridade_criterios`, revisável manualmente):
  ALTA                o SITE da empresa descreve a operação + canal de contato + cadastro ATIVO
  MEDIA               há indício (categoria da fonte, CNAE, nome, IA), mas falta algum dos itens acima
  INVESTIGAR          nenhuma evidência ainda – aplicação a investigar (NÃO é ausência de potencial)
  VERIFICAR_CADASTRO  situação cadastral SUSPENSA/INAPTA/BAIXADA/NULA

Situação cadastral (Receita) – separada do bloqueio interno (Protheus/Salesforce):
  ATIVA              pode avançar para a fila, respeitadas as demais regras
  SUSPENSA / INAPTA  revisão (não libera)
  BAIXADA / NULA     fora da fila de novas abordagens
  ausente / falha    pendente (nunca se presume ATIVA)
A aplicação de óleo/graxa/filtros é sempre HIPÓTESE até confirmação em conversa.
"""
from datetime import date

from django.db import transaction
from django.utils import timezone

from carteira.models import CanalEmpresa, Empresa
from prospeccao.models import DecisaoRevisao, Evidencia, Oportunidade
from prospeccao.services import cruzamento as cz
from prospeccao.services.evidencias import garantir_insuficiente

TIPO_POR_SITUACAO = {
    cz.Situacao.NOVA_EXTERNA: Oportunidade.Tipo.NOVA_EXTERNA,
    cz.Situacao.LEAD_CONHECIDO: Oportunidade.Tipo.LEAD_CONHECIDO,
    cz.Situacao.CADASTRO_SEM_COMPRA: Oportunidade.Tipo.CADASTRO_SEM_COMPRA,
}
PERGUNTA_PADRAO = "Confirmar quem realiza a manutenção dos equipamentos/veículos e quem compra os insumos."


class RegraViolada(Exception):
    pass


def classe_situacao(empresa):
    """OK | REVISAO | FORA | PENDENTE"""
    s = (empresa.situacao_cadastral or "").strip().upper()
    if s == "ATIVA":
        return "OK"
    if s in ("SUSPENSA", "INAPTA"):
        return "REVISAO"
    if s in ("BAIXADA", "NULA"):
        return "FORA"
    return "PENDENTE"


def _anos(d):
    hoje = date.today()
    return hoje.year - d.year - ((hoje.month, hoje.day) < (d.month, d.day))


def _qualidade(empresa, fontes_enriquecimento):
    pend = []
    if not fontes_enriquecimento:
        pend.append("CNPJ não verificado na Receita")
    if not empresa.situacao_cadastral:
        pend.append("Situação cadastral desconhecida (pendente)")
    elif classe_situacao(empresa) != "OK":
        pend.append(f"Situação cadastral {empresa.situacao_cadastral}")
    if not empresa.data_abertura:
        pend.append("Data de abertura desconhecida")
    if not (empresa.telefone or empresa.canais.filter(tipo=CanalEmpresa.Tipo.TELEFONE).exists()):
        pend.append("Sem telefone")
    if not (empresa.cidade and empresa.uf):
        pend.append("Localização incompleta")
    if not empresa.cnpj:
        nivel = Oportunidade.Qualidade.INSUFICIENTE
    elif not pend:
        nivel = Oportunidade.Qualidade.COMPLETA
    elif fontes_enriquecimento:
        nivel = Oportunidade.Qualidade.PARCIAL
    else:
        nivel = Oportunidade.Qualidade.INSUFICIENTE
    return nivel, pend


def _prioridade(empresa, evidencias):
    crit = []
    # só o texto do PRÓPRIO SITE conta como descrição da operação; categoria de mapa/CNAE/nome/IA são indícios
    site_confirmado = empresa.canais.filter(tipo=CanalEmpresa.Tipo.SITE, verificacao__in=[
        CanalEmpresa.Verificacao.CONFIRMADO_FONTE, CanalEmpresa.Verificacao.CONFIRMADO_HUMANO]).exists() or \
        empresa.candidatos.filter(metodo_identificacao="SITE", status="IDENTIFICADO").exists()
    # só o texto de um site cuja identidade foi confirmada (publica o mesmo CNPJ) descreve a operação;
    # site "encontrado" pelo domínio do e-mail pode ser do contador/grupo: vale como indício
    confirmadas = [e for e in evidencias if e.classificacao == Evidencia.Classificacao.CONFIRMADO_FONTE
                   and e.fonte == "site_publico" and site_confirmado]
    hipoteses = [e for e in evidencias if e not in confirmadas]
    tem_canal = bool(empresa.telefone or empresa.canais.filter(
        tipo__in=[CanalEmpresa.Tipo.TELEFONE, CanalEmpresa.Tipo.EMAIL]).exists())
    classe = classe_situacao(empresa)
    ativa = classe == "OK"
    if classe in ("REVISAO", "FORA"):
        return (Oportunidade.Prioridade.VERIFICAR_CADASTRO,
                [f"Situação cadastral '{empresa.situacao_cadastral}' na Receita: "
                 + ("revisar antes de abordar" if classe == "REVISAO" else "fora da fila de novas abordagens")])
    crit.append(f"{len(confirmadas)} descrição(ões) de operação no site da empresa" if confirmadas
                else "site da empresa não descreve a operação (ou não foi lido)")
    if hipoteses:
        crit.append(f"{len(hipoteses)} indício(s) (categoria da fonte, CNAE, nome ou IA) – hipótese")
    crit.append("canal de contato disponível" if tem_canal else "sem canal de contato")
    if ativa:
        ref = empresa.situacao_cadastral_referencia
        crit.append("cadastro ATIVO na Receita"
                    + (f"; atualização do cadastro {empresa.situacao_cadastral_data:%d/%m/%Y}" if empresa.situacao_cadastral_data else "; atualização do cadastro não informada")
                    + (f"; fonte {empresa.situacao_cadastral_fonte}" if empresa.situacao_cadastral_fonte else "")
                    + (f"; competência ou consulta {ref:%d/%m/%Y}" if ref else ""))
    else:
        crit.append("situação cadastral pendente")
    if not confirmadas and not hipoteses:
        return Oportunidade.Prioridade.INVESTIGAR, crit + ["→ aplicação a investigar (falta de dado não é falta de potencial)"]
    if confirmadas and tem_canal and ativa:
        return Oportunidade.Prioridade.ALTA, crit
    return Oportunidade.Prioridade.MEDIA, crit


def _resumo(empresa, evidencias):
    partes = []
    atividade = empresa.atividade_principal or empresa.segmento or ""
    if atividade:
        partes.append(atividade.rstrip(".") + ".")
    for frase in dict.fromkeys(f"Informado pela fonte ({e.fonte}): {e.descricao.lower()}." for e in evidencias
                               if e.classificacao == Evidencia.Classificacao.CONFIRMADO_FONTE and e.gerada_por == "REGRA"):
        partes.append(frase)
    hip = [e.descricao.lower() for e in evidencias if e.classificacao == Evidencia.Classificacao.HIPOTESE
           and e.gerada_por == "REGRA"]
    if hip:
        partes.append("Indício: " + "; ".join(dict.fromkeys(hip)) + ".")
    cats = sorted({c.nome for e in evidencias if e.gerada_por == "REGRA" for c in e.aplicacoes.all()})
    if cats:
        partes.append("Hipótese de aplicação (regras): " + ", ".join(cats) + ". Confirmar em conversa.")
    else:
        partes.append("Aplicação dos produtos a investigar.")
    return " ".join(partes)


def avaliar(empresa, pesquisa=None, *, base="SINCRONIZACAO"):
    """Cria/atualiza a oportunidade da empresa. Retorna (oportunidade|None, criada: bool, situacao)."""
    empresa = Empresa.objects.select_related("indicador").get(pk=empresa.pk)
    sit, detalhe, alertas = cz.situacao_empresa(empresa)
    op = Oportunidade.objects.filter(empresa=empresa).first()
    sem_cnpj_web = sit == cz.Situacao.IDENTIFICACAO_PENDENTE and empresa.origem == Empresa.Origem.DESCOBERTA
    if sem_cnpj_web:
        alertas.append("CNPJ não identificado pelos dados disponíveis – identidade a conferir")
    if sit in (cz.Situacao.CLIENTE_CARTEIRA, cz.Situacao.BLOQUEADA, cz.Situacao.NAO_CONTATAR,
               cz.Situacao.IDENTIFICACAO_PENDENTE) and not sem_cnpj_web:
        if op:
            novo = {cz.Situacao.CLIENTE_CARTEIRA: Oportunidade.Status.JA_CLIENTE}.get(sit, Oportunidade.Status.BLOQUEADA)
            if sit != cz.Situacao.IDENTIFICACAO_PENDENTE and op.status != novo:
                op.status = novo
            op.situacao_carteira, op.cruzamento_detalhe, op.cruzado_em, op.cruzamento_base = sit, detalhe, timezone.now(), base
            op.save()
            if pesquisa:
                op.pesquisas.add(pesquisa)
        return op, False, sit

    criada = op is None
    if criada:
        op = Oportunidade(empresa=empresa, tipo=TIPO_POR_SITUACAO.get(sit, Oportunidade.Tipo.NOVA_EXTERNA))
    elif op.status in (Oportunidade.Status.JA_CLIENTE, Oportunidade.Status.BLOQUEADA,
                       Oportunidade.Status.FORA_FILA) and not op.status_definido_por_humano:
        op.status = Oportunidade.Status.NOVA
        alertas.append("Situação mudou desde a última avaliação")
    if not op.escopo_humano:
        from prospeccao.services import escopo as esc
        op.escopo, mot = esc.classificar(empresa)
        op.escopo_motivo = mot[:255]
    classe = classe_situacao(empresa)
    if classe == "FORA" and op.status in (Oportunidade.Status.NOVA, Oportunidade.Status.LIBERADA):
        op.status = Oportunidade.Status.FORA_FILA
    elif classe == "REVISAO":
        alertas.append(f"Situação cadastral {empresa.situacao_cadastral}: revisão necessária antes de abordar")
    elif classe == "PENDENTE":
        alertas.append("Situação cadastral não verificada: pendente")
    op.tipo = TIPO_POR_SITUACAO.get(sit, Oportunidade.Tipo.NOVA_EXTERNA)
    op.situacao_carteira, op.cruzamento_detalhe, op.cruzado_em, op.cruzamento_base = sit, detalhe, timezone.now(), base
    if sit == cz.Situacao.NOVA_EXTERNA:
        _valido, quando_sync, _horas_sync = cz.cruzamento_atual()
        if quando_sync:
            alertas.append(
                f"Usando a carteira sincronizada em {timezone.localtime(quando_sync).strftime('%d/%m/%Y %H:%M')}")
        else:
            alertas.append("Cruzamento pendente: não há carteira local. Não confirmado como novo.")

    fontes_enr = set(empresa.dados_coletados.filter(campo="situacao_cadastral").values_list("fonte", flat=True))
    garantir_insuficiente(empresa, set(empresa.candidatos.values_list("fonte__codigo", flat=True)) | fontes_enr)
    evidencias = list(Evidencia.objects.filter(empresa=empresa, ativa=True).select_related("regra")
                      .prefetch_related("aplicacoes"))
    reais = [e for e in evidencias if e.classificacao != Evidencia.Classificacao.INSUFICIENTE]
    op.qualidade_dados, op.pendencias_dados = _qualidade(empresa, fontes_enr)
    if not op.prioridade_manual:
        op.prioridade, op.prioridade_criterios = _prioridade(empresa, reais)
    op.resumo = _resumo(empresa, reais)
    op.perguntas = list(dict.fromkeys([e.regra.pergunta_sugerida for e in reais if e.regra and e.regra.pergunta_sugerida]
                                      + [PERGUNTA_PADRAO]))
    op.save()
    if pesquisa:
        op.pesquisas.add(pesquisa)
    for p in op.pesquisas.all():
        if p.idade_min_anos is None and p.idade_max_anos is None:
            continue
        if not empresa.data_abertura:
            alertas.append(f"Tempo de empresa desconhecido (critério da pesquisa '{p.nome}')"
                           + (" – enviar para revisão" if p.idade_desconhecida == p.IdadeDesconhecida.REVISAR else ""))
            continue
        anos = _anos(empresa.data_abertura)
        if (p.idade_min_anos is not None and anos < p.idade_min_anos) or \
                (p.idade_max_anos is not None and anos > p.idade_max_anos):
            alertas.append(f"Fora do critério de tempo de empresa da pesquisa '{p.nome}' ({anos} anos)")
    op.alertas = list(dict.fromkeys(alertas))
    op.save(update_fields=["alertas"])
    return op, criada, sit


def recruzar_todas():
    """Reavalia o cruzamento de todas as oportunidades (após sincronizar o Salesforce)."""
    n = 0
    for op in Oportunidade.objects.select_related("empresa"):
        avaliar(op.empresa)
        n += 1
    return n


def _decisao(tipo, usuario, op=None, *, candidato=None, motivo="", **dados):
    DecisaoRevisao.objects.create(tipo=tipo, usuario=usuario, oportunidade=op, candidato=candidato, motivo=motivo,
                                  dados=dados)


def conferir_antes_de_liberar(op):
    """Confere identidade e atualidade do cruzamento. Levanta RegraViolada com o motivo."""
    e = op.empresa
    if not e.cnpj:
        raise RegraViolada("Empresa sem CNPJ confirmado: identifique antes de liberar.")
    if not (e.candidatos.filter(status="IDENTIFICADO").exists() or e.salesforce_id):
        raise RegraViolada("Identidade não confirmada (nenhum candidato identificado com CNPJ verificado).")
    atual, quando, horas = cz.cruzamento_atual()
    base = "SINCRONIZACAO"
    if not atual:
        res = cz.conferir_direto([e.cnpj]).get(e.cnpj, {"contas": [], "leads": []})
        base = "CONSULTA_DIRETA"
        if res["contas"]:
            c = res["contas"][0]
            raise RegraViolada(f"Conferência direta: CNPJ já é conta BRIDA no Salesforce ({c['nome']}, {c['situacao']}). "
                               "Sincronize os clientes para atualizar a carteira.")
        if res["leads"] and op.tipo != Oportunidade.Tipo.LEAD_CONHECIDO:
            op.tipo = Oportunidade.Tipo.LEAD_CONHECIDO
    op2, _, sit = avaliar(e, base=base)
    if sit not in TIPO_POR_SITUACAO:
        raise RegraViolada(f"Situação atual na carteira não permite abordagem: {cz.Situacao(sit).label}.")
    e.refresh_from_db()
    classe = classe_situacao(e)
    if classe == "REVISAO":
        raise RegraViolada(f"Situação cadastral {e.situacao_cadastral} ({e.situacao_cadastral_fonte}): exige revisão; "
                           "não liberada.")
    if classe == "FORA":
        raise RegraViolada(f"Situação cadastral {e.situacao_cadastral}: fora da fila de novas abordagens.")
    if classe == "PENDENTE":
        raise RegraViolada("Situação cadastral pendente (sem informação ou consulta com falha) – não se presume ATIVA.")
    return op2


def verificar_situacao_cadastral(empresa):
    """
    Se a situação cadastral estiver ausente ou mais antiga que o prazo configurado, consulta a fonte cadastral
    integrada (BrasilAPI) e registra a resposta. Falha na consulta NÃO presume ATIVA (fica pendente).
    Observação: a BrasilAPI também deriva da base pública da Receita (atualização periódica, não tempo real).
    """
    from datetime import timedelta

    from carteira.models import ConfiguracaoAnalise
    from prospeccao import conectores
    from prospeccao.conectores.base import ContextoExecucao
    from prospeccao.models import Fonte
    from prospeccao.services.enriquecimento import aplicar_dados_cnpj, registrar_dado

    dias = ConfiguracaoAnalise.obter().dias_validade_situacao_cadastral
    ref = empresa.situacao_cadastral_referencia
    if empresa.situacao_cadastral and ref and ref >= timezone.localdate() - timedelta(days=dias):
        return "VALIDA"
    fonte = Fonte.objects.filter(codigo="brasilapi_cnpj", habilitada=True).first()
    if not fonte:
        raise RegraViolada(f"Situação cadastral ausente ou com mais de {dias} dias e fonte cadastral desabilitada: "
                           "pendente.")
    try:
        dados = conectores.ENRIQUECIMENTO["brasilapi_cnpj"](fonte).consultar(empresa.cnpj, ContextoExecucao())
    except Exception as ex:
        registrar_dado(empresa, "verificacao_situacao", f"FALHA: {type(ex).__name__}: {ex}"[:500], "brasilapi_cnpj")
        if "429" in str(ex):
            from prospeccao.services.retomada import agendar
            agendar("brasilapi_cnpj", empresa.cnpj, str(ex), getattr(ex, "retry_after", None), empresa=empresa)
        raise RegraViolada(f"Não foi possível verificar a situação cadastral agora ({type(ex).__name__}); pendente.")
    if dados is None:
        registrar_dado(empresa, "verificacao_situacao",
                       "A consulta cadastral não devolveu este CNPJ. Não é empresa inexistente nem inativa.",
                       "brasilapi_cnpj")
        raise RegraViolada("Consulta cadastral sem retorno para este CNPJ: situação pendente, não é inexistente.")
    aplicar_dados_cnpj(empresa, dados, "brasilapi_cnpj", data_referencia=timezone.localdate(),
                       rotulo_fonte="BrasilAPI (consulta)")
    registrar_dado(empresa, "verificacao_situacao", dados.situacao_cadastral, "brasilapi_cnpj", dados.url)
    return "CONSULTADA"


def liberar(op, usuario):
    """Verifica a situação cadastral FORA da transação (a resposta da fonte fica registrada mesmo se a
    liberação for recusada) e depois libera de forma atômica."""
    op = Oportunidade.objects.get(pk=op.pk)
    if op.status != Oportunidade.Status.NOVA:
        raise RegraViolada(f"Só oportunidades 'Nova' podem ser liberadas (atual: {op.get_status_display()}).")
    if op.empresa.cnpj:
        verificar_situacao_cadastral(op.empresa)
    return _liberar_atomico(op, usuario)


@transaction.atomic
def _liberar_atomico(op, usuario):
    op = Oportunidade.objects.select_for_update().get(pk=op.pk)
    if op.status != Oportunidade.Status.NOVA:
        raise RegraViolada(f"Só oportunidades 'Nova' podem ser liberadas (atual: {op.get_status_display()}).")
    op = conferir_antes_de_liberar(op)
    op.status, op.status_definido_por_humano = Oportunidade.Status.LIBERADA, True
    op.liberada_em, op.liberada_por = timezone.now(), usuario
    op.save()
    _decisao("LIBERAR_OPORTUNIDADE", usuario, op, base=op.cruzamento_base)
    return op


@transaction.atomic
def descartar(op, usuario, motivo):
    if not motivo:
        raise RegraViolada("Informe o motivo do descarte.")
    op = Oportunidade.objects.select_for_update().get(pk=op.pk)
    op.status, op.status_definido_por_humano, op.motivo_descarte = Oportunidade.Status.DESCARTADA, True, motivo[:255]
    op.save()
    _decisao("DESCARTAR_OPORTUNIDADE", usuario, op, motivo=motivo)
    return op


MOTIVOS_DESCLASSIFICAR = [
    "Não é empresa (ponto, pessoa física, local público)",
    "Fora do perfil: não usa lubrificantes, filtros ou graxas",
    "Empresa fechada ou não existe mais",
    "Telefones e contatos não pertencem à empresa",
    "Concorrente ou distribuidor",
    "Cadastro duplicado (mesma empresa em outro lead)",
    "Outro motivo",
]


@transaction.atomic
def desclassificar(op, usuario, motivo, justificativa):
    """Lead ruim: sai da lista do Inside Sales e fica DESCARTADA com o motivo e a justificativa de quem atendeu.
    Só quem pode atender o lead (dono ou ADMIN) desclassifica; um ADMIN pode reabrir depois."""
    from prospeccao.services import atendimento as at
    justificativa = (justificativa or "").strip()
    if motivo not in MOTIVOS_DESCLASSIFICAR:
        raise RegraViolada("Escolha o motivo da desclassificação.")
    if len(justificativa) < 10:
        raise RegraViolada("Escreva a justificativa (pelo menos 10 caracteres): o que você verificou.")
    op = Oportunidade.objects.select_for_update(of=("self",)).select_related("responsavel", "em_atendimento_por").get(pk=op.pk)
    if at.dono_de_outro(op, usuario):
        raise RegraViolada(str(at.LeadDeOutro(op)))
    quem = at.vigente(op)
    from prospeccao.papeis import eh_adm
    if quem is not None and quem.pk != usuario.pk and not eh_adm(usuario):
        raise RegraViolada(str(at.AtendimentoOcupado(op)))
    texto = f"Desclassificado: {motivo} — {justificativa}"
    op.status, op.status_definido_por_humano, op.motivo_descarte = Oportunidade.Status.DESCARTADA, True, texto[:255]
    op.em_atendimento_por, op.em_atendimento_desde = None, None
    op.save()
    _decisao("DESCLASSIFICAR_LEAD", usuario, op, motivo=texto, motivo_escolhido=motivo, justificativa=justificativa)
    return op


@transaction.atomic
def reabrir(op, usuario, motivo=""):
    op = Oportunidade.objects.select_for_update().get(pk=op.pk)
    op.status, op.status_definido_por_humano = Oportunidade.Status.NOVA, False
    op.save()
    _decisao("REABRIR_OPORTUNIDADE", usuario, op, motivo=motivo)
    return avaliar(op.empresa)[0]


@transaction.atomic
def atribuir(op, destino, por):
    """Impede atribuições conflitantes: só gestor (staff) troca um responsável já definido."""
    op = Oportunidade.objects.select_for_update().get(pk=op.pk)
    if op.responsavel_id and op.responsavel_id != getattr(destino, "pk", None) and not por.is_staff:
        raise RegraViolada(f"Oportunidade já atribuída a {op.responsavel}. Só um gestor pode reatribuir.")
    anterior = op.responsavel
    op.responsavel, op.atribuida_em = destino, timezone.now() if destino else None
    op.save(update_fields=["responsavel", "atribuida_em", "atualizada_em"])
    _decisao("ATRIBUIR", por, op, de=str(anterior or ""), para=str(destino or ""))
    return op


@transaction.atomic
def definir_prioridade(op, prioridade, usuario, motivo):
    op = Oportunidade.objects.select_for_update().get(pk=op.pk)
    op.prioridade, op.prioridade_manual, op.prioridade_motivo_manual = prioridade, True, motivo[:255]
    op.prioridade_criterios = list(op.prioridade_criterios) + [f"Prioridade revisada por {usuario}: {motivo}"]
    op.save()
    _decisao("ALTERAR_PRIORIDADE", usuario, op, motivo=motivo, prioridade=prioridade)
    return op
