"""Inside Sales: quem falta procurar e quem já foi procurado (e por quem).

- "A procurar": empresas encontradas pela prospecção (com ou sem CNPJ), em aberto, sem nenhum contato registrado.
- "Procurados": empresas com pelo menos um contato registrado (Abordagem), com quem atendeu, quando, canal e resultado.
  Basta registrar o contato para a empresa sair de uma aba e aparecer na outra.
Conhecidas no Salesforce, descartadas e fora da fila não entram (ficam na carteira ou no histórico).
"""
from datetime import date, timedelta
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Exists, Max, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from carteira.models import Abordagem
from prospeccao import apresentacao as apr
from prospeccao.models import Oportunidade, Pesquisa
from prospeccao.papeis import eh_adm

POR_PAGINA = 50


def base_inside():
    """Leads da prospecção (externos) que interessam à equipe. Sem conhecidas, descartadas, fora da fila e fora do
    escopo comercial (escopo FORA: atividade sem relação com o que a Brida vende) — o mesmo recorte da Visão geral."""
    return (Oportunidade.objects.filter(tipo=Oportunidade.Tipo.NOVA_EXTERNA)
            .exclude(status__in=[Oportunidade.Status.DESCARTADA, Oportunidade.Status.JA_CLIENTE,
                                 Oportunidade.Status.FORA_FILA, Oportunidade.Status.FORA_REGIAO])
            .exclude(escopo="FORA")
            .exclude(empresa__salesforce_id__isnull=False))


def _anotar(qs):
    ult = Abordagem.objects.filter(empresa=OuterRef("empresa")).order_by("-data_hora")
    return qs.annotate(
        procurado=Exists(Abordagem.objects.filter(empresa=OuterRef("empresa"))),
        n_contatos=Count("empresa__abordagens", distinct=True),
        ult_quando=Max("empresa__abordagens__data_hora"),
        ult_usuario=Subquery(ult.values("usuario__username")[:1]),
        ult_usuario_nome=Subquery(ult.values("usuario__first_name")[:1]),
        ult_resultado=Subquery(ult.values("resultado")[:1]),
        ult_canal=Subquery(ult.values("canal")[:1]),
        ult_retorno=Subquery(ult.values("data_retorno")[:1]),
    )


def _contagem_abas():
    """Mesmos números das abas A procurar / Procurados (sem filtros)."""
    qs = base_inside().annotate(procurado=Exists(Abordagem.objects.filter(empresa=OuterRef("empresa"))))
    return {"procurar": qs.filter(procurado=False, status__in=[Oportunidade.Status.NOVA,
                                                                Oportunidade.Status.LIBERADA]).count(),
            "procurados": qs.filter(procurado=True).count()}


@login_required
def inside_sales(request):
    aba = "procurados" if request.GET.get("aba") == "procurados" else "procurar"
    q = (request.GET.get("q") or "").strip()
    pesquisa = (request.GET.get("pesquisa") or "").strip()
    cidade = (request.GET.get("cidade") or "").strip()
    cnpj = request.GET.get("cnpj") or ""
    atendente = (request.GET.get("atendente") or "").strip()

    from prospeccao.services import atendimento as at
    from prospeccao.services.conversao import marcar_convertidos
    marcar_convertidos()  # quem já comprou sai da base antes de montar as listas
    qs = _anotar(base_inside().select_related("empresa", "em_atendimento_por", "responsavel"))
    if q:
        dig = "".join(ch for ch in q if ch.isdigit())
        filtro = Q(empresa__nome__icontains=q) | Q(empresa__nome_fantasia__icontains=q) | \
            Q(empresa__razao_social__icontains=q)
        if len(dig) >= 4:
            filtro |= Q(empresa__cnpj__contains=dig)
        qs = qs.filter(filtro)
    if pesquisa.isdigit():
        qs = qs.filter(pesquisas__pk=int(pesquisa))
    if cidade:
        qs = qs.filter(empresa__cidade__icontains=cidade)
    if cnpj == "com":
        qs = qs.exclude(Q(empresa__cnpj__isnull=True) | Q(empresa__cnpj=""))
    elif cnpj == "sem":
        qs = qs.filter(Q(empresa__cnpj__isnull=True) | Q(empresa__cnpj=""))

    a_procurar = qs.filter(procurado=False, status__in=[Oportunidade.Status.NOVA, Oportunidade.Status.LIBERADA])
    procurados = qs.filter(procurado=True)
    if atendente:
        procurados = procurados.filter(empresa__abordagens__usuario__username=atendente)
    contagem = {"procurar": a_procurar.count(), "procurados": procurados.count(),
                "em_atendimento": a_procurar.filter(em_atendimento_por__isnull=False,
                                                    em_atendimento_desde__gte=at.limite_validade()).count()}

    if aba == "procurados":
        lista_qs = procurados.order_by("-ult_quando")
    else:
        lista_qs = a_procurar.order_by("prioridade", "-criada_em")
    pagina = Paginator(lista_qs.distinct(), POR_PAGINA).get_page(request.GET.get("page"))
    itens = list(pagina.object_list)
    apr.anexar_contatos(itens)
    resultados = dict(Abordagem._meta.get_field("resultado").choices)
    canais = dict(Abordagem._meta.get_field("canal").choices)
    for o in itens:
        o.ult_resultado_rot = resultados.get(o.ult_resultado, "")
        o.ult_canal_rot = canais.get(o.ult_canal, "")
        o.atendente = o.ult_usuario_nome or o.ult_usuario or ""
        quem = at.vigente(o)
        o.trava_nome = (quem.get_full_name() or quem.get_username()) if quem else ""
        o.trava_minha = bool(quem) and quem.pk == request.user.pk
        d = o.responsavel
        o.dono_nome = (d.get_full_name() or d.get_username()) if d else ""
        o.dono_outro = at.dono_de_outro(o, request.user)

    params = request.GET.copy()
    params.pop("page", None)
    filtros = {k: v for k, v in params.items() if k != "aba" and v}
    sem_aba = params.copy()
    sem_aba.pop("aba", None)
    return render(request, "prospeccao/inside_sales.html", {
        "aba": aba, "itens": itens, "pagina": pagina, "contagem": contagem,
        "f": {"q": q, "pesquisa": pesquisa, "cidade": cidade, "cnpj": cnpj, "atendente": atendente},
        "tem_filtro": bool(filtros),
        "querystring": params.urlencode(),
        "querystring_sem_aba": sem_aba.urlencode(),
        "pesquisas": Pesquisa.objects.order_by("nome").only("pk", "nome"),
        "atendentes": (get_user_model().objects.filter(abordagens__isnull=False).distinct()
                       .order_by("username").values_list("username", flat=True)),
    })


@login_required
@require_POST
def atendimento(request, pk):
    """Assumir ou liberar o atendimento de um lead (trava: uma pessoa por vez)."""
    from prospeccao.services import atendimento as at
    op = get_object_or_404(Oportunidade.objects.select_related("empresa"), pk=pk)
    proximo = request.POST.get("proximo") or ""
    if not url_has_allowed_host_and_scheme(proximo, allowed_hosts={request.get_host()}):
        proximo = ""
    if request.POST.get("acao") == "liberar":
        try:
            at.liberar(op, request.user, forcar=eh_adm(request.user))
            messages.success(request, f"{op.empresa.nome}: lead liberado para outro atendente.")
        except at.AtendimentoOcupado as e:
            messages.warning(request, str(e))
        return redirect(proximo or "prospeccao:inside_sales")
    try:
        at.assumir(op, request.user)
    except at.AtendimentoOcupado as e:
        messages.warning(request, str(e))
        return redirect(proximo or "prospeccao:inside_sales")
    destino = reverse("carteira:registrar_abordagem", args=[op.empresa_id])
    return redirect(f"{destino}?proximo={quote(proximo or reverse('prospeccao:inside_sales'))}")


@login_required
@require_POST
def desclassificar(request, pk):
    """Lead ruim: sai da lista e fica desclassificado, com o motivo e a justificativa de quem atendeu."""
    from prospeccao.services import oportunidades as ops
    op = get_object_or_404(Oportunidade.objects.select_related("empresa"), pk=pk)
    try:
        ops.desclassificar(op, request.user, request.POST.get("motivo", ""), request.POST.get("justificativa", ""))
    except ops.RegraViolada as e:
        messages.warning(request, f"{op.empresa.nome}: não desclassificado. {e}")
        return redirect(reverse("carteira:registrar_abordagem", args=[op.empresa_id]))
    messages.success(request, f"{op.empresa.nome} desclassificado: saiu da lista de leads. "
                              "A justificativa fica registrada; um ADMIN pode reabrir se necessário.")
    return redirect("prospeccao:inside_sales")


@login_required
def minha_performance(request):
    """Performance simples de quem atende: o mês, os números dele e o que ficou pendente. O ADM escolhe a pessoa."""
    from prospeccao.services import atendimento as at
    from prospeccao.services import bi_captacao as bi
    inicio, fim, mes = bi.periodo_do_mes(request.GET.get("mes"))
    if request.GET.get("simulacao") == "1":
        from django.db import transaction
        with transaction.atomic():  # análise de teste: equipe e contatos fictícios, DESFEITOS ao final
            bi.simular_dados(inicio, fim)
            resposta = _performance(request, inicio, fim, mes, simulacao=True)
            resposta.render()  # monta a página antes de desfazer os dados
            transaction.set_rollback(True)
        return resposta
    return _performance(request, inicio, fim, mes)


def _performance(request, inicio, fim, mes, simulacao=False):
    from django.template.response import TemplateResponse
    from prospeccao.services import atendimento as at
    from prospeccao.services import bi_captacao as bi
    from prospeccao.services import gamificacao as gm
    U = get_user_model()
    pessoa = request.user
    if eh_adm(request.user) and request.GET.get("vendedor"):
        pessoa = U.objects.filter(username=request.GET["vendedor"]).first() or request.user
    jogo = gm.visao(inicio, fim, pessoa)
    como = ""
    if simulacao and (jogo["eu"] is None or not jogo["eu"]["metricas"]["procuradas"]):
        # na simulação, quem olha não tem contatos: mostra a visão de um atendente fictício do meio do ranking
        meio = jogo["ranking"][min(1, len(jogo["ranking"]) - 1)] if jogo["ranking"] else None
        if meio is not None:
            pessoa, como = meio["usuario"], meio["nome"]
            jogo = gm.visao(inicio, fim, pessoa)
    dados = bi.calcular(inicio, fim, usuario=pessoa.username)
    k = dados["kpis"]
    maior = max([d["conversa"] + d["tentativa"] for d in dados["dias"]] + [1])
    for d in dados["dias"]:
        d["alt_c"] = round(d["conversa"] * 100 / maior)
        d["alt_t"] = round(d["tentativa"] * 100 / maior)
    retornos = list(Abordagem.objects.filter(usuario=pessoa, data_retorno__isnull=False, retorno_concluido=False)
                    .select_related("empresa").order_by("data_retorno")[:30])
    atendendo = list(base_inside().filter(em_atendimento_por=pessoa, em_atendimento_desde__gte=at.limite_validade())
                     .select_related("empresa"))
    anterior = (inicio - timedelta(days=1)).strftime("%Y-%m")
    seguinte = (fim + timedelta(days=1)).strftime("%Y-%m")
    return TemplateResponse(request, "prospeccao/minha_performance.html", {
        "k": k, "dias": dados["dias"], "mes": mes,
        "mes_rotulo": f"{MESES[inicio.month - 1].capitalize()} de {inicio.year}",
        "mes_anterior": anterior, "mes_seguinte": seguinte if fim < date.today() else "",
        "pessoa": pessoa, "propria": pessoa.pk == request.user.pk or bool(como), "retornos": retornos,
        "hoje": date.today(), "atendendo": atendendo,
        "vendedores": list(bi.vendedores()) if eh_adm(request.user) else [],
        "contagem": _contagem_abas() if eh_adm(request.user) else {},
        "jogo": jogo, "simulacao": simulacao, "como": como,
    })


def _data(texto, padrao):
    try:
        return date.fromisoformat((texto or "").strip())
    except ValueError:
        return padrao


@login_required
def captacao_detalhe(request, tipo):
    """O que está por trás de um cartão do painel: lista do mais novo ao mais antigo, com filtro de cliente,
    quem atendeu/vendeu e período. Mesmas regras e filtros do painel."""
    from django.http import Http404
    from prospeccao.services import bi_captacao as bi
    if tipo not in bi.DETALHES:
        raise Http404
    inicio, fim, mes = bi.periodo_do_mes(request.GET.get("mes"))
    de, ate = _data(request.GET.get("de"), inicio), _data(request.GET.get("ate"), fim)
    if ate < de:
        de, ate = ate, de
    pesquisa = request.GET.get("pesquisa") or ""
    vendedor = request.GET.get("vendedor") or ""
    q = (request.GET.get("q") or "").strip()
    simulacao = request.GET.get("simulacao") == "1"
    filtros = dict(pesquisa=int(pesquisa) if pesquisa.isdigit() else None, usuario=vendedor or None, q=q)
    if simulacao:
        from django.db import transaction
        with transaction.atomic():  # demonstração: dados fictícios desfeitos ao final; nada fica no banco
            bi.simular_dados(inicio, fim)
            dados = bi.detalhe(tipo, de, ate, **filtros)
            vendedores_lista = list(bi.vendedores())
            transaction.set_rollback(True)
    else:
        dados = bi.detalhe(tipo, de, ate, **filtros)
        vendedores_lista = bi.vendedores()
    pagina = Paginator(dados["linhas"], POR_PAGINA).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    volta = "&".join(f"{k}={v}" for k, v in (("mes", mes), ("pesquisa", pesquisa), ("vendedor", vendedor),
                                             ("simulacao", "1" if simulacao else "")) if v)
    return render(request, "prospeccao/captacao_detalhe.html", {
        "tipo": tipo, "d": dados, "pagina": pagina, "querystring": params.urlencode(),
        "f": {"q": q, "vendedor": vendedor, "de": de.isoformat(), "ate": ate.isoformat(), "pesquisa": pesquisa,
              "mes": mes, "de_rot": de.strftime("%d/%m/%Y"), "ate_rot": ate.strftime("%d/%m/%Y")},
        "tem_filtro": bool(q or vendedor or request.GET.get("de") or request.GET.get("ate")),
        "vendedores": vendedores_lista, "simulacao": simulacao, "volta": volta,
        "abas": [(k, v[0]) for k, v in bi.DETALHES.items()],
    })


MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro"]


@login_required
def captacao(request):
    """Painel de captação (BI): o mês escolhido, por pesquisa e por vendedor. Somente leitura."""
    from prospeccao.services import bi_captacao as bi
    inicio, fim, mes = bi.periodo_do_mes(request.GET.get("mes"))
    pesquisa = request.GET.get("pesquisa") or ""
    vendedor = request.GET.get("vendedor") or ""
    simulacao = request.GET.get("simulacao") == "1"
    filtros = dict(pesquisa=int(pesquisa) if pesquisa.isdigit() else None, usuario=vendedor or None)
    if simulacao:
        from django.db import transaction
        with transaction.atomic():  # demonstração: dados fictícios calculados e DESFEITOS; nada fica no banco
            bi.simular_dados(inicio, fim)
            dados = bi.calcular(inicio, fim, **filtros)
            vendedores_lista = list(bi.vendedores())
            transaction.set_rollback(True)
    else:
        dados = bi.calcular(inicio, fim, **filtros)
        vendedores_lista = bi.vendedores()
    anterior = (inicio - timedelta(days=1)).strftime("%Y-%m")
    seguinte = (fim + timedelta(days=1)).strftime("%Y-%m")
    extra = "&".join(f"{k}={v}" for k, v in (("pesquisa", pesquisa), ("vendedor", vendedor),
                                              ("simulacao", "1" if simulacao else "")) if v)
    return render(request, "prospeccao/captacao.html", {
        "d": dados, "k": dados["kpis"], "mes": mes, "mes_rotulo": f"{MESES[inicio.month - 1].capitalize()} de {inicio.year}",
        "mes_anterior": anterior, "mes_seguinte": seguinte if fim < date.today() else "",
        "extra": extra, "f": {"pesquisa": pesquisa, "vendedor": vendedor},
        "pesquisas": Pesquisa.objects.order_by("nome").only("pk", "nome"), "vendedores": vendedores_lista,
        "simulacao": simulacao,
        "grafico": {"dias": dados["dias"], "pizza": dados["pizza"], "funil": dados["funil"],
                    "resultados": dados["resultados"], "rankings": dados["rankings"]},
    })
