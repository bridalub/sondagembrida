"""Registro de contato: o que o painel de captação precisa é exigido e validado no servidor."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from carteira.forms import AbordagemForm
from carteira.models import Empresa
from carteira.services.cnpj import calcular_dv

BASE = "112223330001"
CNPJ = BASE + calcular_dv(BASE)
MASCARA = f"{CNPJ[:2]}.{CNPJ[2:5]}.{CNPJ[5:8]}/{CNPJ[8:12]}-{CNPJ[12:]}"


class RegistroContatoTests(TestCase):
    def setUp(self):
        self.e = Empresa.objects.create(nome="Coopercell", cidade="Ribeirão Pires", uf="SP",
                                        origem=Empresa.Origem.DESCOBERTA)
        self.agora = timezone.localtime().strftime("%Y-%m-%dT%H:%M")

    def form(self, arquivos=None, **dados):
        base = {"data_hora": self.agora, "canal": "TELEFONE"}
        base.update(dados)
        return AbordagemForm(data=base, files=arquivos, empresa=self.e)

    def test_tentativa_descarta_valores_e_cnpj_escondidos(self):
        f = self.form(resultado="SEM_RESPOSTA", valor_venda="900", valor_orcamento="500", cnpj_confirmado=MASCARA)
        self.assertTrue(f.is_valid(), f.errors)
        a = f.save(commit=False)
        self.assertIsNone(a.valor_venda)
        self.assertIsNone(a.valor_orcamento)
        self.assertEqual(a.cnpj_confirmado, "")

    def test_orcamento_exige_valor_e_aceita_formato_brasileiro(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = self.form(resultado="CONVERSA_ORCAMENTO", cnpj_confirmado=MASCARA)
        self.assertFalse(f.is_valid())
        self.assertIn("valor_orcamento", f.errors)
        self.assertIn("anexo_orcamento", f.errors)  # o arquivo do orçamento também é exigido
        pdf = SimpleUploadedFile("orc.pdf", b"%PDF-1.4", content_type="application/pdf")
        f = self.form(resultado="CONVERSA_ORCAMENTO", cnpj_confirmado=MASCARA, valor_orcamento="R$ 1.500,50",
                      arquivos={"anexo_orcamento": pdf})
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data["valor_orcamento"], Decimal("1500.50"))
        self.assertIsNone(f.cleaned_data["valor_venda"])

    def test_venda_exige_cnpj_e_valor_e_opcional(self):
        # o valor oficial vem das notas fiscais do CNPJ confirmado; o digitado é só previsão
        f = self.form(resultado="CONVERSA_VENDA", valor_venda="2.000,00", sem_cnpj="on")
        self.assertFalse(f.is_valid())
        self.assertIn("cnpj_confirmado", f.errors)
        self.assertTrue(self.form(resultado="CONVERSA_VENDA", cnpj_confirmado=MASCARA).is_valid())
        f = self.form(resultado="CONVERSA_VENDA", cnpj_confirmado=MASCARA, valor_venda="2.000,00")
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data["cnpj_confirmado"], CNPJ)

    def test_conversa_comercial_exige_cnpj_ou_justificativa(self):
        f = self.form(resultado="CONVERSA_INTERESSE")
        self.assertFalse(f.is_valid())
        self.assertIn("cnpj_confirmado", f.errors)
        self.assertTrue(self.form(resultado="CONVERSA_INTERESSE", sem_cnpj="on").is_valid())
        self.assertTrue(self.form(resultado="CONVERSA_NAO_CONTATAR").is_valid())

    def test_data_futura_e_valor_zero_sao_recusados(self):
        futuro = (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        f = self.form(resultado="SEM_RESPOSTA", data_hora=futuro)
        self.assertFalse(f.is_valid())
        self.assertIn("data_hora", f.errors)
        f = self.form(resultado="CONVERSA_ORCAMENTO", cnpj_confirmado=MASCARA, valor_orcamento="0,00")
        self.assertFalse(f.is_valid())
        self.assertIn("valor_orcamento", f.errors)

    def test_valor_invalido_mostra_mensagem_clara(self):
        f = self.form(resultado="CONVERSA_ORCAMENTO", cnpj_confirmado=MASCARA, valor_orcamento="mil reais")
        self.assertFalse(f.is_valid())
        self.assertIn("1.500,00", str(f.errors["valor_orcamento"]))
