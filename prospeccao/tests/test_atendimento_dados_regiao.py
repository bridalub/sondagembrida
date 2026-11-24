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
    def _conta(self, n, cidade, uf, coord, vend, cep="", reg="Grande SP"):
        Empresa.objects.create(nome=f"Conta {n}", salesforce_id=f"001{n:012d}", cidade=cidade, uf=uf, cep=cep,
                               coordenador=coord, vendedor=vend, regiao=reg)

    def setUp(self):
        regiao._cache.update(em=0, mapa=None)
        for i in range(3):
            self._conta(i, "GUARULHOS", "SP", "CARLOS COORD", "VITOR VENDAS")
        self._conta(3, "Guarulhos", "São Paulo", "CARLOS COORD", "OUTRO VENDEDOR")
        self._conta(4, "Mauá", "SP", "MARIA COORD", "JOANA", cep="09370-000")

    def tearDown(self):
        regiao._cache.update(em=0, mapa=None)

    def test_municipio_com_acentos_e_uf_por_extenso(self):
        r = regiao.da_empresa(Empresa(nome="Lead", cidade="Guarulhos", uf="SP"))
        self.assertEqual((r["coordenador"], r["vendedor"], r["contas"], r["coord_pct"], r["vend_pct"]),
                         ("CARLOS COORD", "VITOR VENDAS", 4, 100, 75))
        self.assertEqual(r["base"], "Guarulhos/SP")

    def test_sem_conta_no_municipio_usa_faixa_de_cep(self):
        r = regiao.da_empresa(Empresa(nome="Lead", cidade="Ribeirão Pires", uf="SP", cep="09370-123"))
        self.assertEqual((r["coordenador"], r["base"]), ("MARIA COORD", "CEP 09370-xxx"))
        self.assertIsNone(regiao.da_empresa(Empresa(nome="Lead", cidade="Manaus", uf="AM")))

    def test_lista_inside_sales_mostra_coordenador_e_vendedor(self):
        adm = get_user_model().objects.create_user("chefe", password="x", is_staff=True, is_superuser=True)
        e = Empresa.objects.create(nome="Lead GRU", cidade="Guarulhos", uf="SP", origem=Empresa.Origem.DESCOBERTA)
        Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA, status=Oportunidade.Status.NOVA)
        self.client.force_login(adm)
        html = self.client.get(reverse("prospeccao:inside_sales")).content.decode()
        self.assertIn("Coordenador: <b>CARLOS COORD</b>", html)
        self.assertIn("Vendedor: <b>VITOR VENDAS</b>", html)
