"""
Testes da pesquisa PLANEJADA com dados SINTÉTICOS (arquivos no layout oficial, sites falsos; nenhuma rede).
Casos de controle: lanchonete, rede de alimentação com frota, Sicoob/financiamento, contador, matriz/filial,
sem site, e-mail genérico, atividade genérica com evidência operacional, CNAE inventado.
"""
import shutil
import tempfile
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from carteira.models import Abordagem, CanalEmpresa, Empresa
from carteira.services import abordagens as ab
from carteira.services.importacao import importar_clientes
from carteira.tests.test_carteira import conta
from prospeccao.models import Execucao, Fonte, FrenteComercial, Pesquisa, SelecaoPesquisa, UsoIA
from prospeccao.services import fila, ia, planejamento
from prospeccao.services.pipeline import executar
from receita import importador
from receita.tests.test_receita import cnpj, empresa_linha, estab, gravar_zip, montar_arquivos

TRANSP = cnpj("101010100001")       # transportadora (CNAE 4930) – site descreve frota própria
LANCH = cnpj("202020200001")        # lanchonete isolada (5611) – não deve entrar
LANCH_SEC = cnpj("303030300001")    # lanchonete com secundária de transporte – entra para investigar
REDE = cnpj("404040400001")         # rede de alimentação (5620) com CD/frota no site, nome genérico
SICOOB = cnpj("505050500001")       # cooperativa de crédito com "financiamento de frota" no site e "TRANSPORTE" no nome
GENERICO = cnpj("606060600001")     # CNAE genérico 8219 + nome "TRANSPORTES"
CONTADOR_TEL = cnpj("707070700001")  # telefone/e-mail do contador compartilhados
MATRIZ = cnpj("808080800001")
FILIAL = cnpj("808080800002")       # o site publica só a matriz
CONHECIDA = cnpj("909090900001")
GMAIL = cnpj("111122220001")         # e-mail gmail, sem site


def linhas_estab():
    L = [
        estab(TRANSP, "RAPIDO FROTA", cnae="4930202", email="contato@rapidofrota.com.br", tel=("11", "24001111")),
        estab(LANCH, "LANCHONETE DO ZE", cnae="5611203", email="ze@gmail.com", tel=("11", "24002222"), sec=""),
        estab(LANCH_SEC, "LANCHES E ENTREGAS", cnae="5611203", email="", tel=("11", "24003333")),
        estab(REDE, "SABOR REDE", cnae="5620104", email="rh@saborrede.com.br", tel=("11", "24004444"), sec=""),
        estab(SICOOB, "SICOOB TRANSPORTE CREDITO", cnae="6424703", email="contato@sicoobx.com.br", tel=("11", "24005555"),
              sec=""),
        estab(GENERICO, "ALFA TRANSPORTES", cnae="8219999", email="alfa@alfatransportes.com.br", tel=("11", "24006666"),
              sec=""),
        estab(MATRIZ, "GRUPO BETA LOGISTICA", cnae="4930201", email="beta@betalog.com.br", tel=("11", "24007777")),
        estab(FILIAL, "GRUPO BETA LOGISTICA", cnae="4930201", email="filial@betalog.com.br", tel=("11", "24007777"),
              matriz="2"),
        estab(CONHECIDA, "JA CLIENTE TRANSPORTES", cnae="4930202", email="x@jacliente.com.br"),
        estab(GMAIL, "GAMA CARGAS", cnae="4930202", email="gamacargas@gmail.com", tel=("11", "24009999")),
    ]
    # contador: mesmo telefone e e-mail em 4 empresas de grupos diferentes
    for i, b in enumerate(["717171710001", "727272720001", "737373730001", CONTADOR_TEL[:12]]):
        L.append(estab(cnpj(b) if len(b) == 12 else b, f"TRANSPORTADORA CLIENTE {i}", cnae="4930202",
                       email="fiscal@escritorioabc.com.br", tel=("11", "29990000")))
    return L


def html(corpo):
    return f"<html><body>{corpo}</body></html>"


def fmt(c):
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"


SITES = {
    "rapidofrota.com.br": html(f"Rápido Frota – transporte de cargas com frota própria de 80 caminhões e oficina própria. "
                               f"Fale com compras: compras@rapidofrota.com.br (11) 2400-1112 "
                               f'<a href="https://wa.me/5511940001111">WhatsApp</a> CNPJ {fmt(TRANSP)} '
                               f"Site desenvolvido por agencia@webdev.com.br"),
    "saborrede.com.br": html(f"Sabor Rede: refeições coletivas. Nosso centro de distribuição opera frota própria de "
                             f"caminhões refrigerados. Atendimento: sac@saborrede.com.br CNPJ {fmt(REDE)}"),
    "sicoobx.com.br": html(f"Sicoob Transporte Crédito: linhas de crédito e financiamento para renovação da frota de "
                           f"caminhões e máquinas agrícolas. CNPJ {fmt(SICOOB)}"),
    "alfatransportes.com.br": html(f"Alfa Transportes – frota própria de carretas e manutenção da frota. CNPJ {fmt(GENERICO)}"),
    "betalog.com.br": html(f"Grupo Beta Logística – unidades em SP. Central (11) 3000-4000. CNPJ {fmt(MATRIZ)}"),
}


class Rede:
    def __call__(self, metodo, url, **kw):
        from prospeccao.tests.test_prospeccao import Resp
        if "brasilapi" in url:
            raise AssertionError("não deveria consultar BrasilAPI")
        if url.endswith("robots.txt"):
            return Resp(404, "")
        for dom, pag in SITES.items():
            if dom in url:
                return Resp(200, pag) if url.rstrip("/").endswith(dom) else Resp(404, "")
        return Resp(404, "")


def resolve_falso(dom):
    return any(dom == d for d in SITES)


class Base(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        L = linhas_estab()
        montar_arquivos(self.dir, estab_linhas=L, empresas=[empresa_linha(x[1:9], f"RAZAO {x[1:9]} LTDA") for x in L])
        importador.importar("2026-10", ["SP"], ["Guarulhos"], diretorio=self.dir, baixar=False, log=lambda *a: None)
        importar_clientes([conta("SFC", CONHECIDA, nome="JA CLIENTE TRANSPORTES LTDA")])
        for alvo in ("prospeccao.conectores.base.time.sleep",):
            p = mock.patch(alvo)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("prospeccao.services.contatos.dominio_resolve", side_effect=resolve_falso)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("prospeccao.services.contatos.dominio_tem_mx", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict("os.environ", {"PROSPECCAO_TETO_GLOBAL_USD": "", "OPENAI_API_KEY": "", "SERPAPI_API_KEY": "", "PROSPECCAO_BUSCA_PROVEDOR": ""})
        p.start()
        self.addCleanup(p.stop)
        self.frente = FrenteComercial.objects.get(nome__startswith="Transporte")
        self.p = Pesquisa.objects.create(nome="Piloto sintético", modo="PLANEJADA", uf="SP", cidades="Guarulhos",
                                         objetivo="Empresas com operação de transporte e frota própria em Guarulhos",
                                         quantidade_desejada=20, preferencias="Preferir matriz")
        self.p.fontes.set(Fonte.objects.filter(codigo="receita_cnpj"))
        self.p.frentes.set([self.frente])
        self.user = get_user_model().objects.create_user("v1")

    def rodar(self):
        with mock.patch("requests.Session.request", side_effect=Rede()):
            ex, _ = fila.enfileirar(self.p, Execucao.Gatilho.COMANDO)
            executar(ex.pk)
        ex.refresh_from_db()
        return ex

    def sel(self, c):
        return SelecaoPesquisa.objects.get(pesquisa=self.p, cnpj=c)


class PlanoTests(Base):
    def test_cnae_inventado_rejeitado_e_tabela_oficial(self):
        validos, rejeitados = planejamento.verificar_cnaes(["4930202", "9999999", {"codigo": "4930", "motivo": "x"}])
        self.assertEqual({v["codigo"] for v in validos}, {"4930202"})  # tabela sintética só tem 4930202
        self.assertEqual(rejeitados[0]["codigo"], "9999999")

    def test_plano_ia_sem_credencial_registra_e_nao_inventa(self):
        with mock.patch.dict("os.environ", {"PROSPECCAO_IA_PROVEDOR": ""}):
            with self.assertRaises(planejamento.IANaoConfigurada):
                planejamento.plano_ia(self.p)
        self.assertEqual(UsoIA.objects.get().status, "NAO_CONFIGURADA")
        self.assertEqual(self.p.planos.count(), 0)

    def test_plano_ia_simulado_valida_cnaes_e_nao_aplica_exclusao_sugerida(self):
        bruto = {"interpretacao": "frotas", "consultas_externas": ['"{razao_social}" {municipio}'], "ambiguidades": ["x"],
                 "exclusoes_explicitas": ["restaurantes"], "preferencias_interpretadas": ["preferir matriz"],
                 "por_frente": [{"frente": self.frente.nome, "atividades": ["transporte"], "termos": ["frota"],
                                 "cnaes": [{"codigo": "4930202", "motivo": "carga"}, {"codigo": "1234567", "motivo": "?"}],
                                 "sinais": ["caminhoes"]}]}
        self.p.teto_custo_usd = 1
        self.p.save()
        fake = mock.Mock(nome="anthropic", modelo="claude-opus-5-5", max_retries=1, max_tokens=6000)
        fake.planejar.return_value = (bruto, mock.Mock(input_tokens=1000, output_tokens=500), "claude-opus-5-5")
        with mock.patch.object(ia, "status", return_value=(True, "ok")), mock.patch.object(ia, "obter_analisador",
                                                                                        return_value=fake):
            pl = planejamento.plano_ia(self.p)
        self.assertEqual(pl.origem, "IA")
        self.assertEqual([r["codigo"] for r in pl.cnaes_rejeitados], ["1234567"])
        self.assertEqual(pl.conteudo["exclusoes_explicitas"]["termos"], [])  # sugestão da IA NÃO vira exclusão
        self.assertEqual(pl.conteudo["exclusoes_sugeridas_pela_ia"], ["restaurantes"])
        self.assertAlmostEqual(float(pl.uso_ia.custo_estimado_usd), 0.014)  # 1000*4/1e6 + 500*20/1e6

    def test_versao_registrada_na_execucao(self):
        ex = self.rodar()
        self.assertEqual(ex.plano.versao, 1)
        planejamento.plano_manual(self.p, self.user, {str(self.frente.pk): {"termos": ["FROTA"]}})
        self.p.refresh_from_db()
        self.assertEqual(self.p.plano_ativo.versao, 2)


class SelecaoTests(Base):
    def test_selecao_por_operacao_e_controles(self):
        ex = self.rodar()
        self.assertEqual(ex.status, Execucao.Status.CONCLUIDA, (ex.mensagem, ex.limites_atingidos))
        cnpjs = set(SelecaoPesquisa.objects.filter(pesquisa=self.p).values_list("cnpj", flat=True))
        self.assertNotIn(LANCH, cnpjs)  # lanchonete isolada não entra só pela cidade
        self.assertIn(LANCH_SEC, cnpjs)  # entra pela secundária, mas...
        self.assertEqual(self.sel(LANCH_SEC).fila, "INVESTIGAR")  # ...sem evidência fica para investigar
        self.assertEqual(self.sel(CONHECIDA).fila, "CONHECIDA")
        t = self.sel(TRANSP)
        self.assertEqual(t.aderencia, "SUSTENTADA")
        self.assertEqual(t.fila, "SELECIONADA")
        self.assertTrue(any("Atividade principal 4930202" in m for m in t.motivos_selecao))
        # atividade genérica + nome + site com frota própria
        g = self.sel(GENERICO)
        self.assertEqual(g.aderencia, "SUSTENTADA")
        # Sicoob: financiamento de frota NÃO é operação própria
        s = self.sel(SICOOB)
        self.assertEqual(s.aderencia, "FORA")
        self.assertIn("serviço/produto ou para terceiros", s.aderencia_motivos[-1])
        self.assertEqual(s.fila, "FORA")
        # rede de alimentação: não entrou pela atividade (5620 fora do plano) – não é forçada a entrar
        self.assertNotIn(REDE, cnpjs)

    def test_contatos_com_fonte_e_associacao(self):
        self.rodar()
        e = Empresa.objects.get(cnpj=TRANSP)
        compras = e.canais.get(valor="compras@rapidofrota.com.br")
        self.assertEqual((compras.classificacao, compras.confianca_associacao, compras.origem_canal),
                         ("COMPRAS", "ALTA", "PUBLICACAO"))
        self.assertEqual(compras.departamento, "compras")
        self.assertFalse(e.canais.filter(valor__contains="webdev").exists())  # e-mail de terceiro na página
        w = e.canais.get(valor="11940001111")
        self.assertTrue(w.whatsapp_explicito)
        self.assertFalse(e.canais.get(valor="1124001112").whatsapp_explicito)  # celular/fixo não vira WhatsApp
        self.assertEqual(e.canais.get(valor="1124001111").origem_canal, "CADASTRAL")
        # contador: telefone/e-mail compartilhados por 4 grupos -> conflito, não utilizável
        c = Empresa.objects.get(cnpj=CONTADOR_TEL)
        tel = c.canais.get(valor="1129990000")
        self.assertEqual((tel.aplica_a, tel.confianca_associacao), ("CONTADOR", "CONFLITO"))
        self.assertIn("grupos diferentes", tel.compartilhado)
        self.assertEqual(self.sel(CONTADOR_TEL).fila, "BUSCAR_CONTATO")
        # matriz/filial: o site publica só a matriz -> canais da MATRIZ (não confirmam a filial)
        f = Empresa.objects.get(cnpj=FILIAL)
        site = f.canais.get(tipo="SITE")
        self.assertEqual((site.aplica_a, site.confianca_associacao), ("MATRIZ", "MEDIA"))
        self.assertIn(f.canais.get(valor="1124007777").aplica_a, ("MATRIZ", "GRUPO"))  # mesmo telefone da matriz
        # sem site e e-mail gmail: segue com contato cadastral (não exige domínio próprio)
        self.assertEqual(self.sel(GMAIL).fila, "CONTATO_CADASTRAL")
        self.assertFalse(CanalEmpresa.objects.filter(valor__startswith="contato@gamacargas").exists())  # nada inventado

    def test_repeticao_progressao_e_decisao_humana(self):
        Pesquisa.objects.filter(pk=self.p.pk).update(quantidade_desejada=100, limite_resultados=3)
        self.p.refresh_from_db()
        self.rodar()
        primeira = set(SelecaoPesquisa.objects.filter(pesquisa=self.p).values_list("cnpj", flat=True))
        # ordem por pontuação, não por CNPJ: a menor raiz (0123...) não é obrigatoriamente a primeira
        s = SelecaoPesquisa.objects.filter(pesquisa=self.p).exclude(fila="CONHECIDA").order_by("-pontuacao").first()
        s.decisao_humana, s.decisao_motivo = "RETIRAR", "gestor decidiu"
        s.save()
        self.rodar()
        todas = SelecaoPesquisa.objects.filter(pesquisa=self.p)
        self.assertEqual(todas.count(), len(set(todas.values_list("cnpj", flat=True))))  # sem duplicar
        self.assertTrue(primeira < set(todas.values_list("cnpj", flat=True)))  # avançou para novos candidatos
        s.refresh_from_db()
        self.assertEqual((s.decisao_humana, s.fila), ("RETIRAR", "FORA"))  # decisão preservada

    def test_abordagem_tira_da_primeira_fila(self):
        self.rodar()
        e = Empresa.objects.get(cnpj=TRANSP)
        ab.registrar_abordagem(Abordagem(empresa=e, usuario=self.user, canal="TELEFONE", resultado="CONVERSA_RETORNAR",
                                         data_retorno=date.today() + timedelta(days=2)))
        from prospeccao.services.selecao import recalcular_filas
        recalcular_filas(self.p)
        self.assertEqual(self.sel(TRANSP).fila, "RETORNO")

    def test_incluir_insuficiente_pelo_gestor(self):
        self.rodar()
        s = self.sel(LANCH_SEC)
        s.decisao_humana, s.decisao_motivo = "INCLUIR", "conheço a operação de entregas"
        from prospeccao.services.selecao import calcular_fila
        s.fila, _ = calcular_fila(s, s.empresa, self.p)
        self.assertEqual(s.fila, "CONTATO_CADASTRAL")  # entra, com contato a confirmar


class TelasPlanejadaTests(Base):
    def test_telas_e_acoes(self):
        self.rodar()
        gestor = get_user_model().objects.create_user("g", is_staff=True)
        self.client.force_login(gestor)
        from django.urls import reverse
        for url in [reverse("prospeccao:frentes"), reverse("prospeccao:plano", args=[self.p.pk]),
                    reverse("prospeccao:lista", args=[self.p.pk]), reverse("prospeccao:lista", args=[self.p.pk]) + "?fila=TODAS",
                    reverse("prospeccao:pesquisa_editar", args=[self.p.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        s = self.sel(TRANSP)
        r = self.client.post(reverse("prospeccao:selecao_revisar", args=[s.pk]), {"acao": "RETIRAR", "motivo": "teste"})
        self.assertEqual(r.status_code, 302)
        s.refresh_from_db()
        self.assertEqual(s.fila, "FORA")
        canal = s.empresa.canais.filter(tipo="TELEFONE").first()
        self.client.post(reverse("prospeccao:canal_acao", args=[canal.pk]), {"acao": "incorreto"})
        canal.refresh_from_db()
        self.assertEqual(canal.confirmacao_humana, "INCORRETO")
        self.assertFalse(Abordagem.objects.exists())  # ações em canais não registram abordagem


class IAInvestigacaoTests(TestCase):
    def test_contatos_e_citacoes_inexistentes_rejeitados(self):
        paginas = [("https://x.com.br", "Operamos 40 caminhões. Compras: compras@x.com.br (11) 2400-1000")]
        bruto = {"relacao": "SUSTENTADA", "justificativa": "j", "hipoteses_aplicacao": ["Filtros", "Inventado"],
                 "informacoes_insuficientes": [], "perguntas_para_vendedor": [], "proxima_acao": "ligar",
                 "evidencias": [{"trecho_exato": "Operamos 40 caminhões", "url": "https://x.com.br", "descricao": "frota",
                                 "contexto": "OPERACAO_PROPRIA", "tipo": "FROTA", "aplicacoes": ["Filtros"]},
                                {"trecho_exato": "frota de 900 ônibus", "url": "https://x.com.br", "descricao": "inventado",
                                 "contexto": "OPERACAO_PROPRIA", "tipo": "FROTA", "aplicacoes": []}],
                 "contatos": [{"tipo": "EMAIL", "valor": "compras@x.com.br", "departamento": "compras", "trecho_exato": ""},
                              {"tipo": "EMAIL", "valor": "joao.silva@x.com.br", "departamento": "", "trecho_exato": ""},
                              {"tipo": "TELEFONE", "valor": "(11) 2400-1000", "departamento": "", "trecho_exato": ""},
                              {"tipo": "TELEFONE", "valor": "(11) 9999-0000", "departamento": "", "trecho_exato": ""}]}
        r = ia.validar_investigacao(bruto, paginas, ["Filtros"])
        self.assertEqual((len(r["evidencias"]), r["evidencias_descartadas"]), (1, 1))
        self.assertEqual([c["valor"] for c in r["contatos"]], ["compras@x.com.br", "(11) 2400-1000"])
        self.assertEqual(r["hipoteses_aplicacao"], ["Filtros"])
