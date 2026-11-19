"""Papéis (ADM × Insight Sales) e trava de atendimento do lead."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from carteira.models import Empresa
from prospeccao.models import Oportunidade
from prospeccao.papeis import eh_adm
from prospeccao.services import atendimento as at


class PapeisTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.adm = U.objects.create_user("chefe", password="x")
        self.adm.groups.add(Group.objects.get_or_create(name="ADM")[0])
        self.ana = U.objects.create_user("ana", first_name="Ana", password="x")
        self.ana.groups.add(Group.objects.get_or_create(name="Insight Sales")[0])
        self.bia = U.objects.create_user("bia", first_name="Bia", password="x")
        e = Empresa.objects.create(nome="Lead Teste Ltda", cidade="Santo André", uf="SP",
                                   origem=Empresa.Origem.DESCOBERTA)
        self.e = e
        self.op = Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA,
                                              status=Oportunidade.Status.NOVA)

    def test_grupo_adm_vira_acesso_total(self):
        self.adm.refresh_from_db()
        self.assertTrue(self.adm.is_staff and self.adm.is_superuser and eh_adm(self.adm))
        self.assertFalse(eh_adm(self.ana))
        self.assertFalse(eh_adm(self.bia))  # sem grupo = Insight Sales (menor acesso)

    def test_insight_sales_so_ve_as_telas_dele(self):
        self.client.force_login(self.ana)
        for nome in ("prospeccao:insight_sales", "prospeccao:minha_performance", "carteira:retornos",
                     "carteira:empresas"):
            self.assertEqual(self.client.get(reverse(nome)).status_code, 200, nome)
        for nome in ("prospeccao:pesquisas", "prospeccao:captacao", "prospeccao:parametros", "carteira:listas",
                     "prospeccao:oportunidades", "prospeccao:visao_geral", "prospeccao:usuarios",
                     "carteira:novas_abordagens"):
            r = self.client.get(reverse(nome))
            self.assertRedirects(r, reverse("prospeccao:insight_sales"), msg_prefix=nome)
        r = self.client.get(reverse("prospeccao:insight_sales"))
        self.assertNotContains(r, reverse("prospeccao:pesquisas"))  # menu sem as telas de gestão

    def test_adm_ve_tudo(self):
        self.client.force_login(self.adm)
        for nome in ("prospeccao:pesquisas", "prospeccao:captacao", "prospeccao:insight_sales",
                     "prospeccao:minha_performance"):
            self.assertEqual(self.client.get(reverse(nome)).status_code, 200, nome)

    def test_lead_em_atendimento_nao_pode_ser_atendido_por_outro(self):
        url = reverse("prospeccao:atendimento", args=[self.op.pk])
        self.client.force_login(self.ana)
        r = self.client.post(url, {"acao": "assumir"})
        self.assertRedirects(r, reverse("carteira:registrar_abordagem", args=[self.e.pk]) + "?proximo="
                             + reverse("prospeccao:insight_sales"), fetch_redirect_response=False)
        self.op.refresh_from_db()
        self.assertEqual(self.op.em_atendimento_por, self.ana)
        lista = self.client.get(reverse("prospeccao:insight_sales"))
        self.assertContains(lista, "Continuar atendimento")

        self.client.force_login(self.bia)
        lista = self.client.get(reverse("prospeccao:insight_sales"))
        self.assertContains(lista, "Em atendimento: Ana")
        self.client.post(url, {"acao": "assumir"})
        self.op.refresh_from_db()
        self.assertEqual(self.op.em_atendimento_por, self.ana)  # continua com a Ana
        r = self.client.get(reverse("carteira:registrar_abordagem", args=[self.e.pk]))
        self.assertEqual(r.status_code, 302)  # Bia não abre o registro
        r = self.client.post(url, {"acao": "liberar"})
        self.op.refresh_from_db()
        self.assertEqual(self.op.em_atendimento_por, self.ana)  # Bia não libera a trava da Ana

        self.client.force_login(self.adm)
        self.client.post(url, {"acao": "liberar"})
        self.op.refresh_from_db()
        self.assertIsNone(self.op.em_atendimento_por)  # ADM libera

    def test_trava_vence_sozinha(self):
        at.assumir(self.op, self.ana)
        Oportunidade.objects.filter(pk=self.op.pk).update(
            em_atendimento_desde=timezone.now() - timedelta(minutes=at.minutos() + 1))
        self.op.refresh_from_db()
        self.assertIsNone(at.vigente(self.op))
        at.assumir(self.op, self.bia)  # vencida: outro pode assumir
        self.op.refresh_from_db()
        self.assertEqual(self.op.em_atendimento_por, self.bia)

    def test_registrar_contato_libera_a_trava(self):
        self.client.force_login(self.ana)
        url = reverse("carteira:registrar_abordagem", args=[self.e.pk])
        self.client.get(url)
        self.op.refresh_from_db()
        self.assertEqual(self.op.em_atendimento_por, self.ana)
        r = self.client.post(url, {"data_hora": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                                   "canal": "TELEFONE", "resultado": "SEM_RESPOSTA"})
        self.assertEqual(r.status_code, 302, getattr(r, "context", {}) and r.context["form"].errors)
        self.op.refresh_from_db()
        self.assertIsNone(self.op.em_atendimento_por)
