"""
Situação de contato, fila de novas abordagens, retornos e listas.

Somente o registro de uma Abordagem muda a situação de contato. Visualizar,
importar ou incluir uma empresa numa lista não conta como contato.
"""
from django.db import transaction
from django.db.models import Case, CharField, Exists, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from carteira.models import (
    Abordagem, Empresa, ItemLista, ListaAbordagem, ResultadoAbordagem, SituacaoCompra, SituacaoContato,
)


def _pendentes(empresa_ref):
    return Abordagem.objects.filter(empresa=empresa_ref, data_retorno__isnull=False, retorno_concluido=False)


def anotar(qs):
    """Anota situacao_contato, situacao_compra, proximo_retorno e ultima_abordagem."""
    todas = Abordagem.objects.filter(empresa=OuterRef("pk"))
    qs = qs.annotate(
        _tem_abordagem=Exists(todas),
        _tem_conversa=Exists(todas.filter(houve_conversa=True)),
        _retorno_pendente=Exists(_pendentes(OuterRef("pk"))),
        proximo_retorno=Subquery(_pendentes(OuterRef("pk")).order_by("data_retorno").values("data_retorno")[:1]),
        ultima_abordagem=Subquery(todas.order_by("-data_hora").values("data_hora")[:1]),
    )
    return qs.annotate(
        situacao_contato=Case(
            When(nao_contatar=True, then=Value(SituacaoContato.NAO_CONTATAR)),
        When(situacao_protheus__iexact="Bloqueado", then=Value(SituacaoContato.NAO_CONTATAR)),
            When(_retorno_pendente=True, then=Value(SituacaoContato.RETORNO_AGENDADO)),
            When(_tem_conversa=True, then=Value(SituacaoContato.CONVERSA_REALIZADA)),
            When(_tem_abordagem=True, then=Value(SituacaoContato.TENTATIVA_SEM_RESPOSTA)),
            default=Value(SituacaoContato.NUNCA_ABORDADA),
            output_field=CharField(),
        ),
        situacao_compra=Coalesce("indicador__situacao", Value(SituacaoCompra.INDISPONIVEL), output_field=CharField()),
    )


def novas_abordagens(qs=None):
    """Empresas sem nenhuma tentativa de contato registrada, não marcadas 'não contatar' e não Bloqueadas na origem."""
    qs = Empresa.objects.all() if qs is None else qs
    return (qs.filter(nao_contatar=False).exclude(situacao_protheus__iexact="Bloqueado")
            .exclude(Exists(Abordagem.objects.filter(empresa=OuterRef("pk")))))


def retornos_pendentes():
    return (Abordagem.objects.filter(data_retorno__isnull=False, retorno_concluido=False)
            .select_related("empresa", "contato", "usuario").order_by("data_retorno", "data_hora"))


class RegraAbordagemViolada(Exception):
    def __init__(self, mensagem, permite_excecao=False):
        super().__init__(mensagem)
        self.permite_excecao = permite_excecao


def validar_nova_abordagem(empresa, usuario, justificativa_excecao=""):
    """
    Regras do gestor (ConfiguracaoAnalise) + bloqueios. Exceções às regras de intervalo/tentativas só
    para gestores (is_staff) com justificativa. 'Não contatar' não tem exceção.
    """
    from carteira.models import ConfiguracaoAnalise
    if empresa.nao_contatar:
        raise RegraAbordagemViolada(f"Empresa marcada como 'não contatar' ({empresa.nao_contatar_motivo}).")
    try:
        op = empresa.oportunidade  # app prospecção (OneToOne); ausente = sem oportunidade
    except Exception:
        op = None
    from prospeccao.papeis import eh_adm
    if op and op.responsavel_id and op.responsavel_id != usuario.pk and not eh_adm(usuario):
        dono = op.responsavel.get_full_name() or op.responsavel.get_username()
        raise RegraAbordagemViolada(f"Este lead é de {dono} (primeiro atendimento). Só {dono} ou um ADMIN registra "
                                    "contato nele.")
    cfg = ConfiguracaoAnalise.obter()
    ultimas = list(Abordagem.objects.filter(empresa=empresa).order_by("-data_hora")[:50])
    problemas = []
    if cfg.dias_minimos_nova_tentativa and ultimas:
        dias = (timezone.now() - ultimas[0].data_hora).days
        if dias < cfg.dias_minimos_nova_tentativa:
            problemas.append(f"última abordagem há {dias} dia(s) ({timezone.localtime(ultimas[0].data_hora):%d/%m/%Y}, "
                             f"{ultimas[0].get_resultado_display()}); mínimo definido: {cfg.dias_minimos_nova_tentativa}")
    if cfg.max_tentativas_sem_resposta:
        seguidas = 0
        for a in ultimas:
            if a.houve_conversa:
                break
            seguidas += 1
        if seguidas >= cfg.max_tentativas_sem_resposta:
            problemas.append(f"{seguidas} tentativa(s) seguida(s) sem conversa; máximo definido: {cfg.max_tentativas_sem_resposta}")
    if problemas:
        if usuario.is_staff and justificativa_excecao.strip():
            return f"Exceção de gestor: {justificativa_excecao.strip()} ({'; '.join(problemas)})"
        raise RegraAbordagemViolada("Regra de nova tentativa: " + "; ".join(problemas), permite_excecao=usuario.is_staff)
    return ""


@transaction.atomic
def registrar_abordagem(abordagem, *, concluir_pendentes=True, justificativa_excecao=""):
    """`abordagem` é uma instância não salva com empresa, usuario etc. preenchidos."""
    Empresa.objects.select_for_update().filter(pk=abordagem.empresa_id).first()
    excecao = validar_nova_abordagem(abordagem.empresa, abordagem.usuario, justificativa_excecao)
    if excecao:
        abordagem.observacoes = (abordagem.observacoes + "\n\n" if abordagem.observacoes else "") + f"[{excecao}]"
    try:
        op = abordagem.empresa.oportunidade
    except Exception:
        op = None
    from prospeccao.papeis import eh_adm
    if op is not None and op.responsavel_id is None and not eh_adm(abordagem.usuario):
        # 1º contato de quem atende: o lead passa a ser dele (os outros não registram nem pontuam nele).
        # Contato do ADMIN não toma o lead da equipe.
        type(op).objects.filter(pk=op.pk, responsavel__isnull=True).update(responsavel=abordagem.usuario,
                                                                            atribuida_em=timezone.now())
    agora = timezone.now()
    if abordagem.cnpj_confirmado and not abordagem.cliente_situacao:
        # foto da situação do cliente no momento da confirmação: define se a venda futura conta na captação
        abordagem.cliente_situacao, abordagem.cliente_ultima_compra = situacao_cliente(
            abordagem.cnpj_confirmado, timezone.localdate(abordagem.data_hora))
    if concluir_pendentes:
        _pendentes(abordagem.empresa).update(retorno_concluido=True, retorno_concluido_em=agora)
    abordagem.save()
    if abordagem.resultado == ResultadoAbordagem.CONVERSA_NAO_CONTATAR:
        marcar_nao_contatar(abordagem.empresa, abordagem.usuario, "Pediu para não ser contatado (registrado na abordagem)")
    abordagem.aviso_cnpj = aplicar_cnpj_confirmado(abordagem) if abordagem.cnpj_confirmado else ""
    if abordagem.cnpj_confirmado:
        abordagem.aviso_cnpj = (abordagem.aviso_cnpj + " " + texto_situacao(
            abordagem.cliente_situacao, abordagem.cliente_ultima_compra)).strip()
    return abordagem


def situacao_cliente(cnpj, na_data=None):
    """Situação do CNPJ confirmado na data da confirmação: (situacao, ultima_compra).
    NOVO: CNPJ fora do Salesforce. SEM_COMPRA: cadastrado, sem compra conhecida. INATIVO: última compra há mais que
    ConfiguracaoAnalise.dias_sem_compra_captacao. ATIVO: comprou dentro do limite (venda fica com a carteira).
    Última compra = a mais recente entre as notas de venda importadas e o LastBuyDate da conta, ANTES da data."""
    from django.db.models import Max
    from carteira.models import Abordagem, ConfiguracaoAnalise, NotaFiscal
    S = Abordagem.SituacaoCliente
    na_data = na_data or timezone.localdate()
    contas = Empresa.objects.filter(cnpj=cnpj, salesforce_id__isnull=False)
    if not contas.exists():
        return S.NOVO, None
    datas = []
    nf = (NotaFiscal.objects.filter(empresa__cnpj=cnpj, tipo=NotaFiscal.Tipo.VENDA, data_emissao__lt=na_data)
          .aggregate(m=Max("data_emissao"))["m"])
    if nf:
        datas.append(nf)
    datas += [d for d in contas.values_list("ultima_compra_cadastro", flat=True) if d and d < na_data]
    if not datas:
        return S.SEM_COMPRA, None
    ultima = max(datas)
    limite = ConfiguracaoAnalise.obter().dias_sem_compra_captacao or 90
    return (S.INATIVO if (na_data - ultima).days > limite else S.ATIVO), ultima


def texto_situacao(situacao, ultima):
    from carteira.models import Abordagem
    S = Abordagem.SituacaoCliente
    quando = f" (última compra {ultima:%d/%m/%Y})" if ultima else ""
    return {S.NOVO: "Cliente novo: as vendas deste CNPJ contam na captação.",
            S.SEM_COMPRA: "Já cadastrado, mas nunca comprou: as vendas contam na captação.",
            S.INATIVO: f"Cliente inativo{quando}: a reativação conta na captação.",
            S.ATIVO: f"Cliente ativo da carteira{quando}: as vendas deste CNPJ NÃO contam na captação "
                     "(ficam com a carteira)."}.get(situacao, "")


def criar_pessoa(empresa, usuario, nome, cargo="", departamento="", telefone="", email=""):
    """Pessoa informada pelo cliente no atendimento (nome de quem atendeu, cargo, departamento, telefone, e-mail)."""
    from carteira.models import Contato
    dig = "".join(c for c in telefone or "" if c.isdigit())
    celular = len(dig) >= 11 and dig[-9] == "9"
    return Contato.objects.create(
        empresa=empresa, nome=nome.strip()[:200], cargo=cargo.strip()[:120], cargo_confirmado=bool(cargo.strip()),
        departamento=departamento[:60], telefone="" if celular else telefone.strip()[:60],
        celular=telefone.strip()[:60] if celular else "", email=email.strip(), origem="ATENDIMENTO",
        criado_por=usuario, coletado_em=timezone.now(),
        fonte_referencia=f"informado no atendimento por {usuario.get_full_name() or usuario.get_username()}")


def registrar_canais_informados(empresa, usuario, canais, pessoa=None):
    """Telefones e e-mails informados pelo cliente no atendimento: entram como contatos da empresa, confirmados
    pela equipe, com o departamento. Não duplica o que já existe (só completa departamento/finalidade)."""
    from carteira.forms import FINALIDADE_DEPTO
    from carteira.models import CanalEmpresa
    from prospeccao.services import contatos as ct
    quem = usuario.get_full_name() or usuario.get_username()
    agora, salvos = timezone.now(), []
    for c in canais:
        evid = f"informado pelo cliente no atendimento ({quem}, {timezone.localtime(agora):%d/%m/%Y})"
        if pessoa is not None:
            evid += f"; contato de {pessoa.nome}"
        canal = ct.registrar(empresa, c["tipo"], c["valor"], fonte="atendimento", origem=CanalEmpresa.Origem.HUMANO,
                             confianca=CanalEmpresa.Confianca.ALTA, aplica_a=CanalEmpresa.AplicaA.EMPRESA,
                             evidencia=evid, departamento=c.get("departamento", ""),
                             finalidade=FINALIDADE_DEPTO.get(c.get("departamento", "")))
        if canal is None:
            continue
        mudou = []
        if c.get("departamento") and not canal.departamento:
            canal.departamento = c["departamento"][:120]
            mudou.append("departamento")
        if canal.confirmacao_humana != CanalEmpresa.Confirmacao.ATENDIDO:
            canal.confirmacao_humana, canal.confirmado_por, canal.confirmado_em = (
                CanalEmpresa.Confirmacao.ATENDIDO, usuario, agora)
            mudou += ["confirmacao_humana", "confirmado_por", "confirmado_em"]
        if mudou:
            canal.save(update_fields=mudou)
        salvos.append(canal)
    return salvos


def aplicar_cnpj_confirmado(abordagem):
    """CNPJ confirmado pelo atendimento é a fonte mais confiável: vira o CNPJ do lead.
    Nunca une com outro cadastro: se o CNPJ já é de outro registro (ex.: conta do Salesforce), só registra o aviso.
    Retorna uma frase para a mensagem de confirmação."""
    from prospeccao.services.enriquecimento import registrar_dado
    e, c = abordagem.empresa, abordagem.cnpj_confirmado
    quem = abordagem.usuario.get_full_name() or abordagem.usuario.get_username()
    ref = f"confirmado por {quem} em {timezone.localtime(abordagem.data_hora):%d/%m/%Y %H:%M}"
    registrar_dado(e, "cnpj_confirmado_atendimento", c, "atendimento", ref)
    from carteira.models import CnpjAssociado, empresa_por_cnpj

    def associar(cnpj, origem, obs):
        CnpjAssociado.objects.get_or_create(empresa=e, cnpj=cnpj, defaults={
            "origem": origem, "observacao": obs[:300], "criado_por": abordagem.usuario})

    if e.cnpj == c:
        if e.cnpj_status != "VALIDO":
            Empresa.objects.filter(pk=e.pk).update(cnpj_status="VALIDO")
        associar(c, CnpjAssociado.Origem.ATENDIMENTO, ref)
        return "CNPJ confirmado com o cliente."
    outra = empresa_por_cnpj(c, excluir=e)
    if outra is not None:
        conta = f"a conta do Salesforce {outra.salesforce_id}" if outra.salesforce_id else f"outro cadastro ({outra.nome[:60]})"
        registrar_dado(e, "cnpj_confirmado_conflito", c, "atendimento", f"{ref}; o CNPJ já pertence a {conta}")
        associar(c, CnpjAssociado.Origem.ATENDIMENTO, f"{ref}; também é de {conta} (cadastros não unidos)")
        return f"Atenção: o CNPJ confirmado já pertence a {conta}. Nada foi unido; confira na ficha."
    anterior = e.cnpj or ""
    if anterior:
        # o encontrado na pesquisa CONTINUA associado: reconhece a empresa se aparecer de novo numa pesquisa
        registrar_dado(e, "cnpj_substituido_atendimento", anterior, "atendimento", f"substituído pelo CNPJ {c}; {ref}")
        associar(anterior, CnpjAssociado.Origem.PESQUISA,
                 f"encontrado na pesquisa; o principal passou a ser {c} ({ref})")
    associar(c, CnpjAssociado.Origem.ATENDIMENTO, ref)
    Empresa.objects.filter(pk=e.pk).update(cnpj=c, cnpj_origem=c, cnpj_status="VALIDO")
    e.refresh_from_db()
    try:  # situação cadastral do CNPJ confirmado (base local ou consulta pública); falha não impede o registro
        from prospeccao.conectores.receita import consultar_local
        from prospeccao.services.identificacao_cnpj import _aplicar_situacao
        dados, carga = consultar_local(c)
        _aplicar_situacao(e, dados, carga, None)
    except Exception:
        pass
    return ("CNPJ corrigido com o cliente; o encontrado na pesquisa continua associado à empresa." if anterior
            else "CNPJ confirmado com o cliente e associado ao lead.")


def concluir_retorno(abordagem):
    abordagem.retorno_concluido = True
    abordagem.retorno_concluido_em = timezone.now()
    abordagem.save(update_fields=["retorno_concluido", "retorno_concluido_em"])


def marcar_nao_contatar(empresa, usuario, motivo):
    empresa.nao_contatar = True
    empresa.nao_contatar_motivo = motivo
    empresa.nao_contatar_em = timezone.now()
    empresa.nao_contatar_por = usuario
    empresa.save(update_fields=["nao_contatar", "nao_contatar_motivo", "nao_contatar_em", "nao_contatar_por", "atualizado_em"])


def liberar_contato(empresa):
    empresa.nao_contatar = False
    empresa.nao_contatar_motivo = ""
    empresa.nao_contatar_em = None
    empresa.nao_contatar_por = None
    empresa.save(update_fields=["nao_contatar", "nao_contatar_motivo", "nao_contatar_em", "nao_contatar_por", "atualizado_em"])


@transaction.atomic
def gerar_lista(nome, qs, usuario, filtros=None, limite=None):
    """Cria uma lista só com empresas elegíveis para nova abordagem, independente dos filtros recebidos."""
    ids = list(novas_abordagens(qs).order_by("nome").values_list("pk", flat=True).distinct())
    if limite:
        ids = ids[:limite]
    lista = ListaAbordagem.objects.create(nome=nome, criada_por=usuario, filtros=filtros or {})
    ItemLista.objects.bulk_create([ItemLista(lista=lista, empresa_id=i) for i in ids])
    return lista


def qualidade_cadastral():
    vazio = lambda campo: Q(**{campo: ""})  # noqa: E731
    return {
        "Sem CNPJ válido": Empresa.objects.filter(cnpj__isnull=True).count(),
        "Requer revisão": Empresa.objects.filter(requer_revisao=True).count(),
        "Sem segmento": Empresa.objects.filter(vazio("segmento")).count(),
        "Sem cidade/UF": Empresa.objects.filter(vazio("cidade") | vazio("uf")).count(),
        "Sem telefone": Empresa.objects.filter(vazio("telefone")).count(),
        "Sem e-mail comercial": Empresa.objects.filter(vazio("email")).count(),
        "Sem CNAE": Empresa.objects.filter(vazio("cnae")).count(),
        "Sem data de abertura": Empresa.objects.filter(data_abertura__isnull=True).count(),
        "Sem pessoa de contato": Empresa.objects.exclude(contatos__ativo=True).count(),
    }
