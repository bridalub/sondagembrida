"""Montagem de contatos, motivos e próximos passos para as telas — sem alterar regras comerciais.

Nada aqui grava no banco: abrir a tela, copiar ou abrir um link não registra abordagem.
"""

from django.db.models import Exists, OuterRef, Q

from carteira.models import CanalEmpresa

ORDEM_CANAL = {
    "CONFIRMADO_EQUIPE": 0,
    "CONFERIDO_EQUIPE": 1,
    "CORROBORADO": 2,
    "PUBLICADO": 3,
    "INFORMADO_EQUIPE": 4,
    "CADASTRAL": 5,
    "PISTA": 6,
    "INCORRETO": 9,
}


def canais_uteis(canais):
    return [c for c in canais if c.nivel != "INCORRETO" and not c.restrito]


def _suspeito(c):
    """Canal que provavelmente não é da empresa (contador, terceiro, compartilhado ou em conflito)."""
    return (c.aplica_a == CanalEmpresa.AplicaA.CONTADOR or c.classificacao == CanalEmpresa.Classificacao.CONTADOR
            or bool(c.compartilhado) or c.confianca_associacao == CanalEmpresa.Confianca.CONFLITO)


def alerta_canal(c):
    if c is None:
        return ""
    papel = terceiro(c)
    if papel:
        return papel
    if c.tipo == CanalEmpresa.Tipo.SITE and c.confianca_associacao == CanalEmpresa.Confianca.BAIXA:
        return "Possível site da empresa – vínculo não confirmado"
    if c.tipo != CanalEmpresa.Tipo.SITE and c.nivel in ("CADASTRAL", "PISTA"):
        return "A conferir"
    if c.aplica_a == CanalEmpresa.AplicaA.MATRIZ:
        return "Contato da matriz"
    if c.aplica_a == CanalEmpresa.AplicaA.GRUPO:
        return "Contato do grupo/outra unidade – não confirmado para esta unidade"
    if c.classificacao == CanalEmpresa.Classificacao.FISCAL:
        return "Setor fiscal"
    return ""


def terceiro(c):
    from prospeccao.services.contatos import papel_terceiro
    return papel_terceiro(c)


def melhor_canal(canais, tipo):
    """Melhor contato DIRETO (contato de terceiro nunca é apresentado como da empresa)."""
    cands = [c for c in canais_uteis(canais) if c.tipo == tipo and not terceiro(c)]
    if not cands:
        return None
    return min(cands, key=lambda c: (0 if c.preferido else 1, 1 if _suspeito(c) else 0, ORDEM_CANAL.get(c.nivel, 8), c.pk))


def whatsapp_publicado(canais):
    for c in canais_uteis(canais):
        if c.tipo == CanalEmpresa.Tipo.TELEFONE and c.whatsapp_explicito:
            return c
    return None


def contato_lista(empresa, canais=None):
    canais = list(canais if canais is not None else empresa.canais.all())
    tel = melhor_canal(canais, CanalEmpresa.Tipo.TELEFONE)
    email = melhor_canal(canais, CanalEmpresa.Tipo.EMAIL)
    wa = whatsapp_publicado(canais)
    terc = [c for c in canais_uteis(canais) if c.tipo != CanalEmpresa.Tipo.SITE and terceiro(c)]
    valores_terc = {c.valor for c in terc} | {c.valor_normalizado for c in terc}
    from carteira.services.normalizacao import digitos_telefone
    from prospeccao.services.contatos import mascarado
    tel_cad = empresa.telefone if (empresa.telefone and not mascarado(empresa.telefone)
                                   and digitos_telefone(empresa.telefone) not in valores_terc) else ""
    email_cad = empresa.email if (empresa.email and not mascarado(empresa.email)
                                  and empresa.email.strip().lower() not in valores_terc) else ""
    tel_valor = (tel.valor if tel else "") or tel_cad
    email_valor = (email.valor if email else "") or email_cad
    tel_nivel = tel.nivel if tel else ("CADASTRAL" if tel_cad else "")
    email_nivel = email.nivel if email else ("CADASTRAL" if email_cad else "")
    email_oculto = not email_valor and empresa.dados_coletados.filter(campo__startswith="email_nao_disponibilizado").exists()
    site_c = melhor_canal(canais, CanalEmpresa.Tipo.SITE)
    uteis = [c for c in canais_uteis(canais) if c.tipo in (CanalEmpresa.Tipo.TELEFONE, CanalEmpresa.Tipo.EMAIL)]
    return {
        "telefone": tel_valor,
        "telefone_nivel": tel_nivel,
        "telefone_nivel_display": CanalEmpresa.NIVEIS.get(tel_nivel, ""),
        "telefone_origem": (tel.fonte if tel else ("cadastro" if empresa.telefone else "")),
        "telefone_alerta": alerta_canal(tel) if tel else ("Do cadastro – a conferir" if tel_cad else ""),
        "telefone_finalidade": tel.get_classificacao_display() if tel and tel.classificacao != "INCERTO" else "",
        "email": email_valor,
        "email_nivel": email_nivel,
        "email_nivel_display": CanalEmpresa.NIVEIS.get(email_nivel, ""),
        "email_origem": (email.fonte if email else ("cadastro" if empresa.email else "")),
        "email_alerta": alerta_canal(email) if email else ("Do cadastro – a conferir" if email_cad else ""),
        "email_oculto": email_oculto,
        "terceiros": [{"tipo": c.tipo, "valor": c.valor, "papel": terceiro(c), "fonte": c.fonte} for c in terc],
        "so_terceiro": bool(terc) and not tel_valor and not email_valor,
        "email_finalidade": email.get_classificacao_display() if email and email.classificacao != "INCERTO" else "",
        "whatsapp": wa.valor if wa else "",
        "site": empresa.site or (site_c.valor if site_c else ""),
        "outros": max(0, len(uteis) - (1 if tel else 0) - (1 if email else 0)),
        "departamento": next((c.departamento for c in (tel, email) if c and c.departamento), ""),
    }


def sem_canal_q(tipo):
    """Sem contato DIRETO deste tipo: contato de terceiro (contador) e valores mascarados não contam."""
    from prospeccao.services.contatos import terceiro_q
    cadastro = "telefone" if tipo == CanalEmpresa.Tipo.TELEFONE else "email"
    canal_ok = CanalEmpresa.objects.filter(
        empresa=OuterRef("empresa_id"), tipo=tipo, restrito=False,
    ).exclude(confirmacao_humana=CanalEmpresa.Confirmacao.INCORRETO).exclude(terceiro_q())
    campo_terceiro = CanalEmpresa.objects.filter(empresa=OuterRef("empresa_id"), tipo=tipo).filter(terceiro_q()).filter(
        Q(valor=OuterRef(f"empresa__{cadastro}")) | Q(valor_normalizado=OuterRef(f"empresa__{cadastro}")))
    vazio = (Q(**{f"empresa__{cadastro}": ""}) | Q(**{f"empresa__{cadastro}__isnull": True})
             | Q(**{f"empresa__{cadastro}__contains": "*"}) | Exists(campo_terceiro))
    return vazio & ~Exists(canal_ok)


def anexar_contatos(oportunidades):
    ids = [o.empresa_id for o in oportunidades]
    por_emp = {}
    if ids:
        for c in CanalEmpresa.objects.filter(empresa_id__in=ids):
            por_emp.setdefault(c.empresa_id, []).append(c)
    for o in oportunidades:
        o.contato = contato_lista(o.empresa, por_emp.get(o.empresa_id, []))


def anexar_selecao(oportunidades):
    """Motivo da seleção e frente comercial (lista comercial da pesquisa planejada), quando existirem."""
    from prospeccao.models import SelecaoPesquisa
    ids = [o.empresa_id for o in oportunidades]
    sel = {}
    if ids:
        for s in (SelecaoPesquisa.objects.filter(empresa_id__in=ids).select_related("frente", "pesquisa")
                  .order_by("empresa_id", "-pontuacao")):
            sel.setdefault(s.empresa_id, s)
    for o in oportunidades:
        o.selecao = sel.get(o.empresa_id)


def etapa_oportunidade(o):
    """(código, rótulo) da etapa da prospecção, a partir do status e das abordagens anotadas."""
    if getattr(o, "ab_retorno", False):
        return "retorno", "Retorno pendente"
    if o.status == "NOVA":
        if getattr(o, "ab_conversa", False):
            return "andamento", "Em acompanhamento (contato efetivo)"
        return "conferir", "A conferir"
    if o.status == "LIBERADA":
        if getattr(o, "ab_conversa", False):
            return "andamento", "Em acompanhamento (contato efetivo)"
        if getattr(o, "ab_abordada", False):
            return "primeira", "Tentativa sem resposta – disponível"
        return "primeira", "Pronta para 1ª abordagem"
    return "encerrada", o.get_status_display()


def proximo_passo_pesquisa(pesquisa, ultima_execucao=None):
    if not pesquisa.ativa:
        return "Pesquisa pausada. Ative para voltar a buscar empresas.", "prospeccao:pesquisa_detalhe"
    ex = ultima_execucao
    if ex and ex.status in ("PENDENTE", "EXECUTANDO"):
        return "Busca em andamento. Acompanhe o andamento.", "prospeccao:execucao_detalhe"
    if pesquisa.modo == "PLANEJADA" and not pesquisa.plano_ativo_id:
        return "Revise o plano comercial e execute quando estiver pronto.", "prospeccao:plano"
    if not ex:
        return "Salva. Execute quando quiser buscar empresas — isso não acontece ao salvar.", "prospeccao:pesquisa_detalhe"
    if ex.oportunidades_novas:
        return "Há empresas novas para avaliar.", "prospeccao:oportunidades"
    if ex.pendentes:
        return "Há empresas sem identificação para conferir.", "prospeccao:revisao"
    if ex.status == "CONCLUIDA":
        return "Última busca concluída. Veja as empresas ou execute de novo.", "prospeccao:pesquisa_detalhe"
    if ex.status == "PARCIAL":
        return "Última busca parou nos limites. Veja o que já foi encontrado.", "prospeccao:execucao_detalhe"
    if ex.status == "FALHA":
        return "A última busca falhou. Veja o histórico para o motivo.", "prospeccao:execucao_detalhe"
    return "Abra a pesquisa para o próximo passo.", "prospeccao:pesquisa_detalhe"


def link_proximo_passo(pesquisa, ultima_execucao=None):
    """(texto, url) – a url já aponta para o registro certo (pesquisa, execução ou lista filtrada)."""
    from django.urls import reverse
    texto, rota = proximo_passo_pesquisa(pesquisa, ultima_execucao)
    if rota == "prospeccao:execucao_detalhe" and ultima_execucao:
        return texto, reverse(rota, args=[ultima_execucao.pk])
    if rota == "prospeccao:oportunidades":
        return texto, reverse(rota) + f"?pesquisa={pesquisa.pk}&etapa=conferir"
    if rota == "prospeccao:revisao":
        return texto, reverse(rota) + f"?pesquisa={pesquisa.pk}"
    if rota == "prospeccao:plano":
        return texto, reverse(rota, args=[pesquisa.pk])
    return texto, reverse("prospeccao:pesquisa_detalhe", args=[pesquisa.pk])


def proxima_acao_empresa(empresa, op, abordagens):
    if empresa.nao_contatar:
        return "Não contatar: novas abordagens estão bloqueadas."
    if not op:
        return "Sem oportunidade de prospecção nesta empresa. Use o cadastro da carteira."
    pend = next((a for a in abordagens if a.data_retorno and not a.retorno_concluido), None)
    if pend:
        quando = pend.data_retorno.strftime("%d/%m/%Y")
        extra = " (atrasado)" if pend.retorno_atrasado else ""
        return f"Retorno em {quando}{extra}: {pend.proxima_acao or 'registrar o contato combinado'}."
    if op.status == op.Status.NOVA:
        return "Conferir a empresa e liberar para a primeira abordagem."
    if op.status == op.Status.LIBERADA:
        if not any(True for _ in abordagens):
            return "Registrar a primeira abordagem (telefone, e-mail ou visita)."
        return "Registrar o próximo contato ou agendar o retorno."
    if op.status == op.Status.BLOQUEADA:
        return "Abordagens suspensas pelas regras de tentativa. Um gestor pode registrar exceção."
    return f"Situação: {op.get_status_display()}."
