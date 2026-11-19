"""Botão Executar: requisito indispensável ausente bloqueia NA HORA (sem rodar minutos), e busca que não pesquisou
de fato não termina "Concluída"."""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from prospeccao.models import Execucao, FrenteComercial, Fonte, Pesquisa
from prospeccao.services import prontidao


class ProntidaoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("joel", password="x", is_staff=True)
        self.client.force_login(self.user)
        env = mock.patch.dict("os.environ", {"PROSPECCAO_BUSCA_PROVEDOR": "google", "SERPAPI_API_KEY": "x",
                                             "PROSPECCAO_TETO_GLOBAL_USD": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_grafia_oficial_dos_municipios(self):
        nomes, corr, desc = prontidao.normalizar_municipios(["mauá", "Santo andré", "São caetano", "Xyzlândia"])
        self.assertEqual(nomes, ["Mauá", "Santo André", "São Caetano do Sul", "Xyzlândia"])
        self.assertEqual(len(corr), 3)
        self.assertEqual(desc, ["Xyzlândia"])

    def test_planejada_google_sem_teto_bloqueia_na_hora(self):
        p = Pesquisa.objects.create(nome="ABC", modo="PLANEJADA", uf="SP", cidades="Mauá", objetivo="frotas")
        p.frentes.set([FrenteComercial.objects.first()])
        with mock.patch("prospeccao.services.fila.enfileirar") as enf:
            r = self.client.post(reverse("prospeccao:pesquisa_executar", args=[p.pk]), follow=True)
        enf.assert_not_called()  # nada foi para a fila
        ex = Execucao.objects.get(pesquisa=p)
        self.assertEqual(ex.status, Execucao.Status.BLOQUEADA)
        self.assertIn("Teto de custo", ex.mensagem)
        self.assertContains(r, "Não iniciada")
        self.assertNotContains(r, "Resultado parcial")
        self.assertNotContains(r, "dados encontrados foram mantidos")
        self.assertContains(r, "Como resolver")
        p.teto_custo_usd = Decimal("0.10")
        p.save()
        self.assertEqual(prontidao.verificar(p)[0], [])

    def test_uso_20_nao_bloqueia_o_botao(self):
        from django.utils import timezone
        from prospeccao.models import UsoFonte
        p = Pesquisa.objects.create(nome="SC", modo="PLANEJADA", uf="SP", cidades="São Caetano do Sul", objetivo="frotas")
        p.frentes.set([FrenteComercial.objects.first()])
        fonte, _ = Fonte.objects.get_or_create(
            codigo="google_serpapi", defaults={"nome": "Google", "tipo": Fonte.Tipo.DESCOBERTA, "max_consultas_dia": 20})
        fonte.max_consultas_dia = 20
        fonte.save(update_fields=["max_consultas_dia"])
        UsoFonte.objects.update_or_create(fonte=fonte, dia=timezone.localdate(), defaults={"consultas": 20, "erros": 3})
        with mock.patch.dict("os.environ", {"PROSPECCAO_TETO_GLOBAL_USD": "1.75",
                                            "PROSPECCAO_BUSCA_PROVEDOR": "google", "SERPAPI_API_KEY": "x"}):
            imp, _avisos = prontidao.verificar(p)
        self.assertFalse(any("Limite diário" in i["falta"] or "20/20" in i["falta"] for i in imp))

    def test_franquia_zerada_bloqueia_e_velocidade_reabre(self):
        from datetime import timedelta
        from django.utils import timezone
        from prospeccao.conectores.base import FonteIndisponivel, OrcamentoEsgotado
        from prospeccao.models import Execucao
        from prospeccao.services.retomada import agendar, processar_devidas
        p = Pesquisa.objects.create(nome="SC2", modo="PLANEJADA", uf="SP", cidades="São Caetano do Sul", objetivo="frotas")
        ex = Execucao.objects.create(pesquisa=p, status=Execucao.Status.CONCLUIDA)
        conta_zero = {"plan_name": "Free Plan", "plan_searches_left": 0, "account_status": "Active",
                      "this_hour_searches": 0, "account_rate_limit_per_hour": 250}
        with mock.patch.object(prontidao, "em_teste", return_value=False), \
                mock.patch.object(prontidao, "_ler_conta_serpapi", return_value=conta_zero):
            with self.assertRaises(OrcamentoEsgotado) as ctx:
                prontidao.exigir_vaga_google(mock.Mock(execucao=None))
        self.assertIn("franquia SerpApi incluída esgotada", str(ctx.exception))
        conta_hora = {**conta_zero, "plan_searches_left": 40, "this_hour_searches": 250}
        with mock.patch.object(prontidao, "em_teste", return_value=False), \
                mock.patch.object(prontidao, "_ler_conta_serpapi", return_value=conta_hora):
            with self.assertRaises(FonteIndisponivel):
                prontidao.exigir_vaga_google(mock.Mock(execucao=None))
        item = agendar("google_serpapi", f"velocidade:{ex.pk}", "limite de velocidade", retry_after=1)
        item.disponivel_em = timezone.now() - timedelta(seconds=1)
        item.save(update_fields=["disponivel_em"])
        self.assertEqual(processar_devidas(), 1)
        nova = Execucao.objects.exclude(pk=ex.pk).get(pesquisa=p)
        self.assertEqual(nova.parametros.get("retomada_de"), ex.pk)
        self.assertEqual(nova.status, Execucao.Status.PENDENTE)

    def test_teto_vazio_reaproveita_saldo_global_autorizado(self):
        p = Pesquisa.objects.create(nome="ABC2", modo="PLANEJADA", uf="SP", cidades="Mauá", objetivo="frotas")
        p.frentes.set([FrenteComercial.objects.first()])
        with mock.patch.dict("os.environ", {"PROSPECCAO_TETO_GLOBAL_USD": "1.75",
                                            "PROSPECCAO_BUSCA_PROVEDOR": "google", "SERPAPI_API_KEY": "x"}):
            imp, _avisos = prontidao.verificar(p)
        self.assertEqual(imp, [])

    def test_retomada_de_limite_e_executada_pelo_processador(self):
        from datetime import timedelta
        from django.utils import timezone
        from carteira.models import Empresa
        from prospeccao.services.retomada import agendar, processar_devidas
        e = Empresa.objects.create(nome="Retomada", cnpj="11222333000181")
        item = agendar("brasilapi_cnpj", e.cnpj, "HTTP 429 limite", retry_after=1, empresa=e)
        item.disponivel_em = timezone.now() - timedelta(seconds=1)
        item.save(update_fields=["disponivel_em"])
        with mock.patch("prospeccao.services.oportunidades.verificar_situacao_cadastral", return_value="CONSULTADA"):
            self.assertEqual(processar_devidas(), 1)
        item.refresh_from_db()
        self.assertIsNotNone(item.concluida_em)

    def test_municipio_fora_da_grafia_bloqueia(self):
        p = Pesquisa.objects.create(nome="ABC", modo="SIMPLES", uf="SP", cidades="mauá\nSão caetano", termos_busca="oficina")
        p.fontes.set(Fonte.objects.filter(codigo="osm_overpass"))
        imp, _ = prontidao.verificar(p)
        self.assertTrue(any("São Caetano do Sul" in i["falta"] for i in imp))

    def test_osm_sem_resultado_nao_termina_concluida(self):
        p = Pesquisa.objects.create(nome="OSM", modo="SIMPLES", uf="SP", cidades="Mauá", termos_busca="oficina")
        p.fontes.set(Fonte.objects.filter(codigo="osm_overpass"))
        from prospeccao.conectores import osm
        from prospeccao.services import fila
        from prospeccao.services.pipeline import executar
        with mock.patch.object(osm.OverpassDescoberta, "_executar", return_value={"elements": []}):
            ex, _ = fila.enfileirar(p, Execucao.Gatilho.MANUAL)
            executar(ex.pk)
        ex.refresh_from_db()
        self.assertEqual(ex.status, Execucao.Status.PARCIAL)
        self.assertFalse(ex.completa)
        self.assertIn("não foi efetivamente concluída", ex.mensagem)


class VariasCidadesTests(TestCase):
    def test_consultas_cobrem_todas_as_cidades(self):
        from prospeccao.services import descoberta_google as dg
        p = Pesquisa.objects.create(nome="ABC", modo="PLANEJADA", uf="SP",
                                    cidades="Mauá\nSanto André\nSão Caetano do Sul", objetivo="x")
        f = FrenteComercial.objects.get(nome__startswith="Transporte")
        qs = dg.consultas_frente(f, {}, p)
        for c in ("Mauá", "Santo André", "São Caetano do Sul"):
            self.assertTrue(any(c in q for q in qs), (c, qs))
        self.assertTrue(any("Santo André" in q for q in qs[:3]), qs[:3])  # alterna cidades desde o início


class FormularioPadraoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("g2", password="x", is_staff=True)
        self.client.force_login(self.user)

    def test_padrao_busca_planejada_sem_osm(self):
        r = self.client.get(reverse("prospeccao:pesquisa_nova"))
        f = r.context["form"]
        self.assertEqual(f.initial.get("modo"), "PLANEJADA")
        self.assertEqual(list(f.initial.get("fontes")), [])

    def test_municipio_ambiguo_pede_escolha_e_objetivo_automatico(self):
        from prospeccao.forms import PesquisaForm
        frente = FrenteComercial.objects.first()
        base = {"nome": "T", "modo": "PLANEJADA", "uf": "SP", "frentes": [frente.pk], "quantidade_desejada": 10,
                "idade_desconhecida": "INCLUIR", "ativa": "on"}
        f = PesquisaForm(data={**base, "cidades": "São"})
        self.assertFalse(f.is_valid())
        self.assertIn("mais de um município", str(f.errors.get("cidades")))
        f = PesquisaForm(data={**base, "cidades": "mauá\nSão caetano"})
        ok = f.is_valid()
        self.assertNotIn("cidades", f.errors, f.errors)
        self.assertNotIn("objetivo", f.errors, f.errors)
        if ok:
            self.assertEqual(f.cleaned_data["cidades"], "Mauá\nSão Caetano do Sul")
            self.assertIn("Mauá", f.cleaned_data["objetivo"])
