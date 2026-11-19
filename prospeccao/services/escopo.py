"""
Escopo comercial da BRIDA (lubrificantes, graxas, filtros): quem PODE consumir ou revender os produtos.

Perfis (definidos pelo gestor em 06/11/2026; porte NÃO exclui, só ordena):
  FROTA            – frota própria: transporte de carga/passageiros, coleta de resíduos, logística, locação de veículos
  INDUSTRIA        – indústria com máquinas (toda a indústria de transformação), energia, saneamento
  AGRO_MAQUINAS    – agro, extração/mineração, construção e terraplenagem, locação de máquinas, paisagismo
  REVENDA_OFICINA  – comércio e manutenção de veículos e peças, atacado de máquinas/peças, ferragens
Situações à parte:
  REVISAR          – possível CONCORRENTE ou revenda de combustíveis/lubrificantes (decisão humana), ou atividade
                     fora do escopo com evidência de operação própria
  FORA             – atividade sem relação com os produtos (banco, farmácia, buffet, escola...). NÃO é apagada:
                     sai da lista padrão e não recebe gasto pago; o gestor pode reclassificar.

Classificação pelo CNAE principal; se ele estiver fora, uma atividade SECUNDÁRIA do escopo leva a REVISAR (muitas
empresas listam atividades que não exercem). A decisão humana (Oportunidade.escopo_humano) nunca é sobrescrita.
"""
import re

PERFIS = {"FROTA": "Frota própria", "INDUSTRIA": "Indústria com máquinas",
          "AGRO_MAQUINAS": "Agro, construção e máquinas pesadas", "REVENDA_OFICINA": "Revenda e oficinas"}
ROTULOS = {**PERFIS, "REVISAR": "Revisar (possível concorrente / evidência)", "FORA": "Fora do escopo", "": "Não avaliado"}
DENTRO = tuple(PERFIS)

# prefixo do CNAE (só dígitos) -> perfil; o prefixo MAIS LONGO vence
_REGRAS = {
    # possíveis concorrentes / revenda de combustíveis e lubrificantes -> revisão humana
    "4681": "REVISAR", "4731": "REVISAR", "4732": "REVISAR", "1921": "REVISAR", "1922": "REVISAR",
    # frota própria
    "49": "FROTA", "50": "FROTA", "5211": "FROTA", "5212": "FROTA", "522": "FROTA", "5250": "FROTA",
    "53": "FROTA", "38": "FROTA", "3701": "FROTA", "3702": "FROTA", "3900": "FROTA",
    "7711": "FROTA", "7719": "FROTA", "8012": "FROTA",
    # indústria (transformação inteira) + energia e água
    **{f"{d:02d}": "INDUSTRIA" for d in range(10, 34)}, "35": "INDUSTRIA", "36": "INDUSTRIA",
    # agro, extração, construção, máquinas
    "01": "AGRO_MAQUINAS", "02": "AGRO_MAQUINAS", "03": "AGRO_MAQUINAS",
    **{f"{d:02d}": "AGRO_MAQUINAS" for d in range(5, 10)},
    "41": "AGRO_MAQUINAS", "42": "AGRO_MAQUINAS", "431": "AGRO_MAQUINAS", "439": "AGRO_MAQUINAS",
    "7731": "AGRO_MAQUINAS", "7732": "AGRO_MAQUINAS", "7739": "AGRO_MAQUINAS", "8130": "AGRO_MAQUINAS",
    # revenda e oficinas
    "45": "REVENDA_OFICINA", "4661": "REVENDA_OFICINA", "4662": "REVENDA_OFICINA", "4663": "REVENDA_OFICINA",
    "4664": "REVENDA_OFICINA", "4665": "REVENDA_OFICINA", "4669": "REVENDA_OFICINA", "4744": "REVENDA_OFICINA",
}
_MOTIVO_REVISAR = {"4681": "comércio atacadista de combustíveis/lubrificantes – possível concorrente",
                   "4731": "posto de combustíveis – possível revenda ou concorrente",
                   "4732": "comércio varejista de lubrificantes – possível revenda ou concorrente",
                   "1921": "refino de petróleo – possível fabricante concorrente",
                   "1922": "fabricação de derivados (inclui lubrificantes) – possível concorrente"}


def _dig(c):
    return re.sub(r"\D", "", str(c or ""))


def perfil_cnae(cnae):
    d = _dig(cnae)
    for n in range(len(d), 1, -1):
        if d[:n] in _REGRAS:
            return _REGRAS[d[:n]], d[:n]
    return "FORA", ""


_FRENTE_PERFIL = (("transport", "FROTA"), ("frota", "FROTA"), ("logist", "FROTA"), ("industr", "INDUSTRIA"),
                  ("agro", "AGRO_MAQUINAS"), ("constru", "AGRO_MAQUINAS"), ("equipament", "AGRO_MAQUINAS"),
                  ("maquina", "AGRO_MAQUINAS"), ("revenda", "REVENDA_OFICINA"), ("autope", "REVENDA_OFICINA"),
                  ("oficina", "REVENDA_OFICINA"), ("manuten", "REVENDA_OFICINA"))


def perfil_da_frente(nome):
    import unicodedata
    n = "".join(c for c in unicodedata.normalize("NFKD", (nome or "").lower()) if not unicodedata.combining(c))
    return next((p for k, p in _FRENTE_PERFIL if k in n), "")


def classificar(empresa, tem_evidencia_operacao=None):
    """(escopo, motivo). `tem_evidencia_operacao`: None = consulta as evidências ativas da empresa."""
    if not _dig(empresa.cnpj) and empresa.pk:
        # achada na web sem CNPJ: não há CNAE; vale a frente que a encontrou (a confirmar com o CNPJ)
        from prospeccao.models import SelecaoPesquisa
        sel = SelecaoPesquisa.objects.filter(empresa=empresa).select_related("frente").order_by("pk").first()
        perfil = perfil_da_frente(sel.frente.nome) if sel and sel.frente else ""
        if perfil:
            return perfil, f"sem CNPJ: atividade indicada pela fonte na frente '{sel.frente.nome}' (a confirmar)"
    principal = empresa.cnae
    desc = empresa.atividade_principal or ""
    perfil, chave = perfil_cnae(principal)
    if perfil == "REVISAR":
        return "REVISAR", f"{_MOTIVO_REVISAR.get(chave, 'possível concorrente')} ({principal} {desc[:60]})"
    if perfil in DENTRO:
        return perfil, f"atividade principal {principal} {desc[:70]}"
    for sec in empresa.cnaes_secundarios or []:
        cod = sec.get("codigo") if isinstance(sec, dict) else sec
        p2, _ = perfil_cnae(cod)
        if p2 in DENTRO:  # secundária: muitas empresas listam atividades que não exercem -> confirmação humana
            d2 = sec.get("descricao", "") if isinstance(sec, dict) else ""
            return "REVISAR", (f"principal {principal} {desc[:40]} fora do escopo; atividade SECUNDÁRIA {cod} {d2[:40]} "
                               f"({PERFIS[p2]}) – confirmar se exerce")
    if not _dig(principal):
        return "REVISAR", "sem atividade (CNAE) informada – revisar"
    if tem_evidencia_operacao is None:
        from django.db.models import Q
        from prospeccao.models import Evidencia
        # só operação PRÓPRIA apontada pela IA no material (HIPOTESE gerada pela IA = OPERACAO_PROPRIA) ou pela equipe;
        # regras por palavra-chave confundem serviço vendido ("Para o Agronegócio" de um banco) com operação própria
        tem_evidencia_operacao = Evidencia.objects.filter(empresa=empresa, ativa=True).filter(
            Q(gerada_por="IA", classificacao="HIPOTESE") | Q(gerada_por="HUMANO")).exists()
    if tem_evidencia_operacao:
        return "REVISAR", f"atividade {principal} {desc[:60]} fora do escopo, mas há evidência de operação – revisar"
    return "FORA", f"atividade {principal} {desc[:70]} sem relação com lubrificantes/graxas/filtros"


def escopo_efetivo(empresa):
    """Escopo considerando a decisão humana registrada na oportunidade."""
    from prospeccao.models import Oportunidade
    op = Oportunidade.objects.filter(empresa=empresa, escopo_humano=True).exclude(escopo="").first()
    if op is not None:
        return op.escopo, op.escopo_motivo or "definido pelo gestor"
    return classificar(empresa)


def pertinente_para_gasto(empresa):
    """Só empresas dentro dos perfis recebem chamadas pagas (pesquisa web / IA). Retorna (bool, motivo)."""
    esc, mot = escopo_efetivo(empresa)
    if esc in DENTRO:
        return True, f"{PERFIS[esc]}: {mot}"
    return False, f"{ROTULOS[esc]}: {mot} – sem chamadas pagas até revisão"
