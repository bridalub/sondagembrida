"""
Integração REAL de busca web e IA – testada com provedores FALSOS (nenhuma rede, nenhum custo):
orçamento/teto, registro de consultas, descoberta por busca, complementação sem adivinhar domínio,
falhas do provedor, reaproveitamento de análise de IA e preservação de dados humanos.
"""
from decimal import Decimal
from unittest import mock

from carteira.models import CanalEmpresa, Empresa
from prospeccao.models import ConsultaWeb, Execucao, SelecaoPesquisa, UsoIA
from prospeccao.services import ia, orcamento, selecao
from prospeccao.tests import test_planejada as tp
from prospeccao.tests.test_planejada import GMAIL, REDE, TRANSP, Base, fmt, html

SITES_EXTRA = {
    # site da GAMA CARGAS (cadastro só tem gmail): só é associado porque PUBLICA o CNPJ
    "gamacargas.com.br": html(f"Gama Cargas – transporte rodoviário com frota própria. Compras: (11) 2409-9998 "
                              f"compras@gamacargas.com.br CNPJ {fmt(GMAIL)}"),
    # homônimo que não publica o CNPJ: não pode ser associado
    "gamacargas-pr.com.br": html("Gama Cargas Paraná – mudanças. Ligue (41) 3333-4444 contato@gamacargas-pr.com.br"),
    # diretório com muitos CNPJs: ignorado na descoberta
    "listadetransportadoras.com.br": html(" ".join(f"CNPJ {fmt(c)}" for c in (tp.TRANSP, tp.REDE, tp.SICOOB, tp.MATRIZ))),
}


class RedeComBusca(tp.Rede):
    def __init__(self, resultados=None, status_busca=200):
        self.resultados, self.status_busca, self.urls, self.consultas = resultados or {}, status_busca, [], []

    def __call__(self, metodo, url, **kw):
        from prospeccao.tests.test_prospeccao import Resp
        self.urls.append(url)
        if "api.search.brave.com" in url:
            q = kw["params"]["q"]
            self.consultas.append(q)
            if self.status_busca != 200:
                return Resp(self.status_busca, "erro")
            urls = next((v for k, v in self.resultados.items() if k in q), [])
            return Resp(200, {"web": {"results": [{"url": u, "title": "t"} for u in urls]}}, tipo="application/json")
        for dom, pag in SITES_EXTRA.items():
            if f"//{dom}" in url:
                return Resp(200, pag) if url.rstrip("/").endswith(dom) else Resp(404, "")
        return super().__call__(metodo, url, **kw)


class IntegracaoBase(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict("os.environ", {"BRAVE_SEARCH_API_KEY": "teste", "PROSPECCAO_IA_PROVEDOR": "",
                                             "PROSPECCAO_BUSCA_PROVEDOR": "brave", "OPENAI_API_KEY": "",
                                             "PROSPECCAO_TETO_GLOBAL_USD": ""})
        p.start()
        self.addCleanup(p.stop)
        self.p.max_buscas_descoberta = 2
        self.p.save()

    def rodar_com(self, rede, **params):
        with mock.patch("requests.Session.request", side_effect=rede):
            from prospeccao.services import fila
            from prospeccao.services.pipeline import executar
            ex, _ = fila.enfileirar(self.p, Execucao.Gatilho.COMANDO, parametros=params)
            executar(ex.pk)
        ex.refresh_from_db()
        return ex


RESULTADOS = {
    "GAMA": ["https://gamacargas-pr.com.br/", "https://gamacargas.com.br/", "https://www.facebook.com/gama"],
    "Guarulhos SP": ["https://saborrede.com.br/", "https://listadetransportadoras.com.br/"],  # consulta de descoberta (termo + cidade + UF)
}


class OrcamentoTests(IntegracaoBase):
    def test_sem_teto_nenhuma_chamada_paga_e_pendencia_rastreavel(self):
        rede = RedeComBusca(RESULTADOS)
        ex = self.rodar_com(rede)
        self.assertEqual(ex.status, Execucao.Status.PARCIAL, ex.mensagem)  # parcial: etapa paga ficou pendente
        self.assertEqual(rede.consultas, [])  # nenhuma requisição ao provedor pago
        regs = ConsultaWeb.objects.filter(execucao=ex)
        self.assertTrue(regs.exists())
        self.assertEqual(set(regs.values_list("status", flat=True)), {"SEM_ORCAMENTO"})
        self.assertTrue(any("teto financeiro não definido" in l for l in ex.limites_atingidos), ex.limites_atingidos)

    def test_teto_atingido_interrompe_chamadas(self):
        self.p.teto_custo_usd = Decimal("0.01")  # 2 consultas a US$ 0,005
        self.p.save()
        rede = RedeComBusca(RESULTADOS)
        ex = self.rodar_com(rede)
        self.assertEqual(len(rede.consultas), 2)
        self.assertEqual(ConsultaWeb.objects.filter(execucao=ex, status="OK").count(), 2)
        self.assertTrue(ConsultaWeb.objects.filter(execucao=ex, status="SEM_ORCAMENTO").exists())
        self.assertTrue(any("teto financeiro de US$ 0.01 atingido" in l for l in ex.limites_atingidos))

    def test_estimativa_limitada(self):
        est = orcamento.estimativa(self.p, 5, modelo="gpt-6-luna", provedor_busca="openai")
        self.assertEqual(est["chamadas_busca"], 5 + 1 * 2)  # 1 chamada por empresa + descoberta da frente
        self.assertEqual(est["chamadas_ia"], 1 + 5)          # planejamento + investigação
        self.assertGreater(est["total_max_usd"], est["total_tipico_usd"])
        self.assertGreater(est["total_tipico_usd"], 0)


class BuscaWebTests(IntegracaoBase):
    def setUp(self):
        super().setUp()
        self.p.teto_custo_usd = Decimal("5")
        self.p.save()

    def test_descoberta_so_aceita_cnpj_publicado_e_registra_consultas(self):
        rede = RedeComBusca(RESULTADOS)
        ex = self.rodar_com(rede)
        self.assertEqual(ex.status, Execucao.Status.CONCLUIDA, ex.mensagem)
        # REDE (CNAE fora do plano) entrou porque o próprio site publica o CNPJ – com motivo rastreável
        s = SelecaoPesquisa.objects.get(pesquisa=self.p, cnpj=REDE)
        self.assertIn("Encontrada por busca web", s.motivos_selecao[0])
        self.assertIn("saborrede.com.br", s.motivos_selecao[0])
        # diretório com vários CNPJs não gera seleção nem é tratado como site da empresa
        self.assertFalse(SelecaoPesquisa.objects.filter(pesquisa=self.p, motivos_selecao__icontains="listadetransp").exists())
        desc = ConsultaWeb.objects.filter(execucao=ex, finalidade="DESCOBERTA")
        self.assertTrue(desc.exists())
        r = desc.filter(status="OK").first()
        self.assertEqual(r.custo_estimado_usd, orcamento.PRECO_BRAVE)
        self.assertTrue(r.resultados and "url" in r.resultados[0])

    def test_complementacao_sem_dominio_proprio_e_sem_adivinhar(self):
        rede = RedeComBusca(RESULTADOS)
        self.rodar_com(rede)
        e = Empresa.objects.get(cnpj=GMAIL)
        # site encontrado por busca e associado pelo CNPJ publicado
        site = e.canais.get(tipo="SITE", valor__contains="//gamacargas.com.br")
        self.assertEqual(site.confianca_associacao, "ALTA")
        self.assertTrue(e.canais.filter(valor="compras@gamacargas.com.br", classificacao="COMPRAS").exists())
        # homônimo sem CNPJ e sem telefone/CEP/endereço: site PROVÁVEL para revisão (BAIXA), sem contatos extraídos
        hom = e.canais.filter(tipo="SITE", valor__contains="gamacargas-pr")
        if hom.exists():
            self.assertEqual(hom.get().confianca_associacao, "BAIXA")
        self.assertFalse(e.canais.filter(valor="4133334444").exists())
        self.assertFalse(e.canais.filter(valor__startswith="contato@gamacargas-pr").exists())
        # consultas por CNPJ / razão / fantasia registradas com a empresa
        cons = ConsultaWeb.objects.filter(empresa=e, finalidade="CONTATO")
        self.assertTrue(cons.filter(consulta__contains=fmt(GMAIL)).exists())
        self.assertLessEqual(cons.count(), self.p.max_buscas_web_empresa)
        # nenhum domínio foi "adivinhado" a partir do nome (ex.: alfatransportes só pelo e-mail cadastral)
        adivinhados = [u for u in rede.urls if "gamacargas" in u and "api.search" not in u]
        self.assertTrue(adivinhados)  # só porque a busca devolveu
        self.assertFalse(any("lanchesentregas" in u or "lanches" in u.split("//")[-1] for u in rede.urls))

    def test_falha_do_provedor_registrada_e_execucao_conclui(self):
        rede = RedeComBusca(RESULTADOS, status_busca=500)
        ex = self.rodar_com(rede)
        self.assertIn(ex.status, (Execucao.Status.CONCLUIDA, Execucao.Status.PARCIAL), ex.mensagem)
        falhas = ConsultaWeb.objects.filter(execucao=ex, status="FALHA")
        self.assertEqual(falhas.count(), 3)  # após 3 falhas seguidas não insiste
        self.assertIn("HTTP 500", falhas.first().erro)
        self.assertTrue(any("busca web indisponível" in l for l in ex.limites_atingidos))
        # o restante do trabalho continua (site do e-mail cadastral ainda é lido)
        self.assertEqual(SelecaoPesquisa.objects.get(pesquisa=self.p, cnpj=TRANSP).fila, "SELECIONADA")

    def test_preserva_confirmacao_humana_e_restricao(self):
        self.rodar_com(RedeComBusca(RESULTADOS))
        e = Empresa.objects.get(cnpj=TRANSP)
        c = e.canais.get(valor="compras@rapidofrota.com.br")
        c.confirmacao_humana, c.restrito = CanalEmpresa.Confirmacao.ATENDIDO, True
        c.save()
        self.rodar_com(RedeComBusca(RESULTADOS), somente_investigar=True)
        SelecaoPesquisa.objects.filter(pesquisa=self.p, cnpj=TRANSP).update(fila="AGUARDANDO")
        self.rodar_com(RedeComBusca(RESULTADOS), somente_investigar=True)
        c.refresh_from_db()
        self.assertEqual((c.confirmacao_humana, c.restrito), (CanalEmpresa.Confirmacao.ATENDIDO, True))


class IAOrcamentoTests(IntegracaoBase):
    def _fake(self, contatos=None):
        fake = mock.Mock(nome="anthropic", modelo="claude-opus-5-5", max_retries=1, max_tokens=6000)
        res = {"relacao": "SUSTENTADA", "justificativa": "frota própria citada", "evidencias": [],
               "contatos": contatos or [], "perguntas_para_vendedor": [], "hipoteses_aplicacao": []}
        fake.analisar_operacao.return_value = (res, mock.Mock(input_tokens=10_000, output_tokens=1_000),
                                               "claude-opus-5-5", [])
        return fake

    def test_sem_teto_nao_chama_ia_e_reaproveita_analise_valida(self):
        e = Empresa.objects.create(cnpj=TRANSP, nome="RAPIDO FROTA")
        ex = Execucao.objects.create(pesquisa=self.p)
        paginas = [("https://rapidofrota.com.br/", "frota própria de 80 caminhões")]
        fake = self._fake()
        with mock.patch.object(ia, "status", return_value=(True, "ok")), \
                mock.patch.object(ia, "obter_analisador", return_value=fake):
            _, st = selecao._ia_investigar(e, {}, self.p, paginas, ex, orcamento.Orcamento(self.p, ex))
            self.assertEqual(st, "SEM_ORCAMENTO")
            self.assertFalse(fake.analisar_operacao.called)
            self.p.teto_custo_usd = Decimal("1")
            orc = orcamento.Orcamento(self.p, ex)
            _, st = selecao._ia_investigar(e, {}, self.p, paginas, ex, orc)
            self.assertEqual(st, "OK")
            self.assertEqual(fake.max_tokens, self.p.ia_max_tokens_saida)
            self.assertAlmostEqual(float(orc.gasto), 0.06)  # 10k*4/1e6 + 1k*20/1e6
            _, st = selecao._ia_investigar(e, {}, self.p, paginas, ex, orc)
            self.assertEqual(st, "REAPROVEITADA")
            self.assertEqual(fake.analisar_operacao.call_count, 1)
        self.assertEqual(UsoIA.objects.filter(tipo="INVESTIGACAO", status="OK").count(), 1)

    def test_contato_da_ia_tem_confianca_limitada_e_whatsapp_explicito(self):
        e = Empresa.objects.create(cnpj=TRANSP, nome="RAPIDO FROTA")
        ex = Execucao.objects.create(pesquisa=self.p)
        self.p.teto_custo_usd = Decimal("1")
        fake = self._fake([{"tipo": "TELEFONE", "valor": "(11) 94000-1111", "departamento": "manutenção",
                            "finalidade": "MANUTENCAO", "whatsapp_explicito": True, "confianca": "ALTA",
                            "trecho_exato": "WhatsApp manutenção (11) 94000-1111"}])
        with mock.patch.object(ia, "status", return_value=(True, "ok")), \
                mock.patch.object(ia, "obter_analisador", return_value=fake):
            selecao._ia_investigar(e, {}, self.p, [("https://x.com.br/", "WhatsApp manutenção (11) 94000-1111")], ex,
                                   orcamento.Orcamento(self.p, ex))
        c = e.canais.get(valor="11940001111")
        self.assertEqual(c.confianca_associacao, "MEDIA")  # IA não atribui ALTA sozinha
        self.assertTrue(c.whatsapp_explicito)
        self.assertEqual(c.classificacao, "MANUTENCAO")

    def test_contato_ausente_do_texto_lido_nao_vira_canal(self):
        e = Empresa.objects.create(cnpj=TRANSP, nome="RAPIDO FROTA")
        ex = Execucao.objects.create(pesquisa=self.p)
        self.p.teto_custo_usd = Decimal("1")
        fake = self._fake([{"tipo": "TELEFONE", "valor": "(11) 3999-0000", "confianca": "MEDIA", "trecho_exato": "x"},
                           {"tipo": "EMAIL", "valor": "vendas@rapidofrota.com.br", "confianca": "MEDIA",
                            "trecho_exato": "x"}])
        with mock.patch.object(ia, "status", return_value=(True, "ok")), \
                mock.patch.object(ia, "obter_analisador", return_value=fake):
            selecao._ia_investigar(e, {}, self.p, [("https://x.com.br/", "Fale conosco: (11) 2400-1000")], ex,
                                   orcamento.Orcamento(self.p, ex), site_conf="BAIXA")
        self.assertFalse(e.canais.exists())
        uso = UsoIA.objects.get(tipo="INVESTIGACAO")
        self.assertEqual(len(uso.resultado["_contatos_descartados_nao_literais"]), 2)

    def test_validacao_descarta_contato_inventado_pela_ia(self):
        bruto = {"relacao": "PROVAVEL", "evidencias": [], "contatos": [
            {"tipo": "EMAIL", "valor": "compras@inventado.com.br", "trecho_exato": ""},
            {"tipo": "TELEFONE", "valor": "(11) 2400-1112", "trecho_exato": "(11) 2400-1112"}]}
        r = ia.validar_investigacao(bruto, [("u", "Compras: (11) 2400-1112")], [])
        self.assertEqual([c["valor"] for c in r["contatos"]], ["(11) 2400-1112"])
        self.assertEqual(r["contatos_descartados"], 1)
