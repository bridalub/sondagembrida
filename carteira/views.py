import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from prospeccao.models import Oportunidade

from carteira.forms import AbordagemForm, ContatoForm, FiltroEmpresaForm, GerarListaForm, NaoContatarForm
from carteira.models import (
    ConfiguracaoAnalise, Empresa, Importacao, ListaAbordagem, NotaFiscal, OcorrenciaImportacao,
    SituacaoCompra, SituacaoContato,
)
from carteira.services import abordagens as ab
from carteira.services.indicadores import cobertura_compras, data_limite_inatividade


def _rotulos(choices):
    return dict(choices)


@login_required
def visao_geral(request):
    qs = ab.anotar(Empresa.objects.all())
    por_compra = {r["situacao_compra"]: r["n"]
                  for r in qs.order_by().values("situacao_compra").annotate(n=Count("id"))}
    por_contato = {r["situacao_contato"]: r["n"]
                   for r in qs.order_by().values("situacao_contato").annotate(n=Count("id"))}
    cfg = ConfiguracaoAnalise.obter()
    limite = data_limite_inatividade()
    inativos = (Empresa.objects.filter(indicador__situacao=SituacaoCompra.COMPROU, indicador__ultima_compra__lt=limite)
                .count() if limite else None)
    retornos = ab.retornos_pendentes()
    contexto = {
        "total": Empresa.objects.count(),
        "por_compra": [(k, v, por_compra.get(k, 0)) for k, v in SituacaoCompra.choices],
        "por_contato": [(k, v, por_contato.get(k, 0)) for k, v in SituacaoContato.choices],
        "novas": ab.novas_abordagens().count(),
        "retornos_total": retornos.count(),
        "retornos_atrasados": retornos.filter(data_retorno__lt=timezone.localdate()).count(),
        "qualidade": ab.qualidade_cadastral(),
        "cobertura": cobertura_compras(),
        "cfg": cfg,
        "inativos": inativos,
        "ultimas_importacoes": Importacao.objects.all()[:5],
        "notas_sem_empresa": NotaFiscal.objects.filter(empresa__isnull=True, presente_na_origem=True).count(),
    }
    return render(request, "carteira/visao_geral.html", contexto)


def _lista_empresas(request, *, somente_novas):
    form = FiltroEmpresaForm(request.GET or None)
    qs = ab.anotar(Empresa.objects.select_related("indicador"))
    if somente_novas:
        qs = ab.novas_abordagens(qs)
    qs = form.aplicar(qs) if request.GET else qs
    return form, qs.order_by("nome")


@login_required
def empresas(request):
    form, qs = _lista_empresas(request, somente_novas=False)
    pagina = Paginator(qs, 50).get_page(request.GET.get("page"))
    from prospeccao.services.cruzamento import cruzamento_atual
    atual, quando, horas = cruzamento_atual()
    return render(request, "carteira/empresas.html", {
        "form": form, "pagina": pagina, "total": qs.count(), "titulo": "Carteira Salesforce", "somente_novas": False,
        "cruzamento_atual": atual, "cruzamento_quando": quando, "cruzamento_horas": horas,
        "rot_compra": _rotulos(SituacaoCompra.choices), "rot_contato": _rotulos(SituacaoContato.choices),
        "querystring": _querystring_sem_pagina(request),
    })


@login_required
def novas_abordagens(request):
    form, qs = _lista_empresas(request, somente_novas=True)
    if request.method == "POST":
        gform = GerarListaForm(request.POST)
        if gform.is_valid():
            lista = ab.gerar_lista(gform.cleaned_data["nome"], qs, request.user,
                                   filtros=request.GET.dict(), limite=gform.cleaned_data["limite"])
            messages.success(request, f"Lista '{lista.nome}' criada com {lista.itens.count()} empresas.")
            return redirect("carteira:lista_detalhe", pk=lista.pk)
    else:
        gform = GerarListaForm()
    pagina = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "carteira/empresas.html", {
        "form": form, "pagina": pagina, "total": qs.count(), "titulo": "Novas abordagens", "somente_novas": True,
        "gform": gform, "rot_compra": _rotulos(SituacaoCompra.choices), "rot_contato": _rotulos(SituacaoContato.choices),
        "querystring": _querystring_sem_pagina(request),
    })


def _querystring_sem_pagina(request):
    q = request.GET.copy()
    q.pop("page", None)
    return q.urlencode()


@login_required
def empresa_detalhe(request, pk):
    # Visualizar NÃO registra contato.
    empresa = get_object_or_404(ab.anotar(Empresa.objects.select_related("indicador", "nao_contatar_por")), pk=pk)
    mesma_raiz = (Empresa.objects.filter(cnpj__startswith=empresa.raiz_cnpj).exclude(pk=empresa.pk)
                  if empresa.cnpj else Empresa.objects.none())
    return render(request, "carteira/empresa_detalhe.html", {
        "e": empresa,
        "indicador": getattr(empresa, "indicador", None),
        "contatos": empresa.contatos.all(),
        "abordagens": empresa.abordagens.select_related("contato", "usuario"),
        "notas": empresa.notas.all()[:30],
        "categorias": empresa.categorias_compra.all(),
        "registros": empresa.registros_origem.all(),
        "mesma_raiz": mesma_raiz,
        "contato_form": ContatoForm(),
        "nao_contatar_form": NaoContatarForm(),
        "rot_compra": _rotulos(SituacaoCompra.choices), "rot_contato": _rotulos(SituacaoContato.choices),
        "limite_inatividade": data_limite_inatividade(),
    })


def _proximo_seguro(request, padrao):
    """Destino pós-ação vindo do formulário (?proximo= ou campo), só se for deste site."""
    destino = request.POST.get("proximo") or request.GET.get("proximo") or ""
    if destino and url_has_allowed_host_and_scheme(destino, allowed_hosts={request.get_host()},
                                                   require_https=request.is_secure()):
        return destino
    return padrao


def _url_detalhe(empresa):
    if Oportunidade.objects.filter(empresa=empresa).exists():
        return reverse("prospeccao:empresa", args=[empresa.pk])
    return reverse("carteira:empresa_detalhe", args=[empresa.pk])


@login_required
def registrar_abordagem(request, pk):
    from prospeccao import papeis
    from prospeccao.services import atendimento as at
    empresa = get_object_or_404(Empresa, pk=pk)
    form = AbordagemForm(request.POST or None, request.FILES or None, empresa=empresa)
    bloqueio = None
    voltar = _proximo_seguro(request, _url_detalhe(empresa))
    # Trava de atendimento: só uma pessoa atende o lead de cada vez (o ADM é avisado, não bloqueado)
    op_lead = at.oportunidade_da_empresa(empresa)
    ocupado = None
    if op_lead is not None:
        try:
            at.assumir(op_lead, request.user)
        except at.AtendimentoOcupado as e:
            ocupado = str(e)
    if ocupado and not papeis.eh_adm(request.user):
        messages.warning(request, ocupado)
        return redirect(voltar)
    if ocupado:
        bloqueio = f"{ocupado} Como ADM, você pode registrar mesmo assim."
    if request.method != "POST":
        try:
            ab.validar_nova_abordagem(empresa, request.user)
        except ab.RegraAbordagemViolada as e:
            bloqueio = f"{bloqueio} {e}" if bloqueio else str(e)
    if request.method == "POST" and form.is_valid():
        abordagem = form.save(commit=False)
        abordagem.empresa = empresa
        abordagem.usuario = request.user
        # disputa do mês: só leads da prospecção e quem é do Inside Sales
        from prospeccao.models import Oportunidade
        jogo = (op_lead is not None and op_lead.tipo == Oportunidade.Tipo.NOVA_EXTERNA and not empresa.salesforce_id
                and not papeis.eh_adm(request.user))
        antes = None
        if jogo:
            from prospeccao.services import gamificacao as gm
            antes = gm.foto(request.user)
        cd = form.cleaned_data
        novos_canais = []
        try:
            from django.db import transaction
            with transaction.atomic():  # pessoa, canais e contato entram juntos (ou nada)
                pessoa = None
                if cd.get("pessoa_nome"):
                    pessoa = ab.criar_pessoa(empresa, request.user, cd["pessoa_nome"], cd.get("pessoa_cargo", ""),
                                             cd.get("pessoa_departamento", ""), cd.get("pessoa_telefone", ""),
                                             cd.get("pessoa_email", ""))
                    abordagem.contato = pessoa
                ab.registrar_abordagem(abordagem, concluir_pendentes=cd["concluir_pendentes"],
                                       justificativa_excecao=cd.get("justificativa_excecao", ""))
                canais = list(form.extras)
                if pessoa is not None:
                    depto = cd.get("pessoa_departamento", "")
                    if cd.get("pessoa_telefone"):
                        canais.append({"tipo": "TELEFONE", "valor": cd["pessoa_telefone"], "departamento": depto})
                    if cd.get("pessoa_email"):
                        canais.append({"tipo": "EMAIL", "valor": cd["pessoa_email"], "departamento": depto})
                novos_canais = ab.registrar_canais_informados(empresa, request.user, canais, pessoa)
        except ab.RegraAbordagemViolada as e:
            form.add_error(None, str(e) + (" Informe a justificativa de exceção." if e.permite_excecao else ""))
        else:
            if op_lead is not None:
                at.liberar(op_lead, forcar=True)  # contato registrado: o lead deixa de estar "em atendimento"
            msg = "Abordagem registrada." + (f" {abordagem.aviso_cnpj}" if getattr(abordagem, "aviso_cnpj", "") else "")
            if abordagem.contato_id and cd.get("pessoa_nome"):
                msg += f" {abordagem.contato.nome} cadastrado(a) como pessoa de contato."
            if novos_canais:
                msg += f" {len(novos_canais)} telefone(s)/e-mail(s) da empresa salvo(s)."
            if abordagem.data_retorno:
                msg += f" Retorno agendado para {abordagem.data_retorno.strftime('%d/%m/%Y')} (aparece em Acompanhar retornos)."
            messages.success(request, msg)
            if antes is not None:
                ganho = gm.mensagem_ganho(antes, request.user, abordagem.resultado)
                if ganho:
                    messages.info(request, ganho)
            return redirect(voltar)
    from prospeccao.apresentacao import contato_lista
    from carteira.forms import DEPARTAMENTOS
    from carteira.models import CanalEmpresa
    extras_post = [{"tipo": t, "valor": v, "departamento": d} for t, v, d in zip(
        request.POST.getlist("extra_tipo"), request.POST.getlist("extra_valor"), request.POST.getlist("extra_depto"))
        if (v or "").strip()] if request.method == "POST" else []
    canais = list(empresa.canais.filter(tipo__in=[CanalEmpresa.Tipo.TELEFONE, CanalEmpresa.Tipo.EMAIL], restrito=False)
                  .exclude(confirmacao_humana=CanalEmpresa.Confirmacao.INCORRETO).order_by("tipo", "-preferido", "pk"))
    return render(request, "carteira/abordagem_form.html", {
        "departamentos": DEPARTAMENTOS, "extras_post": extras_post, "canais": canais,
        "jogo_regras": op_lead is not None and not empresa.salesforce_id,  # lead da prospecção: mostra os pontos
        "motivos_desclassificar": (__import__("prospeccao.services.oportunidades", fromlist=["x"]).MOTIVOS_DESCLASSIFICAR
                                   if op_lead is not None and op_lead.tipo == "NOVA_EXTERNA" and not empresa.salesforce_id
                                   and op_lead.status in ("NOVA", "LIBERADA") else []),
        "form": form, "e": empresa, "bloqueio": bloqueio, "voltar": voltar, "contato": contato_lista(empresa),
        "op_lead": op_lead, "minutos_trava": at.minutos(),
        "historico": empresa.abordagens.select_related("usuario", "contato")[:10],
        "pendentes": empresa.abordagens.filter(data_retorno__isnull=False, retorno_concluido=False).count(),
    })


@login_required
def anexo_orcamento(request, pk):
    """Abre o orçamento anexado. Só quem registrou o contato ou um ADMIN (arquivo não fica público)."""
    import mimetypes
    from django.http import FileResponse, Http404
    from carteira.models import Abordagem
    from prospeccao.papeis import eh_adm
    a = get_object_or_404(Abordagem, pk=pk)
    if not a.anexo_orcamento:
        raise Http404
    if a.usuario_id != request.user.pk and not eh_adm(request.user):
        messages.warning(request, "Só quem registrou o orçamento ou um ADMIN pode abrir o arquivo.")
        return redirect("prospeccao:inside_sales")
    try:
        arq = a.anexo_orcamento.open("rb")
    except FileNotFoundError:
        raise Http404("Arquivo do orçamento não encontrado no servidor.")
    nome = a.anexo_orcamento.name.rsplit("/", 1)[-1]
    tipo = mimetypes.guess_type(nome)[0] or "application/octet-stream"
    em_linha = tipo == "application/pdf" or tipo.startswith("image/")
    resp = FileResponse(arq, as_attachment=not em_linha, filename=nome, content_type=tipo)
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@login_required
@require_POST
def adicionar_contato(request, pk):
    empresa = get_object_or_404(Empresa, pk=pk)
    form = ContatoForm(request.POST)
    if form.is_valid():
        contato = form.save(commit=False)
        contato.empresa = empresa
        contato.criado_por = request.user
        contato.save()
        messages.success(request, f"Pessoa '{contato.nome}' adicionada.")
    else:
        messages.error(request, f"Contato não salvo: {form.errors.as_text()}")
    return redirect(_proximo_seguro(request, reverse("carteira:empresa_detalhe", args=[pk])))


@login_required
@require_POST
def nao_contatar(request, pk):
    empresa = get_object_or_404(Empresa, pk=pk)
    if request.POST.get("acao") == "liberar":
        from prospeccao.papeis import eh_adm
        if not eh_adm(request.user):
            messages.warning(request, "Só um ADMIN libera para contato uma empresa marcada como 'não contatar'.")
            return redirect(_proximo_seguro(request, reverse("carteira:empresa_detalhe", args=[pk])))
        ab.liberar_contato(empresa)
        messages.success(request, "Empresa liberada para contato.")
    else:
        form = NaoContatarForm(request.POST)
        if form.is_valid():
            ab.marcar_nao_contatar(empresa, request.user, form.cleaned_data["motivo"])
            messages.success(request, "Empresa marcada como 'não contatar'.")
        else:
            messages.error(request, "Informe o motivo.")
    return redirect("carteira:empresa_detalhe", pk=pk)


@login_required
def retornos(request):
    from prospeccao.papeis import eh_adm
    hoje = timezone.localdate()
    base = ab.retornos_pendentes().select_related("empresa__oportunidade").prefetch_related("empresa__canais")
    if not eh_adm(request.user):
        base = base.filter(usuario=request.user)  # Inside Sales acompanha só os retornos que agendou
    qs = base
    if request.GET.get("meus"):
        qs = qs.filter(usuario=request.user)
    if request.GET.get("atrasados"):
        qs = qs.filter(data_retorno__lt=hoje)
    if request.GET.get("hoje"):
        qs = qs.filter(data_retorno=hoje)
    if request.GET.get("proximos"):
        qs = qs.filter(data_retorno__gt=hoje)
    escopo = base.filter(usuario=request.user) if request.GET.get("meus") else base
    abas = [("", "Todos", escopo.count()), ("atrasados", "Atrasados", escopo.filter(data_retorno__lt=hoje).count()),
            ("hoje", "Hoje", escopo.filter(data_retorno=hoje).count()),
            ("proximos", "Próximos", escopo.filter(data_retorno__gt=hoje).count())]
    aba = next((k for k in ("atrasados", "hoje", "proximos") if request.GET.get(k)), "")
    lista = list(qs)
    from prospeccao.apresentacao import contato_lista
    for a in lista:
        tel_pessoa = (a.contato.telefone or a.contato.celular) if a.contato else ""
        a.tel_mostrar = tel_pessoa or contato_lista(a.empresa)["telefone"]
    return render(request, "carteira/retornos.html", {
        "retornos": lista, "hoje": hoje, "abas": abas, "aba": aba, "meus": bool(request.GET.get("meus")),
        "proximo": request.get_full_path()})


@login_required
@require_POST
def concluir_retorno(request, pk):
    from carteira.models import Abordagem
    from prospeccao.papeis import eh_adm
    abordagem = get_object_or_404(Abordagem, pk=pk)
    if abordagem.usuario_id != request.user.pk and not eh_adm(request.user):
        messages.warning(request, "Só quem agendou o retorno (ou um ADM) pode concluí-lo.")
        return redirect(_proximo_seguro(request, reverse("prospeccao:inside_sales")))
    ab.concluir_retorno(abordagem)
    messages.success(request, f"Retorno de {abordagem.empresa.nome} concluído (sem novo contato registrado).")
    return redirect(_proximo_seguro(request, reverse("carteira:retornos")))


@login_required
def importacoes(request):
    return render(request, "carteira/importacoes.html", {
        "importacoes": Importacao.objects.all()[:100],
        "revisao": Empresa.objects.filter(requer_revisao=True).count(),
    })


@login_required
def importacao_detalhe(request, pk):
    imp = get_object_or_404(Importacao, pk=pk)
    ocorrencias = imp.ocorrencias.select_related("empresa")
    tipo = request.GET.get("tipo")
    if tipo:
        ocorrencias = ocorrencias.filter(tipo=tipo)
    pagina = Paginator(ocorrencias, 100).get_page(request.GET.get("page"))
    contagem = {r["tipo"]: r["n"] for r in imp.ocorrencias.order_by().values("tipo").annotate(n=Count("id"))}
    return render(request, "carteira/importacao_detalhe.html", {
        "imp": imp, "pagina": pagina, "tipo": tipo,
        "tipos": [(k, v, contagem.get(k, 0)) for k, v in OcorrenciaImportacao.Tipo.choices],
    })


@login_required
def revisao(request):
    qs = Empresa.objects.filter(requer_revisao=True).order_by("nome")
    pagina = Paginator(qs, 100).get_page(request.GET.get("page"))
    return render(request, "carteira/revisao.html", {"pagina": pagina, "total": qs.count()})


@login_required
def listas(request):
    return render(request, "carteira/listas.html", {
        "listas": ListaAbordagem.objects.select_related("criada_por").annotate(n=Count("itens")),
    })


def _empresas_da_lista(lista):
    return ab.anotar(Empresa.objects.filter(itens_lista__lista=lista).select_related("indicador")).order_by("nome")


@login_required
def lista_detalhe(request, pk):
    lista = get_object_or_404(ListaAbordagem, pk=pk)
    return render(request, "carteira/lista_detalhe.html", {
        "lista": lista, "empresas": _empresas_da_lista(lista),
        "rot_compra": _rotulos(SituacaoCompra.choices), "rot_contato": _rotulos(SituacaoContato.choices),
    })


@login_required
def lista_csv(request, pk):
    lista = get_object_or_404(ListaAbordagem, pk=pk)
    rc, rt = _rotulos(SituacaoCompra.choices), _rotulos(SituacaoContato.choices)
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="lista_{lista.pk}.csv"'
    resp.write("﻿")
    w = csv.writer(resp, delimiter=";")
    w.writerow(["Empresa", "CNPJ", "Código", "Segmento", "Ramo", "Cidade", "UF", "Telefone", "E-mail",
                "Vendedor", "Situação de compra", "Última compra", "Situação de contato (atual)"])
    for e in _empresas_da_lista(lista):
        ind = getattr(e, "indicador", None)
        w.writerow([e.nome, e.cnpj_formatado, e.codigo_cliente, e.segmento, e.ramo, e.cidade, e.uf, e.telefone,
                    e.email, e.vendedor, rc.get(e.situacao_compra), ind.ultima_compra.strftime("%d/%m/%Y")
                    if ind and ind.ultima_compra else "", rt.get(e.situacao_contato)])
    return resp
