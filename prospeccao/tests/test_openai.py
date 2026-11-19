"""
OpenAI (IA + ferramenta web_search) com cliente FALSO – nenhuma rede, nenhum custo. Cobre: chamada e esquema
estrito, resposta truncada com custo registrado, status sem chave, URLs inventadas descartadas, reserva de custo
(teto da pesquisa e global), associação site/empresa por evidências combinadas, pistas de diretório e descoberta
com CNPJ ausente do recorte da Receita.
"""
import json
import sys
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest import mock

from django.test import TestCase

from carteira.models import CanalEmpresa, Empresa
from carteira.services.cnpj import calcular_dv
from prospeccao.conectores.busca_web import OpenAIBusca
from prospeccao.conectores.site import ResultadoSite
from prospeccao.models import ConsultaWeb, Pesquisa, SelecaoPesquisa, UsoIA
from prospeccao.services import contatos as ct
from prospeccao.services import ia, orcamento
from prospeccao.tests import test_integracoes as ti
from prospeccao.tests.test_planejada import GMAIL, REDE, fmt

ENV_OPENAI = {"PROSPECCAO_IA_PROVEDOR": "openai", "OPENAI_API_KEY": "sk-teste-falso", "PROSPECCAO_IA_MODELO": "gpt-6-luna",
              "PROSPECCAO_TETO_GLOBAL_USD": "", "PROSPECCAO_BUSCA_PROVEDOR": "openai", "BRAVE_SEARCH_API_KEY": ""}


def resp_texto(obj, status="completed", ent=1000, sai=200, output=None, motivo=None):
    return NS(status=status, incomplete_details=NS(reason=motivo) if motivo else None, output=output or [],
              output_text=json.dumps(obj) if obj is not None else "", usage=NS(input_tokens=ent, output_tokens=sai),
              model="gpt-6-luna")


class OpenAIAnalisadorTests(TestCase):
    def setUp(self):
        p = mock.patch.dict("os.environ", ENV_OPENAI)
        p.start()
        self.addCleanup(p.stop)

    def test_status_sem_chave_aguardando_e_sem_exigir_anthropic(self):
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            ok, msg = ia.status()
        self.assertFalse(ok)
        self.assertIn("OpenAI aguardando configuração", msg)
        with mock.patch.dict(sys.modules, {"anthropic": None}):  # anthropic indisponível não afeta a OpenAI
            ok, msg = ia.status()
            self.assertTrue(ok, msg)
            self.assertIn("gpt-6-luna", msg)
            self.assertIsInstance(ia.obter_analisador(), ia.AnalisadorOpenAI)

    def test_chamada_com_esquema_estrito_sem_armazenamento(self):
        an = ia.obter_analisador()
        an.client = mock.Mock()
        an.client.responses.create.return_value = resp_texto({"ok": True})
        bruto, usage, modelo = an._gerar("sis", "pedido", {"type": "object"}, "teste")
        self.assertEqual(bruto, {"ok": True})
        kw = an.client.responses.create.call_args.kwargs
        self.assertFalse(kw["store"])
        self.assertTrue(kw["text"]["format"]["strict"])
        self.assertEqual(kw["max_output_tokens"], an.max_tokens)
        self.assertEqual(kw["model"], "gpt-6-luna")

    def test_resposta_truncada_registra_custo_medido(self):
        an = ia.obter_analisador()
        an.client = mock.Mock()
        an.client.responses.create.return_value = resp_texto(None, status="incomplete", ent=5000, sai=6000,
                                                             motivo="max_output_tokens")
        p = Pesquisa.objects.create(nome="t", uf="SP", cidades="Guarulhos", teto_custo_usd=Decimal("1"))
        orc = orcamento.Orcamento(p)
        uso = UsoIA(tipo="TESTE", provedor="openai", modelo=an.modelo, pesquisa=p)
        with self.assertRaises(ia.IAIncompleta):
            orcamento.executar_ia(orc, an, uso, "x" * 300, lambda: an._gerar("s", "p", {"type": "object"}, "t"))
        uso = UsoIA.objects.get(pesquisa=p)
        self.assertEqual((uso.status, uso.tokens_entrada, uso.tokens_saida), ("FALHA", 5000, 6000))
        self.assertAlmostEqual(float(uso.custo_estimado_usd), 5000 * 0.10 / 1e6 + 6000 * 0.50 / 1e6, places=6)
        self.assertEqual(orc.reservado, 0)  # reserva liquidada
        self.assertEqual(orc.gasto, uso.custo_estimado_usd)

    def test_preco_conferido_e_modelo_desconhecido_conservador(self):
        self.assertEqual(ia.preco("gpt-6-luna"), (0.10, 0.50))
        self.assertEqual(ia.preco("gpt-5-mini"), (0.25, 2.00))
        self.assertEqual(ia.preco("modelo-novo-x"), max(ia.PRECOS_PADRAO.values(), key=lambda v: v[1]))


def resposta_busca(resultados, fontes, acoes=2):
    itens = [NS(type="web_search_call", action=NS(type="search", sources=[NS(type="url", url=u) for u in fontes]))]
    itens += [NS(type="web_search_call", action=NS(type="open_page", sources=None)) for _ in range(acoes - 1)]
    itens.append(NS(type="message", content=[NS(type="output_text", annotations=[])]))
    return resp_texto({"resultados": resultados}, ent=20000, sai=800, output=itens)


def item(url, tipo="SITE_EMPRESA", **kw):
    base = {"url": url, "titulo": "t", "tipo": tipo, "nome_citado": "", "cnpj_citado": "", "municipio_citado": "",
            "endereco_citado": "", "telefones": [], "emails": [], "departamento": "", "trecho": ""}
    return {**base, **kw}


class OpenAIBuscaTests(TestCase):
    def setUp(self):
        p = mock.patch.dict("os.environ", ENV_OPENAI)
        p.start()
        self.addCleanup(p.stop)
        self.p = Pesquisa.objects.create(nome="t", uf="SP", cidades="Guarulhos", teto_custo_usd=Decimal("1"))

    def busca(self, resp):
        b = OpenAIBusca()
        b._client = mock.Mock()
        b._client.responses.create.return_value = resp
        return b

    def test_url_inventada_descartada_e_custo_medido(self):
        resp = resposta_busca([item("https://www.gamacargas.com.br/contato"),
                               item("https://guia.com.br/gama", "DIRETORIO", telefones=["(11) 2409-9998"]),
                               item("https://inventado.com.br/")],
                              ["https://gamacargas.com.br/contato", "https://guia.com.br/gama"], acoes=2)
        b = self.busca(resp)
        orc = orcamento.Orcamento(self.p)
        from prospeccao.conectores.base import ContextoExecucao
        res = orcamento.buscar(b, ["q1", "q2"], finalidade="CONTATO", orc=orc, ctx=ContextoExecucao(),
                               contexto={"cidade": "Guarulhos", "uf": "SP", "empresa": {"razao_social": "GAMA"}})
        self.assertEqual([r["dominio"] for r in res], ["gamacargas.com.br", "guia.com.br"])
        self.assertTrue(res[1]["diretorio"])
        kw = b._client.responses.create.call_args.kwargs
        self.assertEqual(kw["tools"][0]["type"], "web_search")
        self.assertEqual(kw["max_tool_calls"], b.max_acoes)
        self.assertIn("web_search_call.action.sources", kw["include"])
        reg = ConsultaWeb.objects.get()
        self.assertEqual((reg.status, reg.chamadas_ferramenta, reg.tokens_entrada), ("OK", 2, 20000))
        esperado = 20000 * 0.10 / 1e6 + 800 * 0.50 / 1e6 + 2 * 0.01
        self.assertAlmostEqual(float(reg.custo_estimado_usd), esperado, places=5)
        self.assertGreater(reg.custo_reservado_usd, reg.custo_estimado_usd)
        self.assertEqual(b.ultimo["descartados"], 1)

    def test_reserva_maior_que_teto_nao_chama(self):
        self.p.teto_custo_usd = Decimal("0.02")  # 3 ações a US$ 0,01 já não cabem
        self.p.save()
        b = self.busca(resposta_busca([], []))
        from prospeccao.conectores.base import ContextoExecucao
        res = orcamento.buscar(b, "q", finalidade="DESCOBERTA", orc=orcamento.Orcamento(self.p), ctx=ContextoExecucao(),
                               contexto={})
        self.assertEqual(res, [])
        self.assertFalse(b._client.responses.create.called)
        self.assertEqual(ConsultaWeb.objects.get().status, "SEM_ORCAMENTO")

    def test_teto_global_protege_saldo_compartilhado(self):
        with mock.patch.dict("os.environ", {"PROSPECCAO_TETO_GLOBAL_USD": "0.05"}):
            orc = orcamento.Orcamento(self.p)
            self.assertFalse(orc.reservar(Decimal("0.06")))
            self.assertIn("teto global", " ".join(orc.motivos_bloqueio))
            self.assertTrue(orc.reservar(Decimal("0.04")))
            self.assertFalse(orc.reservar(Decimal("0.02")))  # reserva pendente conta


class AssociacaoTests(TestCase):
    def setUp(self):
        self.e = Empresa.objects.create(cnpj="12345678000195", nome="TRANSPORTES VALENTE LTDA",
                                        razao_social="TRANSPORTES VALENTE LTDA", cidade="GUARULHOS", uf="SP",
                                        cep="07000-100", endereco="RUA DAS PALMEIRAS – CENTRO")
        CanalEmpresa.objects.create(empresa=self.e, tipo="TELEFONE", valor="1124445555", valor_normalizado="1124445555",
                                    origem_canal="CADASTRAL", confianca_associacao="MEDIA")
        p = mock.patch("prospeccao.services.contatos.dominio_tem_mx", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def res(self, texto, cnpjs=(), url="https://valente.com.br/"):
        r = ResultadoSite(url_inicial=url)
        r.paginas = [(url, texto)]
        r.cnpjs = {c: "https://valente.com.br/" for c in cnpjs}
        return r

    def test_evidencias_combinadas(self):
        busca = "busca web: q"
        a = ct.associar_site(self.e, self.res("Transportes Valente – ligue (11) 2444-5555"), busca)
        self.assertEqual((a[0], a[1], a[3]), ("MEDIA", "EMPRESA", True))
        self.assertIn("telefone", a[2])
        a = ct.associar_site(self.e, self.res("Valente – Rua das Palmeiras, 100 – Guarulhos"), busca)
        self.assertEqual((a[0], a[3]), ("MEDIA", True))  # nome + logradouro + município
        a = ct.associar_site(self.e, self.res("Valente: mudanças residenciais"), busca)
        self.assertEqual((a[0], a[3]), ("BAIXA", False))  # só o nome: provável, revisar
        self.assertIsNone(ct.associar_site(self.e, self.res("Outra empresa qualquer", url="https://x.com.br/"), busca))
        a = ct.associar_site(self.e, self.res("Valente", cnpjs=["12345678000276"]), busca)
        self.assertEqual((a[0], a[1]), ("MEDIA", "GRUPO"))  # raiz: vínculo com o grupo, não confirma a filial
        self.assertIn("não confirma", a[2])
        a = ct.associar_site(self.e, self.res("Valente", cnpjs=["99888777000100"]), busca)
        self.assertEqual((a[0], a[3]), ("CONFLITO", False))
        a = ct.associar_site(self.e, self.res("x", cnpjs=["12345678000195"]), busca)
        self.assertEqual(a[0], "ALTA")

    def test_pista_de_diretorio_nao_e_utilizavel(self):
        pistas = [{"url": "https://guia.com.br/v", "dominio": "guia.com.br", "diretorio": True,
                   "cnpj_citado": "12.345.678/0001-95", "telefones": ["(11) 3333-2222"], "emails": [],
                   "verificacao": "URL retornada pela ferramenta"},
                  {"url": "https://guia.com.br/w", "dominio": "guia.com.br", "diretorio": True,
                   "cnpj_citado": "99.888.777/0001-00", "telefones": ["(11) 3333-9999"], "emails": []}]
        self.assertEqual(ct.registrar_pistas(self.e, pistas), 1)
        c = self.e.canais.get(valor="1133332222")
        self.assertEqual((c.confianca_associacao, c.origem_canal), ("BAIXA", "PUBLICACAO"))
        self.assertIn("não confirma a identidade", c.evidencia_associacao)
        self.assertFalse(self.e.canais.filter(valor="1133339999").exists())  # outro CNPJ: não é desta empresa
        # contato de diretório fica disponível à equipe, marcado "a conferir" (sem exigir liberação)
        self.assertTrue(ct.canais_utilizaveis(self.e).filter(valor="1133332222").exists())
        from prospeccao import apresentacao as apr
        self.assertEqual(apr.alerta_canal(c), "A conferir")
        c.confirmacao_humana = CanalEmpresa.Confirmacao.ATENDIDO
        c.save()
        self.assertTrue(ct.canais_utilizaveis(self.e).filter(valor="1133332222").exists())  # confirmada por pessoa


AUSENTE = "123456780001" + calcular_dv("123456780001")


class BuscaFalsa:
    codigo = "openai_web_search"
    lote = True

    def __init__(self, resultados):
        self.resultados, self.chamadas, self.ultimo, self.max_acoes = resultados, [], {}, 3

    def custo_maximo(self):
        return Decimal("0.05")

    def custo_uma_tentativa(self):
        return 0.025

    def buscar(self, consulta, ctx, n=10, contexto=None):
        self.chamadas.append((consulta, contexto))
        self.ultimo = {"custo": 0.012, "medido": True, "modelo": "gpt-6-luna", "entrada": 9000, "saida": 500,
                       "acoes": 1, "fontes": [r["url"] for r in self.resultados]}
        if contexto and contexto.get("empresa"):
            return [r for r in self.resultados if r.get("_contato")]
        return [r for r in self.resultados if not r.get("_contato")]


class DescobertaOpenAITests(ti.IntegracaoBase):
    def setUp(self):
        super().setUp()
        self.p.teto_custo_usd = Decimal("1")
        self.p.save()

    def test_pistas_cnpj_ausente_do_recorte_e_contato_de_diretorio(self):
        res = [
            {"url": "https://guia.com.br/sabor", "dominio": "guia.com.br", "diretorio": True, "titulo": "Sabor Rede",
             "nome_citado": "SABOR REDE", "cnpj_citado": fmt(REDE), "telefones": [], "emails": [], "trecho": "",
             "verificacao": "URL retornada pela ferramenta"},
            {"url": "https://guia.com.br/nova", "dominio": "guia2.com.br", "diretorio": True, "titulo": "Nova Frota",
             "nome_citado": "NOVA FROTA", "cnpj_citado": fmt(AUSENTE), "telefones": [], "emails": [], "trecho": "",
             "verificacao": "URL retornada pela ferramenta"},
            {"url": "https://guia.com.br/gama", "dominio": "guia.com.br", "diretorio": True, "titulo": "Gama Cargas",
             "nome_citado": "GAMA CARGAS", "cnpj_citado": fmt(GMAIL), "telefones": ["(11) 2409-1234"], "emails": [],
             "trecho": "", "verificacao": "URL retornada pela ferramenta", "_contato": True},
        ]
        falsa = BuscaFalsa(res)
        from prospeccao.tests.test_prospeccao import Resp

        class RedeCNPJ(ti.RedeComBusca):  # consulta complementar acionada: CNPJ não existe (404)
            def __call__(self, metodo, url, **kw):
                if "brasilapi" in url:
                    self.urls.append(url)
                    return Resp(404, {"message": "CNPJ não encontrado"}, tipo="application/json")
                return super().__call__(metodo, url, **kw)
        rede = RedeCNPJ({})
        with mock.patch("prospeccao.conectores.busca_web.obter_busca", return_value=(falsa, "Busca falsa")):
            ex = self.rodar_com(rede)
        s = SelecaoPesquisa.objects.filter(pesquisa=self.p, cnpj=REDE).first()
        if s is not None:
            self.assertIn("pista, identidade a confirmar", s.motivos_selecao[0])
        a = SelecaoPesquisa.objects.get(pesquisa=self.p, cnpj=AUSENTE)
        self.assertTrue(any("Ausente do recorte mensal" in m for m in a.motivos_selecao))
        self.assertTrue(any(AUSENTE in u for u in rede.urls if "brasilapi" in u))  # consulta complementar acionada
        self.assertEqual(a.fila, "REVISAR_IDENTIDADE")  # pendente, não descartada
        self.assertIn("consulta complementar", " ".join(a.fila_motivos))
        # contato de diretório: pista (BAIXA) que a equipe JÁ PODE USAR, marcada "a conferir"
        g = Empresa.objects.get(cnpj=GMAIL)
        c = g.canais.get(valor="1124091234")
        self.assertEqual((c.confianca_associacao, c.nivel), ("BAIXA", "PISTA"))
        self.assertTrue(ct.canais_utilizaveis(g).filter(pk=c.pk).exists())
        self.assertEqual(SelecaoPesquisa.objects.get(pesquisa=self.p, cnpj=GMAIL).fila, "CONTATO_CADASTRAL")
        # uma chamada por empresa (consultas em lote) com o contexto público da empresa; custos medidos registrados
        ctt = [c for c in falsa.chamadas if c[1] and c[1].get("empresa")]
        self.assertTrue(ctt and isinstance(ctt[0][0], list))
        self.assertEqual(ctt[0][1]["empresa"]["municipio"], g.cidade)
        regs = ConsultaWeb.objects.filter(execucao=ex, status="OK")
        self.assertTrue(regs.exists())
        self.assertEqual(set(regs.values_list("custo_estimado_usd", flat=True)), {Decimal("0.01200")})


class ControlesPilotoTests(TestCase):
    """Controles exigidos antes do piloto pago: reserva no banco (concorrência), custo de falha, limites técnicos,
    fontes completas, corroboração, matriz x filial."""

    def setUp(self):
        p = mock.patch.dict("os.environ", ENV_OPENAI)
        p.start()
        self.addCleanup(p.stop)
        self.p = Pesquisa.objects.create(nome="t", uf="SP", cidades="Guarulhos", teto_custo_usd=Decimal("1"))

    def test_reserva_compartilhada_entre_execucoes_concorrentes(self):
        a, b = orcamento.Orcamento(self.p), orcamento.Orcamento(self.p)
        self.assertTrue(a.reservar(Decimal("0.6")))
        self.assertFalse(b.reservar(Decimal("0.6")))  # outra execução enxerga a reserva em andamento
        self.assertIn("registrado + reservado", " ".join(b.motivos_bloqueio))
        UsoIA.objects.create(tipo="TESTE", provedor="openai", versao_prompt="t", pesquisa=self.p,
                             custo_estimado_usd=Decimal("0.1"))
        a.liquidar(Decimal("0.6"), Decimal("0.1"))
        self.assertEqual(orcamento.ReservaCusto.objects.count(), 0)
        self.assertTrue(b.reservar(Decimal("0.6")))  # 0,1 gasto + 0,6 <= 1
        with mock.patch.dict("os.environ", {"PROSPECCAO_TETO_GLOBAL_USD": "0.75"}):
            c = orcamento.Orcamento(None, teto=Decimal("1"), tipo="TESTE")  # verificação entra no teto global
            self.assertFalse(c.reservar(Decimal("0.06")))  # 0,1 + 0,6 reservado + 0,06 > 0,75

    def test_falha_sem_medicao_conta_o_maximo_e_4xx_conta_zero(self):
        self.assertEqual(orcamento.custo_sem_medicao(TimeoutError("t"), Decimal("0.05")), Decimal("0.05"))
        self.assertEqual(orcamento.custo_sem_medicao(NS(status_code=500), Decimal("0.05")), Decimal("0.05"))
        self.assertEqual(orcamento.custo_sem_medicao(NS(status_code=400), Decimal("0.05")), Decimal("0"))
        b = OpenAIBusca()
        b._client = mock.Mock()
        b._client.responses.create.side_effect = TimeoutError("sem resposta")
        from prospeccao.conectores.base import ContextoExecucao
        orc = orcamento.Orcamento(self.p)
        orcamento.buscar(b, ["q"], finalidade="CONTATO", orc=orc, ctx=ContextoExecucao(), contexto={})
        reg = ConsultaWeb.objects.get()
        self.assertEqual(reg.status, "FALHA")
        self.assertEqual(reg.custo_estimado_usd, reg.custo_reservado_usd)  # pode ter sido cobrada
        self.assertEqual(orcamento.ReservaCusto.objects.count(), 0)

    def test_limites_tecnicos_fontes_completas_e_sem_dupla_contagem(self):
        b = OpenAIBusca(max_acoes=3)
        self.assertEqual(b.client.max_retries, 0)
        itens = [NS(type="web_search_call", status="completed",
                    action=NS(type="search", query="gama cargas guarulhos contato",
                              sources=[NS(type="url", url=f"https://s{i}.com.br/") for i in range(6)])),
                 NS(type="web_search_call", status="completed",
                    action=NS(type="open_page", url="https://s1.com.br/contato", sources=None)),
                 NS(type="message", content=[NS(type="output_text", annotations=[
                     NS(type="url_citation", url="https://s1.com.br/contato")])])]
        b._client = mock.Mock()
        b._client.responses.create.return_value = resp_texto({"resultados": [item("https://s1.com.br/contato")]},
                                                             ent=30000, sai=1000, output=itens)
        from prospeccao.conectores.base import ContextoExecucao
        orcamento.buscar(b, ["q"], finalidade="CONTATO", orc=orcamento.Orcamento(self.p), ctx=ContextoExecucao(),
                         contexto={})
        kw = b._client.responses.create.call_args.kwargs
        self.assertEqual(kw["max_tool_calls"], 3)
        reg = ConsultaWeb.objects.get()
        self.assertEqual(len(reg.fontes), 7)  # 6 de sources (mais que a única citação) + a citação
        self.assertEqual([a["tipo"] for a in reg.acoes], ["search", "open_page"])
        self.assertEqual(reg.acoes[0]["consulta"], "gama cargas guarulhos contato")
        # custo = tokens medidos (conteúdo já incluído em input_tokens) + 2 ações; nada somado em dobro
        self.assertAlmostEqual(float(reg.custo_estimado_usd), 30000 * 0.1 / 1e6 + 1000 * 0.5 / 1e6 + 0.02, places=5)
        self.assertIn("coletado_em", reg.resultados[0])

    def test_corroboracao_e_niveis(self):
        e = Empresa.objects.create(cnpj="12345678000276", nome="VALENTE FILIAL", razao_social="TRANSPORTES VALENTE",
                                   cidade="GUARULHOS", uf="SP")
        with mock.patch("prospeccao.services.contatos.dominio_tem_mx", return_value=True), \
                mock.patch("prospeccao.services.contatos.compartilhamento", return_value=("", None)):
            c = ct.registrar(e, "TELEFONE", "(11) 2444-5555", fonte="receita", origem="CADASTRAL", confianca="MEDIA",
                             aplica_a="ESTABELECIMENTO")
            self.assertEqual(c.nivel, "CADASTRAL")
            ct.registrar(e, "TELEFONE", "(11) 2444-5555", fonte="pista:guia.com.br", url="https://guia.com.br/v",
                         origem="PUBLICACAO", confianca="BAIXA", aplica_a="DESCONHECIDO")
            c.refresh_from_db()
            self.assertEqual(c.nivel, "CADASTRAL")  # diretório não corrobora (costuma copiar a Receita)
            self.assertEqual(len(c.corroboracoes), 1)  # mas fica rastreado
            ct.registrar(e, "TELEFONE", "(11) 2444-5555", fonte="site_publico", url="https://valente.com.br/contato",
                         origem="PUBLICACAO", confianca="MEDIA", aplica_a="EMPRESA")
            c.refresh_from_db()
            self.assertEqual(c.nivel, "CORROBORADO")
            c.confirmacao_humana = "INCORRETO"
            c.save()
            ct.registrar(e, "TELEFONE", "(11) 2444-5555", fonte="site_publico", url="https://valente.com.br/x",
                         origem="PUBLICACAO", confianca="ALTA", aplica_a="EMPRESA")
            c.refresh_from_db()
            self.assertEqual((c.nivel, c.confirmacao_humana), ("INCORRETO", "INCORRETO"))  # correção humana mantida
        # filial cujo site publica só a matriz
        r = ResultadoSite(url_inicial="https://valente.com.br/")
        r.paginas, r.cnpjs = [("https://valente.com.br/", "Valente")], {"12345678000195": "x"}
        a = ct.associar_site(e, r, "busca web: q")
        self.assertEqual((a[0], a[1]), ("MEDIA", "MATRIZ"))

    def test_estimativa_memoria_de_calculo(self):
        from prospeccao.models import FrenteComercial
        est = orcamento.estimativa(self.p, 5, modelo="gpt-6-luna", provedor_busca="openai", descoberta_por_frente=1)
        self.assertEqual(est["chamadas_busca"], 5 + 1)
        self.assertEqual(est["acoes_pesquisa_max"], 6 * 3)
        self.assertAlmostEqual(est["tarifa_busca_max_usd"], 0.18)
        self.assertTrue(any("max_tool_calls" in m for m in est["memoria"]))


class TesteInteligenteTests(TestCase):
    """Caminho gratuito primeiro, controle (força a pesquisa), +1 ação na reserva, revisão humana importada."""

    def setUp(self):
        p = mock.patch.dict("os.environ", ENV_OPENAI)
        p.start()
        self.addCleanup(p.stop)
        self.e = Empresa.objects.create(cnpj="12345678000195", nome="TRANSPORTES VALENTE LTDA",
                                        razao_social="TRANSPORTES VALENTE LTDA", cidade="GUARULHOS", uf="SP",
                                        cep="07000-100")
        CanalEmpresa.objects.create(empresa=self.e, tipo="EMAIL", valor="vendas@valente.com.br",
                                    valor_normalizado="vendas@valente.com.br", origem_canal="CADASTRAL")
        for alvo in ("dominio_tem_mx", "compartilhamento"):
            pt = mock.patch(f"prospeccao.services.contatos.{alvo}",
                            return_value=True if alvo == "dominio_tem_mx" else ("", None))
            pt.start()
            self.addCleanup(pt.stop)

    def site(self):
        r = ResultadoSite(url_inicial="https://valente.com.br/")
        r.paginas = [("https://valente.com.br/", "Transportes Valente – CNPJ 12.345.678/0001-95")]
        r.cnpjs = {"12345678000195": "https://valente.com.br/"}
        r.telefones = {"1124445555": "https://valente.com.br/contato"}
        leitor = mock.Mock()
        leitor.analisar.return_value = r
        return leitor

    def test_caminho_gratuito_dispensa_pesquisa_paga(self):
        buscar = mock.Mock(return_value=[])
        r = ct.investigar_contatos(self.e, self.site(), mock.Mock(), buscar_fn=buscar, consultas=["{razao_social}"])
        self.assertFalse(buscar.called)
        self.assertIn("dispensada", r["busca_paga"])
        self.assertEqual(r["confianca"], "ALTA")

    def test_controle_ignora_caminho_gratuito_e_forca_pesquisa(self):
        buscar = mock.Mock(return_value=[{"url": "https://valente.com.br/", "dominio": "valente.com.br",
                                          "diretorio": False, "verificacao": "URL retornada pela ferramenta"}])
        r = ct.investigar_contatos(self.e, self.site(), mock.Mock(), buscar_fn=buscar, consultas=["{razao_social}"],
                                   forcar_busca=True)
        self.assertTrue(buscar.called)
        self.assertIn("controle", r["busca_paga"])
        self.assertIn("busca web", r["como"])  # reencontrado pela pesquisa, não pelo domínio do e-mail

    def test_reserva_cobre_acao_nao_concluida(self):
        b = OpenAIBusca(max_acoes=1)
        self.assertAlmostEqual(b.custo_uma_tentativa(),
                               ia.custo(b.modelo, 3_000 + b.tokens_por_acao, b.max_tokens) + 2 * 0.01, places=6)

    def test_importar_revisao(self):
        import os
        import tempfile
        from django.core.management import call_command
        c1 = self.e.canais.get()
        c2 = CanalEmpresa.objects.create(empresa=self.e, tipo="TELEFONE", valor="1133332222", origem_canal="PUBLICACAO",
                                         confianca_associacao="BAIXA", fonte="pista:guia.com.br")
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig") as f:
            f.write("canal_id;revisao;observacao\n")
            f.write(f"{c1.pk};ERRADO;é do contador\n{c2.pk};certo;\n")
        try:
            call_command("piloto_ia", importar_revisao=f.name, stdout=open(os.devnull, "w"))
        finally:
            os.unlink(f.name)
        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual((c1.nivel, c2.nivel), ("INCORRETO", "CONFERIDO_EQUIPE"))
        self.assertIn("é do contador", c1.evidencia_associacao)
        self.assertTrue(ct.canais_utilizaveis(self.e).filter(pk=c2.pk).exists())  # pista conferida passa a valer
        self.assertFalse(ct.canais_utilizaveis(self.e).filter(pk=c1.pk).exists())


class EscopoComercialTests(TestCase):
    """Escopo BRIDA: 4 perfis; concorrentes e atividade secundária a revisar; fora do escopo sem gasto pago."""

    def emp(self, cnae, sec=(), nome="X"):
        return Empresa.objects.create(cnpj=f"{len(Empresa.objects.all()) + 10:08d}000100"[:14], nome=nome, cnae=cnae,
                                      atividade_principal="a", cnaes_secundarios=[{"codigo": c} for c in sec])

    def test_perfis_concorrente_secundaria_e_fora(self):
        from prospeccao.services import escopo as esc
        self.assertEqual(esc.classificar(self.emp("4930202"))[0], "FROTA")
        self.assertEqual(esc.classificar(self.emp("3811400"))[0], "FROTA")          # coleta de resíduos
        self.assertEqual(esc.classificar(self.emp("2222600"))[0], "INDUSTRIA")
        self.assertEqual(esc.classificar(self.emp("4313400"))[0], "AGRO_MAQUINAS")  # terraplenagem
        self.assertEqual(esc.classificar(self.emp("4520001"))[0], "REVENDA_OFICINA")
        self.assertEqual(esc.classificar(self.emp("4681805"))[0], "REVISAR")        # lubrificantes atacado
        self.assertIn("concorrente", esc.classificar(self.emp("4681805"))[1])
        self.assertEqual(esc.classificar(self.emp("5620102"))[0], "FORA")           # bufê
        self.assertEqual(esc.classificar(self.emp("6422100"))[0], "FORA")           # banco
        e = self.emp("4711301", sec=["5212500"])                                    # mercado c/ armazenagem secundária
        self.assertEqual(esc.classificar(e)[0], "REVISAR")
        self.assertIn("SECUNDÁRIA", esc.classificar(e)[1])

    def test_evidencia_por_palavra_nao_traz_de_volta_mas_ia_sim(self):
        from prospeccao.models import Evidencia
        from prospeccao.services import escopo as esc
        e = self.emp("6424703", nome="COOP")
        Evidencia.objects.create(empresa=e, tipo="MAQUINAS", classificacao="CONFIRMADO_FONTE", gerada_por="REGRA",
                                 fonte="site_publico", chave="k1", descricao="Site informa: máquinas agrícolas")
        self.assertEqual(esc.classificar(e)[0], "FORA")
        Evidencia.objects.create(empresa=e, tipo="FROTA", classificacao="HIPOTESE", gerada_por="IA",
                                 fonte="ia:gpt-6-luna", chave="k2", descricao="IA [OPERACAO_PROPRIA]: frota")
        self.assertEqual(esc.classificar(e)[0], "REVISAR")

    def test_fora_do_escopo_nao_recebe_gasto_e_decisao_humana_prevalece(self):
        from prospeccao.models import Oportunidade
        from prospeccao.services import escopo as esc
        e = self.emp("5620102", nome="BUFFET")
        self.assertFalse(esc.pertinente_para_gasto(e)[0])
        Oportunidade.objects.create(empresa=e, tipo="NOVA_EXTERNA", escopo="FROTA", escopo_humano=True)
        self.assertTrue(esc.pertinente_para_gasto(e)[0])  # gestor reclassificou (ex.: tem frota de entrega)


class ConsultaComplementarTests(TestCase):
    """A IA analisa o retorno da pesquisa e escolhe UMA consulta complementar quando falta contato."""

    def test_falta_contato_gera_consulta_complementar_com_motivo(self):
        from prospeccao.conectores.busca_web import ListaBusca
        e = Empresa.objects.create(cnpj="12345678000195", nome="AUTO PECAS MAUA", razao_social="AUTO PECAS MAUA LTDA",
                                   cidade="MAUA", uf="SP")
        chamadas = []

        def buscar_fn(qs, empresa, motivo=""):
            chamadas.append((list(qs), motivo))
            r = ListaBusca([])
            r.analise = {"faltando": ["TELEFONE", "EMAIL"], "proximas_consultas": ['"Auto Pecas Maua" telefone'],
                         "justificativa": "nenhuma página com contato"} if not motivo else {}
            return r
        leitor = mock.Mock()
        r = ct.investigar_contatos(e, leitor, mock.Mock(), buscar_fn=buscar_fn, consultas=["{razao_social} {municipio}"])
        self.assertEqual(len(chamadas), 2)
        self.assertEqual(chamadas[1][0], ['"Auto Pecas Maua" telefone'])
        self.assertIn("falta TELEFONE, EMAIL", chamadas[1][1])
        self.assertIn("complementar", r["busca_paga"])
        self.assertIn("resultado", r["complementar"])


class CincoPontosMauaTests(TestCase):
    """Correções do teste real de Mauá: nome fantasia, contato sem site, terceiros, mascarados, CNPJ pendente."""

    def setUp(self):
        self.e = Empresa.objects.create(cnpj="14977885000198", nome="14.977.885 DAVID RODRIGUES DIAS",
                                        razao_social="14.977.885 DAVID RODRIGUES DIAS", cidade="Maua", uf="SP",
                                        cep="09320036", endereco="RUA JOSE SANCHES PARRA 16 – PARQUE BOA ESPERANCA")
        for alvo, ret in (("dominio_tem_mx", True), ("compartilhamento", ("", None))):
            pt = mock.patch(f"prospeccao.services.contatos.{alvo}", return_value=ret)
            pt.start()
            self.addCleanup(pt.stop)

    def site(self, texto):
        r = ResultadoSite(url_inicial="https://piquenocentroautomotivo.com.br/")
        r.paginas = [("https://piquenocentroautomotivo.com.br/", texto)]
        return r

    def test_nome_fantasia_diferente_da_razao_social(self):
        alt = ct.nomes_citados_com_cnpj(self.e, [{"cnpj_citado": "14.977.885/0001-98",
                                                  "nome_citado": "David Rodrigues Dias; Piqueno Centro Automotivo"}])
        self.assertIn("Piqueno Centro Automotivo", alt)
        completo = "Piqueno Centro Automotivo – Rua José Sanches Parra, 16 – Mauá – CEP 09320-036"
        a = ct.associar_site(self.e, self.site(completo), "busca web", nomes_alt=alt)
        self.assertEqual((a[0], a[3]), ("MEDIA", True))  # endereço cadastral + nome citado com o mesmo CNPJ
        a = ct.associar_site(self.e, self.site("Piqueno Centro Automotivo – oficina"), "busca web", nomes_alt=alt)
        self.assertEqual((a[0], a[3]), ("BAIXA", False))  # só o nome: possível site, não confirmado
        self.assertIn("Possível site da empresa", a[2])
        a = ct.associar_site(self.e, self.site("Rua José Sanches Parra, 16 – Mauá – CEP 09320-036"), "busca web")
        self.assertEqual((a[0], a[3]), ("BAIXA", False))  # só endereço: possível site
        outro = ResultadoSite(url_inicial="https://oficinaxyz.com.br/")
        outro.paginas = [("https://oficinaxyz.com.br/", "Outra oficina em Santo André")]
        self.assertIsNone(ct.associar_site(self.e, outro, "busca web", nomes_alt=alt))  # sem nenhuma evidência

    def test_terceiro_separado_e_mascarado_fora(self):
        from prospeccao import apresentacao as apr
        direto = ct.registrar(self.e, "TELEFONE", "(11) 4547-3333", fonte="receita", origem="CADASTRAL",
                              confianca="MEDIA", aplica_a="ESTABELECIMENTO")
        cont = ct.registrar(self.e, "EMAIL", "atendimento@erigicontabilidade.com.br", fonte="receita",
                            origem="CADASTRAL", confianca="MEDIA", aplica_a="ESTABELECIMENTO")
        self.assertEqual(ct.papel_terceiro(cont), "Contador")
        self.assertEqual(list(ct.canais_utilizaveis(self.e)), [direto])  # contador não invalida o telefone direto
        self.assertEqual(list(ct.canais_terceiros(self.e)), [cont])
        c = apr.contato_lista(self.e)
        self.assertEqual((c["telefone"], c["email"]), ("1145473333", ""))
        self.assertEqual(c["terceiros"][0]["papel"], "Contador")
        self.assertFalse(c["so_terceiro"])
        # e-mail mascarado: não vira canal, não conta como e-mail, original no histórico da fonte
        self.assertIsNone(ct.registrar(self.e, "EMAIL", "sa****@****.com", fonte="pista:cnpj.biz",
                                       url="https://cnpj.biz/x", origem="PUBLICACAO", confianca="BAIXA",
                                       aplica_a="DESCONHECIDO"))
        self.assertFalse(self.e.canais.filter(valor__contains="*").exists())
        d = self.e.dados_coletados.get(campo__startswith="email_nao_disponibilizado")
        self.assertIn("sa****@****.com", d.valor)
        self.assertTrue(apr.contato_lista(self.e)["email_oculto"])
        from prospeccao.models import Oportunidade
        op = Oportunidade.objects.create(empresa=self.e, tipo="NOVA_EXTERNA")
        self.assertTrue(Oportunidade.objects.filter(pk=op.pk).filter(apr.sem_canal_q("EMAIL")).exists())
        self.assertFalse(Oportunidade.objects.filter(pk=op.pk).filter(apr.sem_canal_q("TELEFONE")).exists())
        # só contato de terceiro: empresa continua acessível, com o motivo
        direto.delete()
        self.assertTrue(apr.contato_lista(self.e)["so_terceiro"])

    def test_cnpj_de_outra_cidade_busca_mesma_raiz_na_regiao(self):
        from receita.models import CargaReceita, EstabelecimentoReceita
        from prospeccao.services import selecao as sl
        carga = CargaReceita.objects.create(competencia="2026-10", status="CONCLUIDA", ativa=True) \
            if "ativa" in [f.name for f in CargaReceita._meta.fields] else CargaReceita.objects.create(competencia="2026-10")
        base = dict(carga=carga, cnpj_basico="00827591", razao_social="ACTIVAS PLASTICOS INDUSTRIAIS LTDA",
                    situacao_codigo="02", uf="SP")
        EstabelecimentoReceita.objects.create(cnpj="00827591000680", municipio_codigo="7075", **base)
        EstabelecimentoReceita.objects.create(cnpj="00827591000175", municipio_codigo="6689", **base)
        irmaos = sl.irmaos_na_regiao(carga, "00827591000680", {"6689"}, {"02"})
        self.assertEqual([i["cnpj"] for i in irmaos], ["00827591000175"])
