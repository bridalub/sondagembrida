"""
Diagnóstico da base real do Salesforce — SOMENTE LEITURA.

Usa apenas describe() e query()/query_all() (SELECT). Não grava nada no Salesforce,
não envia e-mail, não chama o loop do Excel.py.

Credenciais: lidas do ambiente ou de .env / env.env na raiz do projeto. Nunca impressas.
Saídas (locais, ignoradas pelo git):
  diagnostico/dados/*.json   -> dados brutos extraídos
  diagnostico/saida/relatorio.json e relatorio.txt -> números do diagnóstico

Uso:  python -I diagnostico/coletar.py [--desde 2026-04-01]
"""
import argparse
import json
import os
import re
import sys
import traceback
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
DADOS = RAIZ / "diagnostico" / "dados"
SAIDA = RAIZ / "diagnostico" / "saida"
DADOS.mkdir(parents=True, exist_ok=True)
SAIDA.mkdir(parents=True, exist_ok=True)

from dotenv import load_dotenv  # noqa: E402

for nome in (".env", "env.env"):
    if (RAIZ / nome).exists():
        load_dotenv(RAIZ / nome, override=False)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DB_ENGINE", "sqlite")
os.environ.setdefault("DJANGO_DEBUG", "1")
import django  # noqa: E402

django.setup()
from carteira.services import cnpj as cnpj_srv  # noqa: E402
from carteira.services.salesforce import CFOPS_VENDAS  # noqa: E402

DIST = "%BRIDA%"
REL = {"execucao": {"inicio": datetime.now().isoformat(timespec="seconds"), "falhas": []}}
TXT = []


def out(msg=""):
    print(msg)
    TXT.append(str(msg))


def falha(etapa, e):
    REL["execucao"]["falhas"].append({"etapa": etapa, "erro": f"{type(e).__name__}: {e}"})
    out(f"  !! FALHA em {etapa}: {type(e).__name__}: {e}")


def salvar(nome, obj):
    with open(DADOS / f"{nome}.json", "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, default=str)


def limpar(recs):
    def s(o):
        if isinstance(o, dict):
            return {k: s(v) for k, v in o.items() if k != "attributes"}
        return o
    return [s(r) for r in recs]


def pct(n, d):
    return f"{(100 * n / d):.1f}%" if d else "-"


def preenchido(v):
    return not (v is None or (isinstance(v, str) and v.strip() == ""))


def conectar():
    from simple_salesforce import Salesforce
    faltam = [v for v in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN") if not os.getenv(v)]
    if faltam:
        raise RuntimeError("Variáveis ausentes: " + ", ".join(faltam))
    kw = dict(username=os.environ["SF_USERNAME"], password=os.environ["SF_PASSWORD"],
              security_token=os.environ["SF_SECURITY_TOKEN"])
    if os.getenv("SF_INSTANCE_URL"):
        kw["instance_url"] = os.environ["SF_INSTANCE_URL"]
    if os.getenv("SF_DOMAIN"):
        kw["domain"] = os.environ["SF_DOMAIN"]
    return Salesforce(**kw)


# ------------------------------------------------------------------ metadados

PADRAO_CANDIDATO = re.compile(
    r"cnae|abertura|opening|funda[cç]|foundation|incorporat|fantasia|trade.?name|raz[aã]o|legal.?name|"
    r"phone|telefone|fone|celular|mobile|whats|e.?mail|segment|ramo|metier|atividade|activity|porte|"
    r"capital|natureza|situa[cç][aã]o.?cadastral|inscri", re.I)


def descrever(sf, objeto):
    d = getattr(sf, objeto).describe()
    campos = [{"name": f["name"], "label": f.get("label"), "type": f["type"],
               "relationshipName": f.get("relationshipName"), "referenceTo": f.get("referenceTo"),
               "calculated": f.get("calculated"), "aggregatable": f.get("aggregatable")} for f in d["fields"]]
    salvar(f"describe_{objeto}", campos)
    return campos


def candidatos(campos):
    return [f for f in campos if f["type"] not in ("reference", "id", "base64", "address", "location")
            and (PADRAO_CANDIDATO.search(f["name"]) or PADRAO_CANDIDATO.search(f["label"] or ""))]


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default="2026-04-01")
    args = ap.parse_args()
    desde = date.fromisoformat(args.desde)

    out("== Conexão")
    try:
        sf = conectar()
    except Exception as e:
        falha("conexão", e)
        return finalizar()
    out(f"  conectado (instância {sf.sf_instance}, API {sf.sf_version})")
    REL["execucao"]["instancia"] = sf.sf_instance

    # ---------- objetos acessíveis relevantes
    out("\n== Objetos acessíveis (busca por contato/CNAE/empresa)")
    try:
        globais = sf.describe()["sobjects"]
        rel = [f"{o['name']} ({o['label']})" for o in globais if o.get("queryable") and re.search(
            r"contact|contato|pessoa|cnae|receita|empresa|company|lead", o["name"] + " " + (o.get("label") or ""), re.I)]
        REL["objetos_candidatos"] = rel
        out("  " + "; ".join(rel[:60]))
    except Exception as e:
        falha("describe global", e)

    # ---------- Account metadados
    out("\n== Account: campos candidatos (describe)")
    acc_campos = descrever(sf, "Account")
    nomes_acc = {f["name"] for f in acc_campos}
    cand_acc = candidatos(acc_campos)
    REL["account_candidatos"] = [f"{f['name']} | {f['label']} | {f['type']}" for f in cand_acc]
    for f in cand_acc:
        out(f"  {f['name']:<45} {f['label']:<45} {f['type']}")
    datas_acc = [f"{f['name']} | {f['label']} | {f['type']}" for f in acc_campos if f["type"] in ("date", "datetime")]
    REL["account_campos_data"] = datas_acc
    out("  campos de data: " + "; ".join(datas_acc))

    # ---------- efeito dos filtros
    out("\n== Contas por situação Protheus (distribuidor BRIDA, sem filtro de situação)")
    try:
        agg = sf.query_all(f"SELECT ProtheusCustomerSituation__c s, COUNT(Id) n FROM Account "
                           f"WHERE Distributor__r.Name LIKE '{DIST}' GROUP BY ProtheusCustomerSituation__c")["records"]
        por_sit = {str(r["s"]): r["n"] for r in agg}
        REL["contas_por_situacao_brida"] = por_sit
        for k, v in sorted(por_sit.items(), key=lambda x: -x[1]):
            out(f"  {k:<20} {v}")
        tot_acc = sf.query("SELECT COUNT() FROM Account")["totalSize"]
        sem_dist = sf.query("SELECT COUNT() FROM Account WHERE Distributor__c = null")["totalSize"]
        REL["contas_total_org"] = tot_acc
        REL["contas_sem_distribuidor"] = sem_dist
        out(f"  total de contas visíveis na org: {tot_acc}; sem distribuidor: {sem_dist}")
    except Exception as e:
        falha("contagem por situação", e)

    # ---------- extração de contas (mesmo filtro do Excel.py + Id + candidatos)
    out("\n== Extração de contas (filtro do Excel.py: BRIDA + Ativo/Inativo)")
    base_campos = ["Id", "CNPJ__c", "CustomizedID__c", "IsGrainy__c", "Code__c", "Name", "CoordinatorFormula__c",
                   "Seller__r.Name", "Zone__c", "Metier__c", "IMBP__c", "Segment__c", "Subsegment__c",
                   "BillingPostalCode", "BillingCity__c", "BillingState__c", "ShippingCity__c", "ShippingState__c",
                   "ProtheusCustomerSituation__c", "ConsumptionClassification__c", "CreatedDate", "LastModifiedDate",
                   "Distributor__r.Name"]
    extras = [f["name"] for f in cand_acc if f["name"] not in base_campos][:40]
    campos_q = base_campos + extras
    try:
        contas = limpar(sf.query_all(
            f"SELECT {', '.join(campos_q)} FROM Account WHERE Distributor__r.Name LIKE '{DIST}' "
            f"AND ProtheusCustomerSituation__c IN ('Ativo','Inativo')")["records"])
    except Exception as e:
        falha("extração de contas", e)
        return finalizar()
    salvar("contas", contas)
    n = len(contas)
    REL["contas_extraidas"] = n
    out(f"  contas retornadas: {n}")

    # ---------- CNPJ
    out("\n== CNPJ")
    st = Counter()
    por_cnpj = defaultdict(list)
    restaurados = 0
    for c in contas:
        r = cnpj_srv.analisar(c.get("CNPJ__c"))
        st[str(r.status)] += 1
        if r.cnpj:
            por_cnpj[r.cnpj].append(c["Id"])
            if r.aviso:
                restaurados += 1
        c["_cnpj"] = r.cnpj
        c["_cnpj_status"] = str(r.status)
    repetidos = {k: v for k, v in por_cnpj.items() if len(v) > 1}
    alfanum = sum(1 for k in por_cnpj if not k.isdigit())
    matriz = sum(1 for k in por_cnpj if k[8:12] == "0001")
    REL["cnpj"] = {
        "status": dict(st), "distintos_validos": len(por_cnpj), "repetidos_cnpjs": len(repetidos),
        "repetidos_contas": sum(len(v) for v in repetidos.values()), "zeros_restaurados": restaurados,
        "alfanumericos": alfanum, "matriz": matriz, "filial": len(por_cnpj) - matriz,
        "raizes_distintas": len({k[:8] for k in por_cnpj}),
        "max_contas_mesmo_cnpj": max((len(v) for v in repetidos.values()), default=0),
    }
    for k, v in REL["cnpj"].items():
        out(f"  {k}: {v}")
    exemplos_invalidos = Counter(str(c.get("CNPJ__c")) for c in contas if c["_cnpj_status"] in ("INVALIDO", "CPF"))
    REL["cnpj"]["exemplos_formato_invalido"] = [re.sub(r"\d", "9", k) for k, _ in exemplos_invalidos.most_common(8)]
    out(f"  padrões de valores inválidos (dígitos mascarados): {REL['cnpj']['exemplos_formato_invalido']}")

    # ---------- preenchimento
    out("\n== Preenchimento dos campos (contas extraídas)")

    def get(c, campo):
        v = c
        for p in campo.split("."):
            v = v.get(p) if isinstance(v, dict) else None
        return v
    preench = {}
    for campo in campos_q:
        k = sum(1 for c in contas if preenchido(get(c, campo)))
        preench[campo] = {"preenchidos": k, "pct": round(100 * k / n, 1) if n else 0}
        out(f"  {campo:<45} {k:>7}  {pct(k, n)}")
    REL["preenchimento_account"] = preench
    # valores distintos dos candidatos (amostra de domínio p/ entender significado)
    dominios = {}
    for campo in ["Segment__c", "Subsegment__c", "Metier__c", "IMBP__c", "ConsumptionClassification__c", "Zone__c",
                  "ProtheusCustomerSituation__c"] + [f["name"] for f in cand_acc if f["type"] in ("picklist", "boolean")]:
        if campo in nomes_acc:
            dominios[campo] = Counter(str(get(c, campo)) for c in contas).most_common(40)
    REL["dominios"] = dominios

    # ---------- Contatos
    out("\n== Pessoas de contato (Contact)")
    try:
        ct_campos = descrever(sf, "Contact")
        nomes_ct = {f["name"] for f in ct_campos}
        REL["contact_candidatos"] = [f"{f['name']} | {f['label']} | {f['type']}" for f in candidatos(ct_campos)]
        tot_ct = sf.query("SELECT COUNT() FROM Contact")["totalSize"]
        brida_ct = sf.query(f"SELECT COUNT() FROM Contact WHERE Account.Distributor__r.Name LIKE '{DIST}'")["totalSize"]
        out(f"  contatos visíveis: {tot_ct}; ligados a contas BRIDA: {brida_ct}")
        qcampos = [c for c in ["Id", "AccountId", "Name", "Title", "Phone", "MobilePhone", "Email", "CreatedDate"]
                   if c in nomes_ct]
        extras_ct = [f["name"] for f in candidatos(ct_campos) if f["name"] not in qcampos][:15]
        cts = limpar(sf.query_all(f"SELECT {', '.join(qcampos + extras_ct)} FROM Contact "
                                  f"WHERE Account.Distributor__r.Name LIKE '{DIST}'")["records"]) if brida_ct else []
        salvar("contatos", cts)
        ids_contas = {c["Id"] for c in contas}
        ligados = [x for x in cts if x.get("AccountId") in ids_contas]
        contas_com_ct = len({x["AccountId"] for x in ligados})
        REL["contatos"] = {"visiveis_org": tot_ct, "brida": brida_ct, "ligados_contas_extraidas": len(ligados),
                           "contas_com_contato": contas_com_ct,
                           "preenchimento": {c: sum(1 for x in cts if preenchido(x.get(c))) for c in qcampos + extras_ct}}
        out(f"  contas extraídas com ao menos 1 contato: {contas_com_ct} ({pct(contas_com_ct, n)})")
        for c, k in REL["contatos"]["preenchimento"].items():
            out(f"  {c:<30} {k:>7}  {pct(k, len(cts))}")
    except Exception as e:
        falha("Contact", e)

    # ---------- Notas: metadados e cobertura
    out("\n== Notas fiscais: metadados")
    try:
        inv_campos = descrever(sf, "Invoice__c")
        it_campos = descrever(sf, "InvoiceItem__c")
        tipos_it = {f["name"]: f for f in it_campos}
        for c in ("EmissionDate__c", "ValorTotalFormula__c", "ItemGrossValue__c", "CodeFiscalOperation__c",
                  "TypeofProdutct__c"):
            f = tipos_it.get(c)
            out(f"  InvoiceItem__c.{c}: " + (f"{f['label']} / {f['type']} / fórmula={f['calculated']}" if f else "NÃO EXISTE"))
        val_inv = [f"{f['name']} | {f['label']} | {f['type']}" for f in inv_campos
                   if f["type"] in ("currency", "double") and re.search(r"valor|total|amount|value", f["name"] + (f["label"] or ""), re.I)]
        REL["invoice_campos_valor"] = val_inv
        out("  Invoice__c campos de valor: " + "; ".join(val_inv))
    except Exception as e:
        falha("describe notas", e)

    filtro_saida = (f"Invoice__r.Distributor__r.Name LIKE '{DIST}' AND Invoice__r.Type__c = 'Saída' "
                    f"AND Invoice__r.Status__c != 'Cancelada' AND Invoice__r.IsInvoiceCanceled__c = false "
                    f"AND CodeFiscalOperation__c IN ({', '.join(repr(c) for c in CFOPS_VENDAS)})")
    out("\n== Cobertura histórica disponível na origem")
    try:
        antes = sf.query(f"SELECT COUNT() FROM InvoiceItem__c WHERE {filtro_saida} AND EmissionDate__c < {desde}")["totalSize"]
        prim = sf.query(f"SELECT EmissionDate__c FROM InvoiceItem__c WHERE {filtro_saida} AND EmissionDate__c != null "
                        f"ORDER BY EmissionDate__c ASC LIMIT 1")["records"]
        REL["cobertura_origem"] = {"itens_venda_antes_de_desde": antes,
                                   "primeira_emissao_existente": prim[0]["EmissionDate__c"] if prim else None}
        out(f"  itens de venda antes de {desde}: {antes}; primeira emissão existente: "
            f"{REL['cobertura_origem']['primeira_emissao_existente']}")
    except Exception as e:
        falha("cobertura", e)

    # ---------- extração de itens (venda e devolução)
    campos_it = ["Id", "EmissionDate__c", "ValorTotalFormula__c", "ItemGrossValue__c", "Quantity__c",
                 "TypeofProdutct__c", "CodeFiscalOperation__c", "Invoice__r.Id", "Invoice__r.Name",
                 "Invoice__r.Type__c", "Invoice__r.Status__c", "Invoice__r.Customer__r.Id"]
    itens = {}
    for tipo, filtro in (("VENDA", filtro_saida),
                         ("DEVOLUCAO", f"Invoice__r.Distributor__r.Name LIKE '{DIST}' AND Invoice__r.Type__c = 'Entrada' "
                                       f"AND Invoice__r.Status__c != 'Cancelada' AND Invoice__r.IsInvoiceCanceled__c = false")):
        out(f"\n== Extração de itens de nota: {tipo} desde {desde}")
        try:
            recs = limpar(sf.query_all(f"SELECT {', '.join(campos_it)} FROM InvoiceItem__c WHERE {filtro} "
                                       f"AND EmissionDate__c >= {desde}")["records"])
            itens[tipo] = recs
            salvar(f"itens_{tipo.lower()}", recs)
            out(f"  itens: {len(recs)}")
        except Exception as e:
            falha(f"itens {tipo}", e)
            itens[tipo] = []

    # ---------- agregação por nota e por conta
    ids_contas = {c["Id"]: c for c in contas}
    resumo_tipo = {}
    notas_por_tipo = {}
    for tipo, recs in itens.items():
        notas = {}
        ids_item = Counter(r["Id"] for r in recs)
        for r in recs:
            inv = r.get("Invoice__r") or {}
            n_ = notas.setdefault(inv.get("Id"), {"numero": inv.get("Name"), "cliente": (inv.get("Customer__r") or {}).get("Id"),
                                                 "datas": set(), "valor": Decimal(0), "bruto": Decimal(0), "itens": 0,
                                                 "cats": Counter()})
            n_["itens"] += 1
            if r.get("EmissionDate__c"):
                n_["datas"].add(r["EmissionDate__c"][:10])
            v = Decimal(str(r["ValorTotalFormula__c"])) if r.get("ValorTotalFormula__c") is not None else Decimal(0)
            n_["valor"] += v
            n_["bruto"] += Decimal(str(r["ItemGrossValue__c"])) if r.get("ItemGrossValue__c") is not None else Decimal(0)
            n_["cats"][r.get("TypeofProdutct__c") or "(sem tipo)"] += 1
        notas_por_tipo[tipo] = notas
        datas = sorted(d for x in notas.values() for d in x["datas"])
        sem_cliente = [k for k, x in notas.items() if not x["cliente"]]
        fora = [k for k, x in notas.items() if x["cliente"] and x["cliente"] not in ids_contas]
        resumo_tipo[tipo] = {
            "itens": len(recs), "itens_id_repetido": sum(1 for v in ids_item.values() if v > 1),
            "notas": len(notas), "notas_com_mais_de_uma_data": sum(1 for x in notas.values() if len(x["datas"]) > 1),
            "data_min": datas[0] if datas else None, "data_max": datas[-1] if datas else None,
            "valor_total": float(sum(x["valor"] for x in notas.values())),
            "valor_bruto_itens": float(sum(x["bruto"] for x in notas.values())),
            "notas_sem_cliente": len(sem_cliente),
            "notas_cliente_fora_da_extracao": len(fora),
            "valor_cliente_fora_da_extracao": float(sum(notas[k]["valor"] for k in fora)),
            "clientes_fora_da_extracao": len({notas[k]["cliente"] for k in fora}),
            "valor_nulo_itens": sum(1 for r in recs if r.get("ValorTotalFormula__c") is None),
        }
    REL["notas"] = resumo_tipo
    for tipo, r in resumo_tipo.items():
        out(f"\n== Resumo {tipo}")
        for k, v in r.items():
            out(f"  {k}: {v}")

    # situação das contas fora da extração (explica o não-vínculo)
    try:
        fora_ids = sorted({x["cliente"] for t in notas_por_tipo.values() for x in t.values()
                           if x["cliente"] and x["cliente"] not in ids_contas})
        sit_fora = Counter()
        dist_fora = Counter()
        for i in range(0, len(fora_ids), 300):
            lote = ",".join(f"'{x}'" for x in fora_ids[i:i + 300])
            for r in sf.query_all(f"SELECT ProtheusCustomerSituation__c, Distributor__r.Name FROM Account WHERE Id IN ({lote})")["records"]:
                sit_fora[str(r.get("ProtheusCustomerSituation__c"))] += 1
                dist_fora[str((r.get("Distributor__r") or {}).get("Name"))] += 1
        REL["contas_fora_situacao"] = dict(sit_fora)
        REL["contas_fora_distribuidor"] = dict(dist_fora)
        out(f"\n  contas das notas não vinculadas por situação Protheus: {dict(sit_fora)}")
        out(f"  ... por distribuidor: {dict(dist_fora)}")
    except Exception as e:
        falha("contas fora da extração", e)

    # ---------- classificação por conta
    vendas = notas_por_tipo.get("VENDA", {})
    devol = notas_por_tipo.get("DEVOLUCAO", {})
    por_conta = defaultdict(lambda: {"notas": 0, "valor": Decimal(0), "prim": None, "ult": None,
                                     "dev_notas": 0, "dev_valor": Decimal(0), "cats": Counter()})
    for x in vendas.values():
        if x["cliente"] in ids_contas:
            p = por_conta[x["cliente"]]
            p["notas"] += 1
            p["valor"] += x["valor"]
            d = min(x["datas"]) if x["datas"] else None
            if d:
                p["prim"] = d if not p["prim"] or d < p["prim"] else p["prim"]
                p["ult"] = d if not p["ult"] or d > p["ult"] else p["ult"]
            p["cats"].update(x["cats"].keys())
    for x in devol.values():
        if x["cliente"] in ids_contas:
            por_conta[x["cliente"]]["dev_notas"] += 1
            por_conta[x["cliente"]]["dev_valor"] += x["valor"]
    compradores = {k for k, v in por_conta.items() if v["notas"] > 0}
    sem_compra = [c for c in contas if c["Id"] not in compradores]
    # nível CNPJ (chave de negócio)
    cnpj_compr = {c["_cnpj"] for c in contas if c["Id"] in compradores and c["_cnpj"]}
    REL["classificacao"] = {
        "contas_com_compra_comprovada": len(compradores),
        "contas_sem_compra_no_periodo": len(sem_compra),
        "contas_nao_classificaveis": 0 if resumo_tipo.get("VENDA") else n,
        "observacao_nao_classificaveis": "Toda conta extraída tem Id e a cobertura (vendas desde a data) foi consultada; "
                                         "nenhuma conta fica sem classificação no nível Account.",
        "cnpjs_validos_com_compra": len(cnpj_compr),
        "cnpjs_validos_sem_compra": len(por_cnpj) - len(cnpj_compr),
        "compradores_sem_cnpj_valido": sum(1 for c in contas if c["Id"] in compradores and not c["_cnpj"]),
        "contas_com_devolucao": sum(1 for v in por_conta.values() if v["dev_notas"]),
        "contas_so_devolucao_sem_venda": sum(1 for v in por_conta.values() if v["dev_notas"] and not v["notas"]),
    }
    out("\n== Classificação (cobertura: vendas desde " + str(desde) + ")")
    for k, v in REL["classificacao"].items():
        out(f"  {k}: {v}")
    sem_compra_sit = Counter(c.get("ProtheusCustomerSituation__c") for c in sem_compra)
    compr_sit = Counter(ids_contas[k].get("ProtheusCustomerSituation__c") for k in compradores)
    REL["classificacao"]["compradores_por_situacao"] = dict(compr_sit)
    REL["classificacao"]["sem_compra_por_situacao"] = dict(sem_compra_sit)
    out(f"  compradores por situação Protheus: {dict(compr_sit)}; sem compra: {dict(sem_compra_sit)}")

    # ---------- perfil por segmento
    out("\n== Perfil por segmento")
    seg = defaultdict(lambda: {"contas": 0, "compradores": 0, "valor": Decimal(0), "notas": 0, "devolvido": Decimal(0)})
    for c in contas:
        s = c.get("Segment__c") or "(sem segmento)"
        seg[s]["contas"] += 1
        p = por_conta.get(c["Id"])
        if p and p["notas"]:
            seg[s]["compradores"] += 1
            seg[s]["valor"] += p["valor"]
            seg[s]["notas"] += p["notas"]
        if p:
            seg[s]["devolvido"] += p["dev_valor"]
    tabela_seg = []
    for s, v in sorted(seg.items(), key=lambda x: -x[1]["valor"]):
        linha = {"segmento": s, "contas": v["contas"], "compradores": v["compradores"],
                 "taxa_compradores": round(100 * v["compradores"] / v["contas"], 1), "notas": v["notas"],
                 "valor_vendas": float(v["valor"]), "valor_devolvido": float(v["devolvido"]),
                 "ticket_medio_nota": float(v["valor"] / v["notas"]) if v["notas"] else 0}
        tabela_seg.append(linha)
        out(f"  {s[:40]:<40} contas={v['contas']:>6} compradores={v['compradores']:>6} "
            f"({linha['taxa_compradores']}%) vendas={float(v['valor']):>16,.2f} devol={float(v['devolvido']):>12,.2f}")
    REL["perfil_segmento"] = tabela_seg
    for campo in ("Metier__c", "Subsegment__c", "ConsumptionClassification__c", "IMBP__c", "BillingState__c"):
        cont = defaultdict(lambda: [0, 0, Decimal(0)])
        for c in contas:
            k = c.get(campo) or "(vazio)"
            cont[k][0] += 1
            p = por_conta.get(c["Id"])
            if p and p["notas"]:
                cont[k][1] += 1
                cont[k][2] += p["valor"]
        REL[f"perfil_{campo}"] = sorted([[k, a, b, float(v)] for k, (a, b, v) in cont.items()], key=lambda x: -x[3])[:30]
    cats = Counter()
    for v in por_conta.values():
        cats.update(v["cats"])
    REL["compradores_por_tipo_produto"] = cats.most_common(30)

    # ---------- conferência por amostra (consulta independente por conta)
    out("\n== Conferência: amostra de 5 compradores, recontagem por consulta independente na origem")
    amostra = []
    try:
        ordenados = sorted(compradores, key=lambda k: -por_conta[k]["valor"])
        escolha = ordenados[:2] + ordenados[len(ordenados) // 2: len(ordenados) // 2 + 2] + ordenados[-1:]
        for acc_id in escolha:
            rs = sf.query_all(f"SELECT Invoice__r.Id, ValorTotalFormula__c, EmissionDate__c FROM InvoiceItem__c "
                              f"WHERE {filtro_saida} AND EmissionDate__c >= {desde} "
                              f"AND Invoice__r.Customer__c = '{acc_id}'")["records"]
            notas_ind = {r["Invoice__r"]["Id"] for r in rs}
            val_ind = sum(Decimal(str(r["ValorTotalFormula__c"] or 0)) for r in rs)
            datas_ind = sorted(r["EmissionDate__c"][:10] for r in rs if r.get("EmissionDate__c"))
            p = por_conta[acc_id]
            c = ids_contas[acc_id]
            ok = (len(notas_ind) == p["notas"] and abs(val_ind - p["valor"]) < Decimal("0.01")
                  and datas_ind[0] == p["prim"] and datas_ind[-1] == p["ult"])
            item = {"conta": c.get("Name"), "codigo": c.get("Code__c"), "segmento": c.get("Segment__c"),
                    "cnpj_mascarado": (c["_cnpj"][:2] + "*" * 8 + c["_cnpj"][-4:]) if c["_cnpj"] else None,
                    "notas_calc": p["notas"], "notas_origem": len(notas_ind),
                    "valor_calc": float(p["valor"]), "valor_origem": float(val_ind),
                    "primeira": p["prim"], "ultima": p["ult"], "devolucoes": p["dev_notas"],
                    "valor_devolvido": float(p["dev_valor"]), "confere": ok}
            amostra.append(item)
            out(f"  {item}")
    except Exception as e:
        falha("conferência amostra", e)
    REL["amostra_conferencia"] = amostra

    # amostra cadastral (sem credenciais) p/ conferência humana
    REL["amostra_cadastral"] = [{k: c.get(k) for k in ("Name", "Code__c", "Segment__c", "Metier__c", "BillingCity__c",
                                                        "BillingState__c", "ProtheusCustomerSituation__c", "CreatedDate")}
                                | {"cnpj_status": c["_cnpj_status"], **{e: c.get(e) for e in extras[:12]}}
                                for c in contas[:: max(1, n // 8)][:8]]
    return finalizar()


def finalizar():
    REL["execucao"]["fim"] = datetime.now().isoformat(timespec="seconds")
    with open(SAIDA / "relatorio.json", "w", encoding="utf-8") as f:
        json.dump(REL, f, ensure_ascii=False, indent=1, default=str)
    with open(SAIDA / "relatorio.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(TXT))
    print(f"\nRelatório: {SAIDA / 'relatorio.json'}")
    return 0 if not REL["execucao"]["falhas"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        REL["execucao"]["falhas"].append({"etapa": "geral", "erro": traceback.format_exc()[-800:]})
        finalizar()
        sys.exit(2)
