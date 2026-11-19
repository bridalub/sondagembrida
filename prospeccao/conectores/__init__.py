"""Registro dos conectores por código de Fonte."""
from prospeccao.conectores.brasilapi import BrasilAPICNPJ
from prospeccao.conectores.osm import OverpassDescoberta
from prospeccao.conectores.receita import ReceitaBaseDescoberta
from prospeccao.conectores.site import SitePublico

DESCOBERTA = {OverpassDescoberta.codigo: OverpassDescoberta, ReceitaBaseDescoberta.codigo: ReceitaBaseDescoberta}
ENRIQUECIMENTO = {BrasilAPICNPJ.codigo: BrasilAPICNPJ}
IDENTIFICACAO = {SitePublico.codigo: SitePublico}
CODIGO_CSV = "csv_importacao"


def conector_descoberta(fonte):
    cls = DESCOBERTA.get(fonte.codigo)
    if cls is None:
        raise KeyError(f"Fonte '{fonte.codigo}' não tem conector de descoberta implementado")
    return cls(fonte)
