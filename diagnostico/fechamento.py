"""Fechamento do diagnóstico (SOMENTE LEITURA): números finais a partir dos JSON + agregados de Lead."""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "diagnostico"))
import coletar as C  # noqa: E402
from carteira.services import cnpj as cnpj_srv  # noqa: E402

L = lambda n: json.load(open(C.DADOS / f"{n}.json", encoding="utf-8"))  # noqa: E731
contas, itens, datas, cts = L("contas"), L("itens_venda"), L("contas_datas_compra"), L("contatos")
ids = {c["Id"]: c for c in contas}
lbd = {r["Id"]: r.get("LastBuyDate__c") for r in datas}
compr = {it["Invoice__r"]["Customer__r"]["Id"] for it in itens} & set(ids)
valor = defaultdict(float)
for it in itens:
    valor[it["Invoice__r"]["Customer__r"]["Id"]] += it["ValorTotalFormula__c"] or 0
R = {}

sem = [i for i in ids if i not in compr]
R["sem_compra_detalhe"] = {
    "total": len(sem),
    "LastBuyDate_antes_2026_04": sum(1 for i in sem if lbd.get(i) and lbd[i] < "2026-04-01"),
    "LastBuyDate_no_periodo_sem_NF": sum(1 for i in sem if lbd.get(i) and lbd[i] >= "2026-04-01"),
    "sem_nenhuma_data_de_compra": sum(1 for i in sem if not lbd.get(i)),
    "sem_data_por_situacao": dict(Counter(ids[i]["ProtheusCustomerSituation__c"] for i in sem if not lbd.get(i))),
    "sem_data_por_ano_cadastro": sorted(Counter(ids[i]["CreatedDate"][:4] for i in sem if not lbd.get(i)).items()),
    "antigos_por_ano_ultima_compra": sorted(Counter(lbd[i][:4] for i in sem if lbd.get(i) and lbd[i] < "2026-04-01").items()),
}
R["cadastro_por_ano_todas"] = sorted(Counter(c["CreatedDate"][:4] for c in contas).items())
print("sem compra:", R["sem_compra_detalhe"])
print("cadastro no SF por ano:", R["cadastro_por_ano_todas"])


def perfil(campo, top=15):
    agg = defaultdict(lambda: [0, 0, 0.0])
    for c in contas:
        k = c.get(campo) or "(vazio)"
        agg[k][0] += 1
        if c["Id"] in compr:
            agg[k][1] += 1
            agg[k][2] += valor[c["Id"]]
    return sorted([[k, a, b, round(100 * b / a, 1), round(v, 2)] for k, (a, b, v) in agg.items()], key=lambda x: -x[4])[:top]


for campo in ("Metier__c", "BillingState__c", "ConsumptionClassification__c", "IMBP__c", "Zone__c", "IsGrainy__c"):
    R[f"perfil_{campo}"] = perfil(campo)
    print(f"\nperfil {campo} [valor, contas, compradores, %, vendas]:")
    for linha in R[f"perfil_{campo}"][:12]:
        print("  ", linha)

seg_raw = Counter(c.get("Segment__c") for c in contas)
R["segmentos_sem_codigo"] = {k: v for k, v in seg_raw.items() if k and not re.match(r"^\d{3}-", k)}
R["segmentos_distintos"] = len(seg_raw)
print("\nsegmentos distintos:", len(seg_raw), "sem código numérico:", R["segmentos_sem_codigo"])

mails = [(c.get("Email__c") or "").lower() for c in contas]
fiscal = sum(1 for m in mails if re.match(r"^(nf|nfe|nf-e|xml|fiscal|financeiro|financ|cobranca|faturamento|contab|contabilidade)", m))
R["email_fiscal_ou_financeiro"] = fiscal
print("\nEmail__c com prefixo fiscal/financeiro (nf, xml, financeiro...):", fiscal)

ct_por_conta = Counter(x["AccountId"] for x in cts if x["AccountId"] in ids)
R["contatos"] = {"contas_com_contato": len(ct_por_conta), "compradores_com_contato": sum(1 for i in ct_por_conta if i in compr),
                 "max_por_conta": max(ct_por_conta.values(), default=0),
                 "distribuicao": sorted(Counter(ct_por_conta.values()).items())[:8],
                 "contatos_com_telefone_ou_email": sum(1 for x in cts if x["AccountId"] in ids and (x.get("Phone") or x.get("MobilePhone") or x.get("Email")))}
print("\ncontatos:", R["contatos"])

sf = C.conectar()
try:
    agg = sf.query_all("SELECT Status s, IsConverted c, COUNT(Id) n FROM Lead WHERE Distributor__r.Name LIKE '%BRIDA%' "
                       "GROUP BY Status, IsConverted")["records"]
    R["lead_brida_status"] = [[r["s"], r["c"], r["n"]] for r in agg]
    tot = sum(r["n"] for r in agg)
    dist = sf.query_all("SELECT Distributor__r.Name d, COUNT(Id) n FROM Lead GROUP BY Distributor__r.Name")["records"]
    R["lead_por_distribuidor"] = sorted([[r["d"], r["n"]] for r in dist], key=lambda x: -x[1])[:10]
    print("\nLead BRIDA total:", tot, R["lead_brida_status"])
    print("Lead por distribuidor (top):", R["lead_por_distribuidor"])
    leads = C.limpar(sf.query_all("SELECT CNPJ__c, IsConverted, ConvertedAccountId FROM Lead "
                                  "WHERE Distributor__r.Name LIKE '%BRIDA%'")["records"])
    cnpj_contas = {cnpj_srv.analisar(c.get("CNPJ__c")).cnpj for c in contas} - {None}
    lc = [cnpj_srv.analisar(x.get("CNPJ__c")).cnpj for x in leads]
    validos = [x for x in lc if x]
    R["lead_brida_cnpj"] = {"leads": len(leads), "cnpj_valido": len(validos), "cnpj_distintos": len(set(validos)),
                            "cnpj_ja_e_conta": len(set(validos) & cnpj_contas),
                            "convertidos_em_conta_extraida": sum(1 for x in leads if x.get("ConvertedAccountId") in ids)}
    print("Lead BRIDA x contas:", R["lead_brida_cnpj"])
except Exception as e:
    print("!! Lead:", e)

with open(C.SAIDA / "relatorio_fechamento.json", "w", encoding="utf-8") as f:
    json.dump(R, f, ensure_ascii=False, indent=1, default=str)
