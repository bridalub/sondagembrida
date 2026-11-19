"""Todas as unidades (matriz e filiais) de uma empresa: CNPJ, município, telefones e endereço.

Fontes, sem custo novo:
  - base local da Receita (raiz do CNPJ principal e dos associados): cobre os municípios de atuação carregados;
  - CNPJs citados pela busca web (OpenAI/Google) já gravada para a empresa, com o município citado. Esses
    aparecem como "citado na web" até a base ou o atendimento confirmar.
"""
import re


def _fmt_cnpj(c):
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else c


def _fmt_tel(t):
    d = re.sub(r"\D", "", t or "")
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return d


def cnpjs_citados(empresa):
    """{cnpj: municipio_citado} das buscas web gravadas para a empresa (só CNPJ com 14 dígitos)."""
    from prospeccao.models import ConsultaWeb
    saida = {}
    for res in ConsultaWeb.objects.filter(empresa=empresa, status="OK").values_list("resultados", flat=True):
        for item in res or []:
            if not isinstance(item, dict):
                continue
            c = re.sub(r"\D", "", str(item.get("cnpj_citado") or ""))
            if len(c) == 14 and c not in saida:
                saida[c] = str(item.get("municipio_citado") or "").strip()
    return saida


def unidades(empresa, limite=40):
    """Lista de dicts ordenada: principal, matriz, mesma cidade, demais. Vazia quando não há raiz conhecida."""
    from carteira.services.normalizacao import normalizar_texto
    from prospeccao.conectores.receita import carga_ativa
    from receita.models import EstabelecimentoReceita
    principal = (empresa.cnpj or "").strip()
    if empresa.pk is None:
        return []
    associados = set(empresa.cnpjs_associados.values_list("cnpj", flat=True))
    citados = cnpjs_citados(empresa)
    conhecidos = {c for c in ({principal} | associados | set(citados)) if len(c) == 14}
    raizes = {c[:8] for c in conhecidos}
    if not raizes:
        return []
    vistos, saida = set(), []
    carga = carga_ativa()
    if carga is not None:
        for r in (EstabelecimentoReceita.objects.filter(carga=carga, cnpj_basico__in=raizes)
                  .order_by("cnpj")[:limite]):
            vistos.add(r.cnpj)
            tels = [_fmt_tel(t) for t in (r.telefone1, r.telefone2) if t]
            end = " ".join(x for x in (r.tipo_logradouro, r.logradouro, r.numero) if x)
            saida.append({
                "cnpj": r.cnpj, "cnpj_formatado": _fmt_cnpj(r.cnpj),
                "tipo": "Matriz" if (r.matriz or r.cnpj[8:12] == "0001") else "Filial",
                "nome": r.nome_fantasia or r.razao_social, "municipio": (r.municipio or "").title(), "uf": r.uf,
                "telefones": tels, "endereco": f"{end}{' – ' + r.bairro if r.bairro else ''}".strip(),
                "situacao": r.situacao or "", "fonte": "Receita (base local)",
                "principal": r.cnpj == principal, "associado": r.cnpj in associados,
            })
    for c, mun in citados.items():
        if c in vistos:
            continue
        saida.append({
            "cnpj": c, "cnpj_formatado": _fmt_cnpj(c), "tipo": "Matriz" if c[8:12] == "0001" else "Filial",
            "nome": "", "municipio": mun.title(), "uf": "", "telefones": [], "endereco": "",
            "situacao": "", "fonte": "citado na busca web (conferir)",
            "principal": c == principal, "associado": c in associados,
        })
    cidade = normalizar_texto(empresa.cidade or "")
    saida.sort(key=lambda u: (not u["principal"], normalizar_texto(u["municipio"]) != cidade,
                              u["tipo"] != "Matriz", u["cnpj"]))
    return saida[:limite]
