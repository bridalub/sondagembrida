"""
Segunda etapa do diagnóstico (SOMENTE LEITURA): qualidade de telefone/e-mail/nome fantasia,
datas de última compra na Account, CFOPs e concentração, conferência cabeçalho x itens,
objetos PhisicalPerson__c / Lead / AccountContactRole. Usa os JSON salvos por coletar.py.
"""
import json
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "diagnostico"))
import coletar as C  # noqa: E402  (reaproveita conexão, carga do .env e django.setup)

DADOS = C.DADOS
R = {}


def carregar(nome):
    with open(DADOS / f"{nome}.json", encoding="utf-8") as f:
        return json.load(f)


def p(msg=""):
    print(msg)


def pct(a, b):
    return f"{100 * a / b:.1f}%" if b else "-"


def so_digitos(v):
    return re.sub(r"\D", "", v or "")


contas = carregar("contas")
n = len(contas)
ids = {c["Id"]: c for c in contas}
sf = C.conectar()

# ---------------- telefone / e-mail / fantasia
p("== Qualidade Phone / Email__c / FantasyName__c / StateRegistration__c")
fones = [so_digitos(c.get("Phone")) for c in contas]
cnt_fone = Counter(fones)
validos = [f for f in fones if 10 <= len(f.lstrip("0")) <= 13 and len(set(f)) > 2]
R["phone"] = {"preenchidos": sum(1 for f in fones if f), "plausiveis_10a13_digitos": len(validos),
              "distintos": len(set(validos)), "mais_repetidos_mascarados":
              [(f[:2] + "*" * max(0, len(f) - 4) + f[-2:], k) for f, k in cnt_fone.most_common(6)],
              "comprimentos": Counter(len(f) for f in fones).most_common(8)}
p(f"  Phone: {R['phone']}")
emails = [(c.get("Email__c") or "").strip().lower() for c in contas]
cnt_mail = Counter(e for e in emails if e)
formato_ok = [e for e in emails if re.fullmatch(r"[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+", e)]
multiplos = sum(1 for e in emails if re.search(r"[;,\s]", e))
dominios = Counter(e.split("@")[-1] for e in formato_ok)
R["email"] = {"preenchidos": len([e for e in emails if e]), "formato_valido_unico": len(formato_ok),
              "com_varios_emails": multiplos, "distintos": len(set(formato_ok)),
              "repetidos_em_mais_de_5_contas": sum(1 for e, k in cnt_mail.items() if k > 5),
              "top_dominios": dominios.most_common(10),
              "mais_repetidos_mascarados": [(re.sub(r"^[^@]{0,}", lambda m: m.group(0)[:2] + "***", e), k)
                                            for e, k in cnt_mail.most_common(6)]}
p(f"  Email__c: {R['email']}")
fant = [(c.get("FantasyName__c") or "").strip() for c in contas]
igual_nome = sum(1 for c, f in zip(contas, fant) if f and f.upper() == (c.get("Name") or "").strip().upper())
ltda_nome = sum(1 for c in contas if re.search(r"\b(LTDA|S/?A|EIRELI|ME|EPP|MEI)\b", (c.get("Name") or "").upper()))
R["nomes"] = {"fantasia_preenchida": sum(1 for f in fant if f), "fantasia_igual_ao_name": igual_nome,
              "name_com_sufixo_societario": ltda_nome}
p(f"  Nomes: {R['nomes']}  -> Name tem cara de razão social em {pct(ltda_nome, n)}")

# ---------------- datas de última compra na Account (indício de histórico anterior)
p("\n== Datas de última compra / visita na Account")
campos_data = ["LastBuyDate__c", "LastBuyDateTotalizer__c", "DateLastVisit__c"]
recs = C.limpar(sf.query_all("SELECT Id, " + ", ".join(campos_data) + " FROM Account WHERE Distributor__r.Name LIKE '%BRIDA%' "
                             "AND ProtheusCustomerSituation__c IN ('Ativo','Inativo')")["records"])
C.salvar("contas_datas_compra", recs)
itens = carregar("itens_venda")
ult_nf = {}
for it in itens:
    cli = it["Invoice__r"]["Customer__r"]["Id"]
    d = it["EmissionDate__c"][:10]
    if cli not in ult_nf or d > ult_nf[cli]:
        ult_nf[cli] = d
R["datas_account"] = {}
for campo in campos_data:
    vals = [(r["Id"], r.get(campo)) for r in recs]
    pre = [(i, v) for i, v in vals if v]
    anos = Counter(v[:4] for _, v in pre)
    antes = [(i, v) for i, v in pre if v < "2026-04-01"]
    sem_compra_periodo_com_data_antiga = sum(1 for i, v in antes if i not in ult_nf)
    concord = sum(1 for i, v in pre if i in ult_nf and v[:10] == ult_nf[i])
    depois_nf = sum(1 for i, v in pre if i in ult_nf and v[:10] > ult_nf[i])
    antes_nf = sum(1 for i, v in pre if i in ult_nf and v[:10] < ult_nf[i])
    compr_sem_data = sum(1 for i in ult_nf if i in ids and not dict(vals).get(i))
    R["datas_account"][campo] = {
        "preenchidos": len(pre), "pct": pct(len(pre), n), "min": min((v for _, v in pre), default=None),
        "max": max((v for _, v in pre), default=None), "por_ano": sorted(anos.items()),
        "anteriores_a_2026_04": len(antes), "sem_nf_no_periodo_mas_com_data_antiga": sem_compra_periodo_com_data_antiga,
        "compradores_data_igual_ultima_nf": concord, "data_posterior_a_ultima_nf": depois_nf,
        "data_anterior_a_ultima_nf": antes_nf, "compradores_sem_esta_data": compr_sem_data,
    }
    p(f"  {campo}: {R['datas_account'][campo]}")

# ---------------- CFOP e concentração
p("\n== CFOPs das vendas (valor) e concentração")
por_cfop = defaultdict(lambda: [0, Decimal(0)])
por_cli = defaultdict(Decimal)
for it in itens:
    v = Decimal(str(it["ValorTotalFormula__c"] or 0))
    por_cfop[it["CodeFiscalOperation__c"]][0] += 1
    por_cfop[it["CodeFiscalOperation__c"]][1] += v
    por_cli[it["Invoice__r"]["Customer__r"]["Id"]] += v
total = sum(por_cli.values())
R["cfop"] = sorted([[k, a, float(b), pct(b, total)] for k, (a, b) in por_cfop.items()], key=lambda x: -x[2])
for linha in R["cfop"][:15]:
    p(f"  CFOP {linha[0]}: itens={linha[1]} valor={linha[2]:,.2f} ({linha[3]})")
top = sorted(por_cli.items(), key=lambda x: -x[1])
R["concentracao"] = {"top10_pct": pct(sum(v for _, v in top[:10]), total), "top100_pct": pct(sum(v for _, v in top[:100]), total),
                     "top10": [[ids.get(k, {}).get("Name", "(fora da extração)"), ids.get(k, {}).get("Segment__c"), float(v)]
                               for k, v in top[:10]]}
p(f"  concentração: top10={R['concentracao']['top10_pct']} top100={R['concentracao']['top100_pct']}")
for nome, seg, v in R["concentracao"]["top10"]:
    p(f"    {nome[:45]:<45} {str(seg)[:28]:<28} {v:>15,.2f}")
# CFOPs 5922/6922 (simples faturamento entrega futura) — risco de dupla contagem com remessa
fut = {k: v for k, v in por_cfop.items() if k in ("5922", "6922")}
p(f"  CFOP 5922/6922 (venda p/ entrega futura): {[(k, a, float(b)) for k, (a, b) in fut.items()]}")
try:
    rem = sf.query("SELECT COUNT() FROM InvoiceItem__c WHERE Invoice__r.Distributor__r.Name LIKE '%BRIDA%' "
                   "AND CodeFiscalOperation__c IN ('5116','5117','6116','6117') AND EmissionDate__c >= 2026-04-01")["totalSize"]
    R["remessa_entrega_futura_itens"] = rem
    p(f"  itens com CFOP de remessa de entrega futura (5116/5117/6116/6117, fora da regra): {rem}")
except Exception as e:
    p(f"  !! remessa: {e}")

# CFOPs existentes em Saída fora da lista (o que a regra exclui)
try:
    agg = sf.query_all("SELECT CodeFiscalOperation__c c, COUNT(Id) n, SUM(ItemGrossValue__c) v FROM InvoiceItem__c "
                       "WHERE Invoice__r.Distributor__r.Name LIKE '%BRIDA%' AND Invoice__r.Type__c = 'Saída' "
                       "AND Invoice__r.Status__c != 'Cancelada' AND Invoice__r.IsInvoiceCanceled__c = false "
                       "AND EmissionDate__c >= 2026-04-01 GROUP BY CodeFiscalOperation__c")["records"]
    lista = set(C.CFOPS_VENDAS)
    fora = sorted([[r["c"], r["n"], float(r["v"] or 0)] for r in agg if r["c"] not in lista], key=lambda x: -x[2])
    R["saida_cfops_excluidos"] = fora
    p("  Saídas com CFOP fora da regra (excluídas): " + "; ".join(f"{c}: {k} itens R$ {v:,.0f}" for c, k, v in fora[:12]))
except Exception as e:
    p(f"  !! CFOPs excluídos: {e}")

# ---------------- cabeçalho x itens
p("\n== Conferência cabeçalho da nota (InvoiceGrossValue__c / CommodityValue__c) x soma dos itens")
notas = defaultdict(lambda: Decimal(0))
for it in itens:
    notas[it["Invoice__r"]["Id"]] += Decimal(str(it["ValorTotalFormula__c"] or 0))
amostra = list(notas)[:: max(1, len(notas) // 200)][:200]
cab = {}
for i in range(0, len(amostra), 200):
    lote = ",".join(f"'{x}'" for x in amostra[i:i + 200])
    for r in sf.query_all(f"SELECT Id, InvoiceGrossValue__c, CommodityValue__c FROM Invoice__c WHERE Id IN ({lote})")["records"]:
        cab[r["Id"]] = r
iguais_bruto = sum(1 for k in amostra if k in cab and cab[k].get("InvoiceGrossValue__c") is not None
                   and abs(Decimal(str(cab[k]["InvoiceGrossValue__c"])) - notas[k]) < Decimal("0.05"))
iguais_merc = sum(1 for k in amostra if k in cab and cab[k].get("CommodityValue__c") is not None
                  and abs(Decimal(str(cab[k]["CommodityValue__c"])) - notas[k]) < Decimal("0.05"))
R["cabecalho_x_itens"] = {"amostra_notas": len(amostra), "soma_itens_igual_valor_bruto_nf": iguais_bruto,
                          "soma_itens_igual_valor_mercadoria_nf": iguais_merc}
p(f"  {R['cabecalho_x_itens']}")
dif = [(k, float(notas[k]), cab[k].get("InvoiceGrossValue__c"), cab[k].get("CommodityValue__c")) for k in amostra[:5] if k in cab]
p(f"  exemplos (soma itens, bruto NF, mercadoria NF): {[(a, b, c) for _, a, b, c in dif]}")

# ---------------- CNPJs repetidos
p("\n== CNPJs repetidos (22): mesmos dados?")
por = defaultdict(list)
from carteira.services import cnpj as cnpj_srv  # noqa: E402
for c in contas:
    r = cnpj_srv.analisar(c.get("CNPJ__c"))
    if r.cnpj:
        por[r.cnpj].append(c)
rep = {k: v for k, v in por.items() if len(v) > 1}
mesmo_nome = sum(1 for v in rep.values() if len({(x.get("Name") or "").upper() for x in v}) == 1)
sit = Counter(tuple(sorted(x.get("ProtheusCustomerSituation__c") for x in v)) for v in rep.values())
compra = Counter(sum(1 for x in v if x["Id"] in ult_nf) for v in rep.values())
R["cnpj_repetidos"] = {"total": len(rep), "mesmo_nome": mesmo_nome, "situacoes": {str(k): v for k, v in sit.items()},
                       "contas_com_compra_por_grupo": dict(compra)}
p(f"  {R['cnpj_repetidos']}")
inval = [c for c in contas if c.get("CNPJ__c") and not cnpj_srv.analisar(c.get("CNPJ__c")).cnpj]
R["cnpj_invalidos_tamanhos"] = Counter(len(so_digitos(c["CNPJ__c"])) for c in inval).most_common()
R["cnpj_invalidos_iguais_codigo"] = sum(1 for c in inval if c["CNPJ__c"].strip() == (c.get("Code__c") or "").strip())
p(f"  inválidos por nº de dígitos: {R['cnpj_invalidos_tamanhos']}; CNPJ = código do cliente: {R['cnpj_invalidos_iguais_codigo']}")

# ---------------- outros objetos candidatos
p("\n== PhisicalPerson__c / Lead / AccountContactRole")
for obj in ("PhisicalPerson__c", "Lead", "AccountContactRole"):
    try:
        campos = C.descrever(sf, obj)
        tot = sf.query(f"SELECT COUNT() FROM {obj}")["totalSize"]
        refs = [f"{f['name']}->{f['referenceTo']}" for f in campos if f["type"] == "reference"][:15]
        cand = [f"{f['name']} ({f['label']})" for f in C.candidatos(campos)][:25]
        extra = [f"{f['name']} ({f['label']}, {f['type']})" for f in campos
                 if re.search(r"cnae|abertura|opening|funda|cnpj|cpf|cargo|title|role|papel", f["name"] + " " + (f["label"] or ""), re.I)]
        R[obj] = {"registros": tot, "referencias": refs, "candidatos": cand, "outros": extra}
        p(f"  {obj}: registros={tot}\n    refs={refs}\n    candidatos={cand}\n    outros={extra}")
    except Exception as e:
        R[obj] = {"erro": str(e)}
        p(f"  {obj}: sem acesso ({type(e).__name__}: {str(e)[:150]})")

# preenchimento de PhisicalPerson__c, se ligado a Account
try:
    campos = {f["name"]: f for f in json.load(open(DADOS / "describe_PhisicalPerson__c.json", encoding="utf-8"))}
    ref_acc = [k for k, f in campos.items() if f["type"] == "reference" and "Account" in (f["referenceTo"] or [])]
    if ref_acc and R["PhisicalPerson__c"]["registros"]:
        sel = ["Id", "Name"] + ref_acc + [k for k, f in campos.items() if f["type"] in ("phone", "email")
                                          or re.search(r"cargo|title|role|fun[cç]", k + (f["label"] or ""), re.I)][:10]
        pp = C.limpar(sf.query_all(f"SELECT {', '.join(dict.fromkeys(sel))} FROM PhisicalPerson__c")["records"])
        lig = Counter()
        for x in pp:
            for k in ref_acc:
                if x.get(k) in ids:
                    lig[k] += 1
        R["PhisicalPerson__c"]["preenchimento"] = {k: sum(1 for x in pp if C.preenchido(x.get(k))) for k in dict.fromkeys(sel)}
        R["PhisicalPerson__c"]["ligados_contas_extraidas"] = dict(lig)
        p(f"  PhisicalPerson__c preenchimento: {R['PhisicalPerson__c']['preenchimento']}; ligados: {dict(lig)}")
except Exception as e:
    p(f"  !! PhisicalPerson__c detalhe: {e}")

with open(C.SAIDA / "relatorio_aprofundado.json", "w", encoding="utf-8") as f:
    json.dump(R, f, ensure_ascii=False, indent=1, default=str)
p("\nOK")
