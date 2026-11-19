"""Disputa do mês: pontos, ranking, sequência, conquistas e análise de teste (simulação sem gravar)."""
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from carteira.models import Abordagem, Empresa, NotaFiscal
from prospeccao.models import Oportunidade
from prospeccao.services import bi_captacao as bi
from prospeccao.services import gamificacao as gm


def quando(dia, hora=10):
    return timezone.make_aware(datetime(2026, 11, dia, hora, 0))


def registrado_na_hora():
    """Nos testes, cada contato foi registrado no momento do contato (criado_em = data_hora)."""
    for pk, dh in Abordagem.objects.values_list("pk", "data_hora"):
        Abordagem.objects.filter(pk=pk).update(criado_em=dh)


class GamificacaoTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.ana = U.objects.create_user("ana", password="x", first_name="Ana")
        self.bia = U.objects.create_user("bia", password="x", first_name="Bia")
        self.caio = U.objects.create_user("caio", password="x", first_name="Caio")  # sem contatos: participa com 0
        self.emp = []
        for i in range(4):
            e = Empresa.objects.create(nome=f"Lead {i}", origem=Empresa.Origem.DESCOBERTA)
            Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
            self.emp.append(e)
        # Ana: 2 atendimentos: um chegou ao orçamento (vale só 4) e outro só procurado (1) = 5
        Abordagem.objects.create(empresa=self.emp[0], usuario=self.ana, canal="TELEFONE", resultado="SEM_RESPOSTA",
                                 data_hora=quando(16))
        Abordagem.objects.create(empresa=self.emp[0], usuario=self.ana, canal="TELEFONE", resultado="CONVERSA_ORCAMENTO",
                                 valor_orcamento=Decimal("900"), data_hora=quando(17),
                                 anexo_orcamento="orcamentos/teste.pdf")
        Abordagem.objects.create(empresa=self.emp[1], usuario=self.ana, canal="TELEFONE", resultado="RECADO",
                                 data_hora=quando(18))
        # Bia: 1 atendimento com conversa, CNPJ e venda por NF: vale só a etapa mais alta = 30
        c = "11222333000181"
        Empresa.objects.filter(pk=self.emp[2].pk).update(cnpj=c)
        Abordagem.objects.create(empresa=self.emp[2], usuario=self.bia, canal="TELEFONE", resultado="CONVERSA_INTERESSE",
                                 cnpj_confirmado=c, data_hora=quando(10))
        NotaFiscal.objects.create(id_origem="NF-G1", tipo="VENDA", empresa=self.emp[2], data_emissao=date(2026, 11, 12),
                                  valor_total=Decimal("1500"))
        registrado_na_hora()
        self.ini, self.fim, _ = bi.periodo_do_mes("2026-11")

    def test_pontos_e_ranking(self):
        r = gm.placar(self.ini, self.fim, hoje=date(2026, 11, 19))
        self.assertEqual([(x["nome"], x["pontos"], x["posicao"]) for x in r],
                         [("Bia", 30, 1), ("Ana", 5, 2), ("Caio", 0, 3)])
        self.assertEqual(r[0]["barra"], 100)
        ana = r[1]
        self.assertEqual(ana["metricas"], {"procuradas": 2, "conversas": 1, "cnpjs": 0, "orcamentos": 1, "vendas_nf": 0})
        self.assertEqual({p["chave"]: (p["qtd"], p["pontos"]) for p in ana["parcelas"] if p["qtd"]},
                         {"orcamentos": (1, 4), "procuradas": (1, 1)})

    def test_quem_so_acumula_contatos_nao_passa_quem_vende(self):
        U = get_user_model()
        dani = U.objects.create_user("dani", password="x", first_name="Dani")
        for i in range(25):  # 25 empresas só procuradas (1 ponto cada) = 25 < 30 de uma venda
            e = Empresa.objects.create(nome=f"Volume {i}", origem=Empresa.Origem.DESCOBERTA)
            Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
            Abordagem.objects.create(empresa=e, usuario=dani, canal="TELEFONE", resultado="SEM_RESPOSTA",
                                     data_hora=quando(11))
        registrado_na_hora()
        r = gm.placar(self.ini, self.fim, hoje=date(2026, 11, 19))
        self.assertEqual([x["nome"] for x in r[:2]], ["Bia", "Dani"])

    def test_data_retroativa_nao_monta_sequencia_nem_muda_o_mes(self):
        e = Empresa.objects.create(nome="Retro", origem=Empresa.Origem.DESCOBERTA)
        Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
        a = Abordagem.objects.create(empresa=e, usuario=self.caio, canal="TELEFONE", resultado="SEM_RESPOSTA",
                                     data_hora=timezone.make_aware(datetime(2026, 10, 20, 10)))  # diz outubro
        Abordagem.objects.filter(pk=a.pk).update(criado_em=quando(19))  # registrado em novembro
        caio = next(x for x in gm.placar(self.ini, self.fim, hoje=date(2026, 11, 19)) if x["nome"] == "Caio")
        self.assertEqual((caio["pontos"], caio["sequencia"]), (1, 1))  # conta em novembro, um dia só
        out = gm.placar(*bi.periodo_do_mes("2026-10")[:2], hoje=date(2026, 10, 31))
        self.assertEqual(next(x for x in out if x["nome"] == "Caio")["pontos"], 0)

    def test_registro_mostra_pontos_ganhos_e_lead_ganha_dono(self):
        from prospeccao.services import atendimento as at
        e = Empresa.objects.create(nome="Novo Lead", origem=Empresa.Origem.DESCOBERTA)
        op = Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
        self.client.force_login(self.caio)
        url = reverse("carteira:registrar_abordagem", args=[e.pk])
        r = self.client.post(url, {"data_hora": timezone.localtime().strftime("%Y-%m-%dT%H:%M"), "canal": "TELEFONE",
                                   "resultado": "SEM_RESPOSTA"}, follow=True)
        textos = [str(m) for m in r.context["messages"]]
        self.assertTrue(any("+1 ponto na disputa do mês" in t for t in textos), textos)
        op.refresh_from_db()
        self.assertEqual(op.responsavel, self.caio)  # 1º atendimento: o lead é do Caio
        # outra pessoa não atende nem registra no lead do Caio
        self.assertRaises(at.LeadDeOutro, at.assumir, op, self.ana)
        self.client.force_login(self.ana)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 302)
        lista = self.client.get(reverse("prospeccao:insight_sales"), {"aba": "procurados"})
        self.assertContains(lista, "lead de Caio")

    def test_cnpj_encontrado_nao_vem_preenchido(self):
        from carteira.forms import AbordagemForm
        e = Empresa.objects.create(nome="Com CNPJ", cnpj="11444777000161", origem=Empresa.Origem.DESCOBERTA)
        f = AbordagemForm(empresa=e)
        self.assertFalse(f.initial.get("cnpj_confirmado"))
        self.client.force_login(self.ana)
        Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
        r = self.client.get(reverse("carteira:registrar_abordagem", args=[e.pk]))
        self.assertContains(r, "O cliente confirmou este CNPJ")

    def test_orcamento_sem_arquivo_nao_pontua(self):
        e = Empresa.objects.create(nome="Sem Arquivo", origem=Empresa.Origem.DESCOBERTA)
        Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA)
        Abordagem.objects.create(empresa=e, usuario=self.caio, canal="TELEFONE", resultado="CONVERSA_ORCAMENTO",
                                 valor_orcamento=Decimal("500"), data_hora=quando(18))
        registrado_na_hora()
        caio = next(x for x in gm.placar(self.ini, self.fim, hoje=date(2026, 11, 19)) if x["nome"] == "Caio")
        self.assertEqual((caio["pontos"], caio["metricas"]["orcamentos"]), (3, 0))  # vale só a conversa

    def test_etapa_maior(self):
        self.assertEqual(gm.etapa_maior({"procuradas", "conversas", "cnpjs"}), "cnpjs")  # 3 = 3: a mais avançada
        self.assertEqual(gm.etapa_maior({"procuradas", "orcamentos", "vendas_nf"}), "vendas_nf")

    def test_sequencia_de_dias_uteis(self):
        # 16 (seg), 17 (ter), 18 (qua); hoje 19 (qui) ainda sem contato não quebra
        self.assertEqual(gm.sequencia({date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)},
                                      hoje=date(2026, 11, 19)), 3)
        # sexta 13 + segunda 16: o fim de semana não quebra
        self.assertEqual(gm.sequencia({date(2026, 11, 13), date(2026, 11, 16)}, hoje=date(2026, 11, 16)), 2)
        self.assertEqual(gm.sequencia({date(2026, 11, 10)}, hoje=date(2026, 11, 19)), 0)

    def test_visao_da_pessoa_alvo_e_conquistas(self):
        v = gm.visao(self.ini, self.fim, self.ana, hoje=date(2026, 11, 19))
        self.assertEqual(v["eu"]["posicao"], 2)
        self.assertEqual(v["alvo"]["falta"], 26)  # 30 - 5 + 1
        self.assertIn("Bia", v["alvo"]["texto"])
        conq = {c["codigo"]: c for c in v["eu"]["conquistas"]}
        self.assertTrue(conq["largada"]["ganhou"])
        self.assertFalse(conq["primeira_venda"]["ganhou"])
        self.assertEqual((conq["orcamentista"]["atual"], conq["orcamentista"]["meta"]), (1, 5))
        lider = gm.visao(self.ini, self.fim, self.bia, hoje=date(2026, 11, 19))
        self.assertIn("lidera", lider["alvo"]["texto"])
        self.assertTrue({c["codigo"]: c for c in lider["eu"]["conquistas"]}["primeira_venda"]["ganhou"])

    def test_tela_mostra_disputa_e_simulacao_nao_grava(self):
        self.client.force_login(self.ana)
        r = self.client.get(reverse("prospeccao:minha_performance"), {"mes": "2026-11"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Disputa do mês")
        self.assertContains(r, "Ranking da equipe")
        self.assertContains(r, "para passar Bia")
        antes = (Abordagem.objects.count(), NotaFiscal.objects.count(), get_user_model().objects.count())
        r = self.client.get(reverse("prospeccao:minha_performance"), {"mes": "2026-11", "simulacao": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Análise de teste")
        self.assertContains(r, "Ranking da equipe")
        self.assertEqual(antes, (Abordagem.objects.count(), NotaFiscal.objects.count(), get_user_model().objects.count()))
