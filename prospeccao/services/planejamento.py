"""
Planejamento das pesquisas (versionado).

Origens do plano:
  - IA      : proposto pela IA a partir do objetivo do gestor (exige credencial; ver services/ia.py)
  - FRENTES : montado a partir das frentes configuradas (regras; NÃO é IA)
  - MANUAL  : ajustado pelo gestor sobre uma versão anterior

Regras:
  - todo CNAE do plano é conferido na tabela oficial da Receita (competência da carga ativa); código que não
    existe é REJEITADO e listado em `cnaes_rejeitados` (nunca aceito);
  - preferências NUNCA viram exclusão; exclusões só existem quando o gestor as escreveu explicitamente;
  - cada execução registra a versão do plano usada.
"""
import re
import time

from django.db import transaction
from django.utils import timezone

from carteira.services.normalizacao import normalizar_texto
from prospeccao.models import FrenteComercial, PlanoPesquisa, UsoIA, linhas

VERSAO_PROMPT_PLANO = "plano-v1"

PESOS_PADRAO = {
    "cnae_principal": 3.0, "cnae_secundario": 1.5, "termo_nome": 1.0, "matriz": 0.5, "porte_demais": 0.75,
    "porte_epp": 0.4, "empresario_individual": -0.75, "dominio_proprio": 0.5, "telefone": 0.25,
    "evidencia_existente": 1.0,
}
CONSULTAS_PADRAO = ['"{cnpj_formatado}"', '"{razao_social}" {municipio}', '{nome_fantasia} {municipio} telefone']


class IANaoConfigurada(Exception):
    pass


class OrcamentoIndisponivel(IANaoConfigurada):
    """IA configurada, mas sem teto/saldo de orçamento para a chamada (nada foi enviado)."""

    pass


def tabela_cnae():
    from receita.importador import tabela
    from receita.models import CargaReceita
    carga = CargaReceita.ativa_atual()
    if not carga:
        return {}, None
    return tabela(carga.competencia, "CNAE"), carga.competencia


def verificar_cnaes(propostos):
    """propostos: lista de códigos/prefixos (str) ou {codigo, motivo}. Retorna (validos[{codigo,descricao,origem}], rejeitados)."""
    tab, comp = tabela_cnae()
    validos, rejeitados = {}, []
    for item in propostos:
        bruto = item.get("codigo") if isinstance(item, dict) else item
        motivo = item.get("motivo", "") if isinstance(item, dict) else ""
        cod = re.sub(r"\D", "", str(bruto or ""))
        if not (2 <= len(cod) <= 7):
            rejeitados.append({"codigo": str(bruto), "motivo": "formato inválido"})
            continue
        achados = [c for c in tab if c == cod] if len(cod) == 7 else [c for c in tab if c.startswith(cod)]
        if not achados:
            rejeitados.append({"codigo": cod, "motivo": f"não existe na tabela CNAE da Receita ({comp})"})
            continue
        for c in achados:
            validos.setdefault(c, {"codigo": c, "descricao": tab[c], "origem": f"prefixo {cod}" if len(cod) < 7 else "código",
                                   "motivo": motivo})
    return sorted(validos.values(), key=lambda x: x["codigo"]), rejeitados


def _cfg_frente(f, cnaes, rejeitados_acc, origem="frente"):
    validos, rejeitados = verificar_cnaes(cnaes)
    rejeitados_acc += [{**r, "frente": f.nome} for r in rejeitados]
    return {"frente": f.nome, "frente_id": f.pk, "atividades": [f.descricao], "cnaes": validos,
            "termos": [normalizar_texto(t) for t in linhas(f.termos_operacao)],
            "sinais": linhas(f.sinais_operacionais), "contextos_negativos": linhas(f.contextos_negativos),
            "aplicacoes": list(f.aplicacoes.values_list("nome", flat=True)), "origem": origem}


def conteudo_base(pesquisa):
    return {
        "objetivo": pesquisa.objetivo,
        "interpretacao": "",
        "por_frente": {},
        "consultas_externas": CONSULTAS_PADRAO,
        "ambiguidades": [],
        "exclusoes_explicitas": {"termos": linhas(pesquisa.exclusoes_termos), "cnaes": linhas(pesquisa.exclusoes_cnaes)},
        "criterios_obrigatorios": linhas(pesquisa.criterios_obrigatorios),
        "preferencias": linhas(pesquisa.preferencias),
        "pesos": dict(PESOS_PADRAO),
    }


@transaction.atomic
def salvar(pesquisa, conteudo, origem, usuario=None, rejeitados=None, uso_ia=None, observacao=""):
    ultimo = pesquisa.planos.order_by("-versao").first()
    plano = PlanoPesquisa.objects.create(pesquisa=pesquisa, versao=(ultimo.versao + 1) if ultimo else 1, origem=origem,
                                         conteudo=conteudo, cnaes_rejeitados=rejeitados or [], uso_ia=uso_ia,
                                         criado_por=usuario, observacao=observacao[:255])
    type(pesquisa).objects.filter(pk=pesquisa.pk).update(plano_ativo=plano)
    pesquisa.plano_ativo = plano
    return plano


def plano_das_frentes(pesquisa, usuario=None):
    conteudo = conteudo_base(pesquisa)
    conteudo["interpretacao"] = ("Plano montado a partir das frentes selecionadas e dos critérios do gestor "
                                 "(regras configuradas; NÃO foi gerado por IA).")
    rejeitados = []
    for f in pesquisa.frentes.all():
        conteudo["por_frente"][str(f.pk)] = _cfg_frente(f, linhas(f.cnaes_referencia), rejeitados)
    return salvar(pesquisa, conteudo, PlanoPesquisa.Origem.FRENTES, usuario, rejeitados)


def plano_manual(pesquisa, usuario, dados, observacao=""):
    """dados: {frente_id: {"cnaes": [...], "termos": [...], "sinais": [...], "contextos_negativos": [...]}, "_geral": {...}}"""
    base = (pesquisa.plano_ativo.conteudo if pesquisa.plano_ativo else conteudo_base(pesquisa))
    conteudo = {**base, "por_frente": dict(base.get("por_frente", {}))}
    rejeitados = []
    for fid, campos in dados.items():
        if fid == "_geral":
            for k in ("interpretacao", "consultas_externas", "ambiguidades", "preferencias", "criterios_obrigatorios"):
                if k in campos:
                    conteudo[k] = campos[k]
            if "exclusoes_termos" in campos or "exclusoes_cnaes" in campos:
                conteudo["exclusoes_explicitas"] = {"termos": campos.get("exclusoes_termos", []),
                                                    "cnaes": campos.get("exclusoes_cnaes", [])}
            continue
        f = FrenteComercial.objects.get(pk=int(fid))
        cfg = dict(conteudo["por_frente"].get(fid) or _cfg_frente(f, [], []))
        if "cnaes" in campos:
            validos, rej = verificar_cnaes(campos["cnaes"])
            cfg["cnaes"] = validos
            rejeitados += [{**r, "frente": f.nome} for r in rej]
        for k in ("termos", "sinais", "contextos_negativos"):
            if k in campos:
                cfg[k] = [normalizar_texto(t) for t in campos[k]] if k == "termos" else campos[k]
        cfg["origem"] = "manual"
        conteudo["por_frente"][fid] = cfg
    return salvar(pesquisa, conteudo, PlanoPesquisa.Origem.MANUAL, usuario, rejeitados, observacao=observacao)


def plano_ia(pesquisa, usuario=None):
    """Plano proposto pela IA (exige credencial). Registra UsoIA mesmo em falha/não configurada."""
    from prospeccao.services import ia
    ok, msg = ia.status()
    if not ok:
        UsoIA.objects.create(tipo="PLANO", provedor="-", versao_prompt=VERSAO_PROMPT_PLANO, pesquisa=pesquisa,
                             status="NAO_CONFIGURADA", erro=msg)
        raise IANaoConfigurada(msg)
    from prospeccao.services import orcamento as orcm
    analisador = ia.obter_analisador()
    analisador.max_tokens = pesquisa.ia_max_tokens_saida
    frentes = list(pesquisa.frentes.all())
    uso = UsoIA(tipo="PLANO", provedor=analisador.nome, modelo=analisador.modelo, versao_prompt=VERSAO_PROMPT_PLANO,
                pesquisa=pesquisa, materiais=[{"objetivo": pesquisa.objetivo, "frentes": [f.nome for f in frentes]}])
    orc = orcm.Orcamento(pesquisa)
    try:
        (bruto, usage, modelo), reserva, inicio = orcm.executar_ia(
            orc, analisador, uso, ia._pedido_plano(pesquisa, frentes) + ia.SISTEMA_PLANO,
            lambda: analisador.planejar(pesquisa, frentes))
    except orcm.SemOrcamento as e:
        UsoIA.objects.create(tipo="PLANO", provedor=analisador.nome, modelo=analisador.modelo,
                             versao_prompt=VERSAO_PROMPT_PLANO, pesquisa=pesquisa, status="SEM_ORCAMENTO", erro=str(e))
        raise OrcamentoIndisponivel(f"Planejamento por IA não executado: {e}. Defina o teto financeiro da pesquisa.")
    uso.modelo, uso.resultado, uso.status = modelo, bruto, "OK"
    ia.registrar_uso(uso, usage, inicio)
    orc.liquidar(reserva, uso.custo_estimado_usd, ia=True)
    conteudo = conteudo_base(pesquisa)
    conteudo["interpretacao"] = bruto.get("interpretacao", "")
    conteudo["justificativa_ia"] = bruto.get("justificativa", "")
    conteudo["consultas_externas"] = bruto.get("consultas_externas") or CONSULTAS_PADRAO
    conteudo["ambiguidades"] = bruto.get("ambiguidades", [])
    conteudo["preferencias_interpretadas"] = bruto.get("preferencias_interpretadas", [])
    # exclusões: só as que o gestor escreveu; sugestões da IA ficam como SUGESTÃO, não aplicadas
    conteudo["exclusoes_sugeridas_pela_ia"] = bruto.get("exclusoes_explicitas", [])
    rejeitados = []
    por_nome = {f.nome: f for f in frentes}
    for item in bruto.get("por_frente", []):
        f = por_nome.get(item.get("frente"))
        if not f:
            continue
        cfg = _cfg_frente(f, item.get("cnaes", []), rejeitados, origem="ia")
        cfg["atividades"] = item.get("atividades", [])[:30]
        cfg["termos"] = [normalizar_texto(t) for t in item.get("termos", [])][:40] or cfg["termos"]
        cfg["sinais"] = item.get("sinais", [])[:30] or cfg["sinais"]
        cfg["consultas_descoberta"] = [q for q in item.get("consultas_descoberta", []) if "{" not in q][:10] \
            or cfg.get("consultas_descoberta", [])
        conteudo["por_frente"][str(f.pk)] = cfg
    for f in frentes:  # frente sem proposta da IA continua com a configuração do gestor
        if str(f.pk) not in conteudo["por_frente"]:
            conteudo["por_frente"][str(f.pk)] = _cfg_frente(f, linhas(f.cnaes_referencia), rejeitados)
    return salvar(pesquisa, conteudo, PlanoPesquisa.Origem.IA, usuario, rejeitados, uso_ia=uso,
                  observacao=f"IA {modelo}")


def plano_para_execucao(pesquisa):
    """Plano ativo; se não houver, monta a partir das frentes (sem IA)."""
    return pesquisa.plano_ativo or plano_das_frentes(pesquisa)
