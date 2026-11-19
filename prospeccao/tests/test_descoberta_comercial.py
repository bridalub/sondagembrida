"""
Testes SIMULADOS da descoberta por característica e localidade.

Não há rede, OpenAI nem Salesforce. Fontes, CNPJs e custos são fictícios.
Não comprovam tempo ou qualidade de uma pesquisa real.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from carteira.models import CanalEmpresa, Empresa
from carteira.services import cnpj as cnpj_srv
from prospeccao.conectores.base import ContextoExecucao, ExecucaoCancelada, ExecucaoPausada
from prospeccao.forms import PesquisaForm
from prospeccao.models import Candidato, Execucao, Fonte, FrenteComercial, Pesquisa, SelecaoPesquisa
from prospeccao.services import cruzamento as cz
from prospeccao.services import meta, orcamento, selecao
from prospeccao.services.regioes import expandir


def doc(base12):
    return base12 + cnpj_srv.calcular_dv(base12)


def mascara(c):
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"


CNPJ_OFICINA = doc("112223330001")
CNPJ_FILIAL = doc("112223330002")
CNPJ_SF = doc("445556660001")


class BuscaSimulada:
    """Provedor falso. custo medido zero: a reserva existe, nenhuma chamada externa."""

    codigo = "simulado"
    lote = False

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []
        self.ultimo = {"medido": True, "custo": 0, "modelo": "simulado", "entrada": 0, "saida": 0, "acoes": 0}

    def custo_maximo(self):
        return 0

    def buscar(self, consulta, ctx, contexto=None):
        self.chamadas.append(consulta)
        if len(self.chamadas) > 1:
            self.ja_havia_candidata = Candidato.objects.filter(nome="Oficina do Grande ABC").exists()
        return self.respostas.pop(0) if self.respostas else []


class SiteSemCnpj:
    def __init__(self):
        self.urls = []

    def analisar(self, url, ctx):
        self.urls.append(url)
        return type("Leitura", (), {"paginas": [(url, "oficina de frota própria")], "cnpjs": {}})()


class DescobertaSimuladaTests(TestCase):
    def setUp(self):
        self.frente = FrenteComercial.objects.create(
            nome="Oficinas e frota", descricao="Consumidores com oficina ou frota",
            termos_operacao="oficina\nfrota", sinais_operacionais="caminhão")
        self.p = Pesquisa.objects.create(
            nome="Simulado ABC", modo="PLANEJADA", uf="SP", cidades="Santo André",
            objetivo="Compradores de lubrificantes no ABC", quantidade_desejada=10,
            teto_custo_usd=Decimal("1"), max_buscas_descoberta=2, limite_resultados=20)
        self.p.frentes.add(self.frente)
        self.ex = Execucao.objects.create(pesquisa=self.p, status=Execucao.Status.EXECUTANDO, iniciada_em=timezone.now())
        self.ctx = ContextoExecucao(self.ex, prazo_segundos=30, logar=lambda *a, **k: None)
        self.est = type("Est", (), {})()
        self.est.parar = False
        self.est.externos = 0
        self.est.sf = set()
        self.est.leads = set()
        self.est.orcamento = orcamento.Orcamento(self.p, self.ex)
        self.cfg = {"termos": ["oficina mecânica", "transportadora frota"], "sinais": [], "atividades": [],
                    "cnaes": [], "frente": self.frente.nome}

    def _rodar(self, busca, site=None):
        return selecao.descoberta_web(
            self.ex, self.ctx, self.est, self.frente, self.cfg, None, None, set(), set(), set(), {},
            site, busca, self.est.orcamento)

    def test_candidata_sem_cnpj_aparece_e_site_sem_cnpj_nao_descarta(self):
        site = SiteSemCnpj()
        cnpj_fmt = mascara(CNPJ_OFICINA)
        busca = BuscaSimulada([
            [{"url": "https://oficina-abc.example/contato", "titulo": "Oficina do Grande ABC",
              "nome_citado": "Oficina do Grande ABC", "dominio": "oficina-abc.example",
              "municipio_citado": "Santo André", "telefones": ["1144445555"],
              "emails": ["compras@oficina-abc.example"], "trecho": "oficina mecânica de frota"}],
            [{"url": "https://filial-abc.example/", "titulo": "Filial Transporte ABC",
              "nome_citado": "Filial Transporte ABC", "dominio": "filial-abc.example",
              "municipio_citado": "Santo André", "telefones": ["1155556666"], "emails": [],
              "trecho": f"CNPJ {cnpj_fmt} transportadora"}],
        ])
        self._rodar(busca, site)
        cand = Candidato.objects.get(nome="Oficina do Grande ABC")
        self.assertEqual(cand.status, Candidato.Status.PENDENTE)
        self.assertEqual(cand.cnpj_candidato, "")
        self.assertEqual(cand.telefone, "1144445555")
        self.assertTrue(any("oficina-abc.example" in u for u in site.urls))
        self.assertTrue(busca.ja_havia_candidata)
        self.assertTrue((self.ex.progresso or {}).get("primeiro_resultado_em"))
        self.assertFalse(SelecaoPesquisa.objects.filter(pesquisa=self.p, cnpj="").exists())
        ligada = Candidato.objects.get(nome="Filial Transporte ABC")
        self.assertEqual(ligada.cnpj_candidato, CNPJ_OFICINA)
        self.assertEqual(ligada.telefone, "1155556666")
        self.assertEqual(SelecaoPesquisa.objects.filter(pesquisa=self.p, cnpj=CNPJ_OFICINA).count(), 1)

    def test_cnpj_posterior_preserva_historico_e_nao_funde_filial(self):
        url = "https://oficina-abc.example/contato"
        busca1 = BuscaSimulada([[{"url": url, "titulo": "Oficina do Grande ABC", "nome_citado": "Oficina do Grande ABC",
                                  "dominio": "oficina-abc.example", "municipio_citado": "Santo André",
                                  "telefones": ["1144445555"], "emails": ["compras@oficina-abc.example"],
                                  "trecho": "oficina de frota"}]])
        self.p.max_buscas_descoberta = 1
        self.p.save()
        self._rodar(busca1, SiteSemCnpj())
        antes = Candidato.objects.get(nome="Oficina do Grande ABC")
        self.cfg = {"consultas_descoberta": ["oficina mecânica"], "termos": [], "cnaes": []}
        busca2 = BuscaSimulada([[{"url": url, "titulo": "Oficina do Grande ABC", "nome_citado": "Oficina do Grande ABC",
                                  "dominio": "oficina-abc.example", "municipio_citado": "Santo André",
                                  "telefones": ["1144445555"], "emails": ["compras@oficina-abc.example"],
                                  "trecho": f"CNPJ {mascara(CNPJ_OFICINA)} matriz e {mascara(CNPJ_FILIAL)} filial"}]])
        self._rodar(busca2, SiteSemCnpj())
        antes.refresh_from_db()
        self.assertEqual(antes.cnpj_candidato, CNPJ_OFICINA)
        self.assertEqual(antes.telefone, "1144445555")
        self.assertEqual(antes.email, "compras@oficina-abc.example")
        self.assertEqual(Candidato.objects.filter(url_referencia=url).count(), 1)
        cnpjs = set(SelecaoPesquisa.objects.filter(pesquisa=self.p).values_list("cnpj", flat=True))
        self.assertEqual(cnpjs, {CNPJ_OFICINA, CNPJ_FILIAL})

    def test_conhecida_no_salesforce_nao_entra_na_meta(self):
        self.est.sf = {CNPJ_SF}
        self.p.max_buscas_descoberta = 1
        self.p.save()
        self.cfg = {"consultas_descoberta": ["empresa de onibus"], "termos": [], "cnaes": []}
        busca = BuscaSimulada([[{"url": "https://onibus-abc.example/", "titulo": "Viação Simulada",
                                 "nome_citado": "Viação Simulada", "dominio": "onibus-abc.example",
                                 "municipio_citado": "Santo André", "telefones": ["1133332222"], "emails": [],
                                 "trecho": f"CNPJ {mascara(CNPJ_SF)}"}]])
        self._rodar(busca)
        sel = SelecaoPesquisa.objects.get(pesquisa=self.p, cnpj=CNPJ_SF)
        self.assertEqual(sel.fila, SelecaoPesquisa.Fila.CONHECIDA)
        painel = meta.resumo(self.p, self.est)
        self.assertEqual(painel["conhecidas"], 1)
        self.assertEqual(painel["novos_validados"], 0)
        self.assertTrue(Candidato.objects.filter(nome="Viação Simulada").exists())

    def test_consulta_de_distribuidora_sai_quando_a_frente_e_consumo(self):
        self.p.cidades = "Santo André\nSão Bernardo do Campo"
        self.p.max_buscas_descoberta = 4
        self.p.save()
        cfg = {"consultas_descoberta": ["distribuidora de lubrificantes", "transportadora frota", "oficina mecânica"],
               "termos": ["coleta de resíduos"], "cnaes": []}
        consultas = selecao.consultas_descoberta(cfg, self.p, frente=self.frente)
        texto = " ".join(consultas).lower()
        self.assertNotIn("distribuidora de lubrificantes", texto)
        self.assertIn("transportadora", texto)
        self.assertTrue(any("Santo André" in c for c in consultas))
        self.assertTrue(any("São Bernardo" in c for c in consultas))
        revenda = FrenteComercial.objects.create(nome="Revenda de lubrificantes", descricao="frente de distribuição")
        self.assertTrue(selecao.consulta_permitida("distribuidora de lubrificantes", revenda))
        citada = {"consultas_descoberta": ["terraplenagem em São Bernardo do Campo e região do ABC"], "termos": [], "cnaes": []}
        pronta = selecao.consultas_descoberta(citada, self.p, frente=self.frente)
        self.assertTrue(pronta)
        self.assertIn("São Bernardo", pronta[0])
        self.assertNotIn("Santo André", pronta[0])

    def test_pausa_cancelamento_e_teto_vazio_bloqueiam_chamada_nova(self):
        busca = BuscaSimulada([[]])
        self.ex.pausa_solicitada = True
        self.ex.save(update_fields=["pausa_solicitada"])
        with self.assertRaises(ExecucaoPausada):
            orcamento.buscar(busca, "oficina Santo André", finalidade="DESCOBERTA", orc=self.est.orcamento, ctx=self.ctx)
        self.assertEqual(busca.chamadas, [])
        self.ex.pausa_solicitada = False
        self.ex.cancelamento_solicitado = True
        self.ex.save(update_fields=["pausa_solicitada", "cancelamento_solicitado"])
        self.ctx._ultima_verificacao = 0
        with self.assertRaises(ExecucaoCancelada):
            orcamento.buscar(busca, "oficina Santo André", finalidade="DESCOBERTA", orc=self.est.orcamento, ctx=self.ctx)
        self.assertEqual(busca.chamadas, [])
        self.p.teto_custo_usd = None
        self.p.save()
        orc = orcamento.Orcamento(self.p, self.ex)
        self.ex.cancelamento_solicitado = False
        self.ex.save(update_fields=["cancelamento_solicitado"])
        self.ctx._ultima_verificacao = 0
        self.assertEqual(orcamento.buscar(busca, "oficina", finalidade="DESCOBERTA", orc=orc, ctx=self.ctx), [])
        self.assertTrue(any("teto" in m for m in orc.motivos_bloqueio))
        self.assertEqual(busca.chamadas, [])


class MetaSimuladaTests(TestCase):
    """Volume e classificação simulados. Não medem uma pesquisa real."""

    def setUp(self):
        self.frente = FrenteComercial.objects.create(nome="Frota simulada", descricao="teste")
        self.antiga = Empresa.objects.create(
            nome="Já pesquisada LTDA", cnpj=doc("220000000001"), cidade="Santo André", uf="SP",
            situacao_cadastral="ATIVA", situacao_cadastral_referencia=date.today(), telefone="11900000000",
            origem=Empresa.Origem.DESCOBERTA)
        Empresa.objects.filter(pk=self.antiga.pk).update(criado_em=timezone.now() - timedelta(days=2))
        self.p = Pesquisa.objects.create(
            nome="Meta simulada", modo="PLANEJADA", uf="SP", cidades="Santo André", quantidade_desejada=100)
        self.p.frentes.add(self.frente)

    def _empresa(self, n, *, sf=False, ativa=True, contato=True):
        base = f"{100000000000 + n:012d}"[:12]
        e = Empresa.objects.create(
            nome=f"Empresa simulada {n}", cnpj=doc(base), cidade="Santo André", uf="SP",
            situacao_cadastral="ATIVA" if ativa else "",
            situacao_cadastral_referencia=date.today() if ativa else None,
            telefone=f"11{n:09d}"[:11] if contato else "",
            salesforce_id=f"001SIM{n:010d}" if sf else None,
            origem=Empresa.Origem.SALESFORCE if sf else Empresa.Origem.DESCOBERTA)
        return e

    def _sel(self, empresa, fila=SelecaoPesquisa.Fila.SELECIONADA):
        return SelecaoPesquisa.objects.create(
            pesquisa=self.p, frente=self.frente, cnpj=empresa.cnpj, empresa=empresa, pontuacao=10,
            fila=fila, aderencia=SelecaoPesquisa.Aderencia.SUSTENTADA,
            aderencia_motivos=["operação de frota citada na fonte simulada"],
            motivos_selecao=["atividade compatível"])

    def test_volume_conhecidas_repetidas_e_falha_cadastral(self):
        for n in range(100):
            self._sel(self._empresa(n))
        for n in range(100, 110):
            self._sel(self._empresa(n, sf=True), fila=SelecaoPesquisa.Fila.CONHECIDA)
        self._sel(self.antiga, fila=SelecaoPesquisa.Fila.JA_PESQUISADA)
        sem_situacao = self._empresa(200, ativa=False, contato=True)
        self._sel(sem_situacao, fila=SelecaoPesquisa.Fila.REVISAR_IDENTIDADE)
        est = type("Est", (), {"sync": (True, timezone.now(), 24), "conferencia": None})()
        painel = meta.resumo(self.p, est)
        # encontrada antes, sem contato efetivo: continua disponível (reencontrada, não inédita)
        self.assertEqual(painel["novos_validados"], 101)
        self.assertEqual(painel["conhecidas"], 10)
        self.assertEqual((painel["reencontradas"], painel["ineditas"]), (1, 100))
        self.assertEqual(meta.resumo(self.p, est)["novos_validados"], 101)
        velho = type("Est", (), {"sync": (False, timezone.now() - timedelta(days=40), 24), "conferencia": None})()
        parado = meta.resumo(self.p, velho)
        self.assertEqual(parado["novos_validados"], 101)
        self.assertIn("Usando a carteira sincronizada em", parado["aviso_carteira"])
        sem_base = type("Est", (), {"sync": (False, None, 24), "conferencia": None})()
        vazio = meta.resumo(self.p, sem_base)
        self.assertEqual(vazio["novos_validados"], 0)
        self.assertGreaterEqual(vazio["provisorias"], 100)
        fila, motivos = selecao.calcular_fila(
            SelecaoPesquisa.objects.get(cnpj=self.antiga.cnpj), self.antiga, self.p)
        self.assertNotEqual(fila, SelecaoPesquisa.Fila.JA_PESQUISADA)

    def test_telefone_ou_email_conta_e_contador_nao_anula_o_telefone(self):
        from carteira.models import CanalEmpresa
        so_telefone = self._empresa(30, contato=True)
        so_telefone.email = ""
        so_telefone.save()
        self._sel(so_telefone)
        inativa = self._empresa(31, ativa=False, contato=True)
        inativa.situacao_cadastral = "BAIXADA"
        inativa.situacao_cadastral_referencia = date.today()
        inativa.save()
        self._sel(inativa)
        mista = self._empresa(32, contato=False)
        CanalEmpresa.objects.create(
            empresa=mista, tipo=CanalEmpresa.Tipo.EMAIL, valor="contato@escritorio.example",
            aplica_a=CanalEmpresa.AplicaA.CONTADOR, confianca_associacao="ALTA")
        CanalEmpresa.objects.create(
            empresa=mista, tipo=CanalEmpresa.Tipo.TELEFONE, valor="1140000000",
            aplica_a=CanalEmpresa.AplicaA.ESTABELECIMENTO, confianca_associacao="MEDIA")
        self._sel(mista)
        est = type("Est", (), {"sync": (False, timezone.now() - timedelta(days=10), 24), "conferencia": None})()
        nomes = {item["nome"] for item in meta.classificar(self.p, est)["novos"]}
        self.assertIn(so_telefone.nome, nomes)
        self.assertIn(mista.nome, nomes)
        self.assertNotIn(inativa.nome, nomes)
        painel = meta.resumo(self.p, est)
        self.assertGreaterEqual(painel["inativas"], 1)

    def test_candidata_sem_cnpj_entra_com_pendencia_e_ambiguo_nao(self):
        fonte = Fonte.objects.create(nome="Busca simulada", codigo="busca_sim_meta", tipo=Fonte.Tipo.IDENTIFICACAO, habilitada=True)
        Candidato.objects.create(
            fonte=fonte, id_externo="sem-cnpj-1", nome="Oficina Sustentada ABC", cidade="Santo André", uf="SP",
            telefone="11911112222", email="compras@oficina-sustentada.example",
            dominio="oficina-sustentada.example", site="https://oficina-sustentada.example/",
            status=Candidato.Status.PENDENTE)
        Candidato.objects.filter(id_externo="sem-cnpj-1").first().pesquisas.add(self.p)
        ambiguo = Candidato.objects.create(
            fonte=fonte, id_externo="ambiguo-1", nome="Oficina Talvez ABC", cidade="Santo André", uf="SP",
            telefone="11933334444", status=Candidato.Status.PENDENTE, sugestoes=[{"nome": "Outra"}])
        ambiguo.pesquisas.add(self.p)
        est = type("Est", (), {"sync": (True, timezone.now(), 24), "conferencia": None})()
        quadro = meta.classificar(self.p, est)
        nomes = {item["nome"] for item in quadro["novos"]}
        self.assertIn("Oficina Sustentada ABC", nomes)
        self.assertNotIn("Oficina Talvez ABC", nomes)
        lead = next(item for item in quadro["novos"] if item["nome"] == "Oficina Sustentada ABC")
        self.assertEqual(lead["cnpj"], "")
        self.assertIn("CNPJ não localizado", lead["pendencias"])
        self.assertGreaterEqual(quadro["correspondencias"], 1)
        metal = self._empresa(41)
        CanalEmpresa.objects.create(empresa=metal, tipo=CanalEmpresa.Tipo.TELEFONE, valor="1198274881",
                                    valor_normalizado="1198274881", fonte="brasilapi_cnpj", origem_canal="CADASTRAL")
        CanalEmpresa.objects.create(empresa=metal, tipo=CanalEmpresa.Tipo.TELEFONE, valor="11998274881",
                                    valor_normalizado="11998274881", fonte="site_publico", origem_canal="PUBLICACAO",
                                    whatsapp_explicito=True)
        self._sel(metal)
        item = next(i for i in meta.classificar(self.p, est)["novos"] if i["nome"] == metal.nome)
        valores = [c["valor"] for c in item["contatos"] if c["tipo"] == "TELEFONE"]
        self.assertEqual(valores, ["1198274881", "11998274881"])
        self.assertTrue(item["divergencia"])

    def test_falha_do_salesforce_preserva_a_base_e_reconcilia_conhecido(self):
        quando = timezone.now() - timedelta(days=9)
        with mock.patch("prospeccao.services.cruzamento.cruzamento_atual", return_value=(False, quando, 24)), \
                mock.patch("carteira.services.salesforce.conectar", side_effect=RuntimeError("login")):
            ok, aviso = cz.tentar_atualizar_carteira(permitir_rede=True)
        self.assertFalse(ok)
        self.assertIn("Usando a carteira sincronizada em", aviso)
        conhecida = self._empresa(40, sf=True)
        self._sel(conhecida, fila=SelecaoPesquisa.Fila.SELECIONADA)
        self.assertEqual(cz.reconciliar_selecoes(), 1)
        self.assertEqual(SelecaoPesquisa.objects.get(cnpj=conhecida.cnpj).fila, SelecaoPesquisa.Fila.CONHECIDA)
        with mock.patch("carteira.services.salesforce.conectar", return_value=object()), \
                mock.patch("carteira.services.salesforce.buscar_accounts", return_value=([], "q")):
            ok_vazio, aviso_vazio = cz.tentar_atualizar_carteira(permitir_rede=True)
        self.assertFalse(ok_vazio)
        self.assertIn("preservada", aviso_vazio)
        self.assertTrue(Empresa.objects.filter(pk=conhecida.pk, salesforce_id=conhecida.salesforce_id).exists())

    def test_matriz_e_filial_nao_se_fundem_e_gravacao_e_idempotente(self):
        matriz = self._empresa(1)
        filial = Empresa.objects.create(
            nome="Filial distinta", cnpj=doc(matriz.cnpj[:8] + "0002"), cidade="São Bernardo do Campo", uf="SP",
            situacao_cadastral="ATIVA", situacao_cadastral_referencia=date.today(), telefone="1188887777",
            origem=Empresa.Origem.DESCOBERTA)
        self._sel(matriz)
        self._sel(filial)
        self.assertEqual(Empresa.objects.filter(cnpj__startswith=matriz.cnpj[:8]).count(), 2)
        self.assertEqual(SelecaoPesquisa.objects.filter(pesquisa=self.p).count(), 2)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._sel(matriz)
        self.assertEqual(SelecaoPesquisa.objects.filter(pesquisa=self.p, cnpj=matriz.cnpj).count(), 1)

    def test_abc_paulista_vira_municipios_explicitos(self):
        nomes, avisos = expandir(["ABC Paulista", "Guarulhos"])
        self.assertEqual(len(nomes), 8)
        self.assertIn("Santo André", nomes)
        self.assertIn("Rio Grande da Serra", nomes)
        self.assertTrue(avisos)
        fonte = Fonte.objects.create(nome="Mapa simulado", codigo="osm_teste_abc", tipo=Fonte.Tipo.DESCOBERTA, habilitada=True)
        form = PesquisaForm(data={
            "nome": "Piloto ABC", "modo": "PLANEJADA", "uf": "SP", "cidades": "ABC Paulista",
            "objetivo": "Encontrar potenciais compradores de lubrificantes, graxas e filtros no ABC Paulista.",
            "frentes": [self.frente.pk], "fontes": [fonte.pk], "quantidade_desejada": "10",
            "situacoes_ui": "1", "situacoes_sel": ["ATIVA"], "idade_desconhecida": "SINALIZAR", "ativa": "on"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn("Santo André", form.cleaned_data["cidades"])
        self.assertNotIn("ABC Paulista", form.cleaned_data["cidades"])
        form2 = PesquisaForm(data={
            "nome": "Demais", "modo": "SIMPLES", "uf": "SP", "cidades": "Guarulhos", "fontes": [fonte.pk],
            "quantidade_desejada": "101", "situacoes_ui": "1", "situacoes_sel": ["ATIVA"],
            "idade_desconhecida": "SINALIZAR", "termos_busca": "oficina"})
        self.assertFalse(form2.is_valid())
        self.assertIn("quantidade_desejada", form2.errors)
