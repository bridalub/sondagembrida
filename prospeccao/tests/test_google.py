"""
Descoberta pelo GOOGLE (SerpApi FALSA – nenhuma rede, nenhum custo) e investigação dirigida pela IA (simulada).
Cobre: prova de origem (id da requisição e URL do Google), identificação do CNPJ pela base local (telefone),
lead sem CNPJ com a incerteza indicada, conhecidos fora da meta, reprocessamento sem duplicar, decisões da IA
registradas, sem troca silenciosa de motor e a regressão "site da mesma raiz não chegava à tela" (caso Activas).
"""
from decimal import Decimal
from unittest import mock

from carteira.models import CanalEmpresa, Empresa
from prospeccao.conectores.site import ResultadoSite
from prospeccao.models import Candidato, ConsultaWeb, LogExecucao, Oportunidade, SelecaoPesquisa
from prospeccao.services import contatos as ct
from prospeccao.services import descoberta_google as dg
from prospeccao.tests.test_planejada import CONHECIDA, TRANSP, Base, Rede, fmt
from prospeccao.tests.test_prospeccao import Resp

LOCAL = {"search_metadata": {"id": "req-local-1", "status": "Success",
                             "google_url": "https://www.google.com.br/maps/search/transportadora+Guarulhos?api_key=SEGREDO"},
         "local_results": [
             {"position": 1, "title": "Rápido Frota Transportes", "address": "R. A, 10 - Guarulhos - SP",
              "phone": "(11) 2400-1111", "website": "https://rapidofrota.com.br/", "place_id": "P1", "type": "Transportadora"},
             {"position": 2, "title": "Transportes Novos Ares", "address": "Av. B, 99 - Guarulhos - SP",
              "phone": "(11) 2555-0000", "place_id": "P2", "type": "Transportadora"},
             {"position": 4, "title": "Garagem Sem Fone Transportes", "address": "R. E, 7 - Guarulhos - SP",
              "place_id": "P4", "type": "Transportadora"},
             {"position": 3, "title": "Ja Cliente Transportes", "address": "R. C, 1 - Guarulhos - SP",
              "phone": "(11) 2777-8888", "website": "https://jacliente.com.br/", "place_id": "P3"}]}
ORGANICO = {"search_metadata": {"id": "req-org-1", "status": "Success", "google_url": "https://www.google.com.br/search?q=x"},
            "organic_results": [{"position": 1, "title": "Transportadoras em Guarulhos - Guia", "link": "https://guiamais.com.br/x",
                                 "snippet": "Rápido Frota Transportes (11) 2400-1111"}]}


class RedeGoogle(Rede):
    def __init__(self):
        self.consultas = []

    def __call__(self, metodo, url, **kw):
        if "serpapi.com" in url:
            p = kw.get("params") or {}
            self.consultas.append((p.get("engine"), p.get("q"), p.get("start")))
            return Resp(200, LOCAL if p.get("engine") == "google_maps" else ORGANICO, tipo="application/json")
        return super().__call__(metodo, url, **kw)


def ia_falsa(orc, tipo, sistema, pedido, esquema, nome, pesquisa, empresa=None, materiais=None, **_ignorados):
    if tipo == "EXTRACAO_GOOGLE":
        return {"empresas": [{"indice": 0, "nome": "Rápido Frota Transportes", "telefone": "(11) 2400-1111",
                              "email": "", "endereco": "", "cnpj": "", "atividade": "transporte",
                              "tipo_fonte": "DIRETORIO", "pertinente": True}],
                "proximas_consultas": ["frota própria Guarulhos"], "justificativa": "x"}, None
    # o programa já fez a busca dirigida de CNPJ antes da IA; a IA pede a de telefone enquanto ela não foi feita
    telefone_ja_buscado = "Guarulhos telefone" in pedido
    if "Sem Fone" in pedido and not telefone_ja_buscado:
        return {"acao": "BUSCAR_GOOGLE", "proxima_acao": "BUSCAR_GOOGLE",
                "consulta": '"Garagem Sem Fone Transportes" Guarulhos telefone', "url": "",
                "cnpj": "", "motivo": "falta telefone direto", "sintese": []}, None
    return {"acao": "ENCERRAR", "proxima_acao": "ENCERRAR", "consulta": "", "url": "", "cnpj": "",
            "motivo": "informação suficiente", "sintese": []}, None


class GoogleTests(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict("os.environ", {"PROSPECCAO_BUSCA_PROVEDOR": "google", "SERPAPI_API_KEY": "SEGREDO",
                                             "PROSPECCAO_IA_PROVEDOR": ""})
        p.start()
        self.addCleanup(p.stop)
        self.p.fontes.clear()  # sem varredura da Receita: só Google descobre
        self.p.teto_custo_usd = Decimal("1")
        self.p.quantidade_desejada = 5
        self.p.save()

    def rodar_google(self):
        rede = RedeGoogle()
        with mock.patch("requests.Session.request", side_effect=rede), mock.patch.object(dg, "_ia", ia_falsa):
            from prospeccao.services import fila
            from prospeccao.services.pipeline import executar
            from prospeccao.models import Execucao
            ex, _ = fila.enfileirar(self.p, Execucao.Gatilho.MANUAL)
            executar(ex.pk)
        ex.refresh_from_db()
        return ex, rede

    def test_google_ponta_a_ponta(self):
        Empresa.objects.filter(cnpj=CONHECIDA).update(telefone="1127778888")
        CanalEmpresa.objects.create(empresa=Empresa.objects.get(cnpj=CONHECIDA), tipo="TELEFONE", valor="1127778888",
                                    valor_normalizado="1127778888", origem_canal="CADASTRAL")
        ex, rede = self.rodar_google()
        self.assertIn(ex.status, ("CONCLUIDA", "PARCIAL"), ex.mensagem)
        # origem Google comprovada: id da requisição e URL do Google (sem a chave)
        cw = ConsultaWeb.objects.filter(execucao=ex, provedor="google_serpapi", finalidade="DESCOBERTA").order_by("pk").first()
        self.assertEqual(cw.acoes[0]["request_id"], "req-local-1")
        self.assertIn("google.com.br/maps", cw.acoes[0]["google_url"])
        self.assertNotIn("SEGREDO", str(cw.acoes) + cw.erro)
        self.assertEqual(cw.acoes[0]["tipo"], "google:local")
        # identificação pelo telefone na base local (sem custo) -> lead com CNPJ
        s = SelecaoPesquisa.objects.get(pesquisa=self.p, cnpj=TRANSP)
        self.assertTrue(any("telefone" in m and "Receita" in m for m in s.motivos_selecao))
        self.assertTrue(Oportunidade.objects.filter(empresa__cnpj=TRANSP, tipo="NOVA_EXTERNA").exists())
        # empresa sem CNPJ: lead com a incerteza, contato visto no perfil local do Google
        na = Empresa.objects.get(nome="Transportes Novos Ares")
        self.assertIsNone(na.cnpj)
        op = Oportunidade.objects.get(empresa=na)
        self.assertEqual(op.tipo, "NOVA_EXTERNA")
        self.assertIn("CNPJ não identificado", " ".join(op.alertas))
        tel = na.canais.get(valor="1125550000")
        self.assertEqual((tel.fonte, tel.confianca_associacao), ("google_local", "MEDIA"))
        self.assertTrue(ct.canais_utilizaveis(na).exists())
        # conhecida no Salesforce pelo telefone: não vira lead nem conta na meta
        self.assertFalse(Empresa.objects.filter(nome="Ja Cliente Transportes").exists())
        # a IA escolheu a próxima ação para a lacuna real (e a consulta complementar foi ao Google)
        logs = " ".join(LogExecucao.objects.filter(execucao=ex).values_list("mensagem", flat=True))
        self.assertIn("BUSCAR_GOOGLE", logs)
        self.assertIn(("google", '"Garagem Sem Fone Transportes" Guarulhos telefone', 0), rede.consultas)
        comps = ConsultaWeb.objects.filter(execucao=ex, finalidade="COMPLEMENTAR", empresa__nome="Garagem Sem Fone Transportes")
        # busca dirigida de CNPJ feita pelo programa (sem depender da IA) + busca de telefone escolhida pela IA
        self.assertTrue(any("CNPJ" in c.consulta for c in comps))
        comp = comps.get(consulta__contains="telefone")
        comp_na = ConsultaWeb.objects.filter(finalidade="COMPLEMENTAR", empresa__nome="Transportes Novos Ares")
        for consulta in comp_na:
            self.assertIn("CNPJ", consulta.consulta.upper())
        self.assertIsNone(na.cnpj)
        self.assertNotEqual(na.dados_coletados.filter(campo="motivo_encerramento_identificacao").count(), 0)
        self.assertIn("falta", comp.analise.get("motivo", ""))
        # reprocessar os mesmos resultados não duplica
        self.rodar_google()
        self.assertEqual(Empresa.objects.filter(nome="Transportes Novos Ares").count(), 1)
        self.assertEqual(SelecaoPesquisa.objects.filter(pesquisa=self.p, cnpj=TRANSP).count(), 1)

    def test_sem_chave_nao_troca_de_motor(self):
        from prospeccao.conectores.busca_web import obter_busca
        with mock.patch.dict("os.environ", {"SERPAPI_API_KEY": "", "OPENAI_API_KEY": "sk-x", "PROSPECCAO_BUSCA_FALLBACK": ""}):
            busca, msg = obter_busca()
        self.assertIsNone(busca)
        self.assertIn("SERPAPI_API_KEY", msg)
        with mock.patch.dict("os.environ", {"SERPAPI_API_KEY": "", "OPENAI_API_KEY": "sk-x",
                                            "PROSPECCAO_BUSCA_FALLBACK": "openai"}):
            busca, msg = obter_busca()
        self.assertIn("NÃO é Google", msg)  # fallback só explícito e declarado


class RegressaoActivasTests(Base):
    def test_site_da_mesma_raiz_chega_a_tela_como_grupo(self):
        from prospeccao import apresentacao as apr
        from prospeccao.models import Fonte
        matriz = Empresa.objects.create(cnpj="00827591000175", nome="ACTIVAS PLASTICOS INDUSTRIAIS LTDA",
                                        razao_social="ACTIVAS PLASTICOS INDUSTRIAIS LTDA", cidade="Maua", uf="SP")
        fonte, _ = Fonte.objects.get_or_create(codigo="busca_web", defaults={"nome": "x", "tipo": "IMPORTACAO"})
        Candidato.objects.create(fonte=fonte, id_externo="a1", nome="activas", site="https://grupoactivas.com.br/",
                                 cnpj_candidato="00827591000680")
        r = ResultadoSite(url_inicial="https://grupoactivas.com.br/")
        r.paginas = [("https://grupoactivas.com.br/", "Grupo Activas – plásticos. Fale conosco +55 11 3525-5000")]
        r.cnpjs = {"00827591000680": "https://grupoactivas.com.br/"}
        r.telefones = {"1135255000": "https://grupoactivas.com.br/contato"}
        leitor = mock.Mock()
        leitor.analisar.return_value = r
        res = ct.investigar_contatos(matriz, leitor, mock.Mock())
        self.assertEqual(res["aplica_a"], "GRUPO")  # outra unidade: não atribuído à unidade de Mauá
        site = matriz.canais.get(tipo="SITE")
        self.assertEqual(site.confianca_associacao, "MEDIA")
        tel = matriz.canais.get(valor="1135255000")
        self.assertIn("grupo", apr.alerta_canal(tel).lower())


class PontosAutorizacaoTests(GoogleTests):
    def test_contato_compartilhado_vira_possivel_correspondencia(self):
        Empresa.objects.filter(cnpj=CONHECIDA).update(telefone="1127778888")
        CanalEmpresa.objects.create(empresa=Empresa.objects.get(cnpj=CONHECIDA), tipo="TELEFONE", valor="1127778888",
                                    valor_normalizado="1127778888", origem_canal="CADASTRAL")
        extra = {"position": 9, "title": "Oficina Diferente Ltda", "address": "R. D, 5 - Guarulhos - SP",
                 "phone": "(11) 2777-8888", "place_id": "P9"}
        with mock.patch.dict(LOCAL, {"local_results": LOCAL["local_results"] + [extra]}):
            ex, _ = self.rodar_google()
        # mesmo telefone da conta, nome diferente: NÃO excluída; conferência separada, fora da meta
        s = SelecaoPesquisa.objects.get(pesquisa=self.p, fila="REVISAR_IDENTIDADE",
                                        motivos_selecao__icontains="Oficina Diferente")
        self.assertIn("Possível correspondência", s.fila_motivos[0])
        self.assertFalse(Oportunidade.objects.filter(empresa__nome="Oficina Diferente Ltda").exists())
        # mesmo telefone e nome compatível: conhecida (excluída das novidades)
        self.assertFalse(Empresa.objects.filter(nome="Ja Cliente Transportes").exists())

    def test_consultas_da_ia_na_regiao_sem_repeticao(self):
        self.assertEqual(dg.norm_consulta('"Oficina X" Guarulhos contato'), dg.norm_consulta("contato oficina x GUARULHOS"))
        self.assertEqual(dg.na_regiao("oficina diesel", self.p), "oficina diesel Guarulhos")
        self.assertEqual(dg.na_regiao("oficina diesel São Paulo", self.p), "")  # fora da região
        self.assertEqual(dg.na_regiao("autopeças Guarulhos", self.p), "autopeças Guarulhos")


class SemCnpjSitePublicaCnpjTests(Base):
    def test_cnpj_no_site_de_empresa_sem_cnpj_e_pista_nao_erro(self):
        e = Empresa.objects.create(nome="Oficina Boa Vista", origem="DESCOBERTA", cidade="Guarulhos", uf="SP")
        r = ResultadoSite(url_inicial="https://oficinaboavista.com.br/")
        r.paginas = [("https://oficinaboavista.com.br/", "Oficina Boa Vista – CNPJ 12.345.678/0001-95")]
        r.cnpjs = {"12345678000195": "https://oficinaboavista.com.br/"}
        a = ct.associar_site(e, r, "aberta pela IA")  # antes: TypeError ('NoneType' object is not subscriptable)
        self.assertEqual(a[0], "BAIXA")  # só o nome: possível site
        self.assertEqual(e.dados_coletados.get(campo="cnpj_publicado_no_site").valor, "12345678000195")


class QualidadeDescobertaTests(GoogleTests):
    def test_regiao_mesma_empresa_e_justificativa(self):
        self.assertEqual(dg.cidade_uf_do_endereco("R. X, 229 - Jardim Mauá, Novo Hamburgo - RS, 93548-110"),
                         ("NOVO HAMBURGO", "RS"))
        self.assertTrue(dg.mesma_empresa("Opala Terraplanagem Mauá", "Opala Terraplanagem", ["Mauá"]))
        self.assertFalse(dg.mesma_empresa("Souza & Filhos Mecânica Diesel", "Opala Terraplanagem", ["Mauá"]))
        a = dg.chave_sem_cnpj({"dominio": "facebook.com", "diretorio": True, "nome_citado": "Opala", "telefones": []})
        b = dg.chave_sem_cnpj({"dominio": "facebook.com", "diretorio": True, "nome_citado": "Souza", "telefones": []})
        self.assertNotEqual(a, b)  # domínio de rede social não identifica empresa
        from prospeccao.models import FrenteComercial
        j = dg.justificativa({"tipo": "LOCAL", "atividade": "Oficina mecânica"}, FrenteComercial.objects.first())
        self.assertIn("categoria no Google “Oficina mecânica”", j)
        q = dg.consultas_frente(self.frente, {"consultas_descoberta": ["transportadora frota própria Guarulhos SP"]}, self.p)
        self.assertTrue(all("frota própria" not in x for x in q))
        self.assertIn("transportadora Guarulhos SP", q)

    def test_aproveita_toda_a_resposta_e_soma_mesma_empresa(self):
        self.p.quantidade_desejada = 1  # cota pequena NÃO pode descartar resultados já pagos
        self.p.save()
        extra = {"position": 5, "title": "Rápido Frota", "address": "R. A, 10 - Guarulhos - SP",
                 "phone": "(11) 2400-1112", "place_id": "P5"}
        with mock.patch.dict(LOCAL, {"local_results": LOCAL["local_results"] + [extra]}):
            self.rodar_google()
        self.assertTrue(Empresa.objects.filter(nome="Transportes Novos Ares").exists())
        self.assertTrue(Empresa.objects.filter(nome="Garagem Sem Fone Transportes").exists())
        rapido = Empresa.objects.get(cnpj=TRANSP)
        self.assertTrue(rapido.canais.filter(valor="1124001112").exists())  # contato da outra listagem somado
        self.assertEqual(Empresa.objects.filter(nome__icontains="Rápido Frota").count(), 0)  # sem duplicar


class GoogleSemCreditoTests(Base):
    """Classe própria: as subclasses de GoogleTests (limites diferentes) não herdam este cenário."""
    def setUp(self):
        super().setUp()
        p = mock.patch.dict("os.environ", {"PROSPECCAO_BUSCA_PROVEDOR": "google", "SERPAPI_API_KEY": "SEGREDO",
                                             "PROSPECCAO_IA_PROVEDOR": ""})
        p.start()
        self.addCleanup(p.stop)
        self.p.fontes.clear()
        self.p.teto_custo_usd = Decimal("1")
        self.p.quantidade_desejada = 5
        self.p.save()

    def test_credito_acaba_e_descoberta_segue_pela_openai(self):
        """SerpApi responde "run out of searches": a descoberta de empresas NOVAS e a identificação seguem sozinhas
        pela busca web da OpenAI (simulada, sem custo), com limite de consultas por execução."""
        from prospeccao.conectores.busca_web import ListaBusca, OpenAIBusca

        class RedeSemCredito(RedeGoogle):
            def __call__(self, metodo, url, **kw):
                if "serpapi.com" in url:
                    self.consultas.append((kw.get("params") or {}).get("q"))
                    return Resp(200, {"error": "Your account has run out of searches."}, tipo="application/json")
                return super().__call__(metodo, url, **kw)
        chamadas = []

        def openai_falsa(self_, consulta, ctx, n=5, contexto=None):
            chamadas.append((consulta, dict(contexto or {})))
            self_.ultimo = {"custo": Decimal("0.01"), "medido": True, "modelo": "falso", "acoes": 1}
            if (contexto or {}).get("empresa"):  # identificação de CNPJ
                return ListaBusca([])
            r = ListaBusca([{"url": "https://ventonovo.com.br/", "titulo": "Vento Novo Transportes",
                             "dominio": "ventonovo.com.br", "tipo": "SITE_OFICIAL", "diretorio": False,
                             "verificacao": "URL retornada pela ferramenta", "nome_citado": "Vento Novo Transportes",
                             "cnpj_citado": "", "municipio_citado": "Guarulhos",
                             "endereco_citado": "R. Z, 5 - Guarulhos - SP", "telefones": ["(11) 2466-7777"],
                             "emails": []}])
            r.analise = {"proximas_consultas": [], "justificativa": ""}
            return r
        rede = RedeSemCredito()
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-teste", "PROSPECCAO_DESCOBERTA_RESERVA_MAX": "2"}), \
                mock.patch("prospeccao.conectores.busca_web.openai_configurada", return_value=True), \
                mock.patch.object(OpenAIBusca, "buscar", openai_falsa), \
                mock.patch.object(OpenAIBusca, "custo_maximo", lambda self_: Decimal("0.02")), \
                mock.patch("requests.Session.request", side_effect=rede), mock.patch.object(dg, "_ia", ia_falsa):
            from prospeccao.models import Execucao
            from prospeccao.services import fila
            from prospeccao.services.pipeline import executar
            ex, _ = fila.enfileirar(self.p, Execucao.Gatilho.MANUAL)
            executar(ex.pk)
        ex.refresh_from_db()
        self.assertEqual(len(rede.consultas), 1)  # o Google é tentado uma vez e não insiste
        desc = [c for c in chamadas if not c[1].get("empresa")]
        self.assertEqual(len(desc), 2)  # limite PROSPECCAO_DESCOBERTA_RESERVA_MAX
        self.assertEqual(desc[0][1].get("uf"), "SP")
        regs = ConsultaWeb.objects.filter(execucao=ex, provedor="openai_web_search", finalidade="DESCOBERTA")
        self.assertEqual(regs.count(), 2)
        nova = Empresa.objects.get(nome="Vento Novo Transportes")
        self.assertTrue(SelecaoPesquisa.objects.filter(pesquisa=self.p, empresa=nova).exists())
        logs = " ".join(LogExecucao.objects.filter(execucao=ex).values_list("mensagem", flat=True))
        self.assertIn("descoberta de empresas novas segue automaticamente pela busca web da OpenAI", logs)
        self.assertIn("franquia SerpApi incluída esgotada", logs)
        self.assertIn("limite de 2 consulta(s)", logs)
