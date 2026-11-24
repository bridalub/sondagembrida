"""Salvar pessoa/contatos sem registrar o atendimento, só ADMIN libera o lead e coordenador/vendedor pela região."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from carteira.models import Abordagem, CanalEmpresa, Contato, Empresa
from prospeccao.models import Oportunidade
from prospeccao.services import regiao


class DadosETravaTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.adm = U.objects.create_user("chefe", password="x")
        self.adm.groups.add(Group.objects.get_or_create(name="ADM")[0])
        self.ana = U.objects.create_user("ana", first_name="Ana", password="x")
        self.ana.groups.add(Group.objects.get_or_create(name="Inside Sales")[0])
        self.e = Empresa.objects.create(nome="Soma Centro Automotivo", cidade="Guarulhos", uf="SP",
                                        origem=Empresa.Origem.DESCOBERTA)
        self.op = Oportunidade.objects.create(empresa=self.e, tipo=Oportunidade.Tipo.NOVA_EXTERNA,
                                              status=Oportunidade.Status.NOVA)
        self.url = reverse("carteira:registrar_abordagem", args=[self.e.pk])

    def test_salvar_dados_sem_registrar_atendimento(self):
        self.client.force_login(self.ana)
        self.client.get(self.url)  # assume o atendimento
        r = self.client.post(self.url, {
            "acao": "salvar_dados", "observacoes": "falar com o comprador",
            "pessoa_nome": "Marcos", "pessoa_cargo": "Comprador", "pessoa_departamento": "Compras",
            "pessoa_telefone": "(11) 91326-4290", "pessoa_email": "",
            "extra_tipo": ["EMAIL"], "extra_valor": ["compras@soma.com.br"], "extra_depto": ["Compras"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Abordagem.objects.count(), 0)  # atendimento NÃO registrado
        marcos = Contato.objects.get(empresa=self.e, nome="Marcos")
        valores = set(CanalEmpresa.objects.filter(empresa=self.e).values_list("valor", flat=True))
        self.assertTrue(any("913264290" in "".join(ch for ch in v if ch.isdigit()) for v in valores))
        self.assertIn("compras@soma.com.br", valores)
        self.op.refresh_from_db()
        self.assertEqual(self.op.em_atendimento_por, self.ana)  # continua em atendimento
        html = r.content.decode()
        self.assertIn("falar com o comprador", html)  # o que foi digitado continua na tela
        self.assertIn(f'value="{marcos.pk}" selected', html)  # a pessoa salva já fica escolhida
        self.assertNotIn("Revise antes de salvar", html)

    def test_salvar_dados_valida_so_a_pessoa_e_os_contatos(self):
        self.client.force_login(self.ana)
        r = self.client.post(self.url, {"acao": "salvar_dados", "pessoa_cargo": "Comprador",
                                        "extra_tipo": ["TELEFONE"], "extra_valor": ["123"], "extra_depto": [""]})
        msgs = [str(m) for m in r.context["messages"]]
        self.assertTrue(any("nome da pessoa" in m for m in msgs), msgs)
        self.assertTrue(any("Telefone inválido" in m for m in msgs), msgs)
        self.assertEqual(Contato.objects.filter(empresa=self.e).count(), 0)
        self.assertNotIn("Revise antes de salvar", r.content.decode())

    def test_so_admin_libera_e_todos_veem_quem_atende(self):
        self.client.force_login(self.ana)
        self.client.get(self.url)
        lista = self.client.get(reverse("prospeccao:inside_sales")).content.decode()
        self.assertIn("Em atendimento: você", lista)
        self.assertNotIn('value="liberar"', lista)  # Inside Sales não tem o botão Liberar
        self.client.post(reverse("prospeccao:atendimento", args=[self.op.pk]), {"acao": "liberar"})
        self.op.refresh_from_db()
        self.assertEqual(self.op.em_atendimento_por, self.ana)  # nem a própria pessoa libera
        form = self.client.get(self.url).content.decode()
        self.assertNotIn("Cancelar e liberar", form)
        self.assertIn("Sair sem registrar", form)
        self.client.force_login(self.adm)
        lista = self.client.get(reverse("prospeccao:inside_sales")).content.decode()
        self.assertIn("Em atendimento: Ana", lista)
        self.client.post(reverse("prospeccao:atendimento", args=[self.op.pk]), {"acao": "liberar"})
        self.op.refresh_from_db()
        self.assertIsNone(self.op.em_atendimento_por)


class RegiaoTests(TestCase):
    """Casos reais: time interno da Tatiane e VENDEDOR INSIDE dominavam as cidades; coordenador e vendedor saíam
    de contagens separadas (ex.: 'Tatiane + Daniela', par que não existe)."""

    def _contas(self, n, cidade, vend, coord, tipo="Varejo", cep="", zona_owner=None):
        for i in range(n):
            self.k += 1
            Empresa.objects.create(nome=f"Conta {self.k}", salesforce_id=f"001{self.k:012d}", cidade=cidade, uf="SP",
                                   cep=cep, vendedor=vend, tipo_vendedor=tipo, coordenador_vendedor=coord,
                                   coordenador=zona_owner or coord)

    def setUp(self):
        regiao._cache.update(em=0, mapa=None)
        self.k = 0
        T = "TATIANE FRITZ DE QUEROZ CORREIA"
        # Mauá: muitas contas do interno e do VENDEDOR INSIDE; o vendedor de rua é a Daniela (coord. Washington)
        self._contas(30, "Mauá", "VENDEDOR INSIDE", T)
        self._contas(20, "MAUA", "DAYANE APARECIDA DA SILVA", T, tipo="Consumo / Industria")
        self._contas(8, "Mauá", "DANIELA TOSSATO", "WASHINGTON NETO", zona_owner=T)  # dono da região diferente
        self._contas(3, "Mauá", "JOÃO VITOR", "PAULO NETO", tipo="Consumo / Industria")
        # Guarulhos: varejo (Rosangela) e indústria (Paulo Neto)
        self._contas(6, "GUARULHOS", "VAGNER PINTO DOS SANTOS", "ROSANGELA COSTA", cep="07000-000")
        self._contas(4, "GUARULHOS", "LUAN MENEGHESSO FARIA", "PAULO NETO", tipo="Consumo / Industria", cep="07100-000")

    def tearDown(self):
        regiao._cache.update(em=0, mapa=None)

    def test_ignora_interno_e_genericos_e_mantem_o_par_do_vendedor(self):
        r = regiao.da_empresa(Empresa(nome="Lead", cidade="Mauá", uf="SP"))
        self.assertEqual((r["vendedor"], r["coordenador"]), ("DANIELA TOSSATO", "WASHINGTON NETO"))
        self.assertEqual(r["contas"], 11)  # só vendedores de rua

    def test_escopo_escolhe_o_tipo_do_vendedor(self):
        lead = Empresa(nome="Lead", cidade="Guarulhos", uf="SP")
        self.assertEqual(regiao.da_empresa(lead, "REVENDA_OFICINA")["vendedor"], "VAGNER PINTO DOS SANTOS")
        r = regiao.da_empresa(lead, "INDUSTRIA")
        self.assertEqual((r["vendedor"], r["coordenador"], r["tipo"]), ("LUAN MENEGHESSO FARIA", "PAULO NETO",
                                                                         "Consumo / Indústria"))
        self.assertEqual(regiao.da_empresa(Empresa(nome="L", cidade="Mauá", uf="SP"), "INDUSTRIA")["vendedor"],
                         "JOÃO VITOR")

    def test_faixa_de_cep_antes_do_municipio_quando_tem_contas(self):
        r = regiao.da_empresa(Empresa(nome="Lead", cidade="Guarulhos", uf="SP", cep="07000-123"))
        self.assertEqual((r["vendedor"], r["base"]), ("VAGNER PINTO DOS SANTOS", "CEP 07000-xxx"))
        r = regiao.da_empresa(Empresa(nome="Lead", cidade="Guarulhos", uf="SP", cep="07100-123"))  # 4 < mínimo
        self.assertEqual(r["base"], "Guarulhos/SP")
        self.assertIsNone(regiao.da_empresa(Empresa(nome="Lead", cidade="Manaus", uf="AM")))

    def test_lista_inside_sales_mostra_coordenador_e_vendedor(self):
        adm = get_user_model().objects.create_user("chefe", password="x", is_staff=True, is_superuser=True)
        e = Empresa.objects.create(nome="Lead Mauá", cidade="Mauá", uf="SP", origem=Empresa.Origem.DESCOBERTA)
        Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA, status=Oportunidade.Status.NOVA)
        self.client.force_login(adm)
        html = self.client.get(reverse("prospeccao:inside_sales")).content.decode()
        self.assertIn("Coordenador: <b>WASHINGTON NETO</b>", html)
        self.assertIn("Vendedor: <b>DANIELA TOSSATO</b>", html)
        self.assertNotIn("TATIANE", html)


    def test_filtro_por_coordenador_no_inside_sales(self):
        adm = get_user_model().objects.create_user("chefe", password="x", is_staff=True, is_superuser=True)
        for nome, cidade in (("Lead Mauá", "Mauá"), ("Lead GRU", "Guarulhos"), ("Lead Manaus", "Manaus")):
            e = Empresa.objects.create(nome=nome, cidade=cidade, uf="SP", origem=Empresa.Origem.DESCOBERTA)
            Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA, status=Oportunidade.Status.NOVA)
        self.client.force_login(adm)
        url = reverse("prospeccao:inside_sales")
        html = self.client.get(url).content.decode()
        self.assertIn('<select id="f-coordenador" name="coordenador">', html)
        self.assertIn('<option value="WASHINGTON NETO"', html)
        self.assertNotIn('id="f-pesquisa"', html)
        nomes = lambda r: {o.empresa.nome for o in r.context["itens"]}
        self.assertEqual(nomes(self.client.get(url, {"coordenador": "WASHINGTON NETO"})), {"Lead Mauá"})
        self.assertEqual(nomes(self.client.get(url, {"coordenador": "ROSANGELA COSTA"})), {"Lead GRU"})
        self.assertEqual(nomes(self.client.get(url, {"coordenador": "__sem__"})), {"Lead Manaus"})


class ImportacaoVendedorTests(TestCase):
    def test_mapeia_tipo_e_coordenador_do_vendedor(self):
        from carteira.services.importacao import mapear_account
        d = mapear_account({"Name": "X", "CoordinatorFormula__c": "TATIANE FRITZ DE QUEROZ CORREIA",
                            "Seller__r": {"Name": "DANIELA TOSSATO", "Type__c": "Varejo",
                                          "UserCoordinatorSync__r": {"Name": "WASHINGTON NETO"}}})
        self.assertEqual((d["vendedor"], d["tipo_vendedor"], d["coordenador_vendedor"]),
                         ("DANIELA TOSSATO", "Varejo", "WASHINGTON NETO"))
