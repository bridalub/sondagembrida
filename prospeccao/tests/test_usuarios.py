"""Gestão de usuários própria (sem o /admin/ do Django): dois perfis, ADMIN e Insight Sales."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from carteira.models import Abordagem, Empresa
from prospeccao import papeis

U = get_user_model()
SENHA = "Brida-teste-2026!"


class UsuariosTests(TestCase):
    def setUp(self):
        self.admin = U.objects.create_user("chefe", password=SENHA, first_name="Chefe")
        papeis.definir_perfil(self.admin, "ADM")
        self.ana = U.objects.create_user("ana", password=SENHA, first_name="Ana")
        papeis.definir_perfil(self.ana, "INSIGHT")
        self.client.force_login(self.admin)

    def test_criar_insight_sales_e_entrar(self):
        r = self.client.post(reverse("prospeccao:usuario_novo"), {
            "nome": "Bruno Lima", "username": "bruno", "email": "bruno@exemplo.com", "perfil": "INSIGHT",
            "ativo": "on", "senha1": SENHA, "senha2": SENHA})
        self.assertRedirects(r, reverse("prospeccao:usuarios"))
        b = U.objects.get(username="bruno")
        self.assertEqual((b.first_name, papeis.papel(b), b.is_staff, b.is_superuser), ("Bruno Lima", "INSIGHT", False, False))
        self.client.logout()
        self.assertTrue(self.client.login(username="bruno", password=SENHA))
        self.assertRedirects(self.client.get(reverse("prospeccao:usuarios")), reverse("prospeccao:insight_sales"))

    def test_validacoes_do_cadastro(self):
        r = self.client.post(reverse("prospeccao:usuario_novo"), {
            "nome": "X", "username": "ANA", "perfil": "ADM", "ativo": "on", "senha1": "123", "senha2": "1234"})
        self.assertEqual(r.status_code, 200)
        f = r.context["form"]
        self.assertIn("username", f.errors)  # login repetido (sem diferenciar maiúsculas)
        self.assertIn("senha2", f.errors)

    def test_trocar_perfil_e_senha(self):
        url = reverse("prospeccao:usuario_editar", args=[self.ana.pk])
        r = self.client.post(url, {"nome": "Ana Souza", "username": "ana", "email": "", "perfil": "ADM", "ativo": "on"})
        self.assertRedirects(r, reverse("prospeccao:usuarios"))
        self.ana.refresh_from_db()
        self.assertEqual((papeis.papel(self.ana), self.ana.first_name), ("ADM", "Ana Souza"))
        self.client.post(url, {"nome": "Ana Souza", "username": "ana", "perfil": "INSIGHT", "ativo": "on"})
        self.ana.refresh_from_db()
        self.assertEqual((papeis.papel(self.ana), self.ana.is_superuser), ("INSIGHT", False))
        nova = "Outra-senha-2026!"
        self.client.post(reverse("prospeccao:usuario_senha", args=[self.ana.pk]), {"senha1": nova, "senha2": nova})
        self.ana.refresh_from_db()
        self.assertTrue(self.ana.check_password(nova))

    def test_desativar_mantem_historico_e_bloqueia_entrada(self):
        e = Empresa.objects.create(nome="Cliente X", origem=Empresa.Origem.DESCOBERTA)
        Abordagem.objects.create(empresa=e, usuario=self.ana, canal="TELEFONE", resultado="SEM_RESPOSTA")
        self.client.post(reverse("prospeccao:usuario_ativo", args=[self.ana.pk]), {"ativo": "0"})
        self.ana.refresh_from_db()
        self.assertFalse(self.ana.is_active)
        self.assertEqual(Abordagem.objects.filter(usuario=self.ana).count(), 1)
        self.client.logout()
        self.assertFalse(self.client.login(username="ana", password=SENHA))

    def test_nao_se_tira_o_proprio_admin_nem_fica_sem_admin(self):
        url = reverse("prospeccao:usuario_editar", args=[self.admin.pk])
        r = self.client.post(url, {"nome": "Chefe", "username": "chefe", "perfil": "INSIGHT", "ativo": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "próprio perfil ADMIN")
        self.client.post(reverse("prospeccao:usuario_ativo", args=[self.admin.pk]), {"ativo": "0"})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active and papeis.eh_adm(self.admin))

    def test_lista_mostra_perfis_e_insight_nao_acessa(self):
        r = self.client.get(reverse("prospeccao:usuarios"))
        self.assertContains(r, "ADMIN")
        self.assertContains(r, "Insight Sales")
        self.assertEqual({u.username for u in r.context["usuarios"]}, {"chefe", "ana"})
        self.assertEqual([u.username for u in self.client.get(reverse("prospeccao:usuarios"), {"perfil": "INSIGHT"})
                          .context["usuarios"]], ["ana"])
        self.client.force_login(self.ana)
        for nome in ("prospeccao:usuarios", "prospeccao:usuario_novo"):
            self.assertRedirects(self.client.get(reverse(nome)), reverse("prospeccao:insight_sales"))
        menu = self.client.get(reverse("prospeccao:insight_sales"))
        for texto in ("Minha performance", "Acompanhar retornos", "Empresas já conhecidas"):
            self.assertContains(menu, texto)
        self.assertNotContains(menu, reverse("prospeccao:usuarios"))

    def test_login_do_django_vai_para_o_login_do_sistema(self):
        self.client.logout()
        r = self.client.get("/admin/auth/user/")
        self.assertRedirects(r, reverse("prospeccao:usuarios"), fetch_redirect_response=False)
        r = self.client.get("/admin/login/?next=/admin/auth/user/")
        self.assertRedirects(r, "/entrar/?next=%2Fgestao%2Fusuarios%2F", fetch_redirect_response=False)
        r = self.client.get("/admin/", follow=True)
        self.assertEqual(r.resolver_match.url_name, "login")  # tela de entrada do sistema, não a do Django
        r = self.client.post("/entrar/?next=/gestao/usuarios/", {"username": "chefe", "password": SENHA})
        self.assertRedirects(r, "/gestao/usuarios/", fetch_redirect_response=False)
        self.client.force_login(self.ana)  # Insight Sales no /admin/ volta para a tela dele
        self.assertRedirects(self.client.get("/admin/"), reverse("prospeccao:insight_sales"))

    def test_insight_ve_so_os_proprios_retornos(self):
        from datetime import date, timedelta
        e = Empresa.objects.create(nome="Cliente Y", origem=Empresa.Origem.DESCOBERTA)
        Abordagem.objects.create(empresa=e, usuario=self.ana, canal="TELEFONE", resultado="CONVERSA_RETORNAR",
                                 data_retorno=date.today() + timedelta(days=1))
        e2 = Empresa.objects.create(nome="Cliente do Chefe", origem=Empresa.Origem.DESCOBERTA)
        Abordagem.objects.create(empresa=e2, usuario=self.admin, canal="TELEFONE", resultado="CONVERSA_RETORNAR",
                                 data_retorno=date.today() + timedelta(days=1))
        self.client.force_login(self.ana)
        r = self.client.get(reverse("carteira:retornos"))
        self.assertContains(r, "Cliente Y")
        self.assertNotContains(r, "Cliente do Chefe")
