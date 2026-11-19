"""
Conector de DESCOBERTA: OpenStreetMap via Overpass API.

- Documentação: https://wiki.openstreetmap.org/wiki/Overpass_API
- Licença dos dados: ODbL 1.0 (https://www.openstreetmap.org/copyright) – permite armazenar e reutilizar
  com atribuição "© OpenStreetMap contributors". Guardamos apenas: id, nome, endereço, telefone, site,
  e-mail e as etiquetas de categoria do estabelecimento, com o link do objeto.
- Uso justo: as instâncias públicas pedem baixo volume e uma consulta por vez; para uso intenso
  e recorrente a recomendação oficial é instância própria (configurável em Fonte.endpoints).
- OSM raramente traz CNPJ: os resultados entram como candidatos pendentes de identificação.
"""
import re

from prospeccao.conectores.base import ClienteFonte, FonteIndisponivel, ResultadoDescoberta
from prospeccao.models import linhas

# Etiquetas que indicam estabelecimento/organização (o resto – ruas, pontos de ônibus etc. – é ignorado)
CHAVES_NEGOCIO = ("shop", "office", "craft", "amenity", "industrial", "company", "man_made", "landuse", "building")
VALORES_IGNORADOS = {
    "amenity": {"parking", "bench", "waste_basket", "toilets", "bus_station", "drinking_water", "shelter",
                "place_of_worship", "school", "kindergarten", "parking_space", "bicycle_parking", "telephone"},
    "building": {"yes", "house", "residential", "apartments", "roof", "church", "school", "public"},
    "landuse": {"residential", "grass", "forest", "meadow", "cemetery", "recreation_ground"},
}
CHAVES_ATIVIDADE = ("shop", "office", "craft", "amenity", "industrial", "company", "man_made", "landuse",
                    "building", "service", "trade", "cuisine", "description")
_SEGURO = re.compile(r"^[\wÀ-ÿ .,&/\-]{2,60}$")
_CHAVE_OSM = re.compile(r"^[a-z_:]{2,40}$")
_ACENTOS = {"a": "aáàâã", "e": "eéê", "i": "ií", "o": "oóôõ", "u": "uúü", "c": "cç"}


def regex_sem_acento(termo):
    termo = termo.strip().lower()
    if not _SEGURO.match(termo):
        raise ValueError(f"termo de busca inválido: {termo!r}")
    saida = ""
    for ch in termo:
        base = next((k for k, v in _ACENTOS.items() if ch in v), None)
        if base:
            saida += f"[{_ACENTOS[base]}]"
        elif ch in ".-/":
            saida += f"[{ch}]"  # sem barras invertidas (evita escapes no Overpass QL)
        else:
            saida += ch
    return saida


def montar_consulta(uf, cidade, termos, filtros, limite, timeout=60):
    if not _SEGURO.match(cidade) or not re.fullmatch(r"[A-Z]{2}", uf):
        raise ValueError("cidade/UF inválidas")
    partes = [f'nwr(area.a)["name"~"{regex_sem_acento(t)}",i];' for t in termos]
    for f in filtros:
        if "=" not in f:
            raise ValueError(f"filtro OSM inválido (use chave=valor): {f!r}")
        k, v = (x.strip() for x in f.split("=", 1))
        if not _CHAVE_OSM.match(k) or not _SEGURO.match(v):
            raise ValueError(f"filtro OSM inválido: {f!r}")
        partes.append(f'nwr(area.a)["{k}"="{v}"]["name"];')
    if not partes:
        raise ValueError("a pesquisa precisa de termos de busca ou filtros OSM")
    return (f'[out:json][timeout:{timeout}];area["ISO3166-2"="BR-{uf}"]->.uf;'
            f'rel(area.uf)["boundary"="administrative"]["admin_level"="8"]["name"="{cidade}"];map_to_area->.a;'
            f'({"".join(partes)});out tags center {int(limite)};')


def eh_negocio(tags):
    if not tags.get("name"):
        return False
    for k in CHAVES_NEGOCIO:
        v = tags.get(k)
        if v and v not in VALORES_IGNORADOS.get(k, set()):
            return True
    return False


def _primeiro(tags, *chaves):
    for k in chaves:
        if tags.get(k):
            return tags[k].split(";")[0].strip()
    return ""


def elemento_para_resultado(el, cidade_pesquisa, uf):
    tags = el.get("tags") or {}
    rua = " ".join(x for x in (tags.get("addr:street"), tags.get("addr:housenumber")) if x)
    centro = el.get("center") or ({"lat": el.get("lat"), "lon": el.get("lon")} if "lat" in el else {})
    cnpj = re.sub(r"\D", "", _primeiro(tags, "ref:vatin", "ref:CNPJ", "cnpj", "ref:cnpj"))
    return ResultadoDescoberta(
        id_externo=f"{el['type']}/{el['id']}",
        nome=tags["name"][:255],
        endereco=rua[:255],
        cidade=(tags.get("addr:city") or cidade_pesquisa)[:120],
        uf=uf,
        cep=re.sub(r"\D", "", tags.get("addr:postcode", ""))[:8],
        telefone=_primeiro(tags, "phone", "contact:phone", "contact:mobile", "mobile")[:60],
        email=_primeiro(tags, "email", "contact:email")[:254],
        site=_primeiro(tags, "website", "contact:website", "url")[:300],
        atividade="; ".join(f"{k}={tags[k]}" for k in CHAVES_ATIVIDADE if tags.get(k))[:500],
        cnpj=cnpj if len(cnpj) == 14 else "",
        url_referencia=f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
        dados={"tags": {k: v for k, v in tags.items() if k.startswith(("addr:", "contact:")) or k in CHAVES_ATIVIDADE
                        or k in ("name", "brand", "operator", "phone", "website", "email", "opening_hours")},
               "centro": centro, "cidade_da_pesquisa": cidade_pesquisa,
               "atribuicao": "© OpenStreetMap contributors (ODbL)"},
    )


class OverpassDescoberta:
    codigo = "osm_overpass"

    def __init__(self, fonte):
        self.fonte = fonte

    def descobrir(self, pesquisa, ctx):
        cliente = ClienteFonte(self.fonte, ctx, limite_execucao=pesquisa.limite_consultas)
        termos, filtros = linhas(pesquisa.termos_busca), linhas(pesquisa.filtros_osm)
        restante = pesquisa.limite_resultados
        vistos = set()
        for cidade in linhas(pesquisa.cidades):
            if restante <= 0:
                break
            consulta = montar_consulta(pesquisa.uf.upper(), cidade, termos, filtros, limite=min(restante * 3, 2000))
            dados = self._executar(cliente, consulta, ctx)
            elementos = dados.get("elements", [])
            if dados.get("remark"):
                ctx.log(f"Overpass ({cidade}): {dados['remark'][:300]}", "AVISO", self.codigo)
            ctx.log(f"{cidade}/{pesquisa.uf}: {len(elementos)} elementos retornados", fonte=self.codigo)
            if not elementos:
                ctx.log(f"Nenhum resultado em '{cidade}'. Confira o nome do município (limite administrativo OSM).",
                        "AVISO", self.codigo)
                if not hasattr(ctx, "pendencias"):
                    ctx.pendencias = []
                ctx.pendencias.append(f"OpenStreetMap sem resultado em '{cidade}' (município não encontrado ou sem "
                                      f"empresas com esses termos)")
            for el in elementos:
                if restante <= 0:
                    break
                chave = f"{el['type']}/{el['id']}"
                if chave in vistos or not eh_negocio(el.get("tags") or {}):
                    continue
                vistos.add(chave)
                restante -= 1
                yield elemento_para_resultado(el, cidade, pesquisa.uf.upper())

    def _executar(self, cliente, consulta, ctx):
        erros = []
        for endpoint in self.fonte.lista_endpoints:
            try:
                resp = cliente.requisitar("POST", endpoint, data={"data": consulta})
            except FonteIndisponivel as e:
                erros.append(str(e))
                ctx.log(f"Endpoint indisponível ({endpoint}): {e}. Tentando o próximo.", "AVISO", self.codigo)
                continue
            if resp.status_code != 200:
                erros.append(f"{endpoint}: HTTP {resp.status_code}")
                ctx.log(f"{endpoint}: HTTP {resp.status_code}: {resp.text[:200]}", "AVISO", self.codigo)
                continue
            try:
                return resp.json()
            except ValueError:
                erros.append(f"{endpoint}: resposta não-JSON")
        raise FonteIndisponivel("; ".join(erros) or "nenhum endpoint configurado")
