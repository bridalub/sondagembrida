"""Telas da pesquisa PLANEJADA: frentes, plano, lista comercial (filas), canais e revisão da seleção."""
from collections import Counter

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from carteira.models import CanalEmpresa
from prospeccao.models import DecisaoRevisao, FrenteComercial, Pesquisa, PlanoPesquisa, SelecaoPesquisa, UsoIA, linhas
from prospeccao.services import ia, planejamento
from prospeccao.services.selecao import calcular_fila


@login_required
def frentes(request):
    dados = []
    for f in FrenteComercial.objects.all():
        filas = dict(Counter(SelecaoPesquisa.objects.filter(frente=f).values_list("fila", flat=True)))
        dados.append({"f": f, "filas": filas, "pesquisas": f.pesquisas.count(), "total": sum(filas.values())})
    return render(request, "prospeccao/frentes.html", {"dados": dados, "rot": dict(SelecaoPesquisa.Fila.choices),
                                                       "ordem": [k for k, _ in SelecaoPesquisa.Fila.choices]})


@login_required
def plano(request, pk):
    p = get_object_or_404(Pesquisa, pk=pk)
    versao = request.GET.get("v")
    atual = p.planos.filter(versao=versao).first() if versao else p.plano_ativo
    frentes_cfg = []
    if atual:
        for fid, cfg in atual.conteudo.get("por_frente", {}).items():
            frentes_cfg.append({"fid": fid, **cfg, "cnaes_txt": "\n".join(c["codigo"] for c in cfg.get("cnaes", [])),
                                "termos_txt": "\n".join(cfg.get("termos", [])), "sinais_txt": "\n".join(cfg.get("sinais", [])),
                                "neg_txt": "\n".join(cfg.get("contextos_negativos", []))})
    return render(request, "prospeccao/plano.html", {
        "p": p, "plano": atual, "versoes": p.planos.all()[:20], "frentes_cfg": frentes_cfg, "ia_status": ia.status(),
        "usos_ia": UsoIA.objects.filter(pesquisa=p, tipo="PLANO")[:5]})


@staff_member_required
@require_POST
def plano_gerar(request, pk):
    p = get_object_or_404(Pesquisa, pk=pk)
    try:
        if request.POST.get("origem") == "ia":
            pl = planejamento.plano_ia(p, request.user)
        else:
            pl = planejamento.plano_das_frentes(p, request.user)
        messages.success(request, f"Plano v{pl.versao} ({pl.get_origem_display()}) salvo e ativado."
                         + (f" {len(pl.cnaes_rejeitados)} CNAE(s) rejeitado(s) por não existirem na tabela."
                            if pl.cnaes_rejeitados else ""))
    except planejamento.IANaoConfigurada as e:
        messages.error(request, f"{e}. Nenhum plano de IA foi gerado.")
    except Exception as e:
        messages.error(request, f"Falha ao gerar plano: {type(e).__name__}: {e}")
    return redirect("prospeccao:plano", pk=pk)


@staff_member_required
@require_POST
def plano_salvar(request, pk):
    p = get_object_or_404(Pesquisa, pk=pk)
    dados = {"_geral": {"interpretacao": request.POST.get("interpretacao", ""),
                        "consultas_externas": linhas(request.POST.get("consultas_externas")),
                        "ambiguidades": linhas(request.POST.get("ambiguidades")),
                        "exclusoes_termos": linhas(request.POST.get("exclusoes_termos")),
                        "exclusoes_cnaes": linhas(request.POST.get("exclusoes_cnaes"))}}
    for fid in request.POST.getlist("fid"):
        dados[fid] = {"cnaes": linhas(request.POST.get(f"cnaes_{fid}")), "termos": linhas(request.POST.get(f"termos_{fid}")),
                      "sinais": linhas(request.POST.get(f"sinais_{fid}")),
                      "contextos_negativos": linhas(request.POST.get(f"neg_{fid}"))}
    pl = planejamento.plano_manual(p, request.user, dados, request.POST.get("observacao", ""))
    messages.success(request, f"Plano v{pl.versao} (ajuste do gestor) salvo e ativado."
                     + (f" CNAEs rejeitados: {', '.join(r['codigo'] for r in pl.cnaes_rejeitados)}" if pl.cnaes_rejeitados else ""))
    return redirect("prospeccao:plano", pk=pk)


@login_required
def lista(request, pk):
    p = get_object_or_404(Pesquisa, pk=pk)
    fila = request.GET.get("fila", "SELECIONADA")
    qs = SelecaoPesquisa.objects.filter(pesquisa=p).select_related("frente", "empresa", "empresa__oportunidade",
                                                                    "empresa__oportunidade__responsavel")
    contagem = dict(Counter(qs.values_list("fila", flat=True)))  # dict: Counter quebra o .items no template
    if fila != "TODAS":
        qs = qs.filter(fila=fila)
    if request.GET.get("frente"):
        qs = qs.filter(frente_id=request.GET["frente"])
    if request.GET.get("aplicacao"):
        qs = qs.filter(frente__aplicacoes__nome=request.GET["aplicacao"])
    if request.GET.get("contato") == "telefone":
        qs = qs.filter(empresa__canais__tipo="TELEFONE").distinct()
    elif request.GET.get("contato") == "email":
        qs = qs.filter(empresa__canais__tipo="EMAIL").distinct()
    elif request.GET.get("contato") == "publicado":
        qs = qs.filter(empresa__canais__origem_canal="PUBLICACAO", empresa__canais__confianca_associacao__in=["ALTA", "MEDIA"]).distinct()
    if request.GET.get("responsavel") == "meus":
        qs = qs.filter(empresa__oportunidade__responsavel=request.user)
    elif request.GET.get("responsavel") == "nenhum":
        qs = qs.filter(empresa__oportunidade__responsavel__isnull=True)
    pagina = Paginator(qs.order_by("-pontuacao"), 50).get_page(request.GET.get("page"))
    for s in pagina:
        if s.empresa:
            canais = list(s.empresa.canais.all())
            ordem = {"ALTA": 0, "MEDIA": 1, "BAIXA": 2, "CONFLITO": 3}
            uteis = sorted([c for c in canais if c.tipo in ("TELEFONE", "EMAIL") and not c.restrito
                            and c.confirmacao_humana != "INCORRETO" and c.confianca_associacao != "CONFLITO"
                            and c.aplica_a != "CONTADOR" and c.classificacao not in ("FISCAL", "CONTADOR")],
                           key=lambda c: (not c.preferido, ordem.get(c.confianca_associacao, 9)))
            s.tel = next((c for c in uteis if c.tipo == "TELEFONE"), None)
            marcado = next((c for c in uteis if c.tipo == "TELEFONE" and c.whatsapp_explicito), None)
            s.whatsapp = marcado.valor if marcado else ""
            s.mail = next((c for c in uteis if c.tipo == "EMAIL"), None)
            s.depto = next((c.departamento for c in uteis if c.departamento), "")
            s.outros = len(canais)
    from prospeccao.models import CategoriaProduto
    q = request.GET.copy()
    q.pop("page", None)
    filas = [(k, rot, contagem.get(k, 0)) for k, rot in SelecaoPesquisa.Fila.choices
             if k != "JA_PESQUISADA" or contagem.get(k)]  # regra antiga: só aparece se ainda houver registro
    return render(request, "prospeccao/lista.html", {
        "p": p, "pagina": pagina, "fila": fila, "contagem": contagem, "filas": filas,
        "total_todas": sum(contagem.values()),
        "frentes": p.frentes.all(), "aplicacoes": CategoriaProduto.objects.filter(frentecomercial__isnull=False).distinct(),
        "q": request.GET, "querystring": q.urlencode(),
        "mais_ativos": sum(1 for k in ("frente", "aplicacao", "contato", "responsavel") if request.GET.get(k))})


@login_required
@require_POST
def selecao_revisar(request, pk):
    s = get_object_or_404(SelecaoPesquisa, pk=pk)
    acao, motivo = request.POST.get("acao"), request.POST.get("motivo", "").strip()
    if not motivo:
        messages.error(request, "Informe o motivo da revisão.")
        return redirect(_proximo(request) or "prospeccao:pesquisas")
    if acao in ("INCLUIR", "RETIRAR"):
        s.decisao_humana = acao
    elif acao == "CORRIGIR":
        s.decisao_humana, s.aderencia_humana = "CORRIGIR", request.POST.get("aderencia", "")
    elif acao == "DESFAZER":
        s.decisao_humana, s.aderencia_humana = "", ""
    s.decisao_motivo, s.decisao_por, s.decisao_em = motivo[:255], request.user, timezone.now()
    s.fila, s.fila_motivos = calcular_fila(s, s.empresa, s.pesquisa) if s.empresa else (s.fila, s.fila_motivos)
    s.save()
    op = getattr(s.empresa, "oportunidade", None) if s.empresa else None
    DecisaoRevisao.objects.create(tipo=f"SELECAO_{acao}", usuario=request.user, oportunidade=op, motivo=motivo,
                                  dados={"selecao": s.pk, "pesquisa": s.pesquisa_id, "frente": s.frente_id,
                                         "aderencia_humana": s.aderencia_humana})
    messages.success(request, f"Seleção revisada ({acao}); etapa: {s.get_fila_display()}.")
    return redirect(_proximo(request) or "prospeccao:pesquisas")


def _proximo(request):
    destino = request.POST.get("proximo") or ""
    if destino and url_has_allowed_host_and_scheme(destino, allowed_hosts={request.get_host()},
                                                   require_https=request.is_secure()):
        return destino
    return ""


@login_required
@require_POST
def canal_acao(request, pk):
    c = get_object_or_404(CanalEmpresa, pk=pk)
    acao = request.POST.get("acao")
    agora = timezone.now()
    if acao == "incorreto":
        c.confirmacao_humana, c.confirmado_por, c.confirmado_em = CanalEmpresa.Confirmacao.INCORRETO, request.user, agora
    elif acao == "atendido":
        c.confirmacao_humana, c.confirmado_por, c.confirmado_em = CanalEmpresa.Confirmacao.ATENDIDO, request.user, agora
    elif acao == "preferido":
        c.preferido = not c.preferido
    elif acao == "restringir":
        c.restrito, c.restricao_motivo = True, request.POST.get("motivo", "")[:255] or "restrição registrada pela equipe"
    elif acao == "liberar":
        c.restrito, c.restricao_motivo = False, ""
    elif acao == "corrigir" and request.POST.get("valor"):
        from prospeccao.services import contatos as ct
        novo = ct.registrar(c.empresa, c.tipo, request.POST["valor"], fonte="equipe", origem=CanalEmpresa.Origem.HUMANO,
                            confianca=CanalEmpresa.Confianca.ALTA, aplica_a=c.aplica_a,
                            evidencia=f"correção de {c.valor} por {request.user}")
        c.confirmacao_humana, c.confirmado_por, c.confirmado_em = CanalEmpresa.Confirmacao.INCORRETO, request.user, agora
        if novo:
            novo.confirmacao_humana = CanalEmpresa.Confirmacao.ATENDIDO
            novo.save(update_fields=["confirmacao_humana"])
    c.save()
    for s in SelecaoPesquisa.objects.filter(empresa=c.empresa):
        s.fila, s.fila_motivos = calcular_fila(s, c.empresa, s.pesquisa)
        s.save(update_fields=["fila", "fila_motivos", "atualizada_em"])
    messages.success(request, "Contato atualizado (isto não registra abordagem).")
    if _proximo(request):
        return redirect(_proximo(request))
    return redirect("prospeccao:empresa", pk=c.empresa_id)
