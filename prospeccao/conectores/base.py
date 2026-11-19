"""
Infraestrutura comum dos conectores.

Tipos de conector:
  - Descoberta     (descobrir(pesquisa, ctx) -> iterável de ResultadoDescoberta)
  - Identificação  (confirma qual empresa/CNPJ corresponde a um resultado)
  - Enriquecimento (consulta dados de um CNPJ já conhecido)
Uma API que só consulta CNPJ conhecido NÃO é conector de descoberta.
"""
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

import requests
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger("prospeccao")

USER_AGENT = "BridaProspeccao/0.1 (prospeccao comercial B2B; baixo volume; respeita robots.txt)"


class OrcamentoEsgotado(Exception):
    """Limite de consultas/tempo atingido: a execução termina como PARCIAL, sem perder dados."""


class PrazoEsgotado(OrcamentoEsgotado):
    """Tempo máximo da execução: interrompe todas as etapas (resultado PARCIAL)."""


class FonteIndisponivel(Exception):
    def __init__(self, mensagem="", retry_after=None):
        super().__init__(mensagem)
        self.retry_after = retry_after


class ExecucaoCancelada(Exception):
    pass


class ExecucaoPausada(ExecucaoCancelada):
    """Pausa: não inicia chamada nova. O que já estava em andamento continua contabilizado."""


@dataclass
class ResultadoDescoberta:
    id_externo: str
    nome: str
    endereco: str = ""
    cidade: str = ""
    uf: str = ""
    cep: str = ""
    telefone: str = ""
    email: str = ""
    site: str = ""
    atividade: str = ""
    cnpj: str = ""
    url_referencia: str = ""
    dados: dict = field(default_factory=dict)


class ContextoExecucao:
    """Orçamentos, prazo, cancelamento e log de uma execução."""

    def __init__(self, execucao=None, prazo_segundos=None, logar=None):
        self.execucao = execucao
        self.inicio = time.monotonic()
        self.prazo = prazo_segundos
        self.consultas = dict(execucao.consultas) if execucao else {}
        self.custo = Decimal("0")
        self._logar = logar
        self._ultima_verificacao = 0.0

    def log(self, mensagem, nivel="INFO", fonte=""):
        logger.info("[%s] %s", fonte, mensagem)
        if self._logar:
            self._logar(mensagem, nivel, fonte)

    def verificar(self, forcar=False):
        """Prazo, pausa e cancelamento. forcar=True lê o pedido antes de abrir chamada paga."""
        if self.prazo and time.monotonic() - self.inicio > self.prazo:
            raise PrazoEsgotado(f"tempo máximo da execução ({self.prazo}s)")
        if self.execucao and (forcar or time.monotonic() - self._ultima_verificacao > 5):
            self._ultima_verificacao = time.monotonic()
            from prospeccao.models import Execucao
            atual = Execucao.objects.filter(pk=self.execucao.pk).values(
                "cancelamento_solicitado", "pausa_solicitada").first()
            Execucao.objects.filter(pk=self.execucao.pk).update(heartbeat_em=timezone.now(), consultas=self.consultas)
            if atual and atual.get("pausa_solicitada"):
                raise ExecucaoPausada("pausa solicitada pelo usuário")
            if atual and atual.get("cancelamento_solicitado"):
                raise ExecucaoCancelada()


class ClienteFonte:
    """Cliente HTTP de uma fonte: respeita intervalo, limites por execução/dia, retentativas e custo."""

    STATUS_RETENTAVEL = {429, 500, 502, 503, 504}

    def __init__(self, fonte, ctx, limite_execucao=None, sessao=None):
        self.fonte = fonte
        self.ctx = ctx
        self.limite = min(filter(None, [fonte.max_consultas_execucao, limite_execucao])) \
            if (fonte.max_consultas_execucao or limite_execucao) else None
        self.sessao = sessao or requests.Session()
        self.sessao.headers["User-Agent"] = USER_AGENT
        self._ultima = 0.0
        self._retry_after = None

    @property
    def usadas(self):
        return self.ctx.consultas.get(self.fonte.codigo, 0)

    def _checar_orcamento(self):
        from prospeccao.models import UsoFonte
        if self.fonte.codigo == "google_serpapi":
            # A franquia incluída da conta é o limite. max_consultas_dia não volta a valer aqui.
            from prospeccao.services.prontidao import exigir_vaga_google
            exigir_vaga_google(self.ctx)
            return
        if self.limite is not None and self.usadas >= self.limite:
            raise OrcamentoEsgotado(f"limite de consultas de '{self.fonte.codigo}' nesta execução ({self.limite})")
        if self.fonte.max_consultas_dia:
            uso = UsoFonte.objects.filter(fonte=self.fonte, dia=timezone.localdate()).values_list("consultas", flat=True).first()
            if (uso or 0) >= self.fonte.max_consultas_dia:
                raise OrcamentoEsgotado(f"limite diário de '{self.fonte.codigo}' ({self.fonte.max_consultas_dia})")

    def _registrar(self, erro=False):
        from prospeccao.models import UsoFonte
        self.ctx.consultas[self.fonte.codigo] = self.usadas + 1
        custo = self.fonte.custo_por_consulta or Decimal("0")
        self.ctx.custo += custo
        uso, _ = UsoFonte.objects.get_or_create(fonte=self.fonte, dia=timezone.localdate())
        UsoFonte.objects.filter(pk=uso.pk).update(consultas=F("consultas") + 1, custo=F("custo") + custo,
                                                  erros=F("erros") + (1 if erro else 0))

    def _esperar(self):
        falta = self.fonte.intervalo_minimo_segundos - (time.monotonic() - self._ultima)
        if falta > 0:
            time.sleep(falta)

    def requisitar(self, metodo, url, **kwargs):
        kwargs.setdefault("timeout", self.fonte.timeout_segundos)
        ultimo_erro = None
        for tentativa in range(self.fonte.max_retentativas + 1):
            self.ctx.verificar()
            self._checar_orcamento()
            self._esperar()
            self._ultima = time.monotonic()
            try:
                try:
                    resp = self.sessao.request(metodo, url, **kwargs)
                except requests.RequestException as e:
                    self._registrar(erro=True)
                    ultimo_erro = f"{type(e).__name__}: {e}"
                else:
                    self._registrar(erro=resp.status_code >= 400)
                    if resp.status_code not in self.STATUS_RETENTAVEL:
                        return resp
                    ultimo_erro = f"HTTP {resp.status_code}"
                    espera = resp.headers.get("Retry-After")
                    if espera and str(espera).isdigit():
                        self._retry_after = int(espera)
                        time.sleep(min(self._retry_after, 60))
            finally:
                if self.fonte.codigo == "google_serpapi":
                    from prospeccao.services.prontidao import liberar_vaga_google
                    liberar_vaga_google(self.ctx)
            if tentativa < self.fonte.max_retentativas:
                time.sleep(min(2 ** tentativa * max(self.fonte.intervalo_minimo_segundos, 1), 30))
        extra = f"; Retry-After={self._retry_after}" if getattr(self, "_retry_after", None) else ""
        raise FonteIndisponivel(
            f"{self.fonte.codigo}: {ultimo_erro}{extra} após {self.fonte.max_retentativas + 1} tentativa(s)",
            retry_after=getattr(self, "_retry_after", None))
