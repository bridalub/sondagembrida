from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from carteira.models import Empresa, Importacao, IndicadorCompra, NotaFiscal, SituacaoCompra
from carteira.tests.test_carteira import CNPJ_A, CNPJ_B, conta, item


class FakeSF:
    """Simula o Salesforce. Qualquer método além de describe/query_all quebra o teste (somente leitura)."""

    sf_instance = "fake.my.salesforce.com"

    def __init__(self):
        self.consultas = []
        self.Account = mock.Mock(spec=["describe"])
        self.Account.describe.return_value = {"fields": [
            {"name": "Id", "label": "ID", "type": "id"},
            {"name": "Phone", "label": "Telefone", "type": "phone"},
            {"name": "CNPJ__c", "label": "CNPJ", "type": "string"},
        ]}

    def query_all(self, q):
        self.consultas.append(q)
        if "FROM Account" in q:
            return {"records": [conta("A", CNPJ_A, Phone="11 3333-4444"), conta("B", CNPJ_B)]}
        if "Type__c = 'Saída'" in q:
            return {"records": [item("N1", "A", "2026-05-02", 100)]}
        return {"records": []}


class ComandoSyncTests(TestCase):
    def test_sync_completo_e_idempotente(self):
        sf = FakeSF()
        with mock.patch("carteira.services.salesforce.conectar", return_value=sf):
            call_command("sync_salesforce", stdout=StringIO(), stderr=StringIO())
            call_command("sync_salesforce", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(Empresa.objects.count(), 2)
        self.assertEqual(NotaFiscal.objects.count(), 1)
        self.assertEqual(Empresa.objects.get(salesforce_id="A").telefone, "11 3333-4444")
        self.assertEqual(IndicadorCompra.objects.get(empresa__salesforce_id="A").situacao, SituacaoCompra.COMPROU)
        self.assertEqual(IndicadorCompra.objects.get(empresa__salesforce_id="B").situacao,
                         SituacaoCompra.SEM_COMPRA_PERIODO)
        self.assertEqual(Importacao.objects.filter(status="SUCESSO").count(), 8)  # clientes, contatos, leads, compras x2
        self.assertTrue(all(q.lstrip().upper().startswith("SELECT") for q in sf.consultas))
        q_conta = next(q for q in sf.consultas if "FROM Account" in q)
        self.assertIn("ProtheusCustomerSituation__c IN ('Ativo','Inativo','Bloqueado')", q_conta)  # bloqueadas entram no cruzamento
        self.assertIn("Phone", q_conta)

    def test_falha_de_conexao_fica_registrada(self):
        with mock.patch("carteira.services.salesforce.conectar", side_effect=RuntimeError("sem rede")):
            with self.assertRaises(Exception):
                call_command("sync_salesforce", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(Importacao.objects.get().status, "FALHA")
