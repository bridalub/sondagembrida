"""
Conector de IDENTIFICAÇÃO/ENRIQUECIMENTO: BrasilAPI – CNPJ.

- Documentação: https://brasilapi.com.br/docs#tag/CNPJ  (GET /api/cnpj/v1/{cnpj})
- Os dados vêm do Cadastro Nacional da Pessoa Jurídica (dados abertos da Receita Federal).
- Só consulta um CNPJ JÁ CONHECIDO: não descobre empresas.
- CNPJ alfanumérico ainda não é aceito pela API: nesses casos a consulta é pulada (registrado no log).
"""
from dataclasses import dataclass, field

from prospeccao.conectores.base import ClienteFonte


@dataclass
class DadosCNPJ:
    cnpj: str
    razao_social: str = ""
    nome_fantasia: str = ""
    situacao_cadastral: str = ""
    situacao_cadastral_data: str = ""
    motivo: str = ""
    data_abertura: str = ""
    cnae: str = ""
    cnae_descricao: str = ""
    cnaes_secundarios: list = field(default_factory=list)
    natureza_juridica: str = ""
    porte: str = ""
    matriz_filial: str = ""
    logradouro: str = ""
    bairro: str = ""
    municipio: str = ""
    uf: str = ""
    cep: str = ""
    telefones: list = field(default_factory=list)
    email: str = ""
    socios: list = field(default_factory=list)  # [{"nome","qualificacao"}] – sócio NÃO é comprador
    url: str = ""


def _txt(v):
    return "" if v is None else str(v).strip()


def converter(d, url=""):
    tels = [t for t in (_txt(d.get("ddd_telefone_1")), _txt(d.get("ddd_telefone_2"))) if t]
    logr = " ".join(x for x in (_txt(d.get("descricao_tipo_de_logradouro")), _txt(d.get("logradouro")),
                                _txt(d.get("numero"))) if x)
    return DadosCNPJ(
        cnpj=_txt(d.get("cnpj")), razao_social=_txt(d.get("razao_social")), nome_fantasia=_txt(d.get("nome_fantasia")),
        situacao_cadastral=_txt(d.get("descricao_situacao_cadastral")),
        situacao_cadastral_data=_txt(d.get("data_situacao_cadastral")),
        motivo=_txt(d.get("descricao_motivo_situacao_cadastral")),
        data_abertura=_txt(d.get("data_inicio_atividade")), cnae=_txt(d.get("cnae_fiscal")),
        cnae_descricao=_txt(d.get("cnae_fiscal_descricao")),
        cnaes_secundarios=[{"codigo": _txt(c.get("codigo")), "descricao": _txt(c.get("descricao"))}
                           for c in (d.get("cnaes_secundarios") or []) if c.get("codigo")],
        natureza_juridica=_txt(d.get("natureza_juridica")), porte=_txt(d.get("porte")),
        matriz_filial=_txt(d.get("descricao_identificador_matriz_filial")), logradouro=logr,
        bairro=_txt(d.get("bairro")), municipio=_txt(d.get("municipio")), uf=_txt(d.get("uf")), cep=_txt(d.get("cep")),
        telefones=tels, email=_txt(d.get("email")).lower(),
        socios=[],  # QSA ignorado de propósito: dados pessoais/societários não são necessários nesta etapa
        url=url,
    )


class BrasilAPICNPJ:
    codigo = "brasilapi_cnpj"

    def __init__(self, fonte):
        self.fonte = fonte
        self._cliente = None

    def consultar(self, cnpj, ctx):
        """DadosCNPJ, ou None se o CNPJ não existe na base (404)."""
        if not cnpj.isdigit():
            ctx.log(f"CNPJ alfanumérico {cnpj}: BrasilAPI ainda não suporta; verificação manual", "AVISO", self.codigo)
            return None
        if self._cliente is None:
            self._cliente = ClienteFonte(self.fonte, ctx)
        base = (self.fonte.lista_endpoints or ["https://brasilapi.com.br/api/cnpj/v1/"])[0]
        url = base.rstrip("/") + "/" + cnpj
        resp = self._cliente.requisitar("GET", url)
        if resp.status_code == 429:
            from prospeccao.conectores.base import FonteIndisponivel
            espera = str(resp.headers.get("Retry-After") or "")
            segundos = int(espera) if espera.isdigit() else None
            raise FonteIndisponivel(
                f"HTTP 429 limite de acesso da BrasilAPI; não é CNPJ inexistente. Retry-After={espera}"[:300],
                retry_after=segundos)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return converter(resp.json(), url)
