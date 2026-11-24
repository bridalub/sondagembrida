"""Painel de captação (BI): abordagens, conversas, orçamentos e vendas da prospecção, por período e por pessoa.

Universo: empresas da prospecção (oportunidades NOVA_EXTERNA, em qualquer situação — uma empresa descartada
depois do contato continua contando como abordada). Contas do Salesforce ficam de fora.
- Abordada: tem pelo menos um contato registrado no período.
- Conversou: algum contato do período teve conversa efetiva.
- Sem resposta: só tentativas no período (nenhuma conversa).
- Orçamento: contato com resultado "orçamento enviado" ou com valor de orçamento informado.
- Venda informada: contato com resultado "venda fechada" ou com valor vendido informado.
- Venda confirmada por NF: nota fiscal de venda do Salesforce para o CNPJ CONFIRMADO pelo atendimento, emitida a
  partir do primeiro contato com a empresa. A venda é atribuída a quem confirmou o CNPJ. CNPJ só encontrado na
  pesquisa (não confirmado) não conta: pode ser de outra unidade. As notas chegam pela sincronização de hora em hora.
  Só conta se, na confirmação, o cliente era NOVO (fora do Salesforce), cadastrado SEM COMPRA ou INATIVO (sem comprar
  há mais que ConfiguracaoAnalise.dias_sem_compra_captacao, padrão 90 dias). Cliente ATIVO da carteira não é captação.
  Só contam as notas do MÊS DA PRIMEIRA COMPRA; do mês seguinte em diante o cliente não soma mais.
Somente leitura; nada é gravado.
"""
import calendar
from datetime import date, timedelta
from datetime import time as dtime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, Min, Q, Sum

from django.utils import timezone

from carteira.models import RESULTADOS_COM_CONVERSA as _COM_CONVERSA
from carteira.models import Abordagem, Empresa, NotaFiscal, ResultadoAbordagem
from prospeccao.models import Oportunidade

ORC = Q(resultado=ResultadoAbordagem.CONVERSA_ORCAMENTO) | Q(valor_orcamento__isnull=False)
VENDA = Q(resultado=ResultadoAbordagem.CONVERSA_VENDA) | Q(valor_venda__isnull=False)


def periodo_do_mes(texto=None, hoje=None):
    """(início, fim, 'AAAA-MM') do mês pedido; mês atual se vazio ou inválido."""
    hoje = hoje or date.today()
    try:
        ano, mes = (int(x) for x in (texto or "").split("-"))
        inicio = date(ano, mes, 1)
    except (ValueError, TypeError):
        inicio = hoje.replace(day=1)
    fim = inicio.replace(day=calendar.monthrange(inicio.year, inicio.month)[1])
    return inicio, fim, f"{inicio.year}-{inicio.month:02d}"


def empresas_prospeccao(pesquisa=None):
    qs = Oportunidade.objects.filter(tipo=Oportunidade.Tipo.NOVA_EXTERNA).exclude(empresa__salesforce_id__isnull=False)
    if pesquisa:
        qs = qs.filter(pesquisas__pk=pesquisa)
    return qs.values_list("empresa_id", flat=True).distinct()


def _nome(u):
    return (u.get("usuario__first_name") or u.get("usuario__username") or "—").strip()


def vendas_por_nf(empresas, inicio=None, fim=None, usuario=None):
    """Notas de venda dos CNPJs confirmados pelo atendimento, emitidas a partir do 1º contato com a empresa."""
    from carteira.services.abordagens import situacao_cliente
    confirmados = {}
    for a in (Abordagem.objects.filter(empresa_id__in=empresas).exclude(cnpj_confirmado="").order_by("data_hora")
              .values("empresa_id", "cnpj_confirmado", "usuario__username", "usuario__first_name", "data_hora",
                      "cliente_situacao", "cliente_ultima_compra")):
        confirmados.setdefault(a["cnpj_confirmado"], a)
    saida = {"valor": Decimal("0"), "notas": 0, "empresas": set(), "por_usuario": {}, "linhas": [],
             "ativos_ignorados": 0}
    # Cliente ATIVO da carteira (comprou dentro do limite) não é captação: as notas dele ficam fora.
    # A situação vem da confirmação; contatos antigos sem ela são classificados agora, na data do contato. Quem já
    # tinha compra é reavaliado pela última compra gravada e pelo limite ATUAL (ex.: limite mudou de 730 para 90 dias).
    from carteira.models import ConfiguracaoAnalise
    limite = ConfiguracaoAnalise.obter().dias_sem_compra_captacao or 90
    S = Abordagem.SituacaoCliente
    for c, a in list(confirmados.items()):
        if not a["cliente_situacao"]:
            a["cliente_situacao"], a["cliente_ultima_compra"] = situacao_cliente(
                c, timezone.localtime(a["data_hora"]).date())
        elif a["cliente_situacao"] in (S.ATIVO, S.INATIVO) and a["cliente_ultima_compra"]:
            dias = (timezone.localtime(a["data_hora"]).date() - a["cliente_ultima_compra"]).days
            a["cliente_situacao"] = S.INATIVO if dias > limite else S.ATIVO
        if a["cliente_situacao"] == Abordagem.SituacaoCliente.ATIVO:
            del confirmados[c]
            saida["ativos_ignorados"] += 1
    if not confirmados:
        return saida
    # A venda é de quem ATENDEU o cliente: o dono do lead (1º atendimento) quando houver; senão, quem confirmou o CNPJ.
    # (Ex.: ADMIN confirmou o CNPJ no lead da Ana: a venda continua sendo da Ana.)
    donos = {o["empresa_id"]: o for o in Oportunidade.objects.filter(
        empresa_id__in=[a["empresa_id"] for a in confirmados.values()], responsavel__isnull=False)
        .values("empresa_id", "responsavel__username", "responsavel__first_name")}
    for a in confirmados.values():
        d = donos.get(a["empresa_id"])
        if d:
            a["usuario__username"], a["usuario__first_name"] = d["responsavel__username"], d["responsavel__first_name"]
    primeiro = {r["empresa_id"]: timezone.localtime(r["m"]).date() for r in (
        Abordagem.objects.filter(empresa_id__in=[a["empresa_id"] for a in confirmados.values()])
        .values("empresa_id").annotate(m=Min("data_hora")))}
    notas = NotaFiscal.objects.filter(tipo=NotaFiscal.Tipo.VENDA, presente_na_origem=True,
                                      empresa__cnpj__in=list(confirmados))
    # Só conta o MÊS DA PRIMEIRA COMPRA (a 1ª nota a partir do 1º contato): compras de outros dias desse mês somam;
    # do mês seguinte em diante o cliente é da carteira e não soma mais nada.
    primeira_compra = {}
    for cnpj, d in notas.values_list("empresa__cnpj", "data_emissao").distinct():
        ini = primeiro.get(confirmados[cnpj]["empresa_id"])
        if d and (ini is None or d >= ini) and (cnpj not in primeira_compra or d < primeira_compra[cnpj]):
            primeira_compra[cnpj] = d
    if inicio:
        notas = notas.filter(data_emissao__gte=inicio)
    if fim:
        notas = notas.filter(data_emissao__lte=fim)
    for n in notas.values("empresa__cnpj", "data_emissao", "valor_total", "numero", "empresa__nome"):
        a = confirmados[n["empresa__cnpj"]]
        if not n["data_emissao"] or n["data_emissao"] < primeiro.get(a["empresa_id"], n["data_emissao"]):
            continue  # compra anterior ao contato não é captação
        pc = primeira_compra.get(n["empresa__cnpj"])
        if pc and (n["data_emissao"].year, n["data_emissao"].month) != (pc.year, pc.month):
            continue  # passou do mês da primeira compra: não soma mais
        if usuario and a["usuario__username"] != usuario:
            continue
        saida["linhas"].append({"data": n["data_emissao"], "numero": n["numero"], "valor": n["valor_total"] or 0,
                                "cnpj": n["empresa__cnpj"], "cliente_nf": n["empresa__nome"],
                                "empresa_id": a["empresa_id"], "quem": _nome(a), "quem_login": a["usuario__username"],
                                "situacao": a["cliente_situacao"]})
        saida["valor"] += n["valor_total"] or 0
        saida["notas"] += 1
        saida["empresas"].add(a["empresa_id"])
        saida.setdefault("dias_compra", set()).add((a["empresa_id"], n["data_emissao"]))  # 1 venda = 1 dia de compra
        u = saida["por_usuario"].setdefault(a["usuario__username"], {"nome": _nome(a), "valor": Decimal("0"), "notas": 0})
        u["valor"] += n["valor_total"] or 0
        u["notas"] += 1
    return saida


def conversao_orcamentos(empresas, inicio, fim, usuario=None):
    """Para o gestor: quanto cada orçamento virou em venda (toda venda tem orçamento; nem todo orçamento vira compra).
    Cada nota fiscal que conta na captação é atribuída ao orçamento MAIS RECENTE do mesmo lead emitido até a data da
    compra. Orçamentos do período: quantos viraram compra, R$ vendido a partir deles e taxa de conversão.
    Vendas do período sem nenhum orçamento antes ficam à parte (sinal de orçamento não registrado)."""
    todos = list(Abordagem.objects.filter(empresa_id__in=empresas).filter(ORC)
                 .values("pk", "empresa_id", "data_hora", "valor_orcamento", "usuario__username").order_by("data_hora"))
    por_empresa = {}
    for o in todos:
        o["dia"] = timezone.localtime(o["data_hora"]).date()
        por_empresa.setdefault(o["empresa_id"], []).append(o)
    vendido = {}  # pk do orçamento -> {"valor", "dias"}
    sem_orc_valor, sem_orc_clientes = Decimal("0"), set()
    for n in vendas_por_nf(empresas)["linhas"]:
        antes = [o for o in por_empresa.get(n["empresa_id"], []) if o["dia"] <= n["data"]]
        if antes:
            alvo = vendido.setdefault(antes[-1]["pk"], {"valor": Decimal("0"), "dias": set()})
            alvo["valor"] += n["valor"] or 0
            alvo["dias"].add(n["data"])
        elif inicio <= n["data"] <= fim:
            sem_orc_valor += n["valor"] or 0
            sem_orc_clientes.add(n["empresa_id"])
    do_periodo = [o for o in todos if inicio <= o["dia"] <= fim and (not usuario or o["usuario__username"] == usuario)]
    convertidos = [o for o in do_periodo if o["pk"] in vendido]
    leads = {o["empresa_id"] for o in do_periodo}
    leads_conv = {o["empresa_id"] for o in convertidos}
    return {
        "por_orcamento": vendido,
        "orcamentos": len(do_periodo), "convertidos": len(convertidos),
        "leads": len(leads), "leads_convertidos": len(leads_conv),
        "taxa": round(len(leads_conv) * 100 / len(leads)) if leads else 0,
        "valor_orcado": sum((o["valor_orcamento"] or 0 for o in do_periodo), Decimal("0")),
        "valor_orcado_convertido": sum((o["valor_orcamento"] or 0 for o in convertidos), Decimal("0")),
        "valor_vendido": sum((vendido[o["pk"]]["valor"] for o in convertidos), Decimal("0")),
        "sem_orcamento_valor": sem_orc_valor if not usuario else Decimal("0"),
        "sem_orcamento_clientes": len(sem_orc_clientes) if not usuario else 0,
    }


def calcular(inicio, fim, pesquisa=None, usuario=None):
    empresas = empresas_prospeccao(pesquisa)
    ab = Abordagem.objects.filter(empresa_id__in=empresas, data_hora__date__gte=inicio, data_hora__date__lte=fim)
    if usuario:
        ab = ab.filter(usuario__username=usuario)

    abordados = ab.values("empresa_id").distinct().count()
    conversaram = ab.filter(houve_conversa=True).values("empresa_id").distinct().count()
    orc = ab.filter(ORC).aggregate(n=Count("id"), v=Sum("valor_orcamento"))
    ven = ab.filter(VENDA).aggregate(n=Count("id"), v=Sum("valor_venda"))
    kpis = {
        "abordados": abordados,
        "conversaram": conversaram,
        "sem_resposta": abordados - conversaram,
        "orcamentos": orc["n"], "valor_orcamentos": orc["v"] or Decimal("0"),
        "vendas": ven["n"], "valor_vendas": ven["v"] or Decimal("0"),
        "taxa_conversa": round(conversaram * 100 / abordados) if abordados else 0,
        "taxa_venda": round(ven["n"] * 100 / abordados) if abordados else 0,
        "contatos": ab.count(),
    }
    nf = vendas_por_nf(empresas, inicio, fim, usuario)
    kpis.update(valor_nf=nf["valor"], notas_nf=nf["notas"], empresas_nf=len(nf["empresas"]),
                vendas_dias=len(nf.get("dias_compra", ())),
                cnpjs_confirmados=ab.exclude(cnpj_confirmado="").values("empresa_id").distinct().count())
    kpis["conv_orc"] = conversao_orcamentos(empresas, inicio, fim, usuario)

    # por dia: empresas abordadas, separando as que conversaram das que ficaram só em tentativa
    dias = []
    por_dia = {}
    for a in ab.values("empresa_id", "data_hora", "houve_conversa"):
        d = timezone.localtime(a["data_hora"]).date()
        reg = por_dia.setdefault(d, {})
        reg[a["empresa_id"]] = reg.get(a["empresa_id"], False) or a["houve_conversa"]
    d = inicio
    while d <= fim:
        reg = por_dia.get(d, {})
        conv = sum(1 for v in reg.values() if v)
        dias.append({"dia": d.day, "data": d.isoformat(), "conversa": conv, "tentativa": len(reg) - conv,
                     "futuro": d > timezone.localdate()})
        d += timedelta(days=1)

    # situação atual (todo o histórico): procurados x em aberto
    from prospeccao.views_inside import base_inside
    base = base_inside()
    if pesquisa:
        base = base.filter(pesquisas__pk=pesquisa)
    procurados_total = Abordagem.objects.filter(empresa_id__in=empresas).values("empresa_id").distinct().count()
    em_aberto = (base.filter(status__in=[Oportunidade.Status.NOVA, Oportunidade.Status.LIBERADA])
                 .exclude(empresa__abordagens__isnull=False).values("empresa_id").distinct().count())

    # funil desde o início
    todas = Abordagem.objects.filter(empresa_id__in=empresas)
    funil = [
        {"etapa": "Encontradas", "n": empresas.count()},
        {"etapa": "Procuradas", "n": procurados_total},
        {"etapa": "Conversaram", "n": todas.filter(houve_conversa=True).values("empresa_id").distinct().count()},
        {"etapa": "Receberam orçamento", "n": todas.filter(ORC).values("empresa_id").distinct().count()},
        {"etapa": "CNPJ confirmado", "n": todas.exclude(cnpj_confirmado="").values("empresa_id").distinct().count()},
        {"etapa": "Compraram", "n": len(set(todas.filter(VENDA).values_list("empresa_id", flat=True))
                                        | vendas_por_nf(empresas)["empresas"])},
    ]

    # rankings do período (por pessoa)
    pessoas = (ab.values("usuario__username", "usuario__first_name")
               .annotate(abordados=Count("empresa_id", distinct=True),
                         conversas=Count("empresa_id", distinct=True, filter=Q(houve_conversa=True)),
                         orcamentos=Count("id", filter=ORC),
                         valor_orc=Sum("valor_orcamento"),
                         vendas=Count("id", filter=VENDA),
                         valor_venda=Sum("valor_venda")))
    linhas = [{"nome": _nome(p), "usuario": p["usuario__username"], "abordados": p["abordados"],
               "conversas": p["conversas"], "orcamentos": p["orcamentos"],
               "valor_orc": float(p["valor_orc"] or 0), "vendas": p["vendas"],
               "valor_venda": float(p["valor_venda"] or 0),
               "valor_nf": float(nf["por_usuario"].get(p["usuario__username"], {}).get("valor", 0)),
               "notas_nf": nf["por_usuario"].get(p["usuario__username"], {}).get("notas", 0)} for p in pessoas]
    rankings = {
        "orcamentos": sorted([l for l in linhas if l["orcamentos"]], key=lambda l: (-l["orcamentos"], -l["valor_orc"]))[:8],
        "conversas": sorted([l for l in linhas if l["conversas"]], key=lambda l: -l["conversas"])[:8],
        # quem mais vendeu: valor confirmado por nota fiscal primeiro; o informado desempata
        "vendas": sorted([l for l in linhas if l["valor_nf"] or l["valor_venda"] or l["vendas"]],
                         key=lambda l: (-l["valor_nf"], -l["valor_venda"], -l["vendas"]))[:8],
    }

    rotulos = dict(ResultadoAbordagem.choices)
    resultados = [{"resultado": r["resultado"], "rotulo": rotulos.get(r["resultado"], r["resultado"]),
                   "n": r["n"], "conversa": r["resultado"] in _COM_CONVERSA}
                  for r in ab.values("resultado").annotate(n=Count("id")).order_by("-n")]

    return {"kpis": kpis, "dias": dias, "pizza": {"procurados": procurados_total, "em_aberto": em_aberto},
            "funil": funil, "rankings": rankings, "resultados": resultados}


DETALHES = {
    "abordados": ("Empresas abordadas", "Empresas com pelo menos um contato registrado no período (o mais recente primeiro)."),
    "conversaram": ("Conversaram", "Empresas com conversa efetiva em algum contato do período."),
    "sem_resposta": ("Sem resposta", "Empresas que só tiveram tentativas no período, sem nenhuma conversa."),
    "orcamentos": ("Orçamentos", "Contatos com orçamento enviado ou valor de orçamento informado, com o quanto cada "
                   "um virou em venda (notas fiscais do lead depois do orçamento)."),
    "orcamentos_convertidos": ("Orçamentos que viraram venda", "Orçamentos do período cujo cliente comprou depois: "
                               "valor orçado e valor vendido (notas fiscais atribuídas ao orçamento mais recente "
                               "antes de cada compra)."),
    "cnpjs": ("CNPJ confirmados", "Empresas cujo CNPJ foi validado com o cliente no período."),
    "vendas": ("Vendas informadas", "Vendas registradas pelo atendimento no contato (o valor oficial vem da nota fiscal)."),
    "vendas_nf": ("Vendido · notas fiscais", "Notas fiscais de venda do Salesforce para CNPJs confirmados pelo atendimento, "
                  "emitidas a partir do primeiro contato. A venda é de quem confirmou o CNPJ."),
}


def detalhe(tipo, inicio, fim, pesquisa=None, usuario=None, q=""):
    """Linhas por trás de cada cartão do painel, mesmas regras de `calcular`, da mais nova para a mais antiga.
    Cada linha: data, empresa_id, cliente, cnpj, cidade, detalhe, valor (ou None), quem."""
    from carteira.services.normalizacao import normalizar_texto
    empresas = empresas_prospeccao(pesquisa)
    ab = (Abordagem.objects.filter(empresa_id__in=empresas, data_hora__date__gte=inicio, data_hora__date__lte=fim)
          .select_related("empresa", "usuario"))
    if usuario:
        ab = ab.filter(usuario__username=usuario)
    rot = dict(ResultadoAbordagem.choices)

    def quem(u):
        return (u.first_name or u.username) if u else "—"

    def linha_ab(a, valor=None, detalhe=""):
        e = a.empresa
        return {"data": a.data_hora, "empresa_id": e.pk, "cliente": e.nome_fantasia or e.razao_social or e.nome,
                "cnpj": a.cnpj_confirmado or e.cnpj or "", "cidade": e.cidade, "uf": e.uf,
                "detalhe": detalhe or rot.get(a.resultado, a.resultado), "valor": valor, "quem": quem(a.usuario),
                "tem_hora": True, "anexo_pk": a.pk if a.anexo_orcamento else None}

    linhas = []
    if tipo in ("abordados", "conversaram", "sem_resposta"):
        por_empresa = {}
        for a in ab.order_by("data_hora"):
            r = por_empresa.setdefault(a.empresa_id, {"ult": a, "n": 0, "conversa": False})
            r["ult"], r["n"] = a, r["n"] + 1
            r["conversa"] = r["conversa"] or a.houve_conversa
        for r in por_empresa.values():
            if tipo == "conversaram" and not r["conversa"] or tipo == "sem_resposta" and r["conversa"]:
                continue
            a = r["ult"]
            linhas.append(linha_ab(a, detalhe=f"{r['n']} contato{'s' if r['n'] > 1 else ''} · último: "
                                              f"{rot.get(a.resultado, a.resultado)}"))
    elif tipo in ("orcamentos", "orcamentos_convertidos"):
        conv = conversao_orcamentos(empresas, inicio, fim, usuario)["por_orcamento"]
        for a in ab.filter(ORC):
            v = conv.get(a.pk)
            if tipo == "orcamentos_convertidos" and not v:
                continue
            x = linha_ab(a, a.valor_orcamento)
            x["vendido"] = v["valor"] if v else None
            x["vendas_dias"] = len(v["dias"]) if v else 0
            linhas.append(x)
    elif tipo == "vendas":
        linhas = [linha_ab(a, a.valor_venda) for a in ab.filter(VENDA)]
    elif tipo == "cnpjs":
        vistos = set()
        for a in ab.exclude(cnpj_confirmado="").order_by("data_hora"):
            if a.empresa_id not in vistos:
                vistos.add(a.empresa_id)
                linhas.append(linha_ab(a, detalhe="CNPJ confirmado com o cliente"))
    elif tipo == "vendas_nf":
        nf = vendas_por_nf(empresas, inicio, fim, usuario)
        nomes = {e.pk: e for e in Empresa.objects.filter(pk__in={n["empresa_id"] for n in nf["linhas"]})}
        for n in nf["linhas"]:
            e = nomes.get(n["empresa_id"])
            linhas.append({"data": n["data"], "empresa_id": n["empresa_id"],
                           "cliente": (e.nome_fantasia or e.razao_social or e.nome) if e else n["cliente_nf"],
                           "cnpj": n["cnpj"], "cidade": e.cidade if e else "", "uf": e.uf if e else "",
                           "detalhe": f"NF {n['numero']}" + (f" · {n['cliente_nf']}" if n["cliente_nf"] else "")
                           + {"NOVO": " · cliente novo", "SEM_COMPRA": " · nunca tinha comprado",
                              "INATIVO": " · cliente reativado"}.get(n.get("situacao") or "", ""),
                           "valor": n["valor"], "quem": n["quem"]})
    else:
        raise KeyError(tipo)
    if q:
        alvo, dig = normalizar_texto(q), "".join(c for c in q if c.isdigit())
        linhas = [x for x in linhas if alvo in normalizar_texto(f"{x['cliente']} {x['detalhe']}")
                  or (len(dig) >= 4 and dig in (x["cnpj"] or ""))]

    def chave(x):  # nota fiscal só tem data; contato tem data e hora
        d = x["data"]
        if hasattr(d, "hour"):
            d = timezone.localtime(d)
            return d.date(), d.time()
        return d, dtime.min
    linhas.sort(key=chave, reverse=True)
    for x in linhas:
        c = x["cnpj"] or ""
        x["cnpj_fmt"] = f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else c
    total = sum((x["valor"] or 0 for x in linhas), Decimal("0"))
    total_vendido = sum((x.get("vendido") or 0 for x in linhas), Decimal("0"))
    return {"linhas": linhas, "total": total, "total_vendido": total_vendido,
            "com_vendido": tipo in ("orcamentos", "orcamentos_convertidos"),
            "com_valor": tipo in ("orcamentos", "orcamentos_convertidos", "vendas", "vendas_nf"),
            "titulo": DETALHES[tipo][0], "descricao": DETALHES[tipo][1]}


def vendedores():
    return (get_user_model().objects.filter(abordagens__isnull=False).distinct()
            .order_by("first_name", "username").values_list("username", "first_name"))



def simular_dados(inicio, fim, semente=7):
    """Cria contatos, orçamentos, vendas e notas FICTÍCIOS para demonstrar o painel.
    Só deve ser chamada dentro de transaction.atomic() com set_rollback(True): nada pode ficar gravado."""
    import random
    from datetime import datetime
    rnd = random.Random(semente)
    U = get_user_model()
    equipe = [U.objects.create_user(f"simulacao_{n.lower()}", first_name=n) for n in ("Carla", "Diego", "Elisa", "Fábio")]
    empresas = list(Oportunidade.objects.filter(tipo=Oportunidade.Tipo.NOVA_EXTERNA)
                    .exclude(empresa__salesforce_id__isnull=False)
                    .select_related("empresa").order_by("pk")[:140])
    ultimo = min(fim, timezone.localdate())
    resultados = (["SEM_RESPOSTA"] * 5 + ["RECADO"] * 2 + ["CONTATO_INVALIDO", "CONVERSA_INTERESSE", "CONVERSA_INTERESSE",
                  "CONVERSA_RETORNAR", "CONVERSA_SEM_INTERESSE", "CONVERSA_ORCAMENTO", "CONVERSA_ORCAMENTO",
                  "CONVERSA_VENDA"])
    n_nf = 0
    for i, op in enumerate(empresas[:110]):
        e = op.empresa
        dia = rnd.randint(inicio.day, ultimo.day)
        u = equipe[rnd.randrange(len(equipe))]
        r = resultados[rnd.randrange(len(resultados))]
        conversa = r.startswith("CONVERSA")
        cnpj = ""
        if conversa and rnd.random() < 0.8:  # o atendente confirma (ou obtém) o CNPJ na conversa
            if not e.cnpj:
                from carteira.services.cnpj import calcular_dv
                base = f"99{i:06d}0001"
                Empresa.objects.filter(pk=e.pk).update(cnpj=base + calcular_dv(base))
                e.refresh_from_db()
            cnpj = e.cnpj
        quando = timezone.make_aware(datetime(inicio.year, inicio.month, dia, rnd.randint(8, 17), rnd.choice([0, 30])))
        ab_sim = Abordagem.objects.create(
            empresa=e, usuario=u, canal=rnd.choice(["TELEFONE", "WHATSAPP", "TELEFONE"]), resultado=r, data_hora=quando,
            cnpj_confirmado=cnpj,
            data_retorno=ultimo + timedelta(days=3) if r == "CONVERSA_RETORNAR" else None,
            valor_orcamento=Decimal(rnd.randint(8, 60) * 100) if r in ("CONVERSA_ORCAMENTO", "CONVERSA_VENDA") else None,
            valor_venda=Decimal(rnd.randint(5, 40) * 100) if r == "CONVERSA_VENDA" else None)
        Abordagem.objects.filter(pk=ab_sim.pk).update(  # simulação: registrado na hora do contato, orçamento "anexado"
            criado_em=quando, anexo_orcamento="orcamentos/simulacao.pdf" if r == "CONVERSA_ORCAMENTO" else "")
        if cnpj and r in ("CONVERSA_VENDA", "CONVERSA_ORCAMENTO") and rnd.random() < 0.7:
            for _ in range(rnd.randint(1, 2)):  # nota fiscal de venda emitida depois do contato
                n_nf += 1
                NotaFiscal.objects.create(id_origem=f"SIMULACAO-{n_nf}", numero=f"SIM{n_nf:04d}", tipo=NotaFiscal.Tipo.VENDA,
                                          empresa=e, data_emissao=min(ultimo, quando.date() + timedelta(days=rnd.randint(0, 6))),
                                          valor_total=Decimal(rnd.randint(6, 55) * 100), qtd_itens=rnd.randint(1, 8))
    return len(equipe)
