"""Inside Sales: registrar contato move a empresa de "A procurar" para "Procurados", com quem atendeu."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from carteira.models import Abordagem, CanalAbordagem, Empresa, ResultadoAbordagem
from prospeccao.models import Oportunidade


class InsideSalesTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.gestor = U.objects.create_user("ana", password="x", first_name="Ana", is_staff=True)
        self.vendedor = U.objects.create_user("bruno", password="x", first_name="Bruno")
        self.client.force_login(self.vendedor)

        def lead(nome, cnpj=None, **extra):
            e = Empresa.objects.create(nome=nome, cnpj=cnpj, cidade="Mauá", uf="SP", origem=Empresa.Origem.DESCOBERTA)
            Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA,
                                        status=extra.get("status", Oportunidade.Status.NOVA))
            return e
        self.com_cnpj = lead("Transportes Alfa", cnpj="11222333000181")
        self.sem_cnpj = lead("Oficina Beta")
        self.descartada = lead("Ponto de Uber", status=Oportunidade.Status.DESCARTADA)
        conhecida = Empresa.objects.create(nome="Cliente Antigo", salesforce_id="001X", origem=Empresa.Origem.SALESFORCE)
        Oportunidade.objects.create(empresa=conhecida, tipo=Oportunidade.Tipo.CADASTRO_SEM_COMPRA)

    def _get(self, **params):
        return self.client.get(reverse("prospeccao:inside_sales"), params)

    def test_a_procurar_traz_com_e_sem_cnpj_e_exclui_descartada_e_conhecida(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        nomes = [o.empresa.nome for o in r.context["itens"]]
        self.assertCountEqual(nomes, ["Transportes Alfa", "Oficina Beta"])
        self.assertEqual(r.context["contagem"], {"procurar": 2, "procurados": 0, "em_atendimento": 0})

    def test_a_procurar_bate_com_a_conferir_da_visao_geral(self):
        """Lead fora do escopo comercial (ex.: varejo de brinquedos) não vai para a equipe, como na Visão geral."""
        fora = lead_fora = Empresa.objects.create(nome="MVF Usinagem", origem=Empresa.Origem.DESCOBERTA)
        Oportunidade.objects.create(empresa=fora, tipo=Oportunidade.Tipo.NOVA_EXTERNA, escopo="FORA",
                                    status=Oportunidade.Status.NOVA)
        self.assertNotIn("MVF Usinagem", [o.empresa.nome for o in self._get().context["itens"]])
        self.client.force_login(self.gestor)
        visao = self.client.get(reverse("prospeccao:visao_geral"))
        self.assertEqual(visao.context["n_conferir"], self._get().context["contagem"]["procurar"])
        self.assertTrue(lead_fora.pk)

    def test_registrar_contato_move_para_procurados_com_quem_atendeu(self):
        Abordagem.objects.create(empresa=self.sem_cnpj, usuario=self.vendedor, canal=CanalAbordagem.TELEFONE,
                                 resultado=ResultadoAbordagem.SEM_RESPOSTA)
        r = self._get()
        self.assertEqual([o.empresa.nome for o in r.context["itens"]], ["Transportes Alfa"])
        r = self._get(aba="procurados")
        itens = r.context["itens"]
        self.assertEqual([o.empresa.nome for o in itens], ["Oficina Beta"])
        self.assertEqual(itens[0].atendente, "Bruno")
        self.assertIn("sem resposta", itens[0].ult_resultado_rot)
        self.assertContains(r, "Bruno")

    def test_filtros_de_cnpj_e_atendente(self):
        Abordagem.objects.create(empresa=self.com_cnpj, usuario=self.gestor, canal=CanalAbordagem.WHATSAPP,
                                 resultado=ResultadoAbordagem.CONVERSA_INTERESSE)
        self.assertEqual([o.empresa.nome for o in self._get(cnpj="sem").context["itens"]], ["Oficina Beta"])
        self.assertEqual(len(self._get(aba="procurados", atendente="ana").context["itens"]), 1)
        self.assertEqual(len(self._get(aba="procurados", atendente="bruno").context["itens"]), 0)
        self.assertEqual(self._get(aba="procurados").context["itens"][0].atendente, "Ana")
