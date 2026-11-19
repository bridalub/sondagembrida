"""
Aplica dados de uma fonte de enriquecimento (Receita via BrasilAPI) a uma Empresa.

- Campos de cadastro oficial (CNAE, abertura, situação cadastral, natureza, porte) seguem a Receita.
- Campos comerciais vindos do Salesforce (nome, telefone, e-mail, cidade...) NÃO são sobrescritos;
  só são preenchidos se estiverem vazios.
- Telefone/e-mail do cadastro da Receita ficam como canal INCERTO (muitas vezes são do contador).
- Sócios (QSA) NÃO são importados (dados pessoais/societários sem necessidade nesta etapa).
- Cada valor fica registrado em DadoColetado com fonte e data.
"""
import hashlib
from datetime import date

from django.utils import timezone

from carteira.models import CanalEmpresa, Contato, DadoColetado
from carteira.services.normalizacao import classificar_email, digitos_telefone

OFICIAIS = ["cnae", "atividade_principal", "cnaes_secundarios", "data_abertura", "situacao_cadastral",
            "situacao_cadastral_data", "motivo_situacao_cadastral", "natureza_juridica", "porte"]
PREENCHER_SE_VAZIO = ["razao_social", "nome_fantasia", "cidade", "uf", "cep", "endereco"]


def _data(v):
    try:
        return date.fromisoformat(v[:10]) if v else None
    except ValueError:
        return None


def registrar_dado(empresa, campo, valor, fonte, referencia="", agora=None):
    agora = agora or timezone.now()
    valor = "" if valor is None else str(valor)
    obj, criado = DadoColetado.objects.get_or_create(
        empresa=empresa, campo=campo, fonte=fonte,
        defaults={"valor": valor, "referencia": referencia[:500], "primeira_coleta_em": agora, "coletado_em": agora})
    if not criado:
        obj.valor, obj.referencia, obj.coletado_em = valor, referencia[:500], agora
        obj.save(update_fields=["valor", "referencia", "coletado_em"])


def registrar_canal(empresa, tipo, valor, fonte, referencia="", classificacao=None, agora=None, verificacao=None):
    agora = agora or timezone.now()
    if tipo == CanalEmpresa.Tipo.TELEFONE:
        valor = digitos_telefone(valor)
    elif tipo == CanalEmpresa.Tipo.EMAIL:
        valor = (valor or "").strip().lower()
        if "@" not in valor:
            return None
        classificacao = classificacao or classificar_email(valor)
    if not valor:
        return None
    informada = verificacao  # só uma verificação INFORMADA pode elevar o nível de um canal existente
    verificacao = verificacao or CanalEmpresa.Verificacao.ENCONTRADO
    dims = {  # dimensões oficiais a partir do resumo legado
        "CADASTRAL": dict(origem_canal="CADASTRAL", confianca_associacao="MEDIA", aplica_a="ESTABELECIMENTO",
                          evidencia_associacao="cadastro do CNPJ (pode ser do contador; não validado)"),
        "CONFIRMADO_FONTE": dict(origem_canal="PUBLICACAO", confianca_associacao="ALTA", aplica_a="EMPRESA"),
        "CONFLITO": dict(origem_canal="PUBLICACAO", confianca_associacao="CONFLITO", aplica_a="DESCONHECIDO"),
        "CONFIRMADO_HUMANO": dict(origem_canal="HUMANO", confianca_associacao="ALTA", aplica_a="EMPRESA"),
    }.get(verificacao, dict(origem_canal="PUBLICACAO", confianca_associacao="BAIXA", aplica_a="DESCONHECIDO"))
    obj, criado = CanalEmpresa.objects.get_or_create(
        empresa=empresa, tipo=tipo, valor=valor[:300],
        defaults={"fonte": fonte, "referencia": referencia[:500], "classificacao": classificacao or "INCERTO",
                  "verificacao": verificacao, "valor_normalizado": valor[:300], **dims,
                  "primeira_coleta_em": agora, "coletado_em": agora})
    if not criado:
        obj.coletado_em = agora
        campos = ["coletado_em"]
        # só "sobe" o nível de verificação; nunca rebaixa uma confirmação existente
        ordem = ["CONFLITO", "CADASTRAL", "ENCONTRADO", "CONFIRMADO_FONTE", "CONFIRMADO_HUMANO"]
        if informada and ordem.index(informada) > ordem.index(obj.verificacao):
            obj.verificacao = verificacao
            campos.append("verificacao")
        obj.save(update_fields=campos)
    return obj


def aplicar_dados_cnpj(empresa, d, fonte, data_referencia=None, rotulo_fonte=""):
    """
    data_referencia: data a que os dados se referem (competência da base Receita ou data da consulta à API).
    Telefones/e-mail entram como canais CADASTRAIS (não validados; podem ser do contador).
    Sócios (QSA) NÃO são importados nesta etapa.
    """
    agora = timezone.now()
    valores = {
        "razao_social": d.razao_social, "nome_fantasia": d.nome_fantasia, "cnae": d.cnae,
        "atividade_principal": d.cnae_descricao, "cnaes_secundarios": d.cnaes_secundarios,
        "data_abertura": _data(d.data_abertura), "situacao_cadastral": d.situacao_cadastral,
        "situacao_cadastral_data": _data(d.situacao_cadastral_data), "natureza_juridica": d.natureza_juridica,
        "porte": d.porte, "motivo_situacao_cadastral": getattr(d, "motivo", "") or "",
        "cidade": d.municipio.title() if d.municipio else "", "uf": d.uf,
        "cep": d.cep, "endereco": " – ".join(x for x in (d.logradouro, d.bairro) if x),
    }
    for campo, v in valores.items():
        if v in (None, "", []):
            continue
        registrar_dado(empresa, campo, v if not isinstance(v, (list, date)) else (
            v.isoformat() if isinstance(v, date) else "; ".join(f"{c['codigo']} {c['descricao']}" for c in v)),
            fonte, d.url, agora)
        if campo in OFICIAIS or (campo in PREENCHER_SE_VAZIO and not getattr(empresa, campo)):
            setattr(empresa, campo, v)
    if not empresa.nome:
        empresa.nome = d.nome_fantasia or d.razao_social
    if d.situacao_cadastral:
        empresa.situacao_cadastral_referencia = data_referencia or timezone.localdate()
        empresa.situacao_cadastral_fonte = (rotulo_fonte or fonte)[:120]
    for tel in d.telefones:
        registrar_canal(empresa, CanalEmpresa.Tipo.TELEFONE, tel, fonte, d.url, "INCERTO", agora,
                        CanalEmpresa.Verificacao.CADASTRAL)
    if d.email:
        registrar_canal(empresa, CanalEmpresa.Tipo.EMAIL, d.email, fonte, d.url, None, agora,
                        CanalEmpresa.Verificacao.CADASTRAL)
    empresa.save()
    return empresa


def ultima_coleta(empresa, fonte):
    return (DadoColetado.objects.filter(empresa=empresa, fonte=fonte).order_by("-coletado_em")
            .values_list("coletado_em", flat=True).first())
