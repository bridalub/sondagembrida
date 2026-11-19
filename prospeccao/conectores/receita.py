"""
Conector de DESCOBERTA (com CNPJ): base pública de CNPJ da Receita importada localmente (app `receita`).

- Recorta por UF + municípios da pesquisa, situação cadastral (padrão ATIVA) e, opcionalmente,
  prefixos de CNAE (principal ou secundário). Sem filtro de CNAE = todas as atividades.
- Trabalha o universo em LOTES: `Pesquisa.limite_resultados` por execução, ordem por CNPJ; o cursor
  fica em `Pesquisa.cursor` e a execução seguinte continua do ponto em que parou.
- Os dados são da COMPETÊNCIA da carga ativa (mensal) – não é consulta em tempo real.
- Contatos vindos daqui são CADASTRAIS (podem ser do contador; não validados).
"""
from django.db.models import Q

from carteira.services.normalizacao import normalizar_texto
from prospeccao.conectores.base import FonteIndisponivel, ResultadoDescoberta
from prospeccao.conectores.brasilapi import DadosCNPJ
from prospeccao.models import Pesquisa, linhas

ROTULO_FONTE = "Receita Federal – dados abertos do CNPJ"
SITUACAO_CODIGOS = {"NULA": "01", "ATIVA": "02", "SUSPENSA": "03", "INAPTA": "04", "BAIXADA": "08"}


def carga_ativa():
    from receita.models import CargaReceita
    return CargaReceita.ativa_atual()


def referencia(carga):
    pub = f", arquivos de {carga.data_arquivos:%d/%m/%Y}" if carga.data_arquivos else ""
    return f"{ROTULO_FONTE} – competência {carga.competencia}{pub}"


_CNAES = {}


def _desc_cnae(carga, codigo):
    if carga.competencia not in _CNAES:
        from receita.importador import tabela
        _CNAES[carga.competencia] = tabela(carga.competencia, "CNAE")
    return _CNAES[carga.competencia].get(codigo, "")


def estab_para_dados(e, carga):
    tipo = "MATRIZ" if e.matriz else ("FILIAL" if e.matriz is False else "")
    logr = " ".join(x for x in (e.tipo_logradouro, e.logradouro, e.numero) if x)
    if e.complemento:
        logr += f" {e.complemento}"
    return DadosCNPJ(
        cnpj=e.cnpj, razao_social=e.razao_social, nome_fantasia=e.nome_fantasia, situacao_cadastral=e.situacao,
        situacao_cadastral_data=e.data_situacao.isoformat() if e.data_situacao else "", motivo=e.motivo,
        data_abertura=e.data_abertura.isoformat() if e.data_abertura else "", cnae=e.cnae_principal,
        cnae_descricao=e.cnae_descricao,
        cnaes_secundarios=[{"codigo": c, "descricao": _desc_cnae(carga, c)} for c in e.cnaes_secundarios.split() if c],
        natureza_juridica=e.natureza, porte=e.porte, matriz_filial=tipo, logradouro=logr.strip(), bairro=e.bairro,
        municipio=e.municipio, uf=e.uf, cep=e.cep, telefones=[t for t in (e.telefone1, e.telefone2) if t],
        email=e.email, socios=[], url=referencia(carga),
    )


def consultar_local(cnpj):
    """(DadosCNPJ, carga) do CNPJ na carga ativa, ou (None, carga)."""
    from receita.models import EstabelecimentoReceita
    carga = carga_ativa()
    if not carga:
        return None, None
    e = EstabelecimentoReceita.objects.filter(carga=carga, cnpj=cnpj).first()
    return (estab_para_dados(e, carga) if e else None), carga


def universo(pesquisa, carga):
    from receita.models import EstabelecimentoReceita
    qs = EstabelecimentoReceita.objects.filter(carga=carga, uf=pesquisa.uf.upper())
    cidades = {normalizar_texto(c) for c in linhas(pesquisa.cidades)}
    if cidades:
        from receita.importador import tabela
        codigos = [c for c, d in tabela(carga.competencia, "MUNICIPIO").items() if normalizar_texto(d) in cidades]
        qs = qs.filter(municipio_codigo__in=codigos)
    sits = [SITUACAO_CODIGOS[s.strip().upper()] for s in (pesquisa.situacoes_receita or "").split(",")
            if s.strip().upper() in SITUACAO_CODIGOS]
    if sits:
        qs = qs.filter(situacao_codigo__in=sits + [s.lstrip("0") for s in sits])
    filtro = Q()
    for p in linhas(pesquisa.cnaes_filtro):
        p = "".join(ch for ch in p if ch.isdigit())
        if p:
            filtro |= Q(cnae_principal__startswith=p) | Q(cnaes_secundarios__regex=rf"(^| ){p}")
    if filtro:
        qs = qs.filter(filtro)
    return qs


class ReceitaBaseDescoberta:
    codigo = "receita_cnpj"

    def __init__(self, fonte):
        self.fonte = fonte

    def descobrir(self, pesquisa, ctx):
        carga = carga_ativa()
        if not carga:
            raise FonteIndisponivel("nenhuma carga da base Receita concluída (rode manage.py importar_receita)")
        qs = universo(pesquisa, carga).order_by("cnpj")
        total = qs.count()
        cur = (pesquisa.cursor or {}).get(self.codigo, {})
        ultimo = cur.get("ultimo") if cur.get("carga") == carga.pk else None
        lote = list((qs.filter(cnpj__gt=ultimo) if ultimo else qs)[:pesquisa.limite_resultados])
        ctx.consultas[self.codigo] = ctx.consultas.get(self.codigo, 0) + 1
        ctx.log(f"Base Receita ({referencia(carga)}): universo do recorte = {total:,} estabelecimentos; "
                f"lote atual a partir de {ultimo or 'início'}: {len(lote)}", fonte=self.codigo)
        for i, e in enumerate(lote, 1):
            yield ResultadoDescoberta(
                id_externo=e.cnpj, nome=(e.nome_fantasia or e.razao_social or e.cnpj)[:255],
                endereco=" ".join(x for x in (e.tipo_logradouro, e.logradouro, e.numero) if x)[:255],
                cidade=e.municipio.title()[:120], uf=e.uf, cep=e.cep, telefone=e.telefone1, email=e.email,
                atividade=f"CNAE {e.cnae_principal} {e.cnae_descricao}"[:500], cnpj=e.cnpj,
                dados={"fonte": ROTULO_FONTE, "competencia": carga.competencia, "carga": carga.pk,
                       "situacao": e.situacao, "contatos": "cadastrais (não validados)"})
            if i % 25 == 0 or i == len(lote):
                novo = {**(pesquisa.cursor or {}), self.codigo: {"carga": carga.pk, "ultimo": e.cnpj, "total": total}}
                Pesquisa.objects.filter(pk=pesquisa.pk).update(cursor=novo)
                pesquisa.cursor = novo
        if len(lote) < pesquisa.limite_resultados:
            fim = {**(pesquisa.cursor or {}), self.codigo: {"carga": carga.pk, "ultimo": None, "total": total,
                                                            "ciclo_completo": True}}
            Pesquisa.objects.filter(pk=pesquisa.pk).update(cursor=fim)
            ctx.log("Universo do recorte percorrido até o fim; a próxima execução recomeça do início "
                    "(empresas já processadas não são duplicadas).", fonte=self.codigo)
