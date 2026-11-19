"""
Orçamento e limites de serviços PAGOS (IA e busca web) – controlados pelo PROGRAMA, não pela IA.

- `Pesquisa.teto_custo_usd` é o teto ACUMULADO da pesquisa (soma de todas as execuções e do planejamento).
  Vazio = nenhuma chamada paga é feita (fica pendência rastreável).
- `PROSPECCAO_TETO_GLOBAL_USD` (.env): teto ACUMULADO de TODAS as chamadas pagas DESTA aplicação (pesquisas, planos,
  verificações/testes) registradas no banco. Não enxerga gastos de outros projetos na mesma conta do provedor – para
  isso, use também o limite de orçamento do projeto no painel da OpenAI.
- Antes de cada chamada o custo MÁXIMO previsto (entrada estimada alta + máximo de saída + ações de pesquisa) é
  RESERVADO no banco (ReservaCusto, sob trava), somando gasto registrado + reservas em andamento de QUALQUER processo;
  a chamada só sai se couber nos tetos. Depois, o custo MEDIDO (tokens da resposta x preço + ações x tarifa) é gravado
  e só então a reserva é apagada. Sem medição (timeout, queda de conexão, 5xx), conta-se o MÁXIMO reservado (pode ter
  sido cobrada); erro 4xx de requisição recusada conta 0.
- A reserva é ESTIMATIVA conservadora, não limite técnico: tokens de entrada da pesquisa web (conteúdo lido pela
  ferramenta) não têm limite por parâmetro, então a chamada EM ANDAMENTO pode exceder a reserva (e o teto) nesse
  valor; o excesso é registrado pelo uso medido e bloqueia as chamadas seguintes.
- Não há retentativa automática do SDK (max_retries=0): cada tentativa é uma chamada reservada e registrada.
- Registros de custo (UsoIA/ConsultaWeb) são gravados fora de transações longas: uma falha posterior não os apaga.
- Preços: services/ia.py (conferidos nas páginas oficiais); Brave: US$ 5 / 1.000 consultas (BRAVE_PRECO_POR_CONSULTA_USD).
"""
import os
import time
from datetime import timedelta
from decimal import ROUND_UP, Decimal

from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from prospeccao.models import ConsultaWeb, ReservaCusto, UsoIA

PRECO_BRAVE = Decimal(os.getenv("BRAVE_PRECO_POR_CONSULTA_USD", "0.005"))
CHARS_POR_TOKEN = 3  # estimativa conservadora para português (superestima tokens de entrada)
VALIDADE_RESERVA = timedelta(minutes=30)  # chamadas têm timeout de 90-120 s; reserva mais velha = processo interrompido
_TRAVA = 0x0B1DA0  # chave do pg_advisory_xact_lock


def D(v):
    return Decimal(str(round(float(v or 0), 6)))


def Q5(v):
    """Valor de reserva com 5 casas (precisão do banco), arredondado para CIMA."""
    return D(v).quantize(Decimal("0.00001"), rounding=ROUND_UP)


def gasto_registrado(pesquisa=None):
    f = {"pesquisa": pesquisa} if pesquisa is not None else {}
    a = UsoIA.objects.filter(**f).aggregate(s=Sum("custo_estimado_usd"))["s"] or Decimal("0")
    b = ConsultaWeb.objects.filter(**f).aggregate(s=Sum("custo_estimado_usd"))["s"] or Decimal("0")
    return a + b


def reservado_em_andamento(pesquisa=None):
    qs = ReservaCusto.objects.filter(criada_em__gte=timezone.now() - VALIDADE_RESERVA)
    if pesquisa is not None:
        qs = qs.filter(pesquisa=pesquisa)
    return qs.aggregate(s=Sum("valor_usd"))["s"] or Decimal("0")


def teto_global():
    v = os.getenv("PROSPECCAO_TETO_GLOBAL_USD", "").strip()
    return Decimal(v) if v else None


def teto_aplicavel(pesquisa):
    """Teto monetário da pesquisa. Campo vazio reaproveita o saldo global já autorizado.
    Sem os dois, não há autorização de IA — e isso não vira chamadas ilimitadas."""
    proprio = getattr(pesquisa, "teto_custo_usd", None) if pesquisa is not None else None
    if proprio is not None and proprio > 0:
        return proprio
    return teto_global()


def _travar():
    if connection.vendor == "postgresql":
        with connection.cursor() as c:
            c.execute("SELECT pg_advisory_xact_lock(%s)", [_TRAVA])


class Orcamento:
    """Controle de UMA execução (ou verificação). Os tetos são conferidos contra o BANCO a cada reserva."""

    def __init__(self, pesquisa, execucao=None, teto=None, tipo=""):
        self.pesquisa, self.execucao, self.tipo = pesquisa, execucao, tipo
        self.teto = teto if teto is not None else teto_aplicavel(pesquisa)
        self.teto_local = teto is not None and pesquisa is None  # verificação: teto só desta chamada de teste
        self.global_teto = teto_global()
        self.gasto = Decimal("0")       # medido nesta execução
        self.reservado = Decimal("0")
        self.ia_chamadas = 0
        self.motivos_bloqueio = set()
        self.busca_bloqueada = ""       # limite da fonte atingido ou provedor indisponível: não insiste
        self.falhas_seguidas = 0
        self._reservas = []             # ReservaCusto desta execução, em ordem

    @property
    def anterior(self):
        return gasto_registrado(self.pesquisa) - self.gasto if self.pesquisa is not None else Decimal("0")

    @property
    def disponivel(self):
        if self.teto is None:
            return Decimal("0")
        if self.teto_local:
            return self.teto - self.gasto - self.reservado
        return self.teto - gasto_registrado(self.pesquisa) - reservado_em_andamento(self.pesquisa)

    def reservar(self, valor, ia=False):
        valor = Q5(valor)
        if ia and self.pesquisa is not None and self.ia_chamadas >= self.pesquisa.ia_max_chamadas:
            self.motivos_bloqueio.add(f"limite de {self.pesquisa.ia_max_chamadas} chamadas de IA na execução")
            return False
        if self.teto is None:
            if valor <= 0:
                # Franquia com preço zero: não gasta dólar e não autoriza chamadas ilimitadas.
                # A cota de requisições continua no controle da fonte.
                return True
            self.motivos_bloqueio.add(
                "teto de IA não autorizado (pesquisa sem teto próprio e sem saldo global restante); "
                "não é falta de crédito da franquia Google")
            return False
        with transaction.atomic():
            _travar()  # serializa reservas entre processos (PostgreSQL); no SQLite a escrita já é serializada
            if self.teto_local:
                usado = self.gasto + self.reservado
            else:
                usado = gasto_registrado(self.pesquisa) + reservado_em_andamento(self.pesquisa)
            if usado + valor > self.teto:
                self.motivos_bloqueio.add(
                    f"teto financeiro de US$ {self.teto} atingido (registrado + reservado US$ {usado:.4f}; "
                    f"próxima chamada exigiria reservar até US$ {valor:.4f})")
                return False
            if self.global_teto is not None:
                usado_g = gasto_registrado() + reservado_em_andamento()
                if usado_g + valor > self.global_teto:
                    self.motivos_bloqueio.add(
                        f"teto global PROSPECCAO_TETO_GLOBAL_USD={self.global_teto} atingido (registrado + reservado "
                        f"US$ {usado_g:.4f}; próxima chamada exigiria até US$ {valor:.4f})")
                    return False
            self._reservas.append(ReservaCusto.objects.create(pesquisa=self.pesquisa, valor_usd=valor,
                                                              tipo=(("IA" if ia else "BUSCA") + self.tipo)[:20]))
        self.reservado += valor
        return True

    def liquidar(self, reserva, real, ia=False):
        """Chamar DEPOIS de gravar o UsoIA/ConsultaWeb com o custo `real` (assim o custo nunca fica fora da soma)."""
        self.reservado -= Q5(reserva)
        self.gasto += D(real)
        for i, r in enumerate(self._reservas):
            if r.valor_usd == Q5(reserva):
                ReservaCusto.objects.filter(pk=r.pk).delete()
                del self._reservas[i]
                break
        if ia:
            self.ia_chamadas += 1


def custo_max_ia(analisador, texto_pedido):
    """Custo máximo de UMA chamada de IA (com retentativas do SDK), antes de enviá-la."""
    from prospeccao.services import ia
    entrada = len(texto_pedido or "") // CHARS_POR_TOKEN + 1_500  # + instruções e esquema
    uma = ia.custo(analisador.modelo, entrada, analisador.max_tokens)
    return D(uma * (1 + getattr(analisador, "max_retries", 0)))


def reservar_ia(orc, analisador, texto_pedido):
    """
    Reserva o custo máximo de UMA chamada de IA. Retorna a reserva ou levanta SemOrcamento.
    O chamador registra o UsoIA e chama orc.liquidar(reserva, custo_medido, ia=True).
    """
    reserva = custo_max_ia(analisador, texto_pedido)
    if not orc.reservar(reserva, ia=True):
        raise SemOrcamento("; ".join(sorted(orc.motivos_bloqueio)))
    return reserva


class SemOrcamento(Exception):
    pass


def buscar(busca, consulta, *, finalidade, orc, ctx, empresa=None, contexto=None, motivo=""):
    """Executa UMA chamada de busca web (consulta ou lista de consultas), registrando-a (inclusive falhas e bloqueios)."""
    texto = " | ".join(consulta) if isinstance(consulta, (list, tuple)) else consulta
    reg = ConsultaWeb(pesquisa=orc.pesquisa, execucao=orc.execucao, empresa=empresa, finalidade=finalidade,
                      provedor=getattr(busca, "codigo", "-"), consulta=texto[:300],
                      analise={"motivo": motivo[:300]} if motivo else {})
    orc.ultimo_registro = reg
    if ctx is not None:
        ctx.verificar(forcar=True)  # pausa/cancelamento impedem chamada nova; a em andamento segue contada
    if busca is None:
        reg.status = "NAO_CONFIGURADA"
        reg.save()
        return []
    if orc.busca_bloqueada:
        orc.motivos_bloqueio.add(orc.busca_bloqueada)
        reg.status, reg.erro = "SEM_ORCAMENTO", orc.busca_bloqueada
        reg.save()
        return []
    reserva = D(busca.custo_maximo())
    if (contexto or {}).get("modalidade", "organico") != "local" and hasattr(busca, "custo_maximo_com_visao"):
        reserva = D(busca.custo_maximo_com_visao())
    if not orc.reservar(reserva):
        reg.status, reg.erro = "SEM_ORCAMENTO", "; ".join(sorted(orc.motivos_bloqueio))
        reg.save()
        return []
    reg.custo_reservado_usd = reserva
    inicio = time.monotonic()
    res, erro_busca = [], None
    try:
        res = busca.buscar(consulta, ctx, contexto=contexto) if contexto is not None else busca.buscar(consulta, ctx)
        reg.status, reg.resultados = "OK", res
        orc.falhas_seguidas = 0
    except Exception as e:
        from prospeccao.conectores.base import ExecucaoCancelada, FonteIndisponivel, OrcamentoEsgotado, PrazoEsgotado
        erro_busca = e
        if isinstance(e, (ExecucaoCancelada, PrazoEsgotado)):
            reg.status, reg.erro = "FALHA", f"interrompida: {type(e).__name__}"
            reg.custo_estimado_usd = D(reserva)
            reg.duracao_ms = int((time.monotonic() - inicio) * 1000)
            reg.save()
            orc.liquidar(reserva, reserva)
            raise
        if isinstance(e, FonteIndisponivel):
            from prospeccao.services.retomada import agendar
            ex_id = getattr(orc.execucao, "pk", None)
            agendar("google_serpapi", f"velocidade:{ex_id or 'sem-execucao'}", str(e),
                    getattr(e, "retry_after", None))
            orc.busca_bloqueada = f"limite de velocidade SerpApi: {e}"
            reg.status, reg.erro = "FALHA", str(e)[:1000]
        elif isinstance(e, OrcamentoEsgotado):
            orc.busca_bloqueada = f"busca web: {e}"
            reg.status, reg.erro = "SEM_ORCAMENTO", str(e)[:1000]
        else:
            reg.status, reg.erro = "FALHA", f"{type(e).__name__}: {e}"[:1000]
            orc.falhas_seguidas += 1
            if orc.falhas_seguidas >= 3:
                orc.busca_bloqueada = (f"busca web indisponível ({orc.falhas_seguidas} falhas seguidas; "
                                       f"último erro: {reg.erro[:120]})")
        ctx.log(f"Busca web não concluída ({texto[:80]}): {reg.erro[:160]}", "AVISO", reg.provedor)
    ult = getattr(busca, "ultimo", None) or {}
    if reg.status == "SEM_ORCAMENTO" and not ult.get("medido"):
        real = Decimal("0")  # bloqueada antes de sair (limite da fonte)
    elif ult.get("medido"):
        real = D(ult["custo"])
    else:
        real = custo_sem_medicao(erro_busca, reserva)  # sem medição: o máximo reservado (pode ter sido cobrada)
    reg.custo_estimado_usd = real
    reg.modelo = (ult.get("modelo") or "")[:60]
    reg.tokens_entrada, reg.tokens_saida = ult.get("entrada", 0), ult.get("saida", 0)
    reg.chamadas_ferramenta = ult.get("acoes", 0)
    reg.fontes = ult.get("fontes", [])
    reg.acoes = ult.get("acoes_detalhe", [])
    reg.analise = {**(ult.get("analise") or {}), **({"motivo": motivo[:300]} if motivo else {})}
    reg.duracao_ms = int((time.monotonic() - inicio) * 1000)
    reg.save()
    orc.liquidar(reserva, real)  # depois de gravar: o custo nunca fica fora da soma
    return res


def estimativa(pesquisa, empresas, com_ia=True, com_busca=True, modelo=None, provedor_busca=None, descoberta_por_frente=None,
               com_plano=True, n_frentes=None):
    """
    Estimativa LIMITADA (antes de chamadas pagas em lote), com memória de cálculo. Três números:
      - TÍPICO: consumo provável (tokens médios);
      - RESERVA: soma do que o programa reserva antes de cada chamada (máximo de saída, entrada alta, todas as ações);
      - TETO: o programa só inicia uma chamada se a reserva couber; como a entrada da pesquisa web não tem limite
        técnico, a chamada em andamento pode excedê-lo (excesso registrado; chamadas seguintes bloqueadas).
    """
    from prospeccao.conectores.busca_web import OpenAIBusca
    from prospeccao.services import ia
    modelo = modelo or ia.modelo_configurado() or "gpt-6-luna"
    provedor_busca = provedor_busca or ("openai" if ia.provedor() in ("openai", "") else "brave")
    n_frentes = max(1, pesquisa.frentes.count()) if n_frentes is None else n_frentes
    desc = pesquisa.max_buscas_descoberta if descoberta_por_frente is None else descoberta_por_frente
    mx_out = pesquisa.ia_max_tokens_saida
    pe, ps = ia.preco(modelo)
    # IA (sem ferramenta): planejamento (1) + investigação (1 por empresa com site lido)
    plano_tip, plano_max = ia.custo(modelo, 4_000, 3_000), ia.custo(modelo, 6_000, mx_out)
    inv_tip, inv_max = ia.custo(modelo, 12_000, 2_500), ia.custo(modelo, 22_000, mx_out)
    if not com_plano:
        plano_tip = plano_max = 0
    n_ia = (int(com_plano) + empresas) if com_ia else 0
    ia_tip = (plano_tip + empresas * inv_tip) if com_ia else 0
    ia_max = (plano_max + empresas * inv_max) if com_ia else 0
    n_desc = n_frentes * desc if com_busca else 0
    chamadas_busca = (empresas + n_desc) if com_busca else 0
    if provedor_busca == "openai":
        b = OpenAIBusca(max_acoes=pesquisa.max_buscas_web_empresa)
        b.modelo = modelo
        acoes = b.max_acoes
        acoes_max = chamadas_busca * acoes
        tarifa = float(ia.PRECO_WEB_SEARCH_OPENAI)
        busca_tip_un, busca_max_un = b.custo_tipico(), float(b.custo_maximo())
        busca_tarifa_max = acoes_max * tarifa
        tokens_tip_un = busca_tip_un - acoes * tarifa
        tokens_max_un = busca_max_un - (acoes + 1) * tarifa  # a reserva inclui +1 ação por chamada
    else:
        chamadas_busca = (empresas * pesquisa.max_buscas_web_empresa + n_desc) if com_busca else 0
        acoes = 1
        acoes_max = chamadas_busca
        tarifa = float(PRECO_BRAVE)
        busca_tip_un = busca_max_un = tarifa
        busca_tarifa_max = acoes_max * tarifa
        tokens_tip_un = tokens_max_un = 0.0
    busca_tip, busca_max = chamadas_busca * busca_tip_un, chamadas_busca * busca_max_un
    memoria = [
        f"Modelo {modelo}: US$ {pe} (entrada) / US$ {ps} (saída) por milhão de tokens.",
        f"IA sem ferramenta: {n_ia} chamada(s) = {int(com_plano)} planejamento + {empresas} investigação(ões) "
        f"(só ocorre se o site da empresa for lido). Típico: plano 4k+3k tokens = US$ {plano_tip:.4f}; investigação "
        f"12k+2,5k = US$ {inv_tip:.4f}. Reserva: plano 6k + {mx_out} saída = US$ {plano_max:.4f}; investigação 22k + "
        f"{mx_out} saída = US$ {inv_max:.4f}.",
        f"Pesquisa web: {chamadas_busca} chamada(s) = {empresas} empresa(s) + {n_desc} de descoberta "
        f"({n_frentes} frente(s) x {desc}); até {acoes} ação(ões) por chamada (max_tool_calls) = até {acoes_max} "
        f"ações x US$ {tarifa} = US$ {busca_tarifa_max:.4f} em tarifa de ferramenta (a reserva soma +1 ação por "
        f"chamada, para a ação iniciada e não concluída vista na verificação real).",
        f"Conteúdo das pesquisas + saída (tokens): típico US$ {tokens_tip_un:.4f} por chamada "
        f"(3k + {acoes}x12k entrada, 1,5k saída) = US$ {chamadas_busca * tokens_tip_un:.4f}; reserva "
        f"US$ {tokens_max_un:.4f} por chamada = US$ {chamadas_busca * tokens_max_un:.4f}.",
        "Típico de busca considera TODAS as ações usadas (o modelo pode usar menos); a tarifa é a mesma no típico e na "
        "reserva – a diferença está nos tokens.",
        "Sem retentativa automática: falha sem medição conta o máximo reservado; 4xx recusado conta 0.",
        "A reserva é ESTIMATIVA conservadora: tarifa das ações e tokens de saída têm limite técnico (max_tool_calls, "
        "max_output_tokens), mas os tokens de entrada da pesquisa não; a chamada em andamento pode exceder a reserva "
        "e o teto nesse valor (o excesso é registrado e bloqueia as chamadas seguintes).",
    ]
    return {"empresas": empresas, "frentes": n_frentes, "modelo": modelo, "provedor_busca": provedor_busca,
            "chamadas_ia": n_ia, "chamadas_busca": chamadas_busca, "chamadas_descoberta": n_desc,
            "acoes_pesquisa_max": acoes_max, "tarifa_busca_max_usd": round(busca_tarifa_max, 4),
            "ia_tipico_usd": round(ia_tip, 4), "ia_max_usd": round(ia_max, 4),
            "busca_tipico_usd": round(busca_tip, 4), "busca_max_usd": round(busca_max, 4),
            "total_tipico_usd": round(ia_tip + busca_tip, 4), "total_max_usd": round(ia_max + busca_max, 4),
            "memoria": memoria,
            "premissas": " ".join(memoria)}


def custo_sem_medicao(erro, reserva, tentativas=1):
    """Custo a registrar quando a chamada falhou SEM uso medido: 0 só para erro 4xx de requisição recusada pela API
    (400/401/403/404/422/429: não processada); timeout, queda de conexão, 408/409 e 5xx podem ter sido processados
    e cobrados -> conta-se o MÁXIMO reservado."""
    cod = getattr(erro, "status_code", None)
    if isinstance(cod, int) and 400 <= cod < 500 and cod not in (408, 409):
        return Decimal("0")
    return D(reserva)


def executar_ia(orc, analisador, uso, texto_pedido, fn):
    """
    Chamada de IA com reserva prévia e registro do consumo:
      - sem reserva possível -> levanta SemOrcamento (nada é enviado);
      - falha -> UsoIA FALHA com o uso medido (se a resposta chegou) ou custo_sem_medicao; levanta a exceção;
      - sucesso -> devolve (retorno de fn, reserva, inicio); o chamador registra o uso e chama
        orc.liquidar(reserva, uso.custo_estimado_usd, ia=True).
    """
    from prospeccao.services import ia
    ctx = getattr(orc, "ctx", None)
    if ctx is not None:
        ctx.verificar(forcar=True)
    reserva = reservar_ia(orc, analisador, texto_pedido)
    inicio = time.monotonic()
    try:
        return fn(), reserva, inicio
    except Exception as e:
        uso.status, uso.erro = "FALHA", f"{type(e).__name__}: {e}"[:2000]
        usage = getattr(e, "usage", None)
        if usage is not None:
            ia.registrar_uso(uso, usage, inicio)
        else:
            uso.custo_estimado_usd = custo_sem_medicao(e, reserva)
            uso.duracao_ms = int((time.monotonic() - inicio) * 1000)
            uso.save()
        orc.liquidar(reserva, uso.custo_estimado_usd, ia=True)
        raise
