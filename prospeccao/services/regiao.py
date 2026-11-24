"""Coordenador e vendedor da região de um lead, deduzidos das contas do Salesforce.

O lead da prospecção ainda não está no Salesforce, então não tem coordenador nem vendedor. A região dele é inferida
pelas contas do Salesforce do MESMO MUNICÍPIO (Account.BillingCity__c/BillingState__c): vale o coordenador e o vendedor
que mais aparecem nessas contas. Sem contas no município, tenta pela faixa de CEP (5 primeiros dígitos).
É uma indicação para direcionar o lead, não um vínculo gravado: a conta só ganha dono de verdade no Salesforce.
"""
import time
import unicodedata
from collections import Counter, defaultdict

from django.db.models import Count

UFS = {
    "ACRE": "AC", "ALAGOAS": "AL", "AMAPA": "AP", "AMAZONAS": "AM", "BAHIA": "BA", "CEARA": "CE",
    "DISTRITO FEDERAL": "DF", "ESPIRITO SANTO": "ES", "GOIAS": "GO", "MARANHAO": "MA", "MATO GROSSO": "MT",
    "MATO GROSSO DO SUL": "MS", "MINAS GERAIS": "MG", "PARA": "PA", "PARAIBA": "PB", "PARANA": "PR",
    "PERNAMBUCO": "PE", "PIAUI": "PI", "RIO DE JANEIRO": "RJ", "RIO GRANDE DO NORTE": "RN",
    "RIO GRANDE DO SUL": "RS", "RONDONIA": "RO", "RORAIMA": "RR", "SANTA CATARINA": "SC", "SAO PAULO": "SP",
    "SERGIPE": "SE", "TOCANTINS": "TO",
}
VALIDADE_S = 600  # o mapa é refeito a cada 10 min (a carteira é sincronizada de hora em hora)
_cache = {"em": 0.0, "mapa": None}


def _norm(txt):
    t = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().upper()
    return " ".join(t.replace("-", " ").replace("'", " ").split())


def _uf(txt):
    t = _norm(txt)
    return t if len(t) == 2 else UFS.get(t, t)


def _cep5(cep):
    dig = "".join(ch for ch in (cep or "") if ch.isdigit())
    return dig[:5] if len(dig) >= 5 else ""


def _resumo(cont):
    """Counter de (coordenador, vendedor, regiao) -> o mais frequente, com a força da indicação."""
    total = sum(cont.values())
    coord, vend, reg = Counter(), Counter(), Counter()
    for (c, v, r), n in cont.items():
        if c:
            coord[c] += n
        if v:
            vend[v] += n
        if r:
            reg[r] += n
    def topo(cnt):
        if not cnt:
            return "", 0
        nome, n = cnt.most_common(1)[0]
        return nome, round(n * 100 / sum(cnt.values()))
    c, cp = topo(coord)
    v, vp = topo(vend)
    r, _ = topo(reg)
    return {"coordenador": c, "coord_pct": cp, "vendedor": v, "vend_pct": vp, "regiao": r, "contas": total}


def mapa(forcar=False):
    """{('cidade'|'cep', chave): resumo}. Uma consulta agregada às contas do Salesforce, guardada por 10 min."""
    if not forcar and _cache["mapa"] is not None and time.time() - _cache["em"] < VALIDADE_S:
        return _cache["mapa"]
    from carteira.models import Empresa
    por_cidade, por_cep = defaultdict(Counter), defaultdict(Counter)
    linhas = (Empresa.objects.filter(salesforce_id__isnull=False)
              .exclude(coordenador="", vendedor="")
              .values("cidade", "uf", "cep", "coordenador", "vendedor", "regiao").annotate(n=Count("pk")))
    for x in linhas:
        chave = (x["coordenador"].strip(), x["vendedor"].strip(), x["regiao"].strip())
        if x["cidade"]:
            por_cidade[(_norm(x["cidade"]), _uf(x["uf"]))][chave] += x["n"]
        if _cep5(x["cep"]):
            por_cep[_cep5(x["cep"])][chave] += x["n"]
    m = {("cidade",) + k: _resumo(v) for k, v in por_cidade.items()}
    m.update({("cep", k): _resumo(v) for k, v in por_cep.items()})
    _cache.update(em=time.time(), mapa=m)
    return m


def da_empresa(empresa, m=None):
    """Indicação de coordenador/vendedor para o lead, ou None. Inclui `base` ('município' ou 'CEP')."""
    m = mapa() if m is None else m
    r = m.get(("cidade", _norm(empresa.cidade), _uf(empresa.uf))) if empresa.cidade else None
    base = f"{empresa.cidade}/{empresa.uf}" if r else ""
    if not r and _cep5(empresa.cep):
        r = m.get(("cep", _cep5(empresa.cep)))
        base = f"CEP {_cep5(empresa.cep)}-xxx" if r else ""
    if not r or not (r["coordenador"] or r["vendedor"]):
        return None
    return dict(r, base=base)


def anexar(itens):
    """Coloca `o.regiao_sf` em cada oportunidade da lista (uma consulta para a página toda)."""
    m = mapa()
    for o in itens:
        o.regiao_sf = da_empresa(o.empresa, m)
