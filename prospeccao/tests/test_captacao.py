"""Painel de captação: contas por período, por pessoa, orçamento e venda com valor."""
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from carteira.forms import AbordagemForm
from carteira.models import Abordagem, Empresa
from prospeccao.models import Oportunidade
from prospeccao.services import bi_captacao as bi


def quando(dia, hora=10):
    return timezone.make_aware(datetime(2026, 11, dia, hora, 0))


class CaptacaoTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.carla = U.objects.create_user("carla", password="x", first_name="Carla", is_staff=True)
        self.diego = U.objects.create_user("diego", password="x", first_name="Diego")
        self.emp = []
        for i in range(4):
            e = Empresa.objects.create(nome=f"Lead {i}", cidade="Mauá", uf="SP", origem=Empresa.Origem.DESCOBERTA)
            Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
            self.emp.append(e)
        sf = Empresa.objects.create(nome="Conta SF", salesforce_id="001X", origem=Empresa.Origem.SALESFORCE)
        Oportunidade.objects.create(empresa=sf, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
        # Carla: 1 sem resposta + depois orçamento na mesma empresa; Diego: venda; contato fora do mês; conta SF fora
        Abordagem.objects.create(empresa=self.emp[0], usuario=self.carla, canal="TELEFONE", resultado="SEM_RESPOSTA", data_hora=quando(3))
        Abordagem.objects.create(empresa=self.emp[0], usuario=self.carla, canal="TELEFONE", resultado="CONVERSA_ORCAMENTO",
                                 valor_orcamento=Decimal("1500"), data_hora=quando(5))
        Abordagem.objects.create(empresa=self.emp[1], usuario=self.diego, canal="WHATSAPP", resultado="CONVERSA_VENDA",
                                 valor_orcamento=Decimal("2000"), valor_venda=Decimal("1800"), data_hora=quando(5, 15))
        Abordagem.objects.create(empresa=self.emp[2], usuario=self.diego, canal="TELEFONE", resultado="RECADO", data_hora=quando(6))
        Abordagem.objects.create(empresa=self.emp[3], usuario=self.carla, canal="TELEFONE", resultado="SEM_RESPOSTA",
                                 data_hora=timezone.make_aware(datetime(2026, 10, 30, 9)))
        Abordagem.objects.create(empresa=sf, usuario=self.carla, canal="TELEFONE", resultado="CONVERSA_INTERESSE", data_hora=quando(4))
        self.ini, self.fim, _ = bi.periodo_do_mes("2026-11")

    def test_kpis_do_mes(self):
        k = bi.calcular(self.ini, self.fim)["kpis"]
        self.assertEqual((k["abordados"], k["conversaram"], k["sem_resposta"]), (3, 2, 1))
        self.assertEqual((k["orcamentos"], k["valor_orcamentos"]), (2, Decimal("3500")))
        self.assertEqual((k["vendas"], k["valor_vendas"]), (1, Decimal("1800")))
        self.assertEqual(k["contatos"], 4)

    def test_por_dia_conta_empresa_uma_vez_e_separa_conversa(self):
        dias = {d["dia"]: d for d in bi.calcular(self.ini, self.fim)["dias"]}
        self.assertEqual(len(dias), 30)
        self.assertEqual((dias[5]["conversa"], dias[5]["tentativa"]), (2, 0))
        self.assertEqual((dias[3]["conversa"], dias[3]["tentativa"]), (0, 1))

    def test_rankings_e_filtro_por_vendedor(self):
        r = bi.calcular(self.ini, self.fim)["rankings"]
        self.assertEqual(r["vendas"][0]["nome"], "Diego")
        self.assertEqual(r["vendas"][0]["valor_venda"], 1800.0)
        # 1 orçamento cada: desempata pelo valor orçado (Diego R$ 2.000 x Carla R$ 1.500)
        self.assertEqual([x["nome"] for x in r["orcamentos"]], ["Diego", "Carla"])
        so_carla = bi.calcular(self.ini, self.fim, usuario="carla")["kpis"]
        self.assertEqual((so_carla["abordados"], so_carla["vendas"]), (1, 0))

    def test_pizza_e_funil(self):
        d = bi.calcular(self.ini, self.fim)
        self.assertEqual(d["pizza"], {"procurados": 4, "em_aberto": 0})
        # encontradas, procuradas, conversaram, orçamento, CNPJ confirmado, compraram
        self.assertEqual([f["n"] for f in d["funil"]], [4, 4, 2, 2, 0, 1])

    def test_nota_fiscal_so_conta_para_cnpj_confirmado_e_depois_do_contato(self):
        from carteira.models import NotaFiscal
        from carteira.services.cnpj import calcular_dv
        c = "112223330001"
        c += calcular_dv(c)
        Empresa.objects.filter(pk=self.emp[2].pk).update(cnpj=c)
        Abordagem.objects.create(empresa=self.emp[2], usuario=self.diego, canal="TELEFONE", resultado="CONVERSA_INTERESSE",
                                 cnpj_confirmado=c, data_hora=quando(8))
        NotaFiscal.objects.create(id_origem="NF1", tipo="VENDA", empresa=self.emp[2], data_emissao=date(2026, 11, 12),
                                  valor_total=Decimal("700"))
        NotaFiscal.objects.create(id_origem="NF0", tipo="VENDA", empresa=self.emp[2], data_emissao=date(2026, 11, 2),
                                  valor_total=Decimal("999"))  # antes do 1º contato (dia 6): não é captação
        d = bi.calcular(self.ini, self.fim)
        self.assertEqual((d["kpis"]["valor_nf"], d["kpis"]["notas_nf"]), (Decimal("700"), 1))
        self.assertEqual(d["rankings"]["vendas"][0]["nome"], "Diego")
        self.assertEqual(d["rankings"]["vendas"][0]["valor_nf"], 700.0)
        self.assertEqual(bi.calcular(self.ini, self.fim, usuario="carla")["kpis"]["valor_nf"], Decimal("0"))
        # CNPJ só encontrado (sem confirmação) não conta
        Empresa.objects.filter(pk=self.emp[3].pk).update(cnpj="11444777000161")
        NotaFiscal.objects.create(id_origem="NF2", tipo="VENDA", empresa=self.emp[3], data_emissao=date(2026, 11, 20),
                                  valor_total=Decimal("500"))
        self.assertEqual(bi.calcular(self.ini, self.fim)["kpis"]["valor_nf"], Decimal("700"))

    def test_simulacao_nao_grava_nada(self):
        from carteira.models import NotaFiscal
        antes = (Abordagem.objects.count(), NotaFiscal.objects.count(), get_user_model().objects.count())
        self.client.force_login(self.carla)
        r = self.client.get(reverse("prospeccao:captacao"), {"mes": "2026-11", "simulacao": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Simulação.")
        self.assertEqual(antes, (Abordagem.objects.count(), NotaFiscal.objects.count(), get_user_model().objects.count()))


    def test_detalhe_de_cada_cartao_bate_com_o_numero(self):
        k = bi.calcular(self.ini, self.fim)["kpis"]
        for tipo, n in (("abordados", k["abordados"]), ("conversaram", k["conversaram"]),
                        ("sem_resposta", k["sem_resposta"]), ("orcamentos", k["orcamentos"]), ("vendas", k["vendas"]),
                        ("cnpjs", k["cnpjs_confirmados"]), ("vendas_nf", k["notas_nf"])):
            self.assertEqual(len(bi.detalhe(tipo, self.ini, self.fim)["linhas"]), n, tipo)
        orc = bi.detalhe("orcamentos", self.ini, self.fim)
        self.assertEqual(orc["total"], k["valor_orcamentos"])
        self.assertEqual([x["quem"] for x in orc["linhas"]], ["Diego", "Carla"])  # mais novo primeiro (5/11 15h, 10h)

    def test_valor_da_venda_atribuido_ao_orcamento(self):
        """Gestor: toda venda tem orçamento; nem todo orçamento vira compra."""
        from carteira.models import NotaFiscal
        from carteira.services.cnpj import calcular_dv
        # Lead 1 (Diego): orçamento de R$ 2.000 no dia 5 + CNPJ confirmado; o cliente compra dias 9 e 12 (2 notas no 12)
        c = "112223330001"
        c += calcular_dv(c)
        Empresa.objects.filter(pk=self.emp[1].pk).update(cnpj=c)
        Abordagem.objects.filter(empresa=self.emp[1]).update(cnpj_confirmado=c)
        for i, (dia, valor) in enumerate(((9, "700"), (12, "300"), (12, "200"))):
            NotaFiscal.objects.create(id_origem=f"NF-O{i}", tipo="VENDA", empresa=self.emp[1],
                                      data_emissao=date(2026, 11, dia), valor_total=Decimal(valor))
        conv = bi.calcular(self.ini, self.fim)["kpis"]["conv_orc"]
        # 2 leads orçados (Carla emp0, Diego emp1); 1 virou venda
        self.assertEqual((conv["leads"], conv["leads_convertidos"], conv["taxa"]), (2, 1, 50))
        self.assertEqual(conv["valor_vendido"], Decimal("1200"))
        self.assertEqual(conv["valor_orcado_convertido"], Decimal("2000"))
        det = bi.detalhe("orcamentos", self.ini, self.fim)
        por_cliente = {x["cliente"]: (x["valor"], x["vendido"], x["vendas_dias"]) for x in det["linhas"]}
        self.assertEqual(por_cliente["Lead 1"], (Decimal("2000"), Decimal("1200"), 2))  # 2 dias de compra
        self.assertEqual(por_cliente["Lead 0"], (Decimal("1500"), None, 0))  # orçamento que não virou compra
        so = bi.detalhe("orcamentos_convertidos", self.ini, self.fim)
        self.assertEqual([x["cliente"] for x in so["linhas"]], ["Lead 1"])
        self.client.force_login(self.carla)
        painel = self.client.get(reverse("prospeccao:captacao"), {"mes": "2026-11"})
        self.assertContains(painel, "Orçamentos que viraram venda")
        self.assertContains(painel, "50%")

    def test_venda_sem_orcamento_aparece_para_o_gestor(self):
        from carteira.models import NotaFiscal
        c = "11444777000161"
        Empresa.objects.filter(pk=self.emp[2].pk).update(cnpj=c)
        Abordagem.objects.create(empresa=self.emp[2], usuario=self.diego, canal="TELEFONE",
                                 resultado="CONVERSA_INTERESSE", cnpj_confirmado=c, data_hora=quando(7))
        NotaFiscal.objects.create(id_origem="NF-SEM", tipo="VENDA", empresa=self.emp[2],
                                  data_emissao=date(2026, 11, 15), valor_total=Decimal("400"))
        conv = bi.calcular(self.ini, self.fim)["kpis"]["conv_orc"]
        self.assertEqual((conv["sem_orcamento_clientes"], conv["sem_orcamento_valor"]), (1, Decimal("400")))

    def test_detalhe_notas_fiscais_com_cliente_valor_vendedor_data_e_filtros(self):
        from carteira.models import NotaFiscal
        from carteira.services.cnpj import calcular_dv
        c = "112223330001"
        c += calcular_dv(c)
        Empresa.objects.filter(pk=self.emp[2].pk).update(cnpj=c)
        Abordagem.objects.create(empresa=self.emp[2], usuario=self.diego, canal="TELEFONE", resultado="CONVERSA_INTERESSE",
                                 cnpj_confirmado=c, data_hora=quando(8))
        for i, (dia, valor) in enumerate(((12, "700"), (20, "300"))):
            NotaFiscal.objects.create(id_origem=f"NFX{i}", numero=f"00{i}", tipo="VENDA", empresa=self.emp[2],
                                      data_emissao=date(2026, 11, dia), valor_total=Decimal(valor))
        d = bi.detalhe("vendas_nf", self.ini, self.fim)
        self.assertEqual([(x["data"].day, x["valor"], x["quem"], x["cliente"]) for x in d["linhas"]],
                         [(20, Decimal("300"), "Diego", "Lead 2"), (12, Decimal("700"), "Diego", "Lead 2")])
        self.assertEqual(d["total"], Decimal("1000"))
        self.assertEqual(len(bi.detalhe("vendas_nf", date(2026, 11, 1), date(2026, 11, 15))["linhas"]), 1)  # período
        self.assertEqual(bi.detalhe("vendas_nf", self.ini, self.fim, usuario="carla")["linhas"], [])  # quem vendeu
        self.assertEqual(len(bi.detalhe("vendas_nf", self.ini, self.fim, q="lead 2")["linhas"]), 2)  # cliente
        self.assertEqual(len(bi.detalhe("vendas_nf", self.ini, self.fim, q=c[:8])["linhas"]), 2)  # CNPJ
        self.assertEqual(bi.detalhe("vendas_nf", self.ini, self.fim, q="outra")["linhas"], [])
        # tela: cartão com link, lista renderiza com filtros
        self.client.force_login(self.carla)
        painel = self.client.get(reverse("prospeccao:captacao"), {"mes": "2026-11"})
        self.assertContains(painel, reverse("prospeccao:captacao_detalhe", args=["vendas_nf"]) + "?mes=2026-11")
        r = self.client.get(reverse("prospeccao:captacao_detalhe", args=["vendas_nf"]),
                            {"mes": "2026-11", "vendedor": "diego", "de": "2026-11-15", "ate": "2026-11-30"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "R$ 300,00")
        self.assertNotContains(r, "R$ 700,00")
        self.assertEqual(self.client.get(reverse("prospeccao:captacao_detalhe", args=["nada"])).status_code, 404)

    def test_pagina_renderiza(self):
        self.client.force_login(self.carla)
        r = self.client.get(reverse("prospeccao:captacao"), {"mes": "2026-11"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Painel de captação")
        self.assertContains(r, "Novembro de 2026")

    def test_venda_exige_cnpj_valor_opcional(self):
        from carteira.services.cnpj import calcular_dv
        c = "334445550001"
        c += calcular_dv(c)
        base = {"data_hora": "2026-11-10T10:00", "canal": "TELEFONE", "resultado": "CONVERSA_VENDA", "cnpj_confirmado": c}
        self.assertTrue(AbordagemForm(data=base, empresa=self.emp[2]).is_valid())  # valor opcional (vem da NF)
        f = AbordagemForm(data={**base, "valor_venda": "950,50"}, empresa=self.emp[2])
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data["valor_venda"], Decimal("950.50"))

class CnpjConfirmadoTests(TestCase):
    def setUp(self):
        from carteira.services.cnpj import calcular_dv
        self.u = get_user_model().objects.create_user("gil", password="x", first_name="Gil")
        self.e = Empresa.objects.create(nome="Pro Ares", cidade="Santo André", uf="SP", origem=Empresa.Origem.DESCOBERTA)
        b = "172252420001"
        self.cnpj = b + calcular_dv(b)

    def _registrar(self, **dados):
        from carteira.services.abordagens import registrar_abordagem
        f = AbordagemForm(data={"data_hora": "2026-11-10T10:00", "canal": "TELEFONE", "resultado": "CONVERSA_INTERESSE",
                                **dados}, empresa=self.e)
        self.assertTrue(f.is_valid(), f.errors)
        a = f.save(commit=False)
        a.empresa, a.usuario = self.e, self.u
        return registrar_abordagem(a)

    def test_cnpj_invalido_e_recusado(self):
        f = AbordagemForm(data={"data_hora": "2026-11-10T10:00", "canal": "TELEFONE", "resultado": "CONVERSA_INTERESSE",
                                "cnpj_confirmado": "17.225.242/0001-00"}, empresa=self.e)
        self.assertFalse(f.is_valid())
        self.assertIn("cnpj_confirmado", f.errors)

    def test_confirmado_vira_cnpj_do_lead(self):
        mascara = f"{self.cnpj[:2]}.{self.cnpj[2:5]}.{self.cnpj[5:8]}/{self.cnpj[8:12]}-{self.cnpj[12:]}"
        a = self._registrar(cnpj_confirmado=mascara)  # como vem preenchido na tela
        self.e.refresh_from_db()
        self.assertEqual(self.e.cnpj, self.cnpj)
        self.assertEqual(a.cnpj_confirmado, self.cnpj)
        self.assertTrue(self.e.dados_coletados.filter(campo="cnpj_confirmado_atendimento", valor=self.cnpj).exists())

    def test_corrige_o_encontrado_e_guarda_o_anterior(self):
        from carteira.services.cnpj import calcular_dv
        antigo = "998877660001"
        antigo += calcular_dv(antigo)
        Empresa.objects.filter(pk=self.e.pk).update(cnpj=antigo)
        self.e.refresh_from_db()
        a = self._registrar(cnpj_confirmado=self.cnpj)
        self.e.refresh_from_db()
        self.assertEqual(self.e.cnpj, self.cnpj)
        self.assertTrue(self.e.dados_coletados.filter(campo="cnpj_substituido_atendimento", valor=antigo).exists())
        self.assertIn("corrigido", a.aviso_cnpj)

    def test_cnpj_de_conta_salesforce_nao_une(self):
        Empresa.objects.create(nome="Conta SF", cnpj=self.cnpj, salesforce_id="001SF", origem=Empresa.Origem.SALESFORCE)
        a = self._registrar(cnpj_confirmado=self.cnpj)
        self.e.refresh_from_db()
        self.assertFalse(self.e.cnpj)
        self.assertIn("Salesforce", a.aviso_cnpj)


class CnpjAssociadoTests(TestCase):
    """O CNPJ encontrado continua associado quando o atendimento confirma outro; pesquisas futuras reconhecem."""

    def setUp(self):
        from carteira.services.cnpj import calcular_dv
        self.u = get_user_model().objects.create_user("hana", password="x", first_name="Hana")
        b1, b2 = "556667770001", "556667770002"
        self.encontrado, self.confirmado = b1 + calcular_dv(b1), b2 + calcular_dv(b2)
        self.e = Empresa.objects.create(nome="Translima", cnpj=self.encontrado, cidade="Ribeirão Pires", uf="SP",
                                        origem=Empresa.Origem.DESCOBERTA)

    def _confirmar(self, cnpj):
        from carteira.services.abordagens import registrar_abordagem
        f = AbordagemForm(data={"data_hora": "2026-11-10T10:00", "canal": "TELEFONE",
                                "resultado": "CONVERSA_INTERESSE", "cnpj_confirmado": cnpj}, empresa=self.e)
        self.assertTrue(f.is_valid(), f.errors)
        a = f.save(commit=False)
        a.empresa, a.usuario = self.e, self.u
        return registrar_abordagem(a)

    def test_corrigir_mantem_os_dois_associados(self):
        a = self._confirmar(self.confirmado)
        self.e.refresh_from_db()
        self.assertEqual(self.e.cnpj, self.confirmado)
        origens = dict(self.e.cnpjs_associados.values_list("cnpj", "origem"))
        self.assertEqual(origens, {self.encontrado: "PESQUISA", self.confirmado: "ATENDIMENTO"})
        self.assertIn("continua associado", a.aviso_cnpj)

    def test_pesquisa_futura_reconhece_pelo_cnpj_antigo(self):
        from carteira.models import empresa_por_cnpj
        from prospeccao.services.descoberta_google import adotar_cnpj, ja_prospectada
        self._confirmar(self.confirmado)
        self.assertEqual(empresa_por_cnpj(self.encontrado), self.e)
        self.assertEqual(ja_prospectada([], [], cnpj=self.encontrado), self.e)
        outra = Empresa.objects.create(nome="Translima Transportadora (Google)", cidade="Ribeirão Pires", uf="SP",
                                       origem=Empresa.Origem.DESCOBERTA)
        dono, _ = adotar_cnpj(outra, None, self.encontrado, None, "teste")
        self.assertEqual(dono, self.e)  # não cria duplicado: aponta a empresa que já tem o CNPJ associado
        outra.refresh_from_db()
        self.assertFalse(outra.cnpj)

    def test_confirmar_o_mesmo_marca_como_confirmado(self):
        self._confirmar(self.encontrado)
        self.assertEqual(list(self.e.cnpjs_associados.values_list("cnpj", "origem")), [(self.encontrado, "ATENDIMENTO")])


class ContaSalesforceRelacionadaTests(TestCase):
    """CNPJ citado/associado que pertence a uma conta do Salesforce mantém a empresa como conhecida."""

    def test_fila_continua_conhecida_apos_recalculo(self):
        from carteira.models import CnpjAssociado
        from carteira.services.cnpj import calcular_dv
        from prospeccao.models import FrenteComercial, Pesquisa, SelecaoPesquisa
        from prospeccao.services.selecao import calcular_fila, conta_salesforce_relacionada
        b = "445556660001"
        c = b + calcular_dv(b)
        conta = Empresa.objects.create(nome="CONTA ANTIGA LTDA", cnpj=c, salesforce_id="001ANT",
                                       origem=Empresa.Origem.SALESFORCE)
        lead = Empresa.objects.create(nome="Conta Antiga (Google)", cidade="Mauá", uf="SP",
                                      origem=Empresa.Origem.DESCOBERTA)
        CnpjAssociado.objects.create(empresa=lead, cnpj=c, origem="PESQUISA")
        p = Pesquisa.objects.create(nome="P", cidades="Mauá", uf="SP")
        f = FrenteComercial.objects.create(nome="Frota", descricao="x")
        sel = SelecaoPesquisa.objects.create(pesquisa=p, frente=f, cnpj="SEM1", empresa=lead,
                                             fila=SelecaoPesquisa.Fila.BUSCAR_CONTATO)
        self.assertEqual(conta_salesforce_relacionada(lead), conta)
        fila, motivos = calcular_fila(sel, lead, p)
        self.assertEqual(fila, SelecaoPesquisa.Fila.CONHECIDA)
        self.assertIn("001ANT", motivos[0])


class CandidatoForaDaBaseNaoBloqueiaTests(TestCase):
    """Um CNPJ citado que não está na base local não impede testar os outros candidatos."""

    def test_segundo_candidato_resolve(self):
        from types import SimpleNamespace
        from unittest import mock
        from carteira.services.cnpj import calcular_dv
        from prospeccao.models import ConsultaWeb
        from prospeccao.services.identificacao_cnpj import completar_identificacao
        from prospeccao.tests.test_fluxo_identificacao import dados_de
        b1, b2 = "449811940001", "031407150001"
        fora, certo = b1 + calcular_dv(b1), b2 + calcular_dv(b2)
        e = Empresa.objects.create(nome="Terraplenagem Alzira Franco", cidade="Santo André", uf="SP",
                                   origem=Empresa.Origem.DESCOBERTA)
        ConsultaWeb.objects.create(empresa=e, finalidade="DESCOBERTA", provedor="google_serpapi", consulta="x",
                                   status="OK", resultados=[{"titulo": "Alzira", "trecho": f"CNPJ {fora} e CNPJ {certo}"}])
        cad = dados_de(certo, "TERRAPLENAGEM ALZIRA FRANCO LTDA", "TERRAPLENAGEM ALZIRA FRANCO", "Santo André")
        carga = SimpleNamespace(competencia="2026-11")
        with mock.patch("prospeccao.conectores.receita.consultar_local",
                        side_effect=lambda c: (cad, carga) if c == certo else (None, carga)), \
                mock.patch("prospeccao.services.identificacao_cnpj.identificar_pela_base", return_value=None):
            info = completar_identificacao(e, None, max_buscas=0, permitir_consulta_complementar=False)
        e.refresh_from_db()
        self.assertEqual(info["acao"], "cnpj_vinculado")
        self.assertEqual(e.cnpj, certo)
