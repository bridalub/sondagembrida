"""Exclusões comerciais: SOMENTE as configuradas explicitamente pelo gestor (pesquisa ou globais)."""
import re

from prospeccao.models import ExclusaoComercial, linhas
from prospeccao.services.evidencias import _padrao, normalizar_mesmo_tamanho


def motivo_exclusao(pesquisa, *, nome="", atividade="", cnaes=(), cnpj=""):
    texto = normalizar_mesmo_tamanho(f"{nome} | {atividade}")
    termos = [("pesquisa", t) for t in linhas(pesquisa.exclusoes_termos)] if pesquisa else []
    prefixos = [("pesquisa", c) for c in linhas(pesquisa.exclusoes_cnaes)] if pesquisa else []
    for ex in ExclusaoComercial.objects.filter(ativa=True):
        if ex.tipo == ExclusaoComercial.Tipo.TERMO:
            termos.append((f"global: {ex.motivo}", ex.valor))
        elif ex.tipo == ExclusaoComercial.Tipo.CNAE:
            prefixos.append((f"global: {ex.motivo}", ex.valor))
        elif ex.tipo == ExclusaoComercial.Tipo.CNPJ_RAIZ and cnpj and cnpj.startswith(re.sub(r"\D", "", ex.valor)):
            return f"Exclusão comercial (raiz CNPJ {ex.valor}) – {ex.motivo}"
    for origem, t in termos:
        p = _padrao(t)
        if p and p.search(texto):
            return f"Exclusão comercial ({origem}): termo '{t}'"
    codigos = [re.sub(r"\D", "", str(c)) for c in cnaes]
    for origem, pref in prefixos:
        pref_d = re.sub(r"\D", "", pref)
        if pref_d and any(c.startswith(pref_d) for c in codigos):
            return f"Exclusão comercial ({origem}): CNAE {pref}"
    return None
