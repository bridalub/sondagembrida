"""Disputa do mês (gamificação) do Insight Sales: pontos, ranking da equipe, sequência e conquistas.

Regras simples e auditáveis (mesmas bases do Painel de captação; nada é gravado).
Cada atendimento (empresa procurada no mês) vale SÓ a etapa MAIS ALTA que alcançou, sem somar as etapas:
  - Empresa procurada ................... 1 ponto
  - Conversa efetiva .................... 3 pontos
  - CNPJ confirmado com o cliente ....... 3 pontos
  - Orçamento enviado ................... 4 pontos (só com o arquivo do orçamento anexado)
  - Venda (dia de compra) ............... 30 pontos POR DIA em que o cliente comprou no mês (todas as notas do mesmo
                                          dia = 1 venda; comprou de novo outro dia = mais 30). Venda só "informada"
                                          não pontua; a compra vem das notas fiscais do Salesforce.
Exceção: o ORÇAMENTO SOMA À VENDA (lead orçado que comprou = 4 + 30 por dia de compra): quem orçou não perde o
orçamento quando a venda chega. Assim, quem só acumula contatos não passa quem leva o cliente até a venda.
Sequência: dias úteis seguidos (seg. a sex.) com pelo menos um contato registrado, até hoje.
Tudo conta pelo momento em que o contato foi REGISTRADO no sistema (não pela data digitada no formulário).
Quem participa: usuários do Insight Sales (ativos ou com contato no mês). ADMIN não entra no ranking.
A venda é de quem atendeu o cliente (dono do lead) e vale uma vez por cliente, qualquer que seja o número de notas.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone

from carteira.models import Abordagem, ResultadoAbordagem
from prospeccao.services import bi_captacao as bi

PONTOS = {"procuradas": 1, "conversas": 3, "cnpjs": 3, "orcamentos": 4, "vendas_nf": 30}
ROTULOS = {"procuradas": "Empresas procuradas", "conversas": "Conversas", "cnpjs": "CNPJ confirmados",
           "orcamentos": "Orçamentos", "vendas_nf": "Vendas (dias de compra)"}
# rótulo de cada etapa quando ela é a mais alta do atendimento
ETAPAS = {"procuradas": "só procurada", "conversas": "até conversa", "cnpjs": "até CNPJ confirmado",
          "orcamentos": "até orçamento", "vendas_nf": "venda(s) (dia de compra)"}
NIVEIS = [(0, "Bronze"), (40, "Prata"), (100, "Ouro"), (200, "Diamante")]


def etapa_maior(etapas):
    """A etapa de maior pontuação entre as alcançadas (empate: a mais avançada no funil)."""
    ordem = list(PONTOS)
    return max(etapas, key=lambda k: (PONTOS[k], ordem.index(k)))

# (código, nome, descrição, campo, meta)
CONQUISTAS = [
    ("largada", "Largada", "Primeiro contato do mês", "procuradas", 1),
    ("maratona", "Maratonista", "50 empresas procuradas", "procuradas", 50),
    ("papo", "Bom de papo", "10 conversas efetivas", "conversas", 10),
    ("detetive", "Detetive de CNPJ", "5 CNPJ confirmados com o cliente", "cnpjs", 5),
    ("orcamentista", "Orçamentista", "5 orçamentos enviados", "orcamentos", 5),
    ("primeira_venda", "Primeira venda", "1ª venda confirmada por nota fiscal", "vendas_nf", 1),
    ("chamas", "Em ritmo", "5 dias úteis seguidos com contato", "sequencia", 5),
    ("conversao", "Taxa de ouro", "40% de conversa com 20+ procuradas", "taxa_conversa", 40),
]


def nivel(pontos):
    atual, proximo = NIVEIS[0], None
    for i, (minimo, nome) in enumerate(NIVEIS):
        if pontos >= minimo:
            atual = (minimo, nome)
            proximo = NIVEIS[i + 1] if i + 1 < len(NIVEIS) else None
    pct = 100 if proximo is None else round((pontos - atual[0]) * 100 / (proximo[0] - atual[0]))
    return {"nome": atual[1], "proximo": proximo[1] if proximo else "", "falta": (proximo[0] - pontos) if proximo else 0,
            "pct": max(0, min(100, pct))}


def _dia_util_anterior(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def sequencia(dias, hoje=None):
    """Dias úteis seguidos com contato até hoje (hoje ainda sem contato não quebra a sequência)."""
    hoje = hoje or timezone.localdate()
    d = hoje
    while d.weekday() >= 5:  # sábado/domingo: conta a partir da sexta
        d -= timedelta(days=1)
    if d not in dias:
        d = _dia_util_anterior(d)
    n = 0
    while d in dias:
        n += 1
        d = _dia_util_anterior(d)
    return n


def placar(inicio, fim, pesquisa=None, hoje=None):
    """Ranking da equipe no período. Lista de participantes ordenada (pontos, valor NF, procuradas)."""
    U = get_user_model()
    empresas = bi.empresas_prospeccao(pesquisa)
    # Pontos e sequência pelo MOMENTO DO REGISTRO (criado_em, gravado pelo sistema): a data informada no formulário
    # pode ser retroativa e não serve para montar sequência nem mudar o mês dos pontos.
    ab = Abordagem.objects.filter(empresa_id__in=empresas, criado_em__date__gte=inicio, criado_em__date__lte=fim)
    def novo():
        return {"procuradas": set(), "conversas": set(), "cnpjs": set(), "orcamentos": 0, "dias": set(),
                "etapas": defaultdict(set),  # empresa -> etapas alcançadas (o atendimento vale a maior)
                "vendas_dias": defaultdict(set)}  # empresa -> dias em que o cliente comprou (cada dia = 1 venda)
    base = defaultdict(novo)
    for a in ab.values("usuario_id", "empresa_id", "houve_conversa", "cnpj_confirmado", "resultado",
                       "valor_orcamento", "anexo_orcamento", "criado_em"):
        r, e = base[a["usuario_id"]], a["empresa_id"]
        r["procuradas"].add(e)
        r["etapas"][e].add("procuradas")
        if a["houve_conversa"]:
            r["conversas"].add(e)
            r["etapas"][e].add("conversas")
        if a["cnpj_confirmado"]:
            r["cnpjs"].add(e)
            r["etapas"][e].add("cnpjs")
        orcamento = a["resultado"] == ResultadoAbordagem.CONVERSA_ORCAMENTO or a["valor_orcamento"] is not None
        if orcamento and a["anexo_orcamento"]:  # orçamento só pontua com o arquivo anexado (comprovante)
            r["orcamentos"] += 1
            r["etapas"][e].add("orcamentos")
        r["dias"].add(timezone.localtime(a["criado_em"]).date())
    vendas = bi.vendas_por_nf(empresas, inicio, fim)
    nf = vendas["por_usuario"]
    login_pk = dict(U.objects.filter(username__in=list(nf)).values_list("username", "pk"))
    for linha in vendas["linhas"]:
        # VENDA = DIA DE COMPRA do cliente: várias notas no mesmo dia são uma venda só; outro dia é outra venda
        pk = login_pk.get(linha["quem_login"])
        if pk is not None:
            base[pk]["vendas_dias"][linha["empresa_id"]].add(linha["data"])
    from prospeccao.papeis import GRUPO_ADM, eh_adm
    ids = set(base) | set(U.objects.filter(is_active=True, is_staff=False, is_superuser=False)
                          .exclude(groups__name=GRUPO_ADM).values_list("pk", flat=True))
    # a disputa é da equipe Insight Sales: ADMIN não entra no ranking, mesmo registrando contatos
    pessoas = {u.pk: u for u in U.objects.filter(pk__in=ids) if not eh_adm(u)}
    saida = []
    for pk, u in pessoas.items():
        r = base[pk] if pk in base else novo()
        v = nf.get(u.username, {})
        n_vendas = sum(len(d) for d in r["vendas_dias"].values())
        m = {"procuradas": len(r["procuradas"]), "conversas": len(r["conversas"]), "cnpjs": len(r["cnpjs"]),
             "orcamentos": r["orcamentos"], "vendas_nf": n_vendas}
        # Cliente que comprou: 30 por dia de compra no mês + 4 do orçamento (com arquivo), se houve: quem orçou e
        # depois vendeu não perde o orçamento. Procurada/conversa/CNPJ não somam à venda.
        # Cliente sem compra: vale a etapa mais alta que alcançou.
        topo = {k: 0 for k in PONTOS}
        for e in set(r["etapas"]) | set(r["vendas_dias"]):
            if r["vendas_dias"].get(e):
                topo["vendas_nf"] += len(r["vendas_dias"][e])
                if "orcamentos" in r["etapas"].get(e, ()):
                    topo["orcamentos"] += 1
            else:
                topo[etapa_maior(r["etapas"][e])] += 1
        pontos = sum(topo[k] * PONTOS[k] for k in PONTOS)
        seq = sequencia(r["dias"], hoje)
        taxa = round(m["conversas"] * 100 / m["procuradas"]) if m["procuradas"] else 0
        valores = {**m, "sequencia": seq, "taxa_conversa": taxa if m["procuradas"] >= 20 else 0}
        conquistas = [{"codigo": c, "nome": n, "descricao": d, "ganhou": valores[campo] >= meta,
                       "atual": min(valores[campo], meta), "meta": meta,
                       "pct": min(100, round(valores[campo] * 100 / meta)) if meta else 100}
                      for c, n, d, campo, meta in CONQUISTAS]
        saida.append({"usuario": u, "pk": pk, "nome": u.first_name or u.username, "login": u.username,
                      "metricas": m, "pontos": pontos, "valor_nf": v.get("valor", Decimal("0")),
                      "sequencia": seq, "dias_ativos": len(r["dias"]), "taxa_conversa": taxa,
                      "nivel": nivel(pontos), "conquistas": conquistas,
                      "n_conquistas": sum(1 for c in conquistas if c["ganhou"]),
                      "atendimentos": len(set(r["etapas"]) | set(r["vendas_dias"])), "notas_nf": v.get("notas", 0),
                      # pontos por etapa máxima: "12 até orçamento = 48 pts"
                      "parcelas": [{"chave": k, "rotulo": ETAPAS[k], "qtd": topo[k], "pontos": topo[k] * PONTOS[k]}
                                   for k in reversed(list(PONTOS))]})
    saida.sort(key=lambda x: (-x["pontos"], -x["valor_nf"], -x["metricas"]["procuradas"], x["nome"].lower()))
    maximo = max([x["pontos"] for x in saida] + [1])
    for i, x in enumerate(saida):
        x["posicao"] = i + 1
        x["barra"] = round(x["pontos"] * 100 / maximo)
    return saida


def visao(inicio, fim, pessoa, pesquisa=None, hoje=None):
    """Placar + o recorte de quem está olhando: posição, distância para o próximo, comparação com a média."""
    ranking = placar(inicio, fim, pesquisa, hoje)
    eu = next((x for x in ranking if x["pk"] == pessoa.pk), None)
    ativos = [x for x in ranking if x["metricas"]["procuradas"]] or ranking
    media = {k: (sum(x["metricas"][k] for x in ativos) / len(ativos)) if ativos else 0 for k in PONTOS}
    alvo = None
    if eu is not None and eu["posicao"] > 1:
        acima = ranking[eu["posicao"] - 2]
        falta = acima["pontos"] - eu["pontos"] + 1
        alvo = {"texto": f"Faltam {falta} ponto{'s' if falta != 1 else ''} para passar {acima['nome']}",
                "falta": falta, "nome": acima["nome"],
                "dica": _dica(falta)}
    elif eu is not None and len(ranking) > 1:
        vant = eu["pontos"] - ranking[1]["pontos"]
        alvo = {"texto": f"Você lidera com {vant} ponto{'s' if vant != 1 else ''} de vantagem" if vant
                else "Empatado na liderança", "falta": 0, "nome": "", "dica": "Mantenha o ritmo para segurar o 1º lugar."}
    comparacao = []
    if eu is not None:
        for k in PONTOS:
            meu, med = eu["metricas"][k], media[k]
            comparacao.append({"rotulo": ROTULOS[k], "meu": meu, "media": round(med, 1),
                               "acima": meu > med, "igual": abs(meu - med) < 0.05,
                               "pct_meu": round(meu * 100 / max(meu, med, 1)),
                               "pct_media": round(med * 100 / max(meu, med, 1))})
    return {"ranking": ranking, "eu": eu, "alvo": alvo, "comparacao": comparacao, "participantes": len(ranking),
            "regras": [(REGRAS[k], PONTOS[k]) for k in PONTOS]}


REGRAS = {"procuradas": "Empresa procurada", "conversas": "Conversa efetiva", "cnpjs": "CNPJ confirmado com o cliente",
          "orcamentos": "Orçamento enviado", "vendas_nf": "Venda (cada dia de compra do cliente)"}


def _mes_atual(hoje=None):
    hoje = hoje or timezone.localdate()
    return bi.periodo_do_mes(f"{hoje.year}-{hoje.month:02d}")[:2]


def foto(usuario, hoje=None):
    """Pontos e posição de agora (antes de registrar um contato), para medir o ganho depois."""
    inicio, fim = _mes_atual(hoje)
    eu = visao(inicio, fim, usuario, hoje=hoje)["eu"]
    return {"pontos": eu["pontos"] if eu else 0, "posicao": eu["posicao"] if eu else None}


def mensagem_ganho(antes, usuario, resultado="", hoje=None):
    """Retorno imediato no atendimento: pontos ganhos, posição e quanto falta para o próximo."""
    inicio, fim = _mes_atual(hoje)
    v = visao(inicio, fim, usuario, hoje=hoje)
    eu = v["eu"]
    if eu is None:
        return ""
    ganho = eu["pontos"] - antes["pontos"]
    partes = []
    if ganho > 0:
        partes.append(f"+{ganho} ponto{'s' if ganho != 1 else ''} na disputa do mês ({eu['pontos']} no total).")
    else:
        partes.append("Este contato não mudou seus pontos: o lead já tinha chegado a essa etapa.")
    if antes["posicao"] and eu["posicao"] < antes["posicao"]:
        partes.append(f"Você subiu para {eu['posicao']}º de {v['participantes']}!")
    else:
        partes.append(f"Você está em {eu['posicao']}º de {v['participantes']}.")
    if v["alvo"] and v["alvo"]["falta"]:
        partes.append(v["alvo"]["texto"] + ".")
    if resultado == ResultadoAbordagem.CONVERSA_VENDA:
        partes.append(f"A venda soma {PONTOS['vendas_nf']} pontos por dia de compra, quando a nota fiscal chegar do Salesforce.")
    return " ".join(partes)


def _dica(falta):
    """Caminho mais curto até o próximo lugar. Como cada atendimento vale só a etapa mais alta, a venda é o atalho."""
    if falta <= 3:
        return "Uma nova conversa efetiva resolve."
    if falta <= 4:
        return "Um orçamento enviado para uma nova empresa resolve."
    if falta <= 30:
        return "Uma venda confirmada por nota fiscal resolve."
    return f"{-(-falta // 30)} venda(s) confirmada(s) por nota fiscal resolvem."
