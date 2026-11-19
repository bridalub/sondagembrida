"""No registro do contato: cadastrar a pessoa (nome do lead) e vários telefones/e-mails com departamento."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from carteira.models import Abordagem, CanalEmpresa, Contato, Empresa
from prospeccao.models import Oportunidade


class PessoasNoAtendimentoTests(TestCase):
    def setUp(self):
        self.u = get_user_model().objects.create_user("ana", password="x", first_name="Ana")
        self.e = Empresa.objects.create(nome="Lead Pessoas", cidade="Mauá", uf="SP", origem=Empresa.Origem.DESCOBERTA)
        Oportunidade.objects.create(empresa=self.e, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
        self.client.force_login(self.u)
        self.url = reverse("carteira:registrar_abordagem", args=[self.e.pk])

    def base(self, **extra):
        return {"data_hora": timezone.localtime().strftime("%Y-%m-%dT%H:%M"), "canal": "TELEFONE",
                "resultado": "CONVERSA_INTERESSE", "sem_cnpj": "on", **extra}

    def test_cadastra_pessoa_e_varios_contatos_com_departamento(self):
        r = self.client.post(self.url, {
            **self.base(pessoa_nome="Carlos Souza", pessoa_cargo="Comprador", pessoa_departamento="Compras",
                        pessoa_telefone="(11) 98765-4321", pessoa_email="carlos@lead.com.br"),
            "extra_tipo": ["TELEFONE", "EMAIL", "EMAIL"],
            "extra_valor": ["(11) 4002-8922", "manutencao@lead.com.br", ""],
            "extra_depto": ["Manutenção", "Manutenção", ""]})
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        p = Contato.objects.get(empresa=self.e)
        self.assertEqual((p.nome, p.cargo, p.departamento, p.celular, p.email),
                         ("Carlos Souza", "Comprador", "Compras", "(11) 98765-4321", "carlos@lead.com.br"))
        self.assertEqual(Abordagem.objects.get(empresa=self.e).contato, p)  # o contato fica com o nome da pessoa
        canais = {c.valor_normalizado or c.valor: c for c in CanalEmpresa.objects.filter(empresa=self.e)}
        self.assertEqual(len(canais), 4)  # celular e e-mail da pessoa + telefone e e-mail da manutenção
        manut = next(c for c in canais.values() if "manutencao@" in c.valor)
        self.assertEqual((manut.departamento, manut.origem_canal, manut.confirmacao_humana),
                         ("Manutenção", "HUMANO", "ATENDIDO"))

    def test_erros_nao_gravam_nada(self):
        r = self.client.post(self.url, {**self.base(pessoa_cargo="Gerente"),
                                         "extra_tipo": ["EMAIL", "TELEFONE"], "extra_valor": ["nao-e-email", "123"],
                                         "extra_depto": ["", ""]})
        self.assertEqual(r.status_code, 200)
        f = r.context["form"]
        self.assertIn("pessoa_nome", f.errors)  # cargo sem nome
        self.assertTrue(any("E-mail inválido" in e for e in f.non_field_errors()))
        self.assertTrue(any("Telefone inválido" in e for e in f.non_field_errors()))
        self.assertContains(r, 'value="nao-e-email"')  # a linha digitada volta para corrigir
        self.assertEqual((Abordagem.objects.count(), Contato.objects.count(), CanalEmpresa.objects.count()), (0, 0, 0))

    def test_nao_escolhe_e_cadastra_ao_mesmo_tempo(self):
        p = Contato.objects.create(empresa=self.e, nome="Já Existe")
        r = self.client.post(self.url, self.base(contato=p.pk, pessoa_nome="Outra Pessoa"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("pessoa_nome", r.context["form"].errors)

    def test_tela_mostra_campos_novos(self):
        r = self.client.get(self.url)
        self.assertContains(r, "Cadastrar uma pessoa nova")
        self.assertContains(r, "Adicionar telefone ou e-mail")
        self.assertContains(r, "Manutenção")
