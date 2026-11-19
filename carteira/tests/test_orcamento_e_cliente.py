"""Orçamento com arquivo anexado (comprovante da gamificação) e venda só para cliente novo, sem compra ou inativo."""
import shutil
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from carteira.models import Abordagem, Empresa, NotaFiscal
from carteira.services.abordagens import situacao_cliente
from prospeccao.models import Oportunidade
from prospeccao.services import bi_captacao as bi

MIDIA = tempfile.mkdtemp(prefix="brida-teste-midia-")


@override_settings(MEDIA_ROOT=MIDIA)
class OrcamentoAnexoTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MIDIA, ignore_errors=True)

    def setUp(self):
        U = get_user_model()
        self.ana = U.objects.create_user("ana", password="x", first_name="Ana")
        self.bia = U.objects.create_user("bia", password="x", first_name="Bia")
        self.e = Empresa.objects.create(nome="Lead Orçamento", origem=Empresa.Origem.DESCOBERTA)
        Oportunidade.objects.create(empresa=self.e, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
        self.url = reverse("carteira:registrar_abordagem", args=[self.e.pk])

    def post(self, **extra):
        dados = {"data_hora": timezone.localtime().strftime("%Y-%m-%dT%H:%M"), "canal": "TELEFONE",
                 "resultado": "CONVERSA_ORCAMENTO", "valor_orcamento": "1.500,00", "sem_cnpj": "on", **extra}
        return self.client.post(self.url, dados)

    def test_orcamento_exige_arquivo(self):
        self.client.force_login(self.ana)
        r = self.post()
        self.assertEqual(r.status_code, 200)
        self.assertIn("anexo_orcamento", r.context["form"].errors)
        self.assertEqual(Abordagem.objects.count(), 0)

    def test_formato_nao_aceito(self):
        self.client.force_login(self.ana)
        r = self.post(anexo_orcamento=SimpleUploadedFile("virus.exe", b"MZ..."))
        self.assertIn("anexo_orcamento", r.context["form"].errors)

    def test_anexa_e_so_quem_registrou_ou_admin_abre(self):
        self.client.force_login(self.ana)
        pdf = SimpleUploadedFile("orcamento.pdf", b"%PDF-1.4 teste", content_type="application/pdf")
        r = self.post(anexo_orcamento=pdf)
        self.assertEqual(r.status_code, 302)
        a = Abordagem.objects.get()
        self.assertTrue(a.anexo_orcamento.name.startswith("orcamentos/"))
        url = reverse("carteira:anexo_orcamento", args=[a.pk])
        r = self.client.get(url)
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "application/pdf"))
        self.assertEqual(b"".join(r.streaming_content), b"%PDF-1.4 teste")
        self.client.force_login(self.bia)  # outra atendente não abre
        self.assertEqual(self.client.get(url).status_code, 302)


class SituacaoClienteTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.ana = U.objects.create_user("ana", password="x", first_name="Ana")
        self.hoje = date(2026, 11, 19)

    def conta(self, cnpj, ultima=None, nf=None):
        e = Empresa.objects.create(nome=f"Conta {cnpj}", cnpj=cnpj, salesforce_id=f"001{cnpj[:12]}",
                                   ultima_compra_cadastro=ultima)
        if nf:
            NotaFiscal.objects.create(id_origem=f"NF-{cnpj}", tipo="VENDA", empresa=e, data_emissao=nf,
                                      valor_total=Decimal("100"))
        return e

    def test_classificacao(self):
        S = Abordagem.SituacaoCliente
        self.assertEqual(situacao_cliente("11222333000181", self.hoje), (S.NOVO, None))
        self.conta("11444777000161")
        self.assertEqual(situacao_cliente("11444777000161", self.hoje)[0], S.SEM_COMPRA)
        self.conta("45997418000153", ultima=date(2023, 5, 1))  # mais de 2 anos sem comprar
        self.assertEqual(situacao_cliente("45997418000153", self.hoje), (S.INATIVO, date(2023, 5, 1)))
        self.conta("33000167000101", ultima=date(2020, 1, 1), nf=date(2026, 9, 10))  # NF recente vence o cadastro
        self.assertEqual(situacao_cliente("33000167000101", self.hoje), (S.ATIVO, date(2026, 9, 10)))

    def test_venda_de_cliente_ativo_nao_conta_na_captacao(self):
        ativo, novo = "33000167000101", "11222333000181"
        conta_ativa = self.conta(ativo, nf=date(2026, 9, 10))
        for cnpj, nome in ((ativo, "Lead Ativo"), (novo, "Lead Novo")):
            e = Empresa.objects.create(nome=nome, origem=Empresa.Origem.DESCOBERTA,
                                       cnpj=None if cnpj == ativo else cnpj)
            Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
            from carteira.services import abordagens as ab
            a = Abordagem(empresa=e, usuario=self.ana, canal="TELEFONE", resultado="CONVERSA_INTERESSE",
                          cnpj_confirmado=cnpj, data_hora=timezone.make_aware(datetime(2026, 11, 3, 10)))
            ab.registrar_abordagem(a)
            if cnpj == novo:
                lead_novo = e
        self.assertEqual(Abordagem.objects.get(cnpj_confirmado=ativo).cliente_situacao, "ATIVO")
        self.assertEqual(Abordagem.objects.get(cnpj_confirmado=novo).cliente_situacao, "NOVO")
        NotaFiscal.objects.create(id_origem="NF-A2", tipo="VENDA", empresa=conta_ativa, data_emissao=date(2026, 11, 10),
                                  valor_total=Decimal("5000"))
        NotaFiscal.objects.create(id_origem="NF-N1", tipo="VENDA", empresa=lead_novo, data_emissao=date(2026, 11, 12),
                                  valor_total=Decimal("800"))
        k = bi.calcular(*bi.periodo_do_mes("2026-11")[:2])["kpis"]
        self.assertEqual((k["valor_nf"], k["notas_nf"]), (Decimal("800"), 1))  # a venda do cliente ativo fica fora
