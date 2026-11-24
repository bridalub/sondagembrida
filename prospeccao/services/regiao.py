"""Vendedor e coordenador da região de um lead, deduzidos das contas do Salesforce.

O lead da prospecção ainda não está no Salesforce. A região dele é inferida pelas contas do Salesforce da mesma área:
  1. Só contam VENDEDORES DE RUA. Ficam de fora os vendedores genéricos (VENDEDOR INSIDE, VENDEDOR REVENDA,
     A CONTRATAR, Pede direto, PERDAS...) e o time interno de apoio (vendedores cujo coordenador está em
     PROSPECCAO_COORDENADORES_APOIO — a liderança do interno, que ajuda a equipe de rua).
  2. Área: faixa de CEP de 5 e depois de 4 dígitos, quando tem contas suficientes; senão o município (também
     reconhecido no fim do texto, ex.: "Cidade Parque Alvorada Guarulhos"); por último a faixa de CEP de 3 dígitos.
  3. Tipo: lead de revenda/oficina procura vendedor de Varejo; indústria, frota e agro procuram Consumo / Indústria
     (Account.Seller__r.Type__c). Sem vendedor desse tipo na área, vale qualquer tipo.
  4. Escolhe primeiro o VENDEDOR mais frequente; o coordenador é o DESSE vendedor (Seller__r.UserCoordinatorSync__r),
     nunca escolhido à parte (evita pares que não existem, como coordenador de um vendedor com vendedor de outro).
É uma indicação para direcionar o lead, não um vínculo gravado: a conta só ganha dono de verdade no Salesforce.
"""
import os
import re
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
GENERICOS = re.compile(r"^(VENDEDOR\b|A CONTRATAR|PEDE DIRETO|PERDAS)")
APOIO_PADRAO = "TATIANE FRITZ DE QUEROZ CORREIA"
NIVEIS_CEP = ((5, 5), (4, 10))  # (dígitos do CEP, mínimo de contas) antes do município
CEP3_MIN = 20                    # faixa de 3 dígitos: só quando o município não resolve
REGIAO_DIVIDIDA_PCT = 25         # vendedor indicado com menos que isso das contas da área: avisa que é dividida
TIPO_DO_ESCOPO = {"REVENDA_OFICINA": "VAREJO", "INDUSTRIA": "CONSUMO", "FROTA": "CONSUMO", "AGRO_MAQUINAS": "CONSUMO"}
VALIDADE_S = 600            # o mapa é refeito a cada 10 min (a carteira é sincronizada de hora em hora)
_cache = {"em": 0.0, "mapa": None}


def _norm(txt):
    t = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().upper()
    return " ".join(t.replace("-", " ").replace("'", " ").split())


def _uf(txt):
    t = _norm(txt)
    return t if len(t) == 2 else UFS.get(t, t)


def _cep(cep, n=5):
    dig = "".join(ch for ch in (cep or "") if ch.isdigit())
    return dig[:n] if len(dig) >= 5 else ""


def _cep5(cep):
    return _cep(cep, 5)


def _tipo(tipo_vendedor):
    t = _norm(tipo_vendedor)
    return "CONSUMO" if t.startswith("CONSUMO") or "INDUSTRIA" in t else ("VAREJO" if t else "")


def apoio():
    """Coordenadores do time interno (apoio): os vendedores deles não definem a região do lead."""
    bruto = os.getenv("PROSPECCAO_COORDENADORES_APOIO", APOIO_PADRAO)
    return {_norm(x) for x in bruto.split(";") if x.strip()}


def de_rua(vendedor, coordenador, apoio_set=None):
    """Vendedor de rua: não é genérico nem do time interno de apoio."""
    apoio_set = apoio() if apoio_set is None else apoio_set
    return bool(vendedor) and not GENERICOS.match(_norm(vendedor)) and _norm(coordenador) not in apoio_set


def mapa(forcar=False):
    """{(área, chave, tipo): Counter{(vendedor, coordenador): contas}} com tipo '' = todos os tipos.
    Uma consulta agregada às contas do Salesforce, guardada por 10 min."""
    if not forcar and _cache["mapa"] is not None and time.time() - _cache["em"] < VALIDADE_S:
        return _cache["mapa"]
    from carteira.models import Empresa
    m, ap = defaultdict(Counter), apoio()
    linhas = (Empresa.objects.filter(salesforce_id__isnull=False).exclude(vendedor="")
              .values("cidade", "uf", "cep", "vendedor", "coordenador", "coordenador_vendedor", "tipo_vendedor")
              .annotate(n=Count("pk")))
    for x in linhas:
        coord = (x["coordenador_vendedor"] or x["coordenador"]).strip()
        vend = x["vendedor"].strip()
        if not de_rua(vend, coord, ap):
            continue
        par, tipo = (vend, coord), _tipo(x["tipo_vendedor"])
        areas = []
        if x["cidade"]:
            areas.append(("cidade", (_norm(x["cidade"]), _uf(x["uf"]))))
        for n in (5, 4, 3):
            if _cep(x["cep"], n):
                areas.append((f"cep{n}", _cep(x["cep"], n)))
        for area, chave in areas:
            m[(area, chave, "")][par] += x["n"]
            if tipo:
                m[(area, chave, tipo)][par] += x["n"]
    _cache.update(em=time.time(), mapa=dict(m))
    return _cache["mapa"]


def _resumo(cont, base, tipo):
    total = sum(cont.values())
    por_vend = Counter()
    for (v, _c), n in cont.items():
        por_vend[v] += n
    vend, n_vend = por_vend.most_common(1)[0]
    coord = Counter({c: n for (v, c), n in cont.items() if v == vend}).most_common(1)[0][0]
    pct = round(n_vend * 100 / total)
    return {"vendedor": vend, "coordenador": coord, "contas": total, "vend_contas": n_vend,
            "vend_pct": pct, "dividida": pct < REGIAO_DIVIDIDA_PCT, "base": base,
            "tipo": {"VAREJO": "Varejo", "CONSUMO": "Consumo / Indústria"}.get(tipo, "")}


def _chaves_cidade(empresa, m):
    """Município do lead; se não houver contas com esse nome exato, tenta o fim do texto (bairro + cidade)."""
    nome, uf = _norm(empresa.cidade), _uf(empresa.uf)
    if not nome:
        return []
    palavras = nome.split()
    candidatos = [nome] + [" ".join(palavras[-k:]) for k in (3, 2, 1) if k < len(palavras)]
    for c in candidatos:
        if ("cidade", (c, uf), "") in m:
            return [(("cidade", (c, uf)), f"{c.title()}/{uf}" if c != nome else f"{empresa.cidade}/{empresa.uf}")]
    return []


def da_empresa(empresa, escopo="", m=None):
    """Indicação de vendedor/coordenador para o lead, ou None."""
    m = mapa() if m is None else m
    tipo = TIPO_DO_ESCOPO.get(escopo or "", "")
    cep = getattr(empresa, "cep", "")
    areas = [((f"cep{n}", _cep(cep, n)), f"CEP {_cep(cep, n)}" + "x" * (5 - n) + "-xxx", minimo)
             for n, minimo in NIVEIS_CEP if _cep(cep, n)]
    areas += [(chave, base, 1) for chave, base in _chaves_cidade(empresa, m)]
    if _cep(cep, 3):
        areas.append((("cep3", _cep(cep, 3)), f"CEP {_cep(cep, 3)}xx-xxx", CEP3_MIN))
    for (area, chave), base, minimo in areas:
        for t in ([tipo, ""] if tipo else [""]):
            cont = m.get((area, chave, t))
            if cont and sum(cont.values()) >= minimo:
                return _resumo(cont, base, t)
    return None


def anexar(itens):
    """Coloca `o.regiao_sf` em cada oportunidade da lista (uma consulta para a página toda)."""
    m = mapa()
    for o in itens:
        o.regiao_sf = da_empresa(o.empresa, getattr(o, "escopo", ""), m)
