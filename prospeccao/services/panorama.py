"""Panorama do universo da base Receita para um recorte, cruzado com a carteira e leads BRIDA (por CNPJ)."""
from collections import Counter

from carteira.models import Empresa, LeadSalesforce
from carteira.services.normalizacao import dominio_de_email, normalizar_texto


def panorama(uf, cidades, cnaes=None):
    from receita.importador import tabela
    from receita.models import CargaReceita, EstabelecimentoReceita
    carga = CargaReceita.ativa_atual()
    if not carga:
        return None
    alvo = {normalizar_texto(c) for c in cidades}
    codigos = [c for c, d in tabela(carga.competencia, "MUNICIPIO").items() if normalizar_texto(d) in alvo]
    qs = EstabelecimentoReceita.objects.filter(carga=carga, uf=uf.upper(), municipio_codigo__in=codigos)
    por_situacao = Counter(qs.values_list("situacao", flat=True))
    sf = dict(Empresa.objects.filter(salesforce_id__isnull=False, cnpj__isnull=False)
              .values_list("cnpj", "situacao_protheus"))
    leads = set(LeadSalesforce.objects.exclude(cnpj__isnull=True).values_list("cnpj", flat=True))
    r = Counter()
    for cnpj, tel, email, matriz in qs.filter(situacao="ATIVA").values_list("cnpj", "telefone1", "email", "matriz").iterator(
            chunk_size=5000):
        r["ativos"] += 1
        if cnpj in sf:
            r["bloqueadas" if (sf[cnpj] or "").lower() == "bloqueado" else "carteira"] += 1
        elif cnpj in leads:
            r["leads"] += 1
        else:
            r["externas"] += 1
            r["externas_tel"] += bool(tel)
            r["externas_email"] += bool(email)
            r["externas_email_dominio_proprio"] += bool(dominio_de_email(email))
            r["externas_matriz"] += bool(matriz)
    conhecidos_nao_ativos = Counter(s for c, s in qs.exclude(situacao="ATIVA").values_list("cnpj", "situacao") if c in sf)
    cnae_top = [(c, d, n) for (c, d), n in
                Counter(qs.filter(situacao="ATIVA").values_list("cnae_principal", "cnae_descricao")).most_common(15)]
    return {"carga": carga, "codigos": codigos, "total": sum(por_situacao.values()), "por_situacao": dict(por_situacao),
            "r": dict(r), "conhecidos_nao_ativos": dict(conhecidos_nao_ativos), "cnae_top": cnae_top}
