"""Carteira Salesforce: filtro por coordenador (caixa de seleção) e município em "Mais filtros"."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from carteira.models import Empresa


class FiltroCoordenadorTests(TestCase):
    def setUp(self):
        self.adm = get_user_model().objects.create_user("chefe", password="x", is_staff=True, is_superuser=True)
        Empresa.objects.create(nome="Conta Carlos", salesforce_id="001A", cidade="GUARULHOS", uf="SP", coordenador="CARLOS")
        Empresa.objects.create(nome="Conta Maria", salesforce_id="001B", cidade="MAUA", uf="SP", coordenador="MARIA")
        Empresa.objects.create(nome="Conta Sem Coord", salesforce_id="001C", cidade="MAUA", uf="SP")
        self.client.force_login(self.adm)

    def _nomes(self, **params):
        r = self.client.get(reverse("carteira:empresas"), params)
        return r, {e.nome for e in r.context["pagina"].object_list}

    def test_caixa_de_coordenadores_e_filtro(self):
        r, todos = self._nomes()
        html = r.content.decode()
        self.assertIn('<select name="coordenador"', html)
        self.assertIn('<option value="CARLOS">CARLOS</option>', html)
        self.assertEqual(self._nomes(coordenador="MARIA")[1], {"Conta Maria"})
        self.assertEqual(self._nomes(coordenador="__vazio__")[1], {"Conta Sem Coord"})

    def test_municipio_continua_em_mais_filtros(self):
        self.assertEqual(self._nomes(cidade="maua")[1], {"Conta Maria", "Conta Sem Coord"})
