"""
Três dimensões independentes do lead. Indicador, lista e exportação usam estes mesmos critérios.

Identificação (uma empresa cai em um só rótulo, nesta ordem):
  formato inválido → ativo → não ativo → situação pendente → identidade a conferir → CNPJ não identificado.
  "CNPJ identificado" é a soma de ativo + não ativo + situação pendente.
  CNPJ não encontrado não é CNPJ inválido.

Salesforce:
  conhecida, possível correspondência, não encontrada na carteira utilizada, cruzamento pendente.
  Sem CNPJ não se anuncia "não encontrada".

Contato comercial é outra dimensão: ver, copiar ou exportar não conta como contato.
"""
from datetime import date, timedelta

from django.db.models import Exists, OuterRef, Q

from carteira.models import CanalEmpresa, ConfiguracaoAnalise, DadoColetado, Empresa, Importacao
from prospeccao.models import Candidato, Oportunidade, SelecaoPesquisa
from prospeccao.services import cruzamento as cz

CONHECIDAS = (
    cz.Situacao.CLIENTE_CARTEIRA, cz.Situacao.CADASTRO_SEM_COMPRA, cz.Situacao.LEAD_CONHECIDO,
    cz.Situacao.BLOQUEADA, cz.Situacao.NAO_CONTATAR,
)


def carteira_integra():
    """Há sincronização de clientes concluída. Carteira antiga ainda serve; parcial não entra aqui."""
    return Importacao.objects.filter(tipo=Importacao.Tipo.CLIENTES, status=Importacao.Status.SUCESSO).exists()


def data_carteira():
    imp = (Importacao.objects.filter(tipo=Importacao.Tipo.CLIENTES, status=Importacao.Status.SUCESSO)
           .order_by("-finalizado_em").first())
    return imp.finalizado_em if imp else None


def _limite_situacao():
    """Filtro técnico de liberação, não regra da Receita nem prazo comercial nomeado.

    O número vive em ConfiguracaoAnalise.dias_validade_situacao_cadastral (padrão 45, editável
    no admin). Foi criado na migração carteira 0003. Situação mais antiga que esse prazo exige
    consulta complementar; se a consulta falha ou não acha o CNPJ, a situação anterior permanece
    com a competência da base e não conta como confirmação atual.
    """
    dias = int(ConfiguracaoAnalise.obter().dias_validade_situacao_cadastral)
    return date.today() - timedelta(days=dias)


def _tem_cnpj(empresa):
    return bool(empresa.cnpj)


def _ambiguo(empresa):
    if empresa.dados_coletados.filter(campo__in=("cnpj_candidato", "cnpj_outra_unidade")).exists():
        return True
    if empresa.candidatos.filter(status=Candidato.Status.CONFLITO).exists():
        return True
    return SelecaoPesquisa.objects.filter(empresa=empresa, fila=SelecaoPesquisa.Fila.REVISAR_IDENTIDADE).exists()


def classificacao_identificacao(empresa):
    """dict codigo, rotulo, detalhe. Um código por empresa."""
    invalido = empresa.cnpj_status in ("INVALIDO", "CPF") or (
        not _tem_cnpj(empresa) and empresa.dados_coletados.filter(campo="cnpj_informado_invalido").exists())
    if invalido and not _tem_cnpj(empresa):
        return {"codigo": "formato_invalido", "rotulo": "CNPJ informado com formato inválido",
                "detalhe": "O número informado não passa na validação. Isso não é o mesmo que CNPJ não encontrado."}
    if _tem_cnpj(empresa):
        sit = (empresa.situacao_cadastral or "").strip()
        fonte = empresa.situacao_cadastral_fonte or "fonte não registrada"
        atualizacao = empresa.situacao_cadastral_data
        consulta = empresa.situacao_cadastral_referencia
        detalhe_datas = (
            f"Atualização do cadastro: {atualizacao.strftime('%d/%m/%Y') if atualizacao else 'não informada pela fonte'}. "
            f"Fonte: {fonte}. "
            f"Competência ou consulta registrada: {consulta.strftime('%d/%m/%Y') if consulta else 'não informada'}."
        )
        outra = empresa.dados_coletados.filter(campo="cnpj_outra_cidade").values_list("valor", flat=True).first()
        if outra and sit.upper() == "ATIVA" and consulta and consulta >= _limite_situacao():
            partes = [x.strip() for x in outra.split("|")]
            onde = partes[1] if len(partes) > 1 else "outro município"
            return {"codigo": "ativo_outra_cidade", "rotulo": f"CNPJ ativo localizado em outra cidade ({onde})",
                    "detalhe": (f"A pesquisa encontrou a empresa em {empresa.cidade}; o CNPJ com o mesmo nome está "
                                f"registrado em {onde}. Possível matriz, filial ou endereço desatualizado na Receita. "
                                f"Conferir na abordagem. {detalhe_datas}")}
        if sit.upper() == "ATIVA" and consulta and consulta >= _limite_situacao():
            return {"codigo": "ativo", "rotulo": "CNPJ ativo",
                    "detalhe": f"Situação ATIVA. {detalhe_datas}"}
        if sit and sit.upper() != "ATIVA":
            return {"codigo": "nao_ativo", "rotulo": f"CNPJ não ativo ({sit})",
                    "detalhe": f"Situação {sit}. Fora dos aptos para venda. {detalhe_datas}"}
        ausente = empresa.dados_coletados.filter(
            campo="verificacao_situacao", valor__icontains="não encontrado").exists()
        falha = empresa.dados_coletados.filter(campo="verificacao_situacao", valor__startswith="FALHA").exists()
        if ausente and falha:
            porque = ("A fonte complementar não tem este CNPJ. Uma nova consulta não foi concluída. "
                      "A situação anterior fica com a competência da base e não confirma o cadastro agora.")
        elif ausente:
            porque = ("A fonte complementar não tem este CNPJ. A situação anterior permanece com a "
                      "competência da base e não é confirmação atual.")
        elif falha:
            porque = "A consulta cadastral falhou; não se presume ativo."
        else:
            porque = "A situação ainda não foi consultada ou a referência está fora da validade configurada."
        return {"codigo": "situacao_pendente", "rotulo": "CNPJ identificado — situação pendente",
                "detalhe": f"{porque} {detalhe_datas}"}
    if _ambiguo(empresa):
        return {"codigo": "a_conferir", "rotulo": "Identidade a conferir",
                "detalhe": "Há candidato ou conflito. O CNPJ não foi vinculado."}
    return {"codigo": "nao_identificado", "rotulo": "CNPJ não identificado",
            "detalhe": "Nenhum CNPJ foi associado. Não é CNPJ inválido."}


def classificacao_salesforce(empresa, oportunidade=None, carteira_ok=None):
    if carteira_ok is None:
        carteira_ok = carteira_integra()
    sit, detalhe, _ = cz.situacao_empresa(empresa)
    tipo = getattr(oportunidade, "tipo", "") or ""
    status = getattr(oportunidade, "status", "") or ""
    if (empresa.salesforce_id or sit in CONHECIDAS or status == Oportunidade.Status.JA_CLIENTE
            or tipo in (Oportunidade.Tipo.LEAD_CONHECIDO, Oportunidade.Tipo.CADASTRO_SEM_COMPRA)):
        quando = data_carteira()
        extra = f" Carteira de {quando:%d/%m/%Y %H:%M}." if quando else ""
        return {"codigo": "conhecida", "rotulo": "Conhecida na carteira", "detalhe": f"{detalhe}.{extra}"}
    if _possivel(empresa):
        return {"codigo": "possivel", "rotulo": "Possível correspondência",
                "detalhe": "Sinal parecido com a carteira, sem identidade confirmada. Não entra como nova nem como cliente."}
    if not carteira_ok:
        return {"codigo": "cruzamento_pendente", "rotulo": "Cruzamento pendente",
                "detalhe": "Não há carteira Salesforce íntegra. A empresa não está confirmada como nova."}
    if _tem_cnpj(empresa):
        quando = data_carteira()
        quando_txt = quando.strftime("%d/%m/%Y %H:%M") if quando else "data não registrada"
        return {"codigo": "nao_encontrada", "rotulo": "Não encontrada na carteira utilizada",
                "detalhe": f"CNPJ conferido na carteira de {quando_txt}."}
    return {"codigo": "cruzamento_pendente", "rotulo": "Cruzamento pendente",
            "detalhe": "CNPJ ainda não identificado: não há como afirmar ausência na carteira."}


def _possivel(empresa):
    if SelecaoPesquisa.objects.filter(empresa=empresa, fila=SelecaoPesquisa.Fila.REVISAR_IDENTIDADE).exists():
        return True
    return empresa.candidatos.filter(status=Candidato.Status.CONFLITO).exists()


def classificacao_contato(empresa, oportunidade=None, abordagens=None, anotada=None):
    if empresa.nao_contatar:
        return {"codigo": "nao_contatar", "rotulo": "Não contatar", "detalhe": empresa.nao_contatar_motivo or ""}
    retorno = False
    conversa = False
    tentativa = False
    if anotada is not None:
        retorno = bool(getattr(anotada, "ab_retorno", False))
        conversa = bool(getattr(anotada, "ab_conversa", False))
        tentativa = bool(getattr(anotada, "ab_tentativa", False))
    elif abordagens is not None:
        retorno = any(a.data_retorno and not a.retorno_concluido for a in abordagens)
        conversa = any(a.houve_conversa for a in abordagens)
        tentativa = any(not a.houve_conversa for a in abordagens)
    if retorno:
        return {"codigo": "retorno", "rotulo": "Retorno agendado", "detalhe": ""}
    if conversa:
        return {"codigo": "acompanhamento", "rotulo": "Em acompanhamento", "detalhe": "Contato efetivo registrado pela equipe."}
    if oportunidade is not None and oportunidade.status == Oportunidade.Status.DESCARTADA:
        return {"codigo": "encerrado", "rotulo": "Encerrado", "detalhe": oportunidade.motivo_descarte or ""}
    if tentativa:
        return {"codigo": "tentativa", "rotulo": "Tentativa sem resposta",
                "detalhe": "Continua disponível. A tentativa não tira a empresa da fila."}
    return {"codigo": "sem_contato", "rotulo": "Sem contato efetivo",
            "detalhe": "Ver, copiar, exportar ou clicar no telefone não registra contato."}


def resumo(empresa, oportunidade=None, abordagens=None, anotada=None, carteira_ok=None):
    ident = classificacao_identificacao(empresa)
    sf = classificacao_salesforce(empresa, oportunidade, carteira_ok=carteira_ok)
    contato = classificacao_contato(empresa, oportunidade, abordagens, anotada)
    validado = ident["codigo"] in ("ativo", "ativo_outra_cidade") and sf["codigo"] == "nao_encontrada"
    return {"identificacao": ident, "salesforce": sf, "contato": contato, "validado": validado}


def q_contato_direto():
    from prospeccao.services.contatos import terceiro_q
    return Exists(CanalEmpresa.objects.filter(
        empresa=OuterRef("empresa_id"), tipo__in=[CanalEmpresa.Tipo.TELEFONE, CanalEmpresa.Tipo.EMAIL],
        restrito=False, origem_canal=CanalEmpresa.Origem.PUBLICACAO,
        confianca_associacao__in=[CanalEmpresa.Confianca.ALTA, CanalEmpresa.Confianca.MEDIA],
    ).exclude(confirmacao_humana=CanalEmpresa.Confirmacao.INCORRETO).exclude(terceiro_q()))


def q_conhecida():
    return (Q(empresa__salesforce_id__isnull=False)
            | Q(tipo__in=[Oportunidade.Tipo.LEAD_CONHECIDO, Oportunidade.Tipo.CADASTRO_SEM_COMPRA])
            | Q(status=Oportunidade.Status.JA_CLIENTE)
            | Q(situacao_carteira__in=list(CONHECIDAS)))


def q_sem_cnpj():
    return Q(empresa__cnpj__isnull=True) | Q(empresa__cnpj="")


def q_ambiguo():
    return (Exists(Candidato.objects.filter(empresa=OuterRef("empresa_id"), status=Candidato.Status.CONFLITO))
            | Exists(DadoColetado.objects.filter(empresa=OuterRef("empresa_id"),
                                                campo__in=["cnpj_candidato", "cnpj_outra_unidade"]))
            | Exists(SelecaoPesquisa.objects.filter(
                empresa=OuterRef("empresa_id"), fila=SelecaoPesquisa.Fila.REVISAR_IDENTIDADE)))


def q_formato_invalido():
    return q_sem_cnpj() & (Q(empresa__cnpj_status__in=["INVALIDO", "CPF"])
                          | Exists(DadoColetado.objects.filter(
                              empresa=OuterRef("empresa_id"), campo="cnpj_informado_invalido")))


def q_ativa_valida(limite):
    return (~q_sem_cnpj() & Q(empresa__situacao_cadastral__iexact="ATIVA")
            & Q(empresa__situacao_cadastral_referencia__gte=limite))


def filtrar_dimensao(qs, etapa, contato=""):
    """Aplica o mesmo recorte da lista, do indicador e da exportação."""
    limite = _limite_situacao()
    if etapa == "validado":
        if not carteira_integra():
            return qs.none()
        qs = qs.filter(q_ativa_valida(limite), tipo=Oportunidade.Tipo.NOVA_EXTERNA,
                       empresa__salesforce_id__isnull=True).exclude(q_conhecida())
    elif etapa == "completar_cnpj":
        qs = qs.filter(q_sem_cnpj()).exclude(q_formato_invalido()).exclude(q_ambiguo()).exclude(q_conhecida())
    elif etapa == "conferir_cadastro":
        qs = qs.filter(q_formato_invalido() | q_ambiguo() | (~q_sem_cnpj() & ~q_ativa_valida(limite))).exclude(q_conhecida())
    elif etapa == "conhecidas":
        qs = qs.filter(q_conhecida())
    if contato == "direto":
        qs = qs.filter(q_contato_direto())
    elif contato == "sem_direto":
        qs = qs.exclude(q_contato_direto())
    return qs


def contagens(empresas):
    """Contagens do universo reprocessado. Uma empresa pode entrar em mais de uma linha; ver regras no relatório."""
    from prospeccao.services.contatos import terceiro_q
    ids = [e.pk if hasattr(e, "pk") else e for e in empresas]
    qs = Empresa.objects.filter(pk__in=ids)
    carteira_ok = carteira_integra()
    ops = {o.empresa_id: o for o in Oportunidade.objects.filter(empresa_id__in=ids)}
    linhas = {
        "empresas_unicas": qs.count(),
        "cnpj_nao_identificado": 0, "identidade_ambigua": 0, "cnpj_identificado": 0,
        "cnpj_ativo": 0, "cnpj_nao_ativo": 0, "situacao_pendente": 0, "formato_invalido": 0,
        "conhecidas_salesforce": 0, "cadastro_validado": 0, "cadastro_validado_com_contato": 0,
    }
    diretos = set(CanalEmpresa.objects.filter(
        empresa_id__in=ids, tipo__in=[CanalEmpresa.Tipo.TELEFONE, CanalEmpresa.Tipo.EMAIL],
        restrito=False, origem_canal=CanalEmpresa.Origem.PUBLICACAO,
        confianca_associacao__in=[CanalEmpresa.Confianca.ALTA, CanalEmpresa.Confianca.MEDIA],
    ).exclude(confirmacao_humana=CanalEmpresa.Confirmacao.INCORRETO).exclude(terceiro_q())
                  .values_list("empresa_id", flat=True))
    for empresa in qs.iterator():
        ident = classificacao_identificacao(empresa)
        sf = classificacao_salesforce(empresa, ops.get(empresa.pk), carteira_ok=carteira_ok)
        codigo = ident["codigo"]
        if codigo == "nao_identificado":
            linhas["cnpj_nao_identificado"] += 1
        elif codigo == "a_conferir":
            linhas["identidade_ambigua"] += 1
        elif codigo == "formato_invalido":
            linhas["formato_invalido"] += 1
        else:
            linhas["cnpj_identificado"] += 1
            if codigo in ("ativo", "ativo_outra_cidade"):
                linhas["cnpj_ativo"] += 1
            elif codigo == "nao_ativo":
                linhas["cnpj_nao_ativo"] += 1
            elif codigo == "situacao_pendente":
                linhas["situacao_pendente"] += 1
        if sf["codigo"] == "conhecida":
            linhas["conhecidas_salesforce"] += 1
        if codigo in ("ativo", "ativo_outra_cidade") and sf["codigo"] == "nao_encontrada":
            linhas["cadastro_validado"] += 1
            if empresa.pk in diretos:
                linhas["cadastro_validado_com_contato"] += 1
    return linhas
