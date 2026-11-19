"""
Regra central: já pesquisado continua aparecendo; já contatado (contato EFETIVO) vai para acompanhamento;
conhecido no Salesforce fica separado; o cadastro não se duplica e a oportunidade não se perde.
Banco isolado de teste; Google e IA simulados (nenhuma rede, nenhum custo).
"""
from decimal import Decimal

from django.urls import reverse

from django.test import TestCase
from carteira.models import Abordagem, CanalEmpresa, Empresa
from prospeccao.models import Candidato, Oportunidade, Pesquisa, SelecaoPesquisa
from prospeccao.services import selecao as sl
from prospeccao.tests.test_google import GoogleTests
from prospeccao.tests.test_planejada import CONHECIDA, TRANSP

DISP = sl.FILAS_DISPONIVEIS


class JaEncontradoTests(GoogleTests):
    test_google_ponta_a_ponta = None  # já coberto em test_google; aqui só a regra de "já encontrado"

    def setUp(self):
        super().setUp()
        from unittest import mock
        p = mock.patch.dict("os.environ", {"SERPAPI_MAX_REQUISICOES_DIA": "1000"})  # SerpApi falsa: sem limite real
        p.start()
        self.addCleanup(p.stop)
        from prospeccao.models import Fonte
        Fonte.objects.filter(codigo="google_serpapi").update(max_consultas_dia=1000)
        Empresa.objects.filter(cnpj=CONHECIDA).update(telefone="1127778888")
        CanalEmpresa.objects.create(empresa=Empresa.objects.get(cnpj=CONHECIDA), tipo="TELEFONE", valor="1127778888",
                                    valor_normalizado="1127778888", origem_canal="CADASTRAL")
        self.p.permitir_exploratoria = True  # como nas pesquisas reais (1 a 9)
        self.p.save()
        self.p8 = self.p
        self.rodar_google()  # "pesquisa 8"
        self.na = Empresa.objects.get(nome="Transportes Novos Ares")
        self.transp = Empresa.objects.get(cnpj=TRANSP)
        type(self.user).objects.filter(pk=self.user.pk).update(is_staff=True)  # telas de gestão: papel ADM
        self.user.refresh_from_db()

    def _segunda_pesquisa(self):
        p9 = Pesquisa.objects.create(nome="Segunda", modo="PLANEJADA", uf="SP", cidades="Guarulhos",
                                     objetivo=self.p8.objetivo, quantidade_desejada=5, teto_custo_usd=Decimal("1"),
                                     permitir_exploratoria=True)
        p9.frentes.set(self.p8.frentes.all())
        self.p = p9
        self.rodar_google()  # "pesquisa 9": mesmas respostas do Google
        return p9

    def _sel(self, pesquisa, empresa):
        return SelecaoPesquisa.objects.get(pesquisa=pesquisa, empresa=empresa)

    def _disponiveis(self, pesquisa):
        self.client.force_login(self.user)
        r = self.client.get(reverse("prospeccao:oportunidades") + f"?pesquisa={pesquisa.pk}&etapa=disponiveis")
        return {o.empresa_id for o in r.context["pagina"]}, r

    def test_A_encontrada_antes_sem_contato_aparece_na_nova_pesquisa_com_mesmo_id(self):
        s8 = self._sel(self.p8, self.na)
        self.assertIn(s8.fila, DISP, s8.fila_motivos)
        n_empresas = Empresa.objects.count()
        p9 = self._segunda_pesquisa()
        s9 = self._sel(p9, self.na)
        self.assertEqual(s9.empresa_id, self.na.pk)  # mesmo ID
        self.assertIn(s9.fila, DISP, s9.fila_motivos)
        self.assertTrue(any("Encontrada anteriormente" in m for m in s9.fila_motivos))
        self.assertEqual(Empresa.objects.count(), n_empresas)  # nenhum cadastro novo
        op = Oportunidade.objects.get(empresa=self.na)
        self.assertEqual(set(op.pesquisas.values_list("pk", flat=True)), {self.p8.pk, p9.pk})  # vínculos preservados
        ids, _ = self._disponiveis(p9)
        self.assertIn(self.na.pk, ids)
        from prospeccao.views import indicadores_pesquisa
        ind = indicadores_pesquisa(p9, self.user)
        self.assertEqual(ind["disponiveis"], len(ids))
        self.assertEqual(ind["ineditas"] + ind["reencontradas"], ind["disponiveis"])
        self.assertGreaterEqual(ind["reencontradas"], 1)
        self.assertEqual(ind["ineditas"], 0)  # tudo já tinha sido encontrado na primeira pesquisa

    def test_B_tentativa_sem_resposta_continua_disponivel(self):
        Abordagem.objects.create(empresa=self.na, usuario=self.user, canal="TELEFONE", resultado="SEM_RESPOSTA",
                                 observacoes="ninguém atendeu")
        p9 = self._segunda_pesquisa()
        s9 = self._sel(p9, self.na)
        self.assertIn(s9.fila, DISP)
        self.assertTrue(any("Tentativa sem resposta (1)" in m and "v1" in m for m in s9.fila_motivos), s9.fila_motivos)
        ids, r = self._disponiveis(p9)
        self.assertIn(self.na.pk, ids)
        from prospeccao.views import indicadores_pesquisa
        self.assertEqual(indicadores_pesquisa(p9, self.user)["tentativas"], 1)
        self.assertEqual(Abordagem.objects.get(empresa=self.na).observacoes, "ninguém atendeu")  # histórico intacto

    def test_C_contato_efetivo_vai_para_acompanhamento(self):
        Abordagem.objects.create(empresa=self.na, usuario=self.user, canal="TELEFONE", resultado="CONVERSA_INTERESSE")
        p9 = self._segunda_pesquisa()
        s9 = self._sel(p9, self.na)
        self.assertEqual(s9.fila, SelecaoPesquisa.Fila.JA_ABORDADA)
        self.assertIn("Contato efetivo", s9.fila_motivos[0])
        ids, _ = self._disponiveis(p9)
        self.assertNotIn(self.na.pk, ids)
        self.assertTrue(Empresa.objects.filter(pk=self.na.pk).exists())  # não apagada
        self.client.force_login(self.user)
        r = self.client.get(reverse("prospeccao:oportunidades") + "?etapa=andamento")
        self.assertIn(self.na.pk, {o.empresa_id for o in r.context["pagina"]})
        from prospeccao.views import indicadores_pesquisa
        self.assertEqual(indicadores_pesquisa(p9, self.user)["acompanhamento"], 1)

    def test_D_conhecida_no_salesforce_nao_e_primeira_abordagem(self):
        self.assertFalse(Empresa.objects.filter(nome="Ja Cliente Transportes").exists())
        conta = Empresa.objects.get(cnpj=CONHECIDA)
        self.assertFalse(Oportunidade.objects.filter(empresa=conta, status__in=["NOVA", "LIBERADA"]).exists())
        ids, _ = self._disponiveis(self.p8)
        self.assertNotIn(conta.pk, ids)

    def test_E_varias_fontes_um_cadastro_uma_ocorrencia(self):
        # TRANSP veio do Google local e do orgânico (diretório): um cadastro, uma linha por lista, fontes somadas
        self.assertEqual(Empresa.objects.filter(cnpj=TRANSP).count(), 1)
        self.assertEqual(SelecaoPesquisa.objects.filter(pesquisa=self.p8, empresa=self.transp).count(), 1)
        self.assertGreaterEqual(Candidato.objects.filter(empresa=self.transp).count(), 1)
        fontes = set(self.transp.canais.values_list("fonte", flat=True))
        self.assertTrue(fontes, "fontes dos contatos preservadas")
        ids, r = self._disponiveis(self.p8)
        self.assertEqual(list(r.context["pagina"]).count(
            next(o for o in r.context["pagina"] if o.empresa_id == self.transp.pk)), 1)

    def test_reprocessar_duas_vezes_nao_duplica(self):
        antes = (Empresa.objects.count(), SelecaoPesquisa.objects.filter(pesquisa=self.p8).count(),
                 Oportunidade.objects.count(), CanalEmpresa.objects.count())
        self.rodar_google()
        self.rodar_google()
        depois = (Empresa.objects.count(), SelecaoPesquisa.objects.filter(pesquisa=self.p8).count(),
                  Oportunidade.objects.count(), CanalEmpresa.objects.count())
        self.assertEqual(antes, depois)

    def test_nome_igual_sem_sinal_em_comum_nao_funde(self):
        from unittest import mock
        from prospeccao.tests.test_google import LOCAL
        garagem = Empresa.objects.get(nome="Garagem Sem Fone Transportes")
        outro = [dict(r, address="R. Z, 500 - Guarulhos - SP") if r["title"].startswith("Garagem") else r
                 for r in LOCAL["local_results"]]
        n = Empresa.objects.count()
        with mock.patch.dict(LOCAL, {"local_results": outro}):
            p9 = self._segunda_pesquisa()
        self.assertEqual(Empresa.objects.count(), n)  # não criou nem fundiu
        s = SelecaoPesquisa.objects.get(pesquisa=p9, fila="REVISAR_IDENTIDADE", motivos_selecao__icontains="Garagem")
        self.assertIn(f"cadastro ID {garagem.pk}", s.fila_motivos[0])
        self.assertIsNone(s.empresa_id)

    def test_export_com_carteira_local_antiga_e_salesforce_fora(self):
        from datetime import timedelta
        from unittest import mock
        from django.utils import timezone
        from prospeccao.services import cruzamento as cz
        from prospeccao.views import indicadores_pesquisa
        antiga = timezone.now() - timedelta(days=10)
        self.client.force_login(self.user)
        with mock.patch.object(cz, "cruzamento_atual", return_value=(False, antiga, 24)), \
                mock.patch.object(cz, "conferir_direto", side_effect=cz.SalesforceIndisponivel("INVALID_LOGIN")):
            r = self.client.get(reverse("prospeccao:oportunidades_exportar") + f"?pesquisa={self.p8.pk}&etapa=disponiveis")
        self.assertEqual(r.status_code, 200)  # falha do Salesforce não bloqueia o cruzamento local
        linhas = [x for x in r.content.decode("utf-8-sig").splitlines()[1:] if x.strip()]
        self.assertEqual(len(linhas), indicadores_pesquisa(self.p8, self.user)["disponiveis"])


class CidadesGoogleTests(GoogleTests):
    """Três cidades: 1ª rodada distribuída, localização por consulta, cidade só pela evidência da fonte."""
    test_google_ponta_a_ponta = None

    def setUp(self):
        super().setUp()
        from unittest import mock
        p = mock.patch.dict("os.environ", {"SERPAPI_MAX_REQUISICOES_DIA": "1000"})
        p.start()
        self.addCleanup(p.stop)
        from prospeccao.models import Fonte, FrenteComercial
        Fonte.objects.filter(codigo="google_serpapi").update(max_consultas_dia=1000)
        self.p.cidades = "Guarulhos\nMauá\nSanto André"
        self.p.permitir_exploratoria = True
        self.p.quantidade_desejada = 2  # meta por perfil pequena: sem a regra, só a 1ª cidade seria consultada
        self.p.save()
        self.p.frentes.set(FrenteComercial.objects.filter(nome__startswith="Transporte"))

    def test_primeira_rodada_cobre_todas_as_cidades_com_localizacao_da_consulta(self):
        from unittest import mock
        from prospeccao.tests.test_google import LOCAL, RedeGoogle, ia_falsa
        from prospeccao.services import descoberta_google as dg
        locs = []

        class Rede(RedeGoogle):
            def __call__(self, metodo, url, **kw):
                if "serpapi.com" in url:
                    prm = kw.get("params") or {}
                    locs.append((prm.get("q"), prm.get("location") or prm.get("ll") or ""))
                return super().__call__(metodo, url, **kw)
        sem_end = {"position": 8, "title": "Frota Sem Endereço Transportes", "phone": "(11) 2999-1234", "place_id": "P8"}
        with mock.patch.dict(LOCAL, {"local_results": LOCAL["local_results"] + [sem_end]}), \
                mock.patch("requests.Session.request", side_effect=Rede()), mock.patch.object(dg, "_ia", ia_falsa):
            from prospeccao.services import fila
            from prospeccao.services.pipeline import executar
            from prospeccao.models import Execucao
            ex, _ = fila.enfileirar(self.p, Execucao.Gatilho.MANUAL)
            executar(ex.pk)
        ex.refresh_from_db()
        consultas = [q for q, _ in locs]
        for c in ("Guarulhos", "Mauá", "Santo André"):
            self.assertTrue(any(c in (q or "") for q in consultas), (c, consultas))
        self.assertEqual(set(ex.progresso["cidades"]["pesquisadas"]), {"Guarulhos", "Mauá", "Santo André"})
        self.assertEqual(ex.progresso["cidades"]["pendentes"], [])
        # localização enviada ao Google acompanha a cidade da consulta (Maps usa a cidade no texto da consulta)
        from carteira.services.normalizacao import normalizar_texto as nt
        com_loc = [(q, loc) for q, loc in locs if loc]
        for q, loc in com_loc:
            cid = next(c for c in ("Guarulhos", "Mauá", "Santo André") if c in q)
            self.assertIn(nt(cid), nt(loc), (q, loc))
        # empresa sem endereço: cidade NÃO preenchida com a cidade pesquisada; localização pendente
        e = Empresa.objects.get(nome="Frota Sem Endereço Transportes")
        self.assertEqual(e.cidade, "")
        s = SelecaoPesquisa.objects.get(pesquisa=self.p, empresa=e)
        self.assertEqual(s.fila, SelecaoPesquisa.Fila.LOCALIZACAO_PENDENTE)
        na = Empresa.objects.get(nome="Transportes Novos Ares")
        self.assertEqual(na.cidade, "Guarulhos")  # do endereço da fonte

    def test_exportar_nao_registra_contato(self):
        self.rodar_google()
        self.client.force_login(self.user)
        antes = Abordagem.objects.count()
        self.client.get(reverse("prospeccao:oportunidades_exportar") + f"?pesquisa={self.p.pk}&etapa=disponiveis")
        self.client.get(reverse("prospeccao:oportunidades") + "?etapa=sem_abordagem")
        self.assertEqual(Abordagem.objects.count(), antes)


class RetomadaTests(GoogleTests):
    """Execução interrompida: retomada vinculada, sem repetir consultas nem zerar a contagem."""
    test_google_ponta_a_ponta = None

    def setUp(self):
        super().setUp()
        from prospeccao.models import Fonte
        Fonte.objects.filter(codigo="google_serpapi").update(max_consultas_dia=1000)
        self.p.limite_consultas = 6
        self.p.save()

    def test_retomada_conta_requisicoes_anteriores_e_nao_repete(self):
        from prospeccao.models import ConsultaWeb, Execucao
        from prospeccao.tests.test_google import RedeGoogle, ia_falsa
        from prospeccao.services import descoberta_google as dg
        from unittest import mock
        ex1, rede1 = self.rodar_google()
        n1 = ConsultaWeb.objects.filter(execucao=ex1, provedor="google_serpapi").count()
        Execucao.objects.filter(pk=ex1.pk).update(status="INTERROMPIDA")  # simula queda (disco cheio)
        emp_antes = Empresa.objects.count()
        rede = RedeGoogle()
        with mock.patch("requests.Session.request", side_effect=rede), mock.patch.object(dg, "_ia", ia_falsa):
            from prospeccao.services import fila
            from prospeccao.services.pipeline import executar
            ex2, _ = fila.enfileirar(self.p, Execucao.Gatilho.MANUAL)
            self.assertEqual(ex2.parametros.get("retomada_de"), ex1.pk)  # vinculada à anterior
            executar(ex2.pk)
        consultas_1 = {(c.consulta, (c.acoes or [{}])[0].get("tipo")) for c in ConsultaWeb.objects.filter(execucao=ex1)}
        consultas_2 = [(c.consulta, (c.acoes or [{}])[0].get("tipo")) for c in ConsultaWeb.objects.filter(execucao=ex2)]
        self.assertFalse(set(consultas_2) & consultas_1)  # nenhuma consulta concluída é repetida
        total = n1 + ConsultaWeb.objects.filter(execucao=ex2, provedor="google_serpapi", finalidade="DESCOBERTA").count()
        self.assertLessEqual(ConsultaWeb.objects.filter(execucao__in=[ex1, ex2], finalidade="DESCOBERTA").count(), 6 + 0)
        self.assertEqual(Empresa.objects.filter(nome="Transportes Novos Ares").count(), 1)  # sem duplicar
        self.assertGreaterEqual(total, n1)


class DiscoTests(TestCase):
    def test_pouco_disco_bloqueia_na_hora(self):
        from unittest import mock
        from prospeccao.models import Pesquisa
        from prospeccao.services import prontidao
        p = Pesquisa.objects.create(nome="D", modo="SIMPLES", uf="SP", cidades="Mauá", termos_busca="oficina")
        with mock.patch.object(prontidao, "espaco_livre_gb", return_value=(0.5, "C:\\")):
            imp, _ = prontidao.verificar(p)
        self.assertTrue(any("Espaço em disco" in i["falta"] for i in imp))

    def test_disco_cheio_durante_execucao_vira_interrompida(self):
        from unittest import mock
        from django.db import OperationalError
        from prospeccao.models import Execucao, Fonte, Pesquisa
        from prospeccao.services import fila
        from prospeccao.services.pipeline import executar
        p = Pesquisa.objects.create(nome="D2", modo="SIMPLES", uf="SP", cidades="Mauá", termos_busca="oficina")
        p.fontes.set(Fonte.objects.filter(codigo="osm_overpass"))
        ex, _ = fila.enfileirar(p, Execucao.Gatilho.MANUAL)
        with mock.patch("prospeccao.services.pipeline._descobrir",
                        side_effect=OperationalError("could not extend file: No space left on device")):
            executar(ex.pk)
        ex.refresh_from_db()
        self.assertEqual(ex.status, Execucao.Status.INTERROMPIDA)
        self.assertIn("falta de espaço em disco", ex.mensagem)
