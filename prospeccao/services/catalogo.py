"""Catálogo de categorias de produto: SOMENTE as categorias realmente vendidas (InvoiceItem__c.TypeofProdutct__c)."""
from collections import Counter

from carteira.models import NotaFiscal
from prospeccao.models import CategoriaProduto


def sincronizar_catalogo():
    cont = Counter()
    for cats in NotaFiscal.objects.filter(tipo=NotaFiscal.Tipo.VENDA, presente_na_origem=True).values_list(
            "categorias", flat=True):
        for nome in (cats or {}):
            if nome and not nome.startswith("("):
                cont[nome[:120]] += 1
    for nome, n in cont.items():
        CategoriaProduto.objects.update_or_create(nome=nome, defaults={"notas_com_venda": n})
    return len(cont)
