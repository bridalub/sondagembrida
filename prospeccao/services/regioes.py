"""Regiões comerciais conhecidas → municípios explícitos. Nada é executado aqui."""
from carteira.services.normalizacao import normalizar_texto

# Nomes oficiais (a comparação com a Receita ignora acento e maiúsculas).
ABC_PAULISTA = [
    "Santo André",
    "São Bernardo do Campo",
    "São Caetano do Sul",
    "Diadema",
    "Mauá",
    "Ribeirão Pires",
    "Rio Grande da Serra",
]

REGIOES = {
    "ABC": ABC_PAULISTA,
    "ABC PAULISTA": ABC_PAULISTA,
    "GRANDE ABC": ABC_PAULISTA,
    "GRANDE ABC PAULISTA": ABC_PAULISTA,
}


def expandir(itens):
    """(municipios, avisos). avisos = [(texto digitado, [municípios])]. Linha desconhecida é mantida."""
    saida, avisos, vistos = [], [], set()
    for bruto in itens or []:
        chave = normalizar_texto(bruto)
        if not chave:
            continue
        if chave in REGIOES:
            nomes = REGIOES[chave]
            avisos.append((bruto.strip(), list(nomes)))
            for nome in nomes:
                marca = normalizar_texto(nome)
                if marca not in vistos:
                    vistos.add(marca)
                    saida.append(nome)
            continue
        if chave not in vistos:
            vistos.add(chave)
            saida.append(bruto.strip())
    return saida, avisos
