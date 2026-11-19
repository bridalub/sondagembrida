"""Google parado (franquia/velocidade): a identificação de CNPJ do botão Buscar segue pela busca web da OpenAI."""
from types import SimpleNamespace as N
from unittest import mock

from django.test import SimpleTestCase, TestCase

from prospeccao.services import descoberta_google as dg


class Ctx:
    def __init__(self):
        self.logs = []

    def verificar(self, *a, **k):
        pass

    def log(self, msg, *a, **k):
        self.logs.append(msg)


def _sel(nome):
    e = mock.MagicMock(pk=hash(nome) % 1000, cnpj="")
    e.nome = nome
    return mock.MagicMock(empresa=e)


class ReservaIdentificacaoTests(SimpleTestCase):
    def _rodar(self, reserva):
        google, orc_google = N(codigo="google_serpapi", lote=False), N(
            busca_bloqueada="busca web: franquia SerpApi incluída esgotada", motivos_bloqueio=set())
        est, ctx, usadas = N(), Ctx(), []

        def investigar(empresa, sel, pesquisa, busca, leitor, orc, ctx, max_buscas=1):
            usadas.append((busca, orc, max_buscas))
            return {}
        with mock.patch.object(dg, "pendentes_identificacao", return_value=[_sel("A"), _sel("B")]), \
                mock.patch.object(dg, "investigar_dirigido", side_effect=investigar), \
                mock.patch.object(dg, "busca_reserva_identificacao", return_value=reserva), \
                mock.patch("prospeccao.services.selecao.calcular_fila", return_value=("NOVA", [])):
            n = dg.complementar_identidade(None, ctx, est, google, None, orc_google, N(max_buscas_web_empresa=2))
        return n, usadas, est, ctx

    def test_google_parado_segue_pela_openai(self):
        openai, orc_ia = N(codigo="openai_web_search", lote=True), N(busca_bloqueada="", motivos_bloqueio=set())
        n, usadas, est, ctx = self._rodar((openai, orc_ia))
        self.assertEqual(n, 2)
        self.assertTrue(all(b is openai and o is orc_ia and m == 1 for b, o, m in usadas))
        self.assertEqual(est.motivo_fim, "franquia")  # a descoberta continua parada pela franquia
        self.assertTrue(any("OpenAI" in m for m in ctx.logs))

    def test_google_acaba_no_meio_da_empresa_e_ela_e_refeita_pela_openai(self):
        google = N(codigo="google_serpapi", lote=False)
        orc_google = N(busca_bloqueada="", motivos_bloqueio=set())
        openai, orc_ia = N(codigo="openai_web_search", lote=True), N(busca_bloqueada="", motivos_bloqueio=set())
        usadas = []

        def investigar(empresa, sel, pesquisa, busca, leitor, orc, ctx, max_buscas=1):
            usadas.append((empresa.nome, busca.codigo))
            if busca is google:  # a franquia acaba nesta chamada
                orc_google.busca_bloqueada = "busca web: franquia SerpApi incluída esgotada"
            return {}
        with mock.patch.object(dg, "pendentes_identificacao", return_value=[_sel("A"), _sel("B")]), \
                mock.patch.object(dg, "investigar_dirigido", side_effect=investigar), \
                mock.patch.object(dg, "busca_reserva_identificacao", return_value=(openai, orc_ia)), \
                mock.patch("prospeccao.services.selecao.calcular_fila", return_value=("NOVA", [])):
            dg.complementar_identidade(None, Ctx(), N(), google, None, orc_google, N(max_buscas_web_empresa=2))
        self.assertEqual(usadas, [("A", "google_serpapi"), ("A", "openai_web_search"), ("B", "openai_web_search")])

    def test_sem_openai_mantem_pendencia(self):
        n, usadas, est, _ = self._rodar(None)
        self.assertEqual((n, usadas), (0, []))
        self.assertEqual(est.motivo_fim, "franquia")

    def test_teto_financeiro_para_a_reserva(self):
        openai = N(codigo="openai_web_search", lote=True)
        orc_ia = N(busca_bloqueada="", motivos_bloqueio={"teto global PROSPECCAO_TETO_GLOBAL_USD=1.75 atingido"})
        n, usadas, _, ctx = self._rodar((openai, orc_ia))
        self.assertEqual(usadas, [])
        self.assertTrue(any("teto financeiro" in m for m in ctx.logs))


class CadeiaRealFranquiaZeradaTests(TestCase):
    """Cadeia real, sem custo: a conta SerpApi lida com 0 buscas -> o portão de franquia bloqueia antes da
    requisição -> o orçamento marca o Google parado -> a identificação troca sozinha para a OpenAI (simulada)."""

    def test_franquia_zerada_troca_sozinha_para_openai(self):
        from decimal import Decimal
        from carteira.models import Empresa
        from prospeccao.conectores.base import ContextoExecucao
        from prospeccao.conectores.busca_web import GoogleSerpApi, OpenAIBusca
        from prospeccao.models import ConsultaWeb, Fonte, FrenteComercial, Pesquisa, SelecaoPesquisa
        from prospeccao.services import orcamento as orcm

        frente = FrenteComercial.objects.create(nome="Frota", descricao="transporte")
        p = Pesquisa.objects.create(nome="Franquia zerada", modo="PLANEJADA", cidades="Mauá", uf="SP",
                                    quantidade_desejada=5, max_buscas_web_empresa=2, teto_custo_usd=Decimal("1"))
        for nome in ("Alfa Transportes", "Beta Logística"):
            e = Empresa.objects.create(nome=nome, origem=Empresa.Origem.DESCOBERTA, cidade="Mauá", uf="SP")
            SelecaoPesquisa.objects.create(pesquisa=p, frente=frente, cnpj=f"SEM-{e.pk}", empresa=e, pontuacao=5,
                                           fila=SelecaoPesquisa.Fila.BUSCAR_CONTATO,
                                           aderencia=SelecaoPesquisa.Aderencia.PROVAVEL)
        fonte_g = Fonte.objects.create(codigo="google_serpapi", nome="Google", tipo=Fonte.Tipo.IDENTIFICACAO,
                                       habilitada=True, intervalo_minimo_segundos=0, timeout_segundos=5,
                                       max_retentativas=0)
        google, orc_google = GoogleSerpApi(fonte_g), orcm.Orcamento(p)
        ctx, est, chamadas = ContextoExecucao(), N(), []

        def openai_falsa(self, consulta, ctx, contexto=None, **k):
            chamadas.append(("openai", consulta))
            self.ultimo = {"custo": Decimal("0"), "medido": True, "acoes": 1}
            return []

        def investigar(empresa, sel, pesquisa, busca, leitor, orc, ctx, max_buscas=1):
            orcm.buscar(busca, f"{empresa.nome} CNPJ", finalidade="COMPLEMENTAR", orc=orc, ctx=ctx, empresa=empresa)
            return {}
        conta_zerada = {"plan_name": "Free Plan", "plan_searches_left": 0, "account_status": "Active"}
        with mock.patch("prospeccao.services.prontidao.em_teste", return_value=False), \
                mock.patch("prospeccao.services.prontidao._ler_conta_serpapi", return_value=conta_zerada), \
                mock.patch("prospeccao.services.prontidao.vagas_google_alheias", return_value=0), \
                mock.patch("prospeccao.conectores.busca_web.serpapi_configurada", return_value=True), \
                mock.patch("prospeccao.conectores.busca_web.openai_configurada", return_value=True), \
                mock.patch.dict("os.environ", {"SERPAPI_API_KEY": "chave-falsa"}), \
                mock.patch.object(OpenAIBusca, "buscar", openai_falsa), \
                mock.patch("requests.Session.request", side_effect=AssertionError("não pode chamar a SerpApi")), \
                mock.patch.object(dg, "investigar_dirigido", side_effect=investigar):
            n = dg.complementar_identidade(None, ctx, est, google, None, orc_google, p)

        self.assertEqual(n, 2)
        self.assertIn("franquia SerpApi incluída esgotada", orc_google.busca_bloqueada)
        self.assertEqual(est.motivo_fim, "franquia")
        self.assertEqual([c[1] for c in chamadas], ["Alfa Transportes CNPJ", "Beta Logística CNPJ"])
        regs = list(ConsultaWeb.objects.filter(pesquisa=p).order_by("pk").values_list("provedor", "status"))
        self.assertEqual(regs, [("google_serpapi", "SEM_ORCAMENTO"), ("openai_web_search", "OK"),
                                ("openai_web_search", "OK")])
