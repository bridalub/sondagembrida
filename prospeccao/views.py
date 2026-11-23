import csv
from dataclasses import asdict
from datetime import timedelta

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, Exists, IntegerField, OuterRef, Q, Value, When
from django.http import HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views.decorators.http import require_POST

from carteira.models import Abordagem, CanalEmpresa, Empresa
from carteira.services import cnpj as cnpj_srv
from prospeccao import apresentacao as apr
from prospeccao import conectores
from prospeccao.conectores.base import ContextoExecucao
from prospeccao.conectores.csv_arquivo import ler_csv
from prospeccao.forms import (
    SITUACAO_LABELS, CSVForm, FiltroCandidatoForm, FiltroOportunidadeForm, FonteForm, InformarCNPJForm, MotivoForm,
    PesquisaForm, PrioridadeForm,
)
from prospeccao.models import (
    Candidato, DecisaoRevisao, Evidencia, Execucao, Fonte, LogExecucao, Oportunidade, Pesquisa, SelecaoPesquisa, UsoFonte,
)
from prospeccao.services import dimensoes as dm
from prospeccao.services import cruzamento as cz
from prospeccao.services import fila
from prospeccao.services import meta as meta_srv
from prospeccao.services import identificacao as idf
from prospeccao.services import oportunidades as ops

ABERTAS = [Oportunidade.Status.NOVA, Oportunidade.Status.LIBERADA]
FILAS_DISPONIVEIS = ("SELECIONADA", "CONTATO_CADASTRAL", "BUSCAR_CONTATO")  # = selecao.FILAS_DISPONIVEIS
PENDENCIAS_IDENTIFICACAO = [Candidato.Status.PENDENTE, Candidato.Status.CONFLITO, Candidato.Status.CNPJ_ENCONTRADO]
SESSAO_FILTRO_OPS = "filtro_oportunidades"


def _anotar_abordagem(qs):
    ab = Abordagem.objects.filter(empresa=OuterRef("empresa_id"))
    return qs.annotate(
        ab_abordada=Exists(ab),
        ab_retorno=Exists(ab.filter(data_retorno__isnull=False, retorno_concluido=False)),
        ab_conversa=Exists(ab.filter(houve_conversa=True)),
        ab_tentativa=Exists(ab.filter(houve_conversa=False)),
    )


def fila_primeira_abordagem():
    return _anotar_abordagem(Oportunidade.objects.filter(status=Oportunidade.Status.LIBERADA,
                                                         empresa__nao_contatar=False)).filter(ab_conversa=False)


def _qd(**params):
    q = QueryDict(mutable=True)
    q.update(params)
    return q


def _conta_ops(usuario, **params):
    return filtrar_oportunidades(_qd(**params), usuario)[1].count()


def indicadores_pesquisa(pesquisa, usuario):
    """Indicadores comerciais da pesquisa. "Disponíveis" usa o MESMO filtro da lista e da exportação
    (?pesquisa=X&etapa=disponiveis); inéditas + reencontradas = disponíveis, sem duplicação."""
    from prospeccao.services.selecao import reencontrada
    _, disp = filtrar_oportunidades(_qd(pesquisa=str(pesquisa.pk), etapa="disponiveis"), usuario)
    ids = list(disp.values_list("empresa_id", flat=True))
    reenc = sum(1 for e in Empresa.objects.filter(pk__in=ids) if reencontrada(e, pesquisa))
    sels = SelecaoPesquisa.objects.filter(pesquisa=pesquisa)

    def unicas(*filas):
        q = sels.filter(fila__in=filas)
        return q.filter(empresa__isnull=False).values("empresa").distinct().count() +             q.filter(empresa__isnull=True).values("cnpj").distinct().count()
    return {
        "cruzamento_pendente": not Empresa.objects.filter(salesforce_id__isnull=False).exists(),
        "disponiveis": len(ids), "ineditas": len(ids) - reenc, "reencontradas": reenc,
        "tentativas": disp.filter(ab_tentativa=True).count(),
        "acompanhamento": unicas(SelecaoPesquisa.Fila.JA_ABORDADA, SelecaoPesquisa.Fila.RETORNO),
        "conhecidas": unicas(SelecaoPesquisa.Fila.CONHECIDA),
        "nao_contatar": unicas(SelecaoPesquisa.Fila.NAO_CONTATAR),
        "correspondencias": unicas(SelecaoPesquisa.Fila.REVISAR_IDENTIDADE),
        "localizacao_pendente": unicas(SelecaoPesquisa.Fila.LOCALIZACAO_PENDENTE),
        "fora": unicas(SelecaoPesquisa.Fila.FORA),
        "url": f"{reverse('prospeccao:oportunidades')}?pesquisa={pesquisa.pk}&etapa=disponiveis",
        "url_export": f"{reverse('prospeccao:oportunidades_exportar')}?pesquisa={pesquisa.pk}&etapa=disponiveis",
        "validado": _conta_ops(usuario, pesquisa=str(pesquisa.pk), etapa="validado"),
        "completar_cnpj": _conta_ops(usuario, pesquisa=str(pesquisa.pk), etapa="completar_cnpj"),
        "conferir_cadastro": _conta_ops(usuario, pesquisa=str(pesquisa.pk), etapa="conferir_cadastro"),
        "conhecidas_lista": _conta_ops(usuario, pesquisa=str(pesquisa.pk), etapa="conhecidas"),
        "url_validado": f"{reverse('prospeccao:oportunidades')}?pesquisa={pesquisa.pk}&etapa=validado",
        "url_completar": f"{reverse('prospeccao:oportunidades')}?pesquisa={pesquisa.pk}&etapa=completar_cnpj",
        "url_conferir_cadastro": f"{reverse('prospeccao:oportunidades')}?pesquisa={pesquisa.pk}&etapa=conferir_cadastro",
        "url_conhecidas": f"{reverse('prospeccao:oportunidades')}?pesquisa={pesquisa.pk}&etapa=conhecidas",
        "url_validado_export": f"{reverse('prospeccao:oportunidades_exportar')}?pesquisa={pesquisa.pk}&etapa=validado",
    }


@login_required
def visao_geral(request):
    """Página inicial orientada à ação. Cada número usa o MESMO filtro da lista que o link abre."""
    hoje = timezone.localdate()
    from carteira.services.abordagens import retornos_pendentes
    ret = retornos_pendentes()
    atual, quando, horas = cz.cruzamento_atual()
    u = request.user
    _, qs_primeira = filtrar_oportunidades(_qd(etapa="primeira"), u)
    _, qs_sem_abordagem = filtrar_oportunidades(_qd(etapa="sem_abordagem"), u)
    prontas = list(qs_primeira.select_related("empresa").order_by("prioridade", "-criada_em")[:8])
    apr.anexar_contatos(prontas)
    pesquisas_recentes = [_progresso_pesquisa(p) for p in Pesquisa.objects.order_by("-atualizada_em")[:5]]
    a_conferir = []
    if not prontas:  # nada liberado ainda: o trabalho do momento é conferir as novas
        _, qs_conferir = filtrar_oportunidades(_qd(etapa="conferir"), u)
        a_conferir = list(qs_conferir.select_related("empresa").order_by("prioridade", "-criada_em")[:8])
        apr.anexar_contatos(a_conferir)
    pend_ident = Candidato.objects.filter(status__in=PENDENCIAS_IDENTIFICACAO).count()
    contexto = {
        "cruzamento_atual": atual, "cruzamento_quando": quando, "cruzamento_horas": horas,
        "n_primeira": qs_primeira.count(),
        "n_sem_abordagem": qs_sem_abordagem.count(),
        "pesquisa_ativa": (Execucao.objects.filter(status__in=Execucao.ATIVOS)
                           .select_related("pesquisa").order_by("-criada_em").first()),
        "n_conferir": _conta_ops(u, etapa="conferir", tipo=Oportunidade.Tipo.NOVA_EXTERNA),
        "n_sem_contato": _conta_ops(u, contato="sem"),
        "n_pend_ident": pend_ident,
        "n_retornos": ret.count(), "n_atrasados": ret.filter(data_retorno__lt=hoje).count(),
        "n_hoje": ret.filter(data_retorno=hoje).count(),
        "n_meus_retornos": ret.filter(usuario=u).count(),
        "n_abertas": _conta_ops(u),
        "n_andamento": _conta_ops(u, etapa="andamento"),
        "n_minhas": _conta_ops(u, responsavel="meus"),
        "prontas": prontas, "a_conferir": a_conferir, "pesquisas_recentes": pesquisas_recentes,
        "tem_pesquisa": bool(pesquisas_recentes),
        "proximos_retornos": list(ret.filter(data_retorno__lte=hoje + timedelta(days=7))
                                  .select_related("empresa__oportunidade")[:5]),
        "hoje": hoje,
    }
    return render(request, "prospeccao/visao_geral.html", contexto)


# ------------------------------------------------------------------ pesquisas

@login_required
def pesquisas(request):
    qs = Pesquisa.objects.annotate(n_cand=Count("candidatos", distinct=True), n_op=Count("oportunidades", distinct=True))
    itens = [_progresso_pesquisa(p) for p in qs.prefetch_related("fontes", "frentes")]
    return render(request, "prospeccao/pesquisas.html", {"itens": itens})


def _progresso_pesquisa(p):
    """Situação de uma pesquisa para cartões e listas: última busca, próximo passo e progresso da meta
    (leads completos do painel mais recente gravado)."""
    recentes = list(p.execucoes.order_by("-criada_em")[:5])
    ult = recentes[0] if recentes else None
    texto, url = apr.link_proximo_passo(p, ult)
    painel = next(((e.progresso or {}).get("painel") for e in recentes
                   if (e.progresso or {}).get("painel", {}).get("novos_encontrados") is not None), None) or {}
    meta = int(p.quantidade_desejada or 0)
    completos = int(painel.get("leads_completos") or 0)
    return {"p": p, "ult": ult, "proximo": texto, "proximo_url": url, "painel": painel, "meta": meta,
            "completos": completos, "pct": min(100, round(completos * 100 / meta)) if meta else 0,
            "n_cidades": len([c for c in (p.cidades or "").splitlines() if c.strip()])}


def _orcamento_pesquisa(p=None):
    """Somente leitura: tetos e gasto registrado (nenhuma chamada é feita aqui)."""
    from prospeccao.services import orcamento as orc
    return {
        "teto": p.teto_custo_usd if p else None,
        "gasto": orc.gasto_registrado(p) if p else None,
        "reservado": orc.reservado_em_andamento(p) if p else None,
        "teto_global": orc.teto_global(),
        "gasto_global": orc.gasto_registrado(),
    }


@staff_member_required
def pesquisa_form(request, pk=None):
    obj = get_object_or_404(Pesquisa, pk=pk) if pk else None
    from prospeccao.services.prontidao import teto_padrao
    # padrão: Busca planejada (Google + IA + Receita + carteira local). OpenStreetMap só se escolhido no avançado.
    inicial = None if obj else {"modo": "PLANEJADA", "fontes": [], **(
        {"teto_custo_usd": teto_padrao()} if teto_padrao() is not None else {})}
    form = PesquisaForm(request.POST or None, instance=obj, initial=inicial)
    if request.method == "POST" and form.is_valid():
        p = form.save(commit=False)
        if not obj:
            p.criada_por = request.user
        p.save()
        form.save_m2m()
        messages.success(request, "Pesquisa salva. Nenhuma busca foi feita: revise os municípios e clique em Executar quando quiser.")
        if getattr(form, "expansao", None):
            texto = "; ".join(f"{origem} → {', '.join(munis)}" for origem, munis in form.expansao)
            messages.info(request, "Municípios ajustados para o nome oficial. Confira antes de executar: " + texto)
        return redirect("prospeccao:pesquisa_detalhe", pk=p.pk)
    fontes_info = {f.pk: f for f in Fonte.objects.filter(tipo=Fonte.Tipo.DESCOBERTA)}
    return render(request, "prospeccao/pesquisa_form.html", {
        "form": form, "obj": obj, "orc": _orcamento_pesquisa(obj), "fontes_info": fontes_info})


@login_required
def pesquisa_detalhe(request, pk):
    p = get_object_or_404(Pesquisa.objects.prefetch_related("fontes", "frentes", "aplicacoes"), pk=pk)
    ult = p.execucoes.order_by("-criada_em").first()
    texto, url = apr.link_proximo_passo(p, ult)
    rot = dict(Candidato.Status.choices)
    from prospeccao.services import prontidao
    conf = prontidao.conferir(p)
    execucoes = list(p.execucoes.all()[:20])
    # andamento da busca mais recente que já tem painel (funil: candidatas -> leads completos)
    painel = next(((e.progresso or {}).get("painel") for e in execucoes
                   if (e.progresso or {}).get("painel", {}).get("novos_encontrados") is not None), None)
    meta = int(p.quantidade_desejada or 0)
    completos = int((painel or {}).get("leads_completos") or 0)
    return render(request, "prospeccao/pesquisa_detalhe.html", {
        "painel": painel, "meta": meta, "completos": completos,
        "pct_meta": min(100, round(completos * 100 / meta)) if meta else 0,
        "cidades_lista": [c for c in (p.cidades or "").splitlines() if c.strip()],
        "p": p, "execucoes": execucoes, "csv_form": CSVForm(), "ult": ult,
        "proximo": texto, "proximo_url": url, "orc": _orcamento_pesquisa(p), "conf": conf,
        "cand_status": [(r["status"], rot.get(r["status"], r["status"]), r["n"])
                        for r in p.candidatos.values("status").annotate(n=Count("id")).order_by("status")],
        "n_cand": p.candidatos.count(),
        "n_ops": _conta_ops(request.user, pesquisa=str(p.pk), etapa="todas", escopo="todos"),
        "n_ops_conferir": _conta_ops(request.user, pesquisa=str(p.pk), etapa="conferir"),
        "n_ops_primeira": _conta_ops(request.user, pesquisa=str(p.pk), etapa="primeira"),
        "n_pend": p.candidatos.filter(status__in=PENDENCIAS_IDENTIFICACAO).count(),
        "ind": indicadores_pesquisa(p, request.user),
    })


@login_required
@require_POST
def executar_pesquisa(request, pk):
    p = get_object_or_404(Pesquisa, pk=pk)
    if not p.ativa:
        messages.error(request, "Pesquisa pausada. Ative-a para executar.")
        return redirect("prospeccao:pesquisa_detalhe", pk=pk)
    from prospeccao.services import prontidao
    conf = prontidao.conferir(p)
    imp, avisos = conf["impedimentos"], conf["avisos"]
    if imp:  # informa NA HORA o que falta; registra a tentativa bloqueada (histórico), sem rodar nada
        agora = timezone.now()
        ex = Execucao.objects.create(pesquisa=p, gatilho=Execucao.Gatilho.MANUAL, solicitada_por=request.user,
                                     status=Execucao.Status.BLOQUEADA, iniciada_em=agora, finalizada_em=agora,
                                     mensagem=prontidao.texto_impedimentos(imp)[:2000],
                                     progresso={"motivo_rotulo": "Não iniciada"},
                                     limites_atingidos=[f"bloqueio: {i['falta']}" for i in imp])
        for i in imp:
            LogExecucao.objects.create(execucao=ex, nivel="ERRO", fonte="preparacao",
                                       mensagem=f"Falta: {i['falta']}. Impede: {i['etapa']}. Como resolver: {i['como']}.")
        for a in avisos:
            LogExecucao.objects.create(execucao=ex, nivel="AVISO", fonte="preparacao", mensagem=a)
        messages.error(request, "Execução não iniciada – " + prontidao.texto_impedimentos(imp))
        return redirect("prospeccao:execucao_detalhe", pk=ex.pk)
    for a in avisos:
        messages.warning(request, a)
    for i in conf["info"]:
        messages.info(request, i)
    ex, nova = fila.enfileirar(p, Execucao.Gatilho.MANUAL, request.user)
    if nova:
        disparado = fila.disparar_worker()
        if disparado:
            messages.success(request, "A busca foi iniciada. Acompanhe o andamento nesta tela.")
        else:
            messages.success(request, "A busca foi registrada e será processada em seguida. "
                             "Se não avançar, peça a um gestor para conferir o histórico de execução.")
    else:
        messages.warning(request, "Já existe uma busca em andamento desta pesquisa.")
    return redirect("prospeccao:execucao_detalhe", pk=ex.pk)


@staff_member_required
@require_POST
def alternar_pesquisa(request, pk):
    p = get_object_or_404(Pesquisa, pk=pk)
    p.ativa = not p.ativa
    p.save(update_fields=["ativa", "atualizada_em"])
    messages.success(request, f"Pesquisa {'ativada' if p.ativa else 'pausada'}.")
    return redirect("prospeccao:pesquisa_detalhe", pk=pk)


@login_required
@require_POST
def importar_csv(request, pk):
    p = get_object_or_404(Pesquisa, pk=pk)
    form = CSVForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Envie um arquivo CSV.")
        return redirect("prospeccao:pesquisa_detalhe", pk=pk)
    arq = form.cleaned_data["arquivo"]
    if arq.size > 5_000_000:
        messages.error(request, "Arquivo maior que 5 MB.")
        return redirect("prospeccao:pesquisa_detalhe", pk=pk)
    try:
        resultados, erros = ler_csv(arq.read(), arq.name)
    except ValueError as e:
        messages.error(request, f"CSV inválido: {e}")
        return redirect("prospeccao:pesquisa_detalhe", pk=pk)
    ex, nova = fila.enfileirar(p, Execucao.Gatilho.CSV, request.user, {
        "arquivo": arq.name, "resultados": [asdict(r) for r in resultados], "erros_leitura": erros[:200]})
    if not nova:
        messages.warning(request, "Já existe uma execução ativa desta pesquisa; aguarde terminar.")
        return redirect("prospeccao:execucao_detalhe", pk=ex.pk)
    fila.disparar_worker()
    messages.success(request, f"Importação CSV na fila: {len(resultados)} linha(s), {len(erros)} aviso(s) de leitura. "
                              "(Importação de arquivo – não é descoberta automática.)")
    return redirect("prospeccao:execucao_detalhe", pk=ex.pk)


# ------------------------------------------------------------------ execuções

@login_required
def execucoes(request):
    qs = Execucao.objects.select_related("pesquisa", "solicitada_por")
    pagina = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "prospeccao/execucoes.html", {"pagina": pagina})


@login_required
def execucao_detalhe(request, pk):
    ex = get_object_or_404(Execucao.objects.select_related("pesquisa"), pk=pk)
    cands = (Candidato.objects.filter(Q(ultima_execucao=ex) | Q(primeira_execucao=ex)).select_related("empresa", "fonte")
             .order_by("status", "nome"))
    fim = ex.finalizada_em or timezone.now()
    decorrido = int((fim - ex.iniciada_em).total_seconds()) if ex.iniciada_em else None
    quadro = meta_srv.classificar(ex.pesquisa)
    painel = {k: v for k, v in quadro.items() if k not in meta_srv._LISTAS}
    painel["fora_dos_novos"] = painel["conhecidas"] + painel["correspondencias"] + painel["inativas"]
    # execuções gravadas antes de 18/11/2026 podiam dizer "meta atingida" sem os leads completos chegarem à meta
    motivo_fim = (ex.progresso or {}).get("motivo_rotulo") or ""
    mensagem_fim = ex.mensagem or ""
    continuacao_fim = (ex.progresso or {}).get("continuacao") or ""
    if (ex.progresso or {}).get("motivo") == "meta" and painel.get("leads_completos", 0) < (painel.get("meta") or 0):
        mensagem_fim = mensagem_fim.replace(motivo_fim, "").strip()
        motivo_fim = meta_srv.MOTIVOS["tempo"] if "tempo máximo" in mensagem_fim.lower() else "Parada por limite interno"
        if "meta atingida" in continuacao_fim.lower():
            continuacao_fim = ("não disparou nesta execução (registro anterior à correção de 18/11/2026); "
                               "clique em Buscar novos leads para continuar")
    return render(request, "prospeccao/execucao_detalhe.html", {
        "continuacao_fim": continuacao_fim,
        "ex": ex, "logs": ex.logs.all()[:300], "candidatos": cands[:300], "total_cand": cands.count(),
        "atualizar": ex.ativa, "painel": painel,
        "motivo": motivo_fim, "mensagem_ex": mensagem_fim,
        "decorrido": decorrido, "orc": _orcamento_pesquisa(ex.pesquisa),
        "conhecidas_sf": quadro["lista_conhecidas"],
        "ind": indicadores_pesquisa(ex.pesquisa, request.user),
        "correspondencias": quadro["lista_correspondencias"],
        "inativas": quadro["lista_inativas"],
        "novas_prontas": quadro["novos"],
    })


@login_required
@require_POST
def pausar_execucao(request, pk):
    agora = timezone.now()
    parou = Execucao.objects.filter(pk=pk, status=Execucao.Status.PENDENTE).update(
        status=Execucao.Status.PARCIAL, pausa_solicitada=True, finalizada_em=agora,
        mensagem="Pausada antes de iniciar. Nada foi consultado.")
    if not parou:
        Execucao.objects.filter(pk=pk, status=Execucao.Status.EXECUTANDO).update(pausa_solicitada=True)
    messages.success(request, "Pausa solicitada. Nenhuma chamada nova será aberta; o que já estava em andamento continua contabilizado.")
    return redirect("prospeccao:execucao_detalhe", pk=pk)


@login_required
@require_POST
def cancelar_execucao(request, pk):
    Execucao.objects.filter(pk=pk, status__in=Execucao.ATIVOS).update(cancelamento_solicitado=True)
    Execucao.objects.filter(pk=pk, status=Execucao.Status.PENDENTE).update(
        status=Execucao.Status.CANCELADA, finalizada_em=timezone.now(), mensagem="Cancelada antes de iniciar")
    messages.success(request, "Cancelamento solicitado.")
    return redirect("prospeccao:execucao_detalhe", pk=pk)


# ------------------------------------------------------------------ oportunidades

def filtrar_oportunidades(params, usuario):
    """Filtro único das oportunidades: usado pela lista, pela exportação e pelos indicadores da visão geral."""
    form = FiltroOportunidadeForm(params or None, usuario=usuario)
    qs = _anotar_abordagem(Oportunidade.objects.select_related("empresa", "responsavel"))
    d = form.cleaned_data if form.is_valid() else {}
    et, st = d.get("etapa") or "", d.get("status")
    if et == "conhecidas":
        qs = dm.filtrar_dimensao(qs, "conhecidas")
    elif et == "validado":
        qs = dm.filtrar_dimensao(qs.filter(status__in=ABERTAS), "validado")
    elif et == "completar_cnpj":
        qs = dm.filtrar_dimensao(qs.filter(status__in=ABERTAS), "completar_cnpj")
    elif et == "conferir_cadastro":
        qs = dm.filtrar_dimensao(qs, "conferir_cadastro")
    elif st and st != "todas":
        qs = qs.filter(status=st)
    elif et in ("todas", "encerradas") or st == "todas":
        if et == "encerradas":
            qs = qs.exclude(status__in=ABERTAS)
    elif et == "conferir":
        qs = qs.filter(status=Oportunidade.Status.NOVA, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
    elif et == "primeira":
        qs = qs.filter(status=Oportunidade.Status.LIBERADA, ab_conversa=False, empresa__nao_contatar=False)
    elif et in ("sem_abordagem", "disponiveis"):
        qs = qs.filter(status__in=ABERTAS, ab_conversa=False, empresa__nao_contatar=False).filter(
            Q(status=Oportunidade.Status.LIBERADA) | Q(tipo=Oportunidade.Tipo.NOVA_EXTERNA))
        if et == "disponiveis":  # disponíveis na lista da pesquisa (fila calculada), inclusive reencontradas
            sel = SelecaoPesquisa.objects.filter(empresa=OuterRef("empresa_id"), fila__in=FILAS_DISPONIVEIS)
            if d.get("pesquisa"):
                sel = sel.filter(pesquisa=d["pesquisa"])
            qs = qs.filter(Exists(sel))
    else:
        qs = qs.filter(status__in=ABERTAS)
    if not d.get("tipo") and et != "conhecidas":
        qs = qs.filter(tipo=Oportunidade.Tipo.NOVA_EXTERNA)
    if et == "andamento":  # contato efetivo registrado: acompanhamento
        qs = qs.filter(ab_conversa=True, ab_retorno=False)
    elif et == "retorno":
        qs = qs.filter(ab_retorno=True)
    ct = d.get("contato")
    if ct == "direto":
        qs = dm.filtrar_dimensao(qs, "", "direto")
    elif ct == "sem_direto":
        qs = dm.filtrar_dimensao(qs, "", "sem_direto")
    elif ct == "telefone":
        qs = qs.exclude(apr.sem_canal_q(CanalEmpresa.Tipo.TELEFONE))
    elif ct == "email":
        qs = qs.exclude(apr.sem_canal_q(CanalEmpresa.Tipo.EMAIL))
    elif ct == "sem":
        qs = qs.filter(apr.sem_canal_q(CanalEmpresa.Tipo.TELEFONE) & apr.sem_canal_q(CanalEmpresa.Tipo.EMAIL))
    if d.get("q"):
        termo = d["q"].strip()
        f = Q(empresa__nome__icontains=termo) | Q(empresa__razao_social__icontains=termo) | \
            Q(empresa__nome_fantasia__icontains=termo)
        if cnpj_srv.normalizar(termo):
            f |= Q(empresa__cnpj__startswith=cnpj_srv.normalizar(termo))
        qs = qs.filter(f)
    for campo in ("tipo", "prioridade"):
        if d.get(campo):
            qs = qs.filter(**{campo: d[campo]})
    if d.get("evidencia"):
        qs = qs.filter(Exists(Evidencia.objects.filter(empresa=OuterRef("empresa_id"), ativa=True,
                                                       classificacao=d["evidencia"])))
    ab = d.get("abordagem")
    if ab == "primeira":
        qs = qs.filter(status=Oportunidade.Status.LIBERADA, ab_conversa=False, empresa__nao_contatar=False)
    elif ab == "abordada":
        qs = qs.filter(ab_conversa=True)
    elif ab == "tentativa":
        qs = qs.filter(ab_tentativa=True, ab_conversa=False)
    elif ab == "retorno":
        qs = qs.filter(ab_retorno=True)
    r = d.get("responsavel")
    if r == "meus":
        qs = qs.filter(responsavel=usuario)
    elif r == "nenhum":
        qs = qs.filter(responsavel__isnull=True)
    elif r:
        qs = qs.filter(responsavel_id=r)
    if d.get("cidade"):
        qs = qs.filter(empresa__cidade__icontains=d["cidade"])
    if d.get("segmento"):
        s = d["segmento"]
        qs = qs.filter(Q(empresa__segmento__icontains=s) | Q(empresa__atividade_principal__icontains=s))
    if d.get("pesquisa"):
        qs = qs.filter(pesquisas=d["pesquisa"])
    if d.get("alertas"):
        qs = qs.exclude(alertas=[])
    esc_f = d.get("escopo") or ""
    if esc_f == "" and et != "conhecidas":
        qs = qs.exclude(escopo="FORA")
    elif esc_f == "dentro":
        qs = qs.filter(escopo__in=["FROTA", "INDUSTRIA", "AGRO_MAQUINAS", "REVENDA_OFICINA"])
    elif esc_f != "todos":
        qs = qs.filter(escopo=esc_f)
    ordem_prior = Case(*[When(prioridade=p, then=Value(i)) for i, p in enumerate(
        ["ALTA", "MEDIA", "INVESTIGAR", "VERIFICAR_CADASTRO"])], default=Value(9), output_field=IntegerField())
    return form, qs.distinct().annotate(ordem_prior=ordem_prior).order_by("ordem_prior", "-criada_em")


def _filtrar_oportunidades(request):
    return filtrar_oportunidades(request.GET, request.user)


ORDEM_EVIDENCIA = {"CONFIRMADO_FONTE": 0, "HIPOTESE": 1, "INSUFICIENTE": 2}


@login_required
def oportunidades(request):
    form, qs = _filtrar_oportunidades(request)
    pagina = Paginator(qs, 25).get_page(request.GET.get("page"))
    q = request.GET.copy()
    q.pop("page", None)
    # lembra os filtros (com a página) para o "voltar" do detalhe da empresa
    request.session[SESSAO_FILTRO_OPS] = request.GET.urlencode()
    ids = [o.empresa_id for o in pagina]
    evid = {}
    for e in Evidencia.objects.filter(empresa_id__in=ids, ativa=True).prefetch_related("aplicacoes"):
        atual = evid.get(e.empresa_id)
        if atual is None or ORDEM_EVIDENCIA.get(e.classificacao, 9) < ORDEM_EVIDENCIA.get(atual.classificacao, 9):
            evid[e.empresa_id] = e
    lista = list(pagina)
    apr.anexar_contatos(lista)
    apr.anexar_selecao(lista)
    carteira_ok = dm.carteira_integra()
    for o in lista:
        o.evidencia_principal = evid.get(o.empresa_id)
        o.etapa_cod, o.etapa_rot = apr.etapa_oportunidade(o)
        o.dims = dm.resumo(o.empresa, o, anotada=o, carteira_ok=carteira_ok)
    filtros_ativos = []
    if form.is_bound:
        for n in form.PRINCIPAIS + form.MAIS:
            if not form.data.get(n):
                continue
            campo = form[n]
            valor = campo.data
            if hasattr(campo.field, "choices"):
                mapa = dict(campo.field.choices)
                valor = mapa.get(valor, valor) or valor
            if hasattr(valor, "nome"):
                valor = valor.nome
            filtros_ativos.append((campo.label, valor))
    return render(request, "prospeccao/oportunidades.html", {
        "form": form, "pagina": pagina, "lista": lista, "total": qs.count(), "querystring": q.urlencode(),
        "mais_ativos": form.mais_ativos(), "tem_filtro": bool(q), "filtros_ativos": filtros_ativos})


@login_required
def exportar_oportunidades(request):
    """Exporta o mesmo recorte da lista, com as mesmas dimensões."""
    form, qs = _filtrar_oportunidades(request)
    carteira_ok = dm.carteira_integra()
    atual, quando, horas = cz.cruzamento_atual()
    if not atual:
        try:
            conf = cz.conferir_direto([o.empresa.cnpj for o in qs if o.empresa.cnpj])
        except cz.SalesforceIndisponivel as e:
            if not quando:  # nenhuma carteira local: não há como cruzar
                messages.error(request, f"Sem carteira local e {e}. Sincronize os clientes antes de exportar.")
                return redirect("prospeccao:oportunidades")
            # há carteira local íntegra: a falha do Salesforce não bloqueia; o cruzamento usa a última sincronização
            conf = {}
            messages.warning(request, f"Salesforce indisponível ({e}); exportado com o cruzamento da carteira local "
                                      f"sincronizada em {timezone.localtime(quando):%d/%m/%Y %H:%M}.")
        conhecidos = {c for c, v in conf.items() if v["contas"]}
        if conhecidos:
            for o in qs.filter(empresa__cnpj__in=conhecidos):
                ops.avaliar(o.empresa, base="CONSULTA_DIRETA")
            messages.warning(request, f"{len(conhecidos)} empresa(s) passaram a constar na carteira e foram retiradas.")
            qs = qs.exclude(empresa__cnpj__in=conhecidos)
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="oportunidades_liberadas.csv"'
    resp.write("﻿")
    w = csv.writer(resp, delimiter=";")
    w.writerow(["Empresa", "CNPJ", "Identificação", "Salesforce", "Contato", "Situação cadastral",
                "Atualização do cadastro", "Competência ou consulta", "Fonte da situação",
                "Atividade/segmento", "Cidade", "UF", "Telefone", "WhatsApp", "Fonte do WhatsApp",
                "Situação na carteira",
                "Prioridade", "Resumo", "Responsável", "Cruzado em"])
    for o in qs.select_related("empresa", "responsavel"):
        e = o.empresa
        dims = dm.resumo(e, o, carteira_ok=carteira_ok)
        tel = e.telefone or (e.canais.filter(tipo="TELEFONE").values_list("valor", flat=True).first() or "")
        wa = e.canais.filter(tipo="TELEFONE", whatsapp_explicito=True).order_by("pk").first()
        w.writerow([e.nome, e.cnpj_formatado, dims["identificacao"]["rotulo"], dims["salesforce"]["rotulo"],
                    dims["contato"]["rotulo"], e.situacao_cadastral,
                    e.situacao_cadastral_data.strftime("%d/%m/%Y") if e.situacao_cadastral_data else "",
                    e.situacao_cadastral_referencia.strftime("%d/%m/%Y") if e.situacao_cadastral_referencia else "",
                    e.situacao_cadastral_fonte, e.atividade_principal or e.segmento, e.cidade, e.uf, tel,
                    wa.valor if wa else "", (wa.referencia or wa.fonte) if wa else "",
                    o.get_tipo_display(), o.get_prioridade_display(), o.resumo, o.responsavel or "",
                    timezone.localtime(o.cruzado_em).strftime("%d/%m/%Y %H:%M") if o.cruzado_em else ""])
    return resp


@login_required
def oportunidade_detalhe(request, pk):
    op = get_object_or_404(Oportunidade.objects.select_related("empresa", "responsavel"), pk=pk)
    return redirect("prospeccao:empresa", pk=op.empresa_id)


@login_required
def empresa(request, pk):
    # Visualizar NÃO registra abordagem.
    e = get_object_or_404(Empresa.objects.select_related("indicador", "nao_contatar_por"), pk=pk)
    op = (_anotar_abordagem(Oportunidade.objects.filter(empresa=e)).select_related("responsavel", "liberada_por")
          .first())
    sit, detalhe, alertas = cz.situacao_empresa(e)
    canais = list(e.canais.all())
    for c in canais:
        c.alerta = apr.alerta_canal(c)
        c.util = c.nivel != "INCORRETO" and not c.restrito
        c.terceiro = apr.terceiro(c) if c.tipo != CanalEmpresa.Tipo.SITE else ""
    ordem = lambda c: (0 if c.util else 1, 0 if c.preferido else 1, apr.ORDEM_CANAL.get(c.nivel, 8), c.pk)  # noqa: E731
    # contatos de terceiros (contador, compartilhados) ficam em seção própria, nunca como contato direto
    terceiros = sorted([c for c in canais if c.terceiro], key=ordem)
    telefones = sorted([c for c in canais if c.tipo == CanalEmpresa.Tipo.TELEFONE and not c.terceiro], key=ordem)
    emails = sorted([c for c in canais if c.tipo == CanalEmpresa.Tipo.EMAIL and not c.terceiro], key=ordem)
    ocultos = list(e.dados_coletados.filter(campo__contains="_nao_disponibilizado"))
    sites = sorted([c for c in canais if c.tipo == CanalEmpresa.Tipo.SITE], key=ordem)
    valores = {c.valor_normalizado or c.valor for c in canais}
    abordagens = list(e.abordagens.select_related("contato", "usuario"))
    evidencias = list(Evidencia.objects.filter(empresa=e).prefetch_related("aplicacoes")
                      .order_by("-ativa", "classificacao", "-coletada_em"))
    from prospeccao.models import SelecaoPesquisa
    selecoes = list(SelecaoPesquisa.objects.filter(empresa=e).select_related("frente", "pesquisa").order_by("-pontuacao"))
    dims = dm.resumo(e, op, abordagens=abordagens)
    aplicacoes = []
    for ev in evidencias:
        if ev.ativa:
            for a in ev.aplicacoes.all():
                if a.nome not in aplicacoes:
                    aplicacoes.append(a.nome)
    filtro = request.session.get(SESSAO_FILTRO_OPS)
    pode_abordar = not e.nao_contatar and (not op or not op.responsavel_id or op.responsavel_id == request.user.pk
                                           or request.user.is_staff)
    return render(request, "prospeccao/empresa.html", {
        "e": e, "op": op, "sit": sit, "sit_label": SITUACAO_LABELS.get(sit, sit), "sit_detalhe": detalhe,
        "sit_alertas": alertas, "ia_status": __import__("prospeccao.services.ia", fromlist=["status"]).status(),
        "evidencias": evidencias, "evid_ativas": [ev for ev in evidencias if ev.ativa], "aplicacoes": aplicacoes,
        "departamentos": __import__("carteira.forms", fromlist=["DEPARTAMENTOS"]).DEPARTAMENTOS,
        "selecoes": selecoes, "unidades": __import__("prospeccao.services.unidades", fromlist=["unidades"]).unidades(e),
        "candidatos": e.candidatos.select_related("fonte"), "canais": canais, "dados": e.dados_coletados.all(),
        "telefones": telefones, "emails": emails, "sites": sites, "terceiros": terceiros, "ocultos": ocultos,
        "so_terceiro": bool(terceiros) and not any(c.util for c in telefones + emails),
        "tel_cadastro_novo": bool(e.telefone) and e.telefone not in valores and "*" not in e.telefone,
        "email_cadastro_novo": bool(e.email) and e.email not in valores and "*" not in e.email,
        "registros": e.registros_origem.all(), "contatos": e.contatos.all(),
        "abordagens": abordagens, "proxima_acao": apr.proxima_acao_empresa(e, op, abordagens),
        "retorno_pendente": next((a for a in abordagens if a.data_retorno and not a.retorno_concluido), None),
        "decisoes": op.decisoes.select_related("usuario")[:30] if op else [],
        "usuarios": get_user_model().objects.filter(is_active=True).order_by("username"),
        "prioridade_form": PrioridadeForm(initial={"prioridade": op.prioridade} if op else None),
        "cruzamento": cz.cruzamento_atual(),
        "voltar_url": (reverse("prospeccao:oportunidades") + (f"?{filtro}" if filtro else "")
                       if __import__("prospeccao.papeis", fromlist=["eh_adm"]).eh_adm(request.user)
                       else reverse("prospeccao:inside_sales")),
        "pode_abordar": pode_abordar,
        "etapa": apr.etapa_oportunidade(op) if op else None,
        "dims": dims,
        "carteira_em": dm.data_carteira(),
    })


def _op_acao(request, pk, funcao, sucesso):
    op = get_object_or_404(Oportunidade, pk=pk)
    try:
        funcao(op)
        messages.success(request, sucesso)
    except (ops.RegraViolada, cz.SalesforceIndisponivel) as e:
        messages.error(request, str(e))
    return redirect("prospeccao:empresa", pk=op.empresa_id)


@login_required
@require_POST
def liberar(request, pk):
    return _op_acao(request, pk, lambda op: ops.liberar(op, request.user), "Oportunidade liberada para abordagem.")


@staff_member_required
@require_POST
def descartar(request, pk):
    return _op_acao(request, pk, lambda op: ops.descartar(op, request.user, request.POST.get("motivo", "")),
                    "Oportunidade descartada (decisão registrada).")


@staff_member_required
@require_POST
def reabrir(request, pk):
    return _op_acao(request, pk, lambda op: ops.reabrir(op, request.user, request.POST.get("motivo", "")),
                    "Oportunidade reaberta.")


@login_required
@require_POST
def atribuir(request, pk):
    destino_id = request.POST.get("usuario")
    destino = request.user if destino_id == "eu" else (
        get_user_model().objects.filter(pk=destino_id).first() if destino_id else None)
    if destino_id and destino_id != "eu" and not request.user.is_staff and destino != request.user:
        messages.error(request, "Só gestores atribuem a outros usuários.")
        return redirect("prospeccao:oportunidades")
    return _op_acao(request, pk, lambda op: ops.atribuir(op, destino, request.user), "Responsável atualizado.")


@login_required
@require_POST
def prioridade(request, pk):
    form = PrioridadeForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Informe prioridade e motivo.")
        return redirect("prospeccao:oportunidade", pk=pk)
    return _op_acao(request, pk, lambda op: ops.definir_prioridade(op, form.cleaned_data["prioridade"], request.user,
                                                                   form.cleaned_data["motivo"]),
                    "Prioridade revisada.")


@login_required
@require_POST
def reavaliar(request, pk):
    e = get_object_or_404(Empresa, pk=pk)
    op, criada, sit = ops.avaliar(e)
    messages.success(request, f"Cruzamento reavaliado: {SITUACAO_LABELS.get(sit, sit)}.")
    return redirect("prospeccao:empresa", pk=pk)


# ------------------------------------------------------------------ revisão de candidatos

@login_required
def revisao(request):
    form = FiltroCandidatoForm(request.GET or None)
    qs = Candidato.objects.select_related("fonte", "empresa")
    d = form.cleaned_data if form.is_valid() else {}
    if d.get("status"):
        qs = qs.filter(status=d["status"])
    else:
        qs = qs.filter(status__in=[Candidato.Status.PENDENTE, Candidato.Status.CONFLITO, Candidato.Status.CNPJ_ENCONTRADO])
    if d.get("pesquisa"):
        qs = qs.filter(pesquisas=d["pesquisa"])
    if d.get("q"):
        qs = qs.filter(Q(nome__icontains=d["q"]) | Q(cidade__icontains=d["q"]))
    if d.get("com_sugestao"):
        qs = qs.exclude(sugestoes=[])
    pagina = Paginator(qs.order_by("status", "nome"), 50).get_page(request.GET.get("page"))
    q = request.GET.copy()
    q.pop("page", None)
    base = Candidato.objects.filter(status__in=PENDENCIAS_IDENTIFICACAO)
    if d.get("pesquisa"):
        base = base.filter(pesquisas=d["pesquisa"])
    por_status = dict(base.values_list("status").annotate(n=Count("id")))
    abas = [("", "Todas", sum(por_status.values()))] + [
        (s, rot, por_status.get(s, 0)) for s, rot in [
            (Candidato.Status.PENDENTE, "Sem CNPJ"), (Candidato.Status.CNPJ_ENCONTRADO, "CNPJ a verificar"),
            (Candidato.Status.CONFLITO, "Conflito de identificação")]]
    params_contato = {"contato": "sem"}
    if d.get("pesquisa"):
        params_contato["pesquisa"] = str(d["pesquisa"].pk)
    return render(request, "prospeccao/revisao.html", {
        "form": form, "pagina": pagina, "total": qs.count(), "querystring": q.urlencode(), "abas": abas,
        "status_atual": d.get("status") or "", "n_sem_contato": _conta_ops(request.user, **params_contato),
        "url_sem_contato": reverse("prospeccao:oportunidades") + "?" + _qd(**params_contato).urlencode(),
    })


@login_required
def candidato(request, pk):
    c = get_object_or_404(Candidato.objects.select_related("fonte", "empresa"), pk=pk)
    return render(request, "prospeccao/candidato.html", {
        "c": c, "evidencias": c.evidencias.prefetch_related("aplicacoes"), "cnpj_form": InformarCNPJForm(
            initial={"cnpj": c.cnpj_candidato}), "motivo_form": MotivoForm(), "decisoes": c.decisoes.select_related("usuario"),
        "pesquisas": c.pesquisas.all()})


@login_required
@require_POST
def candidato_informar_cnpj(request, pk):
    """Humano informa/confirma o CNPJ. É verificado na Receita; divergência fica registrada."""
    c = get_object_or_404(Candidato, pk=pk)
    form = InformarCNPJForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Informe CNPJ e como foi confirmado.")
        return redirect("prospeccao:candidato", pk=pk)
    cnpj = cnpj_srv.analisar(form.cleaned_data["cnpj"]).cnpj
    if not cnpj:
        messages.error(request, "CNPJ inválido (dígito verificador não confere).")
        return redirect("prospeccao:candidato", pk=pk)
    fonte = Fonte.objects.filter(codigo="brasilapi_cnpj", habilitada=True).first()
    dados, falhou = None, False
    if fonte:
        try:
            dados = conectores.ENRIQUECIMENTO["brasilapi_cnpj"](fonte).consultar(cnpj, ContextoExecucao())
        except Exception as e:
            falhou = True
            messages.warning(request, f"Não foi possível consultar a Receita agora ({e}); vínculo feito sem enriquecimento.")
    if fonte and dados is None and not falhou:
        messages.warning(request, "CNPJ não encontrado na Receita via BrasilAPI; vínculo registrado como decisão humana.")
    detalhe = idf.avaliar_correspondencia(c, dados)[2] if dados else "sem dados da Receita"
    c.detalhe_identificacao = f"[MANUAL por {request.user}] {form.cleaned_data['motivo']} | {detalhe}"
    c.confianca = Candidato.Confianca.ALTA
    c.save()
    empresa = idf.vincular(c, cnpj, dados=dados, fonte_enriquecimento="brasilapi_cnpj", usuario=request.user,
                           metodo="MANUAL")
    DecisaoRevisao.objects.create(tipo="CONFIRMAR_CNPJ", usuario=request.user, candidato=c,
                                  motivo=form.cleaned_data["motivo"], dados={"cnpj": cnpj})
    op, criada, sit = ops.avaliar(empresa, c.pesquisas.first())
    messages.success(request, f"Candidato vinculado a {empresa.nome}: {SITUACAO_LABELS.get(sit, sit)}.")
    return redirect("prospeccao:empresa", pk=empresa.pk)


@login_required
@require_POST
def candidato_vincular(request, pk, empresa_id):
    """Confirma uma SUGESTÃO de correspondência (decisão humana; nunca automática)."""
    c = get_object_or_404(Candidato, pk=pk)
    e = get_object_or_404(Empresa, pk=empresa_id)
    motivo = request.POST.get("motivo", "").strip()
    if not motivo:
        messages.error(request, "Informe por que é a mesma empresa.")
        return redirect("prospeccao:candidato", pk=pk)
    if not e.cnpj:
        messages.error(request, "A empresa sugerida não tem CNPJ válido; corrija o cadastro antes do vínculo.")
        return redirect("prospeccao:candidato", pk=pk)
    idf.vincular(c, e.cnpj, usuario=request.user, metodo="MANUAL_SUGESTAO")
    DecisaoRevisao.objects.create(tipo="VINCULAR_CANDIDATO", usuario=request.user, candidato=c, motivo=motivo,
                                  dados={"empresa_id": e.pk, "cnpj": e.cnpj})
    op, _, sit = ops.avaliar(e, c.pesquisas.first())
    messages.success(request, f"Vinculado a {e.nome}: {SITUACAO_LABELS.get(sit, sit)}.")
    return redirect("prospeccao:empresa", pk=e.pk)


@login_required
@require_POST
def candidato_descartar(request, pk):
    c = get_object_or_404(Candidato, pk=pk)
    motivo = request.POST.get("motivo", "").strip()
    if not motivo:
        messages.error(request, "Informe o motivo.")
        return redirect("prospeccao:candidato", pk=pk)
    anterior = c.status
    c.status, c.motivo_exclusao = Candidato.Status.DESCARTADO, motivo[:255]
    c.save(update_fields=["status", "motivo_exclusao"])
    DecisaoRevisao.objects.create(tipo="DESCARTAR_CANDIDATO", usuario=request.user, candidato=c, motivo=motivo,
                                  dados={"status_anterior": anterior})
    messages.success(request, "Candidato descartado (decisão registrada; pode ser reaberto).")
    return redirect("prospeccao:revisao")


@login_required
@require_POST
def candidato_reabrir(request, pk):
    c = get_object_or_404(Candidato, pk=pk)
    c.status = Candidato.Status.CNPJ_ENCONTRADO if c.cnpj_candidato else Candidato.Status.PENDENTE
    c.motivo_exclusao = ""
    c.save(update_fields=["status", "motivo_exclusao"])
    DecisaoRevisao.objects.create(tipo="REABRIR_CANDIDATO", usuario=request.user, candidato=c,
                                  motivo=request.POST.get("motivo", ""))
    messages.success(request, "Candidato reaberto para identificação.")
    return redirect("prospeccao:candidato", pk=pk)


# ------------------------------------------------------------------ base Receita

@login_required
def base_receita(request):
    from prospeccao.services.panorama import panorama
    from receita.models import CargaReceita
    uf = (request.GET.get("uf") or "SP").upper()[:2]
    cidades = [c.strip() for c in (request.GET.get("cidades") or "Guarulhos").split(",") if c.strip()]
    return render(request, "prospeccao/base_receita.html", {
        "cargas": CargaReceita.objects.prefetch_related("arquivos")[:10], "uf": uf, "cidades": ", ".join(cidades),
        "p": panorama(uf, cidades),
    })


# ------------------------------------------------------------------ integrações e parâmetros (somente leitura)

@login_required
def integracoes(request):
    """Situação das integrações e sincronizações. Nenhuma chamada externa é feita nesta tela."""
    from carteira.models import Importacao, NotaFiscal
    from carteira.services.indicadores import cobertura_compras
    from prospeccao.services import ia
    from receita.models import CargaReceita
    atual, quando, horas = cz.cruzamento_atual()
    fontes_lista = list(Fonte.objects.all())
    return render(request, "prospeccao/integracoes.html", {
        "cruzamento_atual": atual, "cruzamento_quando": quando, "cruzamento_horas": horas,
        "ia_status": ia.status(), "carga_receita": CargaReceita.ativa_atual(),
        "importacoes": Importacao.objects.all()[:8], "cobertura": cobertura_compras(),
        "notas_sem_empresa": NotaFiscal.objects.filter(empresa__isnull=True, presente_na_origem=True).count(),
        "fontes_credencial": [f for f in fontes_lista if f.variavel_credencial],
        "fontes_desabilitadas": [f for f in fontes_lista if not f.habilitada],
        "revisao_cadastral": Empresa.objects.filter(requer_revisao=True).count(),
    })


@login_required
def parametros(request):
    """Tetos, gastos registrados e regras de abordagem (leitura). Edição: Admin, só gestores."""
    from carteira.models import ConfiguracaoAnalise
    from prospeccao.services import ia
    from prospeccao.services import orcamento as orc
    linhas_p = []
    for p in Pesquisa.objects.all():
        linhas_p.append({"p": p, "gasto": orc.gasto_registrado(p), "reservado": orc.reservado_em_andamento(p)})
    return render(request, "prospeccao/parametros.html", {
        "cfg": ConfiguracaoAnalise.obter(), "teto_global": orc.teto_global(), "gasto_global": orc.gasto_registrado(),
        "reservado_global": orc.reservado_em_andamento(), "pesquisas_orc": linhas_p, "ia_status": ia.status(),
        "ia_modelo": ia.modelo_configurado(),
    })


# ------------------------------------------------------------------ fontes

@login_required
def fontes(request):
    hoje = timezone.localdate()
    uso = {u.fonte_id: u for u in UsoFonte.objects.filter(dia=hoje)}
    lista = list(Fonte.objects.all())
    for f in lista:
        f.uso_hoje = uso.get(f.pk)
    return render(request, "prospeccao/fontes.html", {"fontes": lista})


@staff_member_required
def fonte_form(request, pk):
    f = get_object_or_404(Fonte, pk=pk)
    form = FonteForm(request.POST or None, instance=f)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Fonte atualizada.")
        return redirect("prospeccao:fontes")
    return render(request, "prospeccao/fonte_form.html", {"form": form, "f": f})


@staff_member_required
def limpar_resultados(request):
    """Limpa o sistema, mantendo logins e leads já contatados (prévia + confirmação digitada). Ver services/limpeza.py."""
    from prospeccao.services import limpeza
    if request.method == "POST":
        if (request.POST.get("confirmacao") or "").strip().upper() != limpeza.CONFIRMACAO:
            messages.error(request, f"Para confirmar, digite {limpeza.CONFIRMACAO}. Nada foi apagado.")
            return redirect("prospeccao:limpar_resultados")
        carteira = request.POST.get("incluir_carteira") == "1"
        try:
            feito = limpeza.limpar(request.user, incluir_carteira=carteira)
        except limpeza.LimpezaBloqueada as e:
            messages.error(request, str(e))
            return redirect("prospeccao:limpar_resultados")
        messages.success(request, (
            f"Limpeza feita: {feito['empresas_encontradas']} empresa(s) da prospecção, {feito['oportunidades']} lead(s) "
            f"e {feito['execucoes']} execução(ões)"
            + (f"; também a carteira Salesforce local ({feito['contas_salesforce']} conta(s))" if carteira else
               "; a carteira Salesforce local foi mantida")
            + f". Mantidos: logins, {feito['leads_contatados_mantidos']} lead(s) já contatado(s), pesquisas e custos. "
            f"Cópia de segurança: {feito['backup'] or 'não gravada'}."))
        return redirect("prospeccao:parametros")
    return render(request, "prospeccao/limpar.html", {"previa": limpeza.previa(), "confirmacao": limpeza.CONFIRMACAO,
                                                    "pasta_backup": limpeza.pasta_backup()})
