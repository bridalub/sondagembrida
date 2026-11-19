"""
Leitura do Salesforce (SOMENTE LEITURA: apenas query_all e describe).

Consultas derivadas das funções extrair_cliente_soql e extrair_vendas_soql /
extrair_devolucao_soql do Excel.py, mantendo os mesmos objetos, campos e filtros,
com estes acréscimos de campos-padrão de identificação:
  - Account.Id (a automação não buscava; necessário para rastrear a origem)
  - InvoiceItem__c.Id, Invoice__r.Id e Invoice__r.Customer__r.Id (ligação nota -> conta)
"""
import logging
import os
import re

from django.conf import settings

logger = logging.getLogger("carteira")

# Mesma lista de CFOPs de venda do relatório VENDAS (Excel.py, CFOPS_VENDAS).
CFOPS_VENDAS = (
    "6922,5922,6656,6655,6654,6653,6652,6651,6650,6551,6550,6404,6403,6402,6401,"
    "6123,6122,6120,6119,6118,6115,6114,6113,6112,6111,6110,6109,6108,6107,6106,"
    "6105,6104,6103,6102,6101,5656,5655,5654,5653,5652,5651,5551,5405,5403,5402,"
    "5401,5123,5122,5120,5119,5118,5115,5114,5113,5112,5111,5110,5109,5106,5105,"
    "5104,5103,5102,5101,3202,3201,2662,2661,2660,2411,2410,2204,2203,2202,2201,"
    "1662,1661,1660,1411,1410,1204,1203,1202,1201,5667,5502,2652,2653"
).split(",")

# Campos confirmados no diagnóstico real (docs/DIAGNOSTICO_DADOS_REAIS.md).
CAMPOS_ACCOUNT = [
    "Id", "CNPJ__c", "CustomizedID__c", "IsGrainy__c", "Code__c", "Name", "FantasyName__c",
    "CoordinatorFormula__c", "Seller__r.Name", "Zone__r.Name", "Metier__c", "IMBP__c",
    "Segment__c", "Subsegment__c", "BillingPostalCode", "BillingCity__c", "BillingState__c",
    "ShippingCity__c", "ShippingState__c", "ProtheusCustomerSituation__c",
    "ConsumptionClassification__c", "CreatedDate", "Distributor__r.Name",
    "Phone", "Email__c", "LastBuyDate__c",
]

CAMPOS_CONTACT = ["Id", "AccountId", "Name", "Title", "Phone", "MobilePhone", "Email"]
CAMPOS_LEAD = ["Id", "CNPJ__c", "Company", "Status", "IsConverted", "ConvertedAccountId", "CreatedDate",
               "Distributor__r.Name"]

ATRIBUTOS_EXTRAS_ACEITOS = {"telefone", "email", "cnae", "data_abertura", "razao_social", "nome_fantasia"}

# Usado só para LISTAR candidatos no relatório da importação (nunca para mapear sozinho).
_PADRAO_CANDIDATOS = re.compile(
    r"cnae|abertura|opening|funda|foundation|fantasia|trade|phone|telefone|fone|email|e-mail|raz[aã]o", re.I
)


def _sanitizar_sql(valor):
    return str(valor).replace("\\", "\\\\").replace("'", "\\'")


def conectar():
    """Credenciais apenas do ambiente. Nunca são registradas em log."""
    from simple_salesforce import Salesforce  # import tardio: só é necessário na sincronização

    faltando = [v for v in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN") if not os.getenv(v)]
    if faltando:
        raise RuntimeError(f"Variáveis de ambiente ausentes: {', '.join(faltando)}")
    kwargs = dict(
        username=os.environ["SF_USERNAME"],
        password=os.environ["SF_PASSWORD"],
        security_token=os.environ["SF_SECURITY_TOKEN"],
    )
    if os.getenv("SF_INSTANCE_URL"):
        kwargs["instance_url"] = os.environ["SF_INSTANCE_URL"]
    if os.getenv("SF_DOMAIN"):
        kwargs["domain"] = os.environ["SF_DOMAIN"]
    sf = Salesforce(**kwargs)
    logger.info("Conectado ao Salesforce (instância %s)", sf.sf_instance)
    return sf


def parse_campos_extras(texto):
    """'telefone=Phone,cnae=CNAE__c' -> {'telefone': 'Phone', 'cnae': 'CNAE__c'}"""
    mapa = {}
    for parte in (texto or "").split(","):
        if "=" not in parte:
            continue
        attr, campo = (p.strip() for p in parte.split("=", 1))
        if attr in ATRIBUTOS_EXTRAS_ACEITOS and re.fullmatch(r"[A-Za-z0-9_.]+", campo):
            mapa[attr] = campo
    return mapa


def descrever_account(sf, extras_desejados):
    """
    Confere via describe quais campos opcionais existem e lista candidatos
    (por nome/rótulo) para conferência humana.
    Retorna (extras_confirmados, extras_inexistentes, candidatos).
    """
    campos = sf.Account.describe()["fields"]
    nomes = {f["name"] for f in campos}
    confirmados = {a: c for a, c in extras_desejados.items() if c in nomes}
    inexistentes = {a: c for a, c in extras_desejados.items() if c not in nomes}
    candidatos = [
        f"{f['name']} ({f.get('label')}, {f['type']})"
        for f in campos
        if _PADRAO_CANDIDATOS.search(f["name"]) or _PADRAO_CANDIDATOS.search(f.get("label") or "")
    ]
    return confirmados, inexistentes, candidatos


def buscar_accounts(sf, extras_confirmados):
    campos = CAMPOS_ACCOUNT + [c for c in extras_confirmados.values() if c not in CAMPOS_ACCOUNT]
    where = [f"Distributor__r.Name LIKE '{_sanitizar_sql(settings.SF_FILTRO_DISTRIBUIDOR)}'"]
    if settings.SF_SITUACOES_CLIENTE:
        lista = ",".join(f"'{_sanitizar_sql(s)}'" for s in settings.SF_SITUACOES_CLIENTE)
        where.append(f"ProtheusCustomerSituation__c IN ({lista})")
    query = f"SELECT {', '.join(campos)} FROM Account WHERE {' AND '.join(where)}"
    return sf.query_all(query)["records"], query


def buscar_contatos(sf):
    query = (f"SELECT {', '.join(CAMPOS_CONTACT)} FROM Contact "
             f"WHERE Account.Distributor__r.Name LIKE '{_sanitizar_sql(settings.SF_FILTRO_DISTRIBUIDOR)}'")
    return sf.query_all(query)["records"], query


def buscar_leads(sf):
    """Somente leads do distribuidor BRIDA. Leads de outros distribuidores ficam fora."""
    query = (f"SELECT {', '.join(CAMPOS_LEAD)} FROM Lead "
             f"WHERE Distributor__r.Name LIKE '{_sanitizar_sql(settings.SF_FILTRO_DISTRIBUIDOR)}'")
    return sf.query_all(query)["records"], query


def conferir_cnpjs(sf, cnpjs):
    """
    Conferência direta (leitura) de CNPJs na carteira e nos leads BRIDA.
    CNPJ__c está gravado só com dígitos (36.208 de 36.532 contas no diagnóstico).
    Retorna {cnpj: {"contas": [...], "leads": [...]}}.
    """
    cnpjs = [c for c in dict.fromkeys(cnpjs) if re.fullmatch(r"[0-9A-Z]{14}", c or "")]
    res = {c: {"contas": [], "leads": []} for c in cnpjs}
    dist = _sanitizar_sql(settings.SF_FILTRO_DISTRIBUIDOR)
    for i in range(0, len(cnpjs), 200):
        lote = ",".join(f"'{c}'" for c in cnpjs[i:i + 200])
        for r in sf.query_all(f"SELECT Id, CNPJ__c, Name, ProtheusCustomerSituation__c, LastBuyDate__c FROM Account "
                              f"WHERE CNPJ__c IN ({lote}) AND Distributor__r.Name LIKE '{dist}'")["records"]:
            res.setdefault(r["CNPJ__c"], {"contas": [], "leads": []})["contas"].append(
                {"id": r["Id"], "nome": r.get("Name"), "situacao": r.get("ProtheusCustomerSituation__c"),
                 "ultima_compra": r.get("LastBuyDate__c")})
        for r in sf.query_all(f"SELECT Id, CNPJ__c, Company, Status FROM Lead WHERE CNPJ__c IN ({lote}) "
                              f"AND Distributor__r.Name LIKE '{dist}'")["records"]:
            res.setdefault(r["CNPJ__c"], {"contas": [], "leads": []})["leads"].append(
                {"id": r["Id"], "nome": r.get("Company"), "status": r.get("Status")})
    return res


def buscar_itens_nota(sf, desde, tipo):
    """
    tipo='VENDA': NF de Saída não cancelada com CFOP de venda (regra do relatório VENDAS).
    tipo='DEVOLUCAO': NF de Entrada não cancelada (regra do relatório DEVOLUÇÃO) + filtro de data.
    """
    campos = [
        "Id", "EmissionDate__c", "ValorTotalFormula__c", "TypeofProdutct__c",
        "Invoice__r.Id", "Invoice__r.Name", "Invoice__r.Type__c", "Invoice__r.Customer__r.Id",
    ]
    where = [
        f"Invoice__r.Distributor__r.Name LIKE '{_sanitizar_sql(settings.SF_FILTRO_DISTRIBUIDOR)}'",
        "Invoice__r.Status__c != 'Cancelada'",
        "Invoice__r.IsInvoiceCanceled__c = false",
        f"EmissionDate__c >= {desde.isoformat()}",
    ]
    if tipo == "VENDA":
        where.append("Invoice__r.Type__c = 'Saída'")
        where.append("CodeFiscalOperation__c IN (" + ", ".join(f"'{c}'" for c in CFOPS_VENDAS) + ")")
    elif tipo == "DEVOLUCAO":
        where.append("Invoice__r.Type__c = 'Entrada'")
    else:
        raise ValueError(tipo)
    query = f"SELECT {', '.join(campos)} FROM InvoiceItem__c WHERE {' AND '.join(where)}"
    return sf.query_all(query)["records"], query
