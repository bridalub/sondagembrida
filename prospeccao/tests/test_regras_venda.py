"""Regras da venda na captação/disputa: inativo há mais de 90 dias e só o mês da primeira compra soma."""
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from carteira.models import Abordagem, ConfiguracaoAnalise, Empresa, NotaFiscal
from carteira.services.abordagens import situacao_cliente
from prospeccao.models import Oportunidade
from prospeccao.services import bi_captacao as bi
from prospeccao.services import gamificacao as gm

CNPJ = "11222333000181"


def quando(ano, mes, dia):
    return timezone.make_aware(datetime(ano, mes, dia, 10, 0))


class RegrasVendaTests(TestCase):
    def setUp(self):
        self.ana = get_user_model().objects.create_user("ana", password="x", first_name="Ana")
        self.lead = Empresa.objects.create(nome="Lead", origem=Empresa.Origem.DESCOBERTA, cnpj=CNPJ)
        Oportunidade.objects.create(empresa=self.lead, tipo=Oportunidade.Tipo.NOVA_EXTERNA, responsavel=self.ana)

    def _contato(self, dia):
        a = Abordagem.objects.create(empresa=self.lead, usuario=self.ana, canal="TELEFONE",
                                     resultado="CONVERSA_INTERESSE", cnpj_confirmado=CNPJ, data_hora=dia)
        Abordagem.objects.filter(pk=a.pk).update(criado_em=dia)

    def _nf(self, n, d):
        NotaFiscal.objects.create(id_origem=f"NF-{n}", tipo="VENDA", empresa=self.lead, data_emissao=d,
                                  valor_total=Decimal("100"))

    def _pontos(self, mes):
        ini, fim, _ = bi.periodo_do_mes(mes)
        return {x["nome"]: (x["pontos"], x["metricas"]["vendas_nf"]) for x in gm.placar(ini, fim, hoje=fim)}["Ana"]

    def test_padrao_de_inatividade_e_90_dias(self):
        self.assertEqual(ConfiguracaoAnalise.obter().dias_sem_compra_captacao, 90)
        conta = Empresa.objects.create(nome="Conta SF", cnpj="45997418000153", salesforce_id="001X",
                                       ultima_compra_cadastro=date(2026, 7, 1))
        S = Abordagem.SituacaoCliente
        self.assertEqual(situacao_cliente(conta.cnpj, date(2026, 11, 1))[0], S.INATIVO)   # 123 dias
        self.assertEqual(situacao_cliente(conta.cnpj, date(2026, 9, 1))[0], S.ATIVO)      # 62 dias

    def test_so_o_mes_da_primeira_compra_soma(self):
        self._contato(quando(2026, 10, 20))
        self._nf(1, date(2026, 10, 25))   # 1ª compra: outubro
        self._nf(2, date(2026, 10, 25))   # mesmo dia = mesma venda
        self._nf(3, date(2026, 10, 28))   # outro dia do mesmo mês = mais 30
        self._nf(4, date(2026, 11, 5))    # mês seguinte: não soma mais
        self.assertEqual(self._pontos("2026-10"), (60, 2))
        self.assertEqual(self._pontos("2026-11"), (0, 0))
        ini, fim, _ = bi.periodo_do_mes("2026-11")
        self.assertEqual(bi.vendas_por_nf(bi.empresas_prospeccao(), ini, fim)["notas"], 0)

    def test_compra_antes_do_contato_nao_define_o_mes(self):
        self._nf(1, date(2026, 9, 10))    # antes do contato: não é captação nem conta como 1ª compra
        self._contato(quando(2026, 10, 2))
        self._nf(2, date(2026, 10, 15))
        self.assertEqual(self._pontos("2026-10"), (30, 1))

    def test_situacao_gravada_e_reavaliada_pelo_limite_atual(self):
        """Contato gravado como ATIVO com o limite antigo (2 anos) passa a contar: última compra há mais de 90 dias."""
        self._contato(quando(2026, 11, 3))
        Abordagem.objects.update(cliente_situacao=Abordagem.SituacaoCliente.ATIVO, cliente_ultima_compra=date(2026, 6, 1))
        self._nf(1, date(2026, 11, 10))
        self.assertEqual(self._pontos("2026-11"), (30, 1))
        Abordagem.objects.update(cliente_ultima_compra=date(2026, 10, 1))  # comprou há 33 dias: ativo, não conta
        self.assertEqual(self._pontos("2026-11"), (3, 0))
