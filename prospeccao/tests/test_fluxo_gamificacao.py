"""Ponta a ponta: o atendimento traz tudo o que a disputa do mês precisa, e a pontuação acompanha cada passo."""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from carteira.models import Abordagem, CanalEmpresa, Contato, Empresa, NotaFiscal
from prospeccao.models import Oportunidade
from prospeccao.services import atendimento as at
from prospeccao.services import bi_captacao as bi
from prospeccao.services import gamificacao as gm

MIDIA = tempfile.mkdtemp(prefix="brida-fluxo-")
CNPJ = "11222333000181"


@override_settings(MEDIA_ROOT=MIDIA)
class FluxoGamificacaoTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MIDIA, ignore_errors=True)

    def setUp(self):
        U = get_user_model()
        self.ana = U.objects.create_user("ana", password="x", first_name="Ana")
        self.bia = U.objects.create_user("bia", password="x", first_name="Bia")
        self.e = Empresa.objects.create(nome="Transportes Fluxo", cnpj=CNPJ, cidade="Mauá", uf="SP",
                                        origem=Empresa.Origem.DESCOBERTA)
        self.op = Oportunidade.objects.create(empresa=self.e, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
        self.hoje = timezone.localdate()
        self.ini, self.fim, _ = bi.periodo_do_mes(f"{self.hoje.year}-{self.hoje.month:02d}")

    def pontos(self, quem):
        return next(x for x in gm.placar(self.ini, self.fim) if x["pk"] == quem.pk)

    def test_fluxo_completo(self):
        c = self.client
        c.force_login(self.ana)
        # 1. Atender: a tela mostra os pontos e os requisitos da disputa
        r = c.post(reverse("prospeccao:atendimento", args=[self.op.pk]), {"acao": "assumir"})
        form_url = reverse("carteira:registrar_abordagem", args=[self.e.pk])
        self.assertTrue(r["Location"].startswith(form_url))
        tela = c.get(form_url)
        self.assertContains(tela, "Disputa do mês")
        self.assertContains(tela, "Arquivo do orçamento")
        self.assertContains(tela, "O cliente confirmou este CNPJ")
        # 2. Outra atendente não entra no lead em atendimento
        self.assertRaises(at.AtendimentoOcupado, at.assumir, self.op, self.bia)
        # 3. Orçamento: valor + arquivo + CNPJ confirmado + pessoa nova + outro e-mail
        pdf = SimpleUploadedFile("orc.pdf", b"%PDF-1.4 fluxo", content_type="application/pdf")
        r = c.post(form_url, {
            "data_hora": timezone.localtime().strftime("%Y-%m-%dT%H:%M"), "canal": "TELEFONE",
            "resultado": "CONVERSA_ORCAMENTO", "valor_orcamento": "2.300,00", "anexo_orcamento": pdf,
            "cnpj_confirmado": "11.222.333/0001-81", "pessoa_nome": "Rita Lima", "pessoa_departamento": "Compras",
            "extra_tipo": ["EMAIL"], "extra_valor": ["compras@fluxo.com.br"], "extra_depto": ["Compras"]},
            follow=True)
        msgs = " ".join(str(m) for m in r.context["messages"])
        self.assertIn("+4 pontos na disputa do mês", msgs)
        self.assertIn("Cliente novo", msgs)
        a = Abordagem.objects.get(empresa=self.e)
        self.assertTrue(a.anexo_orcamento)
        self.assertEqual((a.cnpj_confirmado, a.cliente_situacao, a.contato.nome), (CNPJ, "NOVO", "Rita Lima"))
        self.assertTrue(CanalEmpresa.objects.filter(empresa=self.e, valor__icontains="compras@").exists())
        self.op.refresh_from_db()
        self.assertEqual(self.op.responsavel, self.ana)  # dono do lead
        self.assertIsNone(self.op.em_atendimento_por)  # trava liberada ao salvar
        ana = self.pontos(self.ana)
        self.assertEqual((ana["pontos"], ana["metricas"]["orcamentos"]), (4, 1))
        # 4. A Bia não pontua no lead da Ana
        self.assertRaises(at.LeadDeOutro, at.assumir, self.op, self.bia)
        c.force_login(self.bia)
        self.assertEqual(c.get(form_url).status_code, 302)
        # 5. A nota fiscal chega (sincronização): a venda (30) SOMA aos 4 do orçamento
        NotaFiscal.objects.create(id_origem="NF-FLUXO", numero="123", tipo="VENDA", empresa=self.e,
                                  data_emissao=self.hoje, valor_total=Decimal("2300"))
        ana = self.pontos(self.ana)
        self.assertEqual(ana["pontos"], 34)
        self.assertEqual(bi.calcular(self.ini, self.fim)["kpis"]["valor_nf"], Decimal("2300"))
        # 6. O arquivo abre para a Ana (dona) e não para a Bia
        url_arq = reverse("carteira:anexo_orcamento", args=[a.pk])
        self.assertEqual(c.get(url_arq).status_code, 302)
        c.force_login(self.ana)
        self.assertEqual(c.get(url_arq).status_code, 200)
        self.assertEqual(Contato.objects.filter(empresa=self.e).count(), 1)

    def test_dez_notas_contam_uma_vez_e_a_venda_e_de_quem_atendeu(self):
        from carteira.services import abordagens as ab
        U = get_user_model()
        chefe = U.objects.create_user("chefe", password="x", first_name="Chefe", is_staff=True)
        # Ana atende e envia orçamento (4 pts), sem CNPJ; depois o ADMIN confirma o CNPJ no lead dela
        recuo = timedelta(days=2) if self.hoje.day > 2 else timedelta(minutes=30)  # contato antes das compras
        a = Abordagem(empresa=self.e, usuario=self.ana, canal="TELEFONE", resultado="CONVERSA_ORCAMENTO",
                      valor_orcamento=Decimal("1000"), anexo_orcamento="orcamentos/x.pdf",
                      data_hora=timezone.now() - recuo)
        ab.registrar_abordagem(a)
        b = Abordagem(empresa=self.e, usuario=chefe, canal="TELEFONE", resultado="CONVERSA_INTERESSE",
                      cnpj_confirmado=CNPJ, data_hora=timezone.now())
        ab.registrar_abordagem(b)
        self.assertEqual(self.pontos(self.ana)["pontos"], 4)
        for i in range(10):  # chegam 10 notas do mesmo cliente, no mesmo dia
            NotaFiscal.objects.create(id_origem=f"NF-10-{i}", numero=str(i), tipo="VENDA", empresa=self.e,
                                      data_emissao=self.hoje, valor_total=Decimal("100"))
        ana = self.pontos(self.ana)
        self.assertEqual(ana["pontos"], 34)  # 10 notas no mesmo dia = 1 venda (30) + o orçamento dela (4)
        self.assertEqual(ana["valor_nf"], Decimal("1000"))  # o valor soma as 10 notas
        self.assertEqual((ana["metricas"]["vendas_nf"], ana["notas_nf"]), (1, 10))
        if self.hoje.day > 2:  # o cliente comprou de novo em outro dia do mês: mais uma venda
            NotaFiscal.objects.create(id_origem="NF-OUTRO-DIA", numero="99", tipo="VENDA", empresa=self.e,
                                      data_emissao=self.hoje - timedelta(days=1), valor_total=Decimal("50"))
            self.assertEqual(self.pontos(self.ana)["pontos"], 64)  # 2 dias de compra (60) + orçamento (4)
        self.assertFalse(any(x["pk"] == chefe.pk and x["pontos"] for x in gm.placar(self.ini, self.fim)))
        self.assertEqual(self.pontos(self.bia)["pontos"], 0)  # quem não atendeu não ganha

    def test_cliente_que_comprou_sai_da_base_mas_continua_pontuando(self):
        from carteira.services import abordagens as ab
        a = Abordagem(empresa=self.e, usuario=self.ana, canal="TELEFONE", resultado="CONVERSA_INTERESSE",
                      cnpj_confirmado=CNPJ, data_hora=timezone.now() - timedelta(minutes=5))
        ab.registrar_abordagem(a)
        self.client.force_login(self.ana)
        lista = self.client.get(reverse("prospeccao:inside_sales"), {"aba": "procurados"})
        self.assertContains(lista, "Transportes Fluxo")
        NotaFiscal.objects.create(id_origem="NF-CONV", tipo="VENDA", empresa=self.e, data_emissao=self.hoje,
                                  valor_total=Decimal("900"))
        lista = self.client.get(reverse("prospeccao:inside_sales"), {"aba": "procurados"})
        self.assertNotContains(lista, "Transportes Fluxo")  # virou cliente: sai da base de trabalho
        self.op.refresh_from_db()
        self.assertEqual(self.op.status, Oportunidade.Status.JA_CLIENTE)
        self.assertEqual(self.pontos(self.ana)["pontos"], 30)  # a venda continua contando
        perf = self.client.get(reverse("prospeccao:minha_performance"))
        self.assertContains(perf, "Regra de pontuação")

    def test_desclassificar_lead_sai_da_lista_com_justificativa(self):
        from prospeccao.models import DecisaoRevisao
        from prospeccao.services.oportunidades import MOTIVOS_DESCLASSIFICAR
        url = reverse("prospeccao:desclassificar", args=[self.op.pk])
        self.client.force_login(self.ana)
        tela = self.client.get(reverse("carteira:registrar_abordagem", args=[self.e.pk]))
        self.assertContains(tela, "Desclassificar lead")
        # sem justificativa: recusa e o lead continua na lista
        self.client.post(url, {"motivo": MOTIVOS_DESCLASSIFICAR[1], "justificativa": "ruim"})
        self.op.refresh_from_db()
        self.assertEqual(self.op.status, Oportunidade.Status.NOVA)
        # a Bia não desclassifica o lead que está com a Ana
        at.assumir(self.op, self.ana)
        self.client.force_login(self.bia)
        self.client.post(url, {"motivo": MOTIVOS_DESCLASSIFICAR[1], "justificativa": "não usa lubrificantes"})
        self.op.refresh_from_db()
        self.assertEqual(self.op.status, Oportunidade.Status.NOVA)
        # a Ana desclassifica: sai da lista, fica o motivo e a decisão registrada
        self.client.force_login(self.ana)
        r = self.client.post(url, {"motivo": MOTIVOS_DESCLASSIFICAR[1],
                                   "justificativa": "Liguei: é escritório de contabilidade, não tem frota."})
        self.assertRedirects(r, reverse("prospeccao:inside_sales"), fetch_redirect_response=False)
        self.op.refresh_from_db()
        self.assertEqual(self.op.status, Oportunidade.Status.DESCARTADA)
        self.assertIn("escritório de contabilidade", self.op.motivo_descarte)
        self.assertIsNone(self.op.em_atendimento_por)
        d = DecisaoRevisao.objects.get(oportunidade=self.op, tipo="DESCLASSIFICAR_LEAD")
        self.assertEqual((d.usuario, d.dados["motivo_escolhido"]), (self.ana, MOTIVOS_DESCLASSIFICAR[1]))
        lista = self.client.get(reverse("prospeccao:inside_sales"))
        self.assertIn("desclassificado", " ".join(str(m) for m in lista.context["messages"]))
        self.assertNotIn(self.e.pk, [o.empresa_id for o in lista.context["itens"]])  # saiu da lista
        self.assertEqual(self.pontos(self.ana)["pontos"], 0)  # desclassificar não dá pontos

    def test_data_retroativa_conta_no_dia_do_registro(self):
        self.client.force_login(self.ana)
        antes = (timezone.localtime() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M")
        self.client.post(reverse("carteira:registrar_abordagem", args=[self.e.pk]),
                         {"data_hora": antes, "canal": "TELEFONE", "resultado": "SEM_RESPOSTA"})
        self.assertEqual(self.pontos(self.ana)["pontos"], 1)  # conta no mês do registro, não no de 40 dias atrás
