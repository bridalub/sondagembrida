from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from carteira.models import (
    Abordagem, ConfiguracaoAnalise, Contato, Empresa, Importacao, IndicadorCompra, ListaAbordagem,
    NotaFiscal, OcorrenciaImportacao, RegistroOrigem, SituacaoCompra, SituacaoContato, StatusCNPJ,
)
from carteira.services import abordagens as ab
from carteira.services import cnpj as cnpj_srv
from carteira.services.importacao import importar_clientes, importar_compras


def cnpj(base12):
    return base12 + cnpj_srv.calcular_dv(base12)


CNPJ_A = cnpj("112223330001")       # matriz
CNPJ_A_FILIAL = cnpj("112223330002")
CNPJ_B = cnpj("445556660001")
CNPJ_ZERO = cnpj("012345670001")    # começa com zero
CNPJ_ALFA = "12ABC34501DE35"         # exemplo oficial do CNPJ alfanumérico


def conta(sf_id, doc, nome=None, **extra):
    r = {
        "attributes": {"type": "Account"}, "Id": sf_id, "CNPJ__c": doc, "Name": nome or f"Empresa {sf_id}",
        "Code__c": f"C{sf_id}", "Segment__c": "Postos", "Metier__c": "Revenda", "BillingCity__c": "São Paulo",
        "BillingState__c": "sp", "CreatedDate": "2026-01-10T12:00:00.000+0000", "IsGrainy__c": False,
        "Seller__r": {"Name": "Vendedor 1"}, "Distributor__r": {"Name": "BRIDA"},
        "ProtheusCustomerSituation__c": "Ativo", "Phone": "",
    }
    r.update(extra)
    return r


def item(inv_id, cliente, emissao, valor, tipo="Saída", cat="Lubrificante"):
    return {
        "attributes": {"type": "InvoiceItem__c"}, "Id": f"it-{inv_id}-{valor}", "EmissionDate__c": emissao,
        "ValorTotalFormula__c": valor, "TypeofProdutct__c": cat,
        "Invoice__r": {"Id": inv_id, "Name": f"NF{inv_id}", "Type__c": tipo, "Customer__r": {"Id": cliente}},
    }


class CNPJTests(TestCase):
    def test_normaliza_e_valida(self):
        self.assertEqual(cnpj_srv.analisar("11.222.333/0001-81").cnpj, "11222333000181")
        self.assertEqual(cnpj_srv.analisar("12.abc.345/01de-35").cnpj, CNPJ_ALFA)
        self.assertEqual(cnpj_srv.analisar("11222333000182").status, StatusCNPJ.INVALIDO)
        self.assertEqual(cnpj_srv.analisar("").status, StatusCNPJ.AUSENTE)
        self.assertEqual(cnpj_srv.analisar("529.982.247-25").status, StatusCNPJ.CPF)

    def test_preserva_e_restaura_zero_a_esquerda(self):
        self.assertEqual(cnpj_srv.analisar(CNPJ_ZERO).cnpj, CNPJ_ZERO)
        r = cnpj_srv.analisar(CNPJ_ZERO.lstrip("0"))  # campo numérico que perdeu o zero
        self.assertEqual(r.cnpj, CNPJ_ZERO)
        self.assertTrue(r.aviso)


class ImportacaoClientesTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ana", password="x")

    def test_importacao_repetida_nao_duplica(self):
        base = [conta("001A", CNPJ_A), conta("001B", CNPJ_B), conta("001C", "")]
        imp1 = importar_clientes(base)
        imp2 = importar_clientes(base)
        self.assertEqual(Empresa.objects.count(), 3)
        self.assertEqual((imp1.criados, imp1.atualizados), (3, 0))
        self.assertEqual((imp2.criados, imp2.atualizados, imp2.inalterados), (0, 0, 3))
        self.assertEqual(Empresa.objects.get(cnpj=CNPJ_A).uf, "SP")

    def test_cnpj_alfanumerico_e_zero_a_esquerda_como_texto(self):
        importar_clientes([conta("1", CNPJ_ALFA), conta("2", CNPJ_ZERO)])
        self.assertTrue(Empresa.objects.filter(cnpj=CNPJ_ALFA).exists())
        self.assertEqual(Empresa.objects.get(salesforce_id="2").cnpj, CNPJ_ZERO)

    def test_matriz_e_filial_sao_empresas_distintas(self):
        importar_clientes([conta("1", CNPJ_A), conta("2", CNPJ_A_FILIAL)])
        self.assertEqual(Empresa.objects.count(), 2)
        self.assertEqual(Empresa.objects.get(cnpj=CNPJ_A).tipo_estabelecimento, "Matriz")
        self.assertEqual(Empresa.objects.get(cnpj=CNPJ_A_FILIAL).tipo_estabelecimento, "Filial")

    def test_campo_vazio_nao_apaga_e_historico_preservado(self):
        importar_clientes([conta("1", CNPJ_A, Segment__c="Postos", Phone="11 9999-0000")], extras={"telefone": "Phone"})
        empresa = Empresa.objects.get()
        contato = Contato.objects.create(empresa=empresa, nome="João")
        Abordagem.objects.create(empresa=empresa, contato=contato, usuario=self.user, canal="TELEFONE",
                                 resultado="CONVERSA_RETORNAR", data_retorno=date.today() + timedelta(days=3),
                                 observacoes="Ligar de novo")
        ab.marcar_nao_contatar(empresa, self.user, "teste")  # bloqueio também não pode sumir

        imp = importar_clientes([conta("1", CNPJ_A, Segment__c="", Phone=None, Name="Novo Nome")],
                                extras={"telefone": "Phone"})
        empresa.refresh_from_db()
        self.assertEqual(empresa.segmento, "Postos")
        self.assertEqual(empresa.telefone, "11 9999-0000")
        self.assertEqual(empresa.nome, "Novo Nome")
        self.assertTrue(empresa.nao_contatar)
        self.assertEqual(empresa.abordagens.count(), 1)
        self.assertEqual(empresa.contatos.count(), 1)
        self.assertEqual(imp.detalhes["campos_vazios_mantidos"], 3)  # segmento, segmento normalizado, telefone
        self.assertEqual(empresa.ultima_importacao, imp)

    def test_sem_cnpj_nao_associa_por_nome(self):
        importar_clientes([conta("1", "", nome="Auto Posto Central"), conta("2", "123", nome="Auto Posto Central")])
        self.assertEqual(Empresa.objects.count(), 2)
        self.assertEqual(Empresa.objects.filter(requer_revisao=True, cnpj__isnull=True).count(), 2)
        self.assertEqual(OcorrenciaImportacao.objects.filter(tipo="INCONSISTENTE").count(), 2)
        imp = Importacao.objects.latest("iniciado_em")
        self.assertEqual(imp.inconsistentes, 2)

    def test_cnpj_duplicado_no_salesforce_vira_uma_empresa_sinalizada(self):
        importar_clientes([conta("1", CNPJ_A, nome="Principal"), conta("2", CNPJ_A, nome="Duplicada")])
        self.assertEqual(Empresa.objects.count(), 1)
        e = Empresa.objects.get()
        self.assertEqual(e.nome, "Principal")
        self.assertEqual(e.registros_origem.count(), 2)
        self.assertTrue(e.requer_revisao)
        self.assertIn("duplicado", e.motivo_revisao)
        # repetir mantém a mesma situação
        importar_clientes([conta("1", CNPJ_A, nome="Principal"), conta("2", CNPJ_A, nome="Duplicada")])
        self.assertEqual(Empresa.objects.count(), 1)

    def test_rejeita_sem_id(self):
        imp = importar_clientes([conta("", CNPJ_A)])
        self.assertEqual(imp.rejeitados, 1)
        self.assertEqual(Empresa.objects.count(), 0)

    def test_dry_run_nao_grava(self):
        imp = importar_clientes([conta("1", CNPJ_A)], dry_run=True)
        self.assertEqual(imp.criados, 1)
        self.assertEqual(Empresa.objects.count(), 0)
        self.assertEqual(Importacao.objects.count(), 0)


class SituacaoCompraTests(TestCase):
    def setUp(self):
        importar_clientes([conta("A", CNPJ_A), conta("B", CNPJ_B)])

    def sit(self, sf_id):
        return IndicadorCompra.objects.get(empresa__salesforce_id=sf_id).situacao

    def test_sem_sincronizacao_de_compras_e_indisponivel(self):
        self.assertEqual(self.sit("A"), SituacaoCompra.INDISPONIVEL)
        self.assertEqual(self.sit("B"), SituacaoCompra.INDISPONIVEL)

    def test_sem_nota_no_periodo_nao_vira_nunca_comprou(self):
        importar_compras([item("N1", "A", "2026-05-02", 100)], desde=date(2026, 4, 1))
        self.assertEqual(self.sit("A"), SituacaoCompra.COMPROU)
        self.assertEqual(self.sit("B"), SituacaoCompra.SEM_COMPRA_PERIODO)

    def test_nunca_comprou_so_com_cobertura_e_configuracao(self):
        cfg = ConfiguracaoAnalise.obter()
        cfg.nunca_comprou_habilitado = True
        cfg.save()
        # cadastro em 10/01/2026; cobertura a partir de 01/04/2026 não cobre -> continua "sem compra no período"
        importar_compras([], desde=date(2026, 4, 1))
        self.assertEqual(self.sit("B"), SituacaoCompra.SEM_COMPRA_PERIODO)
        # cobertura desde antes do cadastro -> pode afirmar
        importar_compras([], desde=date(2025, 1, 1))
        self.assertEqual(self.sit("B"), SituacaoCompra.NUNCA_COMPROU)

    def test_empresa_manual_sem_vinculo_e_indisponivel(self):
        Empresa.objects.create(nome="Manual", cnpj=cnpj("998887770001"), cnpj_status="VALIDO", salesforce_id=None,
                               origem="MANUAL")
        importar_compras([], desde=date(2026, 4, 1))
        self.assertEqual(IndicadorCompra.objects.get(empresa__nome="Manual").situacao, SituacaoCompra.INDISPONIVEL)

    def test_indicadores_e_rastreabilidade(self):
        itens = [item("N1", "A", "2026-05-02", 100), item("N1", "A", "2026-05-02", 50.5, cat="Graxa"),
                 item("N2", "A", "2026-07-10", 200), item("D1", "A", "2026-07-15", 30, tipo="Entrada")]
        imp = importar_compras(itens, desde=date(2026, 4, 1))
        ind = IndicadorCompra.objects.get(empresa__salesforce_id="A")
        self.assertEqual(ind.situacao, SituacaoCompra.COMPROU)
        self.assertEqual(ind.cobertura_inicio, date(2026, 4, 1))
        self.assertEqual(ind.qtd_compras, 2)  # notas distintas, não itens
        self.assertEqual(float(ind.valor_compras), 350.5)
        self.assertEqual((ind.primeira_compra, ind.ultima_compra), (date(2026, 5, 2), date(2026, 7, 10)))
        self.assertEqual((ind.qtd_devolucoes, float(ind.valor_devolvido)), (1, 30.0))
        self.assertEqual(imp.criados, 3)
        self.assertEqual(NotaFiscal.objects.get(id_origem="N1").qtd_itens, 2)
        # reimportar não duplica notas
        imp2 = importar_compras(itens, desde=date(2026, 4, 1))
        self.assertEqual(NotaFiscal.objects.count(), 3)
        self.assertEqual(imp2.inalterados, 3)

    def test_nota_que_some_da_origem_sai_dos_indicadores_sem_ser_apagada(self):
        importar_compras([item("N1", "A", "2026-05-02", 100)], desde=date(2026, 4, 1))
        importar_compras([], desde=date(2026, 4, 1))
        self.assertEqual(NotaFiscal.objects.count(), 1)
        self.assertFalse(NotaFiscal.objects.get().presente_na_origem)
        self.assertEqual(self.sit("A"), SituacaoCompra.SEM_COMPRA_PERIODO)

    def test_nota_de_conta_ainda_nao_importada_e_vinculada_depois(self):
        importar_compras([item("N9", "Z", "2026-05-02", 10)], desde=date(2026, 4, 1))
        self.assertIsNone(NotaFiscal.objects.get().empresa)
        importar_clientes([conta("Z", cnpj("777888990001"))])
        self.assertEqual(NotaFiscal.objects.get().empresa.salesforce_id, "Z")
        self.assertEqual(self.sit("Z"), SituacaoCompra.COMPROU)


class AbordagemTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ana", password="x")
        importar_clientes([conta("A", CNPJ_A), conta("B", CNPJ_B), conta("C", CNPJ_A_FILIAL)])
        self.a = Empresa.objects.get(salesforce_id="A")
        self.b = Empresa.objects.get(salesforce_id="B")
        self.c = Empresa.objects.get(salesforce_id="C")

    def _novas(self):
        return set(ab.novas_abordagens().values_list("salesforce_id", flat=True))

    def test_varias_pessoas_por_empresa(self):
        Contato.objects.create(empresa=self.a, nome="João", cargo="Comprador")
        Contato.objects.create(empresa=self.a, nome="Maria", cargo="Gerente")
        self.assertEqual(self.a.contatos.count(), 2)

    def test_abordada_sai_da_lista_padrao_e_nao_contatar_tambem(self):
        self.assertEqual(self._novas(), {"A", "B", "C"})
        ab.registrar_abordagem(Abordagem(empresa=self.a, usuario=self.user, canal="TELEFONE", resultado="SEM_RESPOSTA"))
        ab.marcar_nao_contatar(self.c, self.user, "pediu")
        self.assertEqual(self._novas(), {"B"})
        sit = dict(ab.anotar(Empresa.objects.all()).values_list("salesforce_id", "situacao_contato"))
        self.assertEqual(sit["A"], SituacaoContato.TENTATIVA_SEM_RESPOSTA)
        self.assertEqual(sit["C"], SituacaoContato.NAO_CONTATAR)

    def test_diferencia_tentativa_de_conversa(self):
        t = ab.registrar_abordagem(Abordagem(empresa=self.a, usuario=self.user, canal="TELEFONE", resultado="RECADO"))
        c = ab.registrar_abordagem(Abordagem(empresa=self.b, usuario=self.user, canal="WHATSAPP",
                                             resultado="CONVERSA_SEM_INTERESSE"))
        self.assertFalse(t.houve_conversa)
        self.assertTrue(c.houve_conversa)
        sit = dict(ab.anotar(Empresa.objects.all()).values_list("salesforce_id", "situacao_contato"))
        self.assertEqual(sit["B"], SituacaoContato.CONVERSA_REALIZADA)

    def test_retorno_fica_na_fila_e_sobrevive_a_reimportacao(self):
        ab.registrar_abordagem(Abordagem(empresa=self.a, usuario=self.user, canal="TELEFONE",
                                         resultado="CONVERSA_RETORNAR", data_retorno=date.today() + timedelta(days=2)))
        importar_clientes([conta("A", CNPJ_A, Segment__c="Outro"), conta("B", CNPJ_B), conta("C", CNPJ_A_FILIAL)])
        self.assertEqual(list(ab.retornos_pendentes().values_list("empresa__salesforce_id", flat=True)), ["A"])
        self.assertNotIn("A", self._novas())
        sit = ab.anotar(Empresa.objects.filter(pk=self.a.pk)).get().situacao_contato
        self.assertEqual(sit, SituacaoContato.RETORNO_AGENDADO)
        # nova abordagem conclui o retorno pendente
        ab.registrar_abordagem(Abordagem(empresa=self.a, usuario=self.user, canal="TELEFONE",
                                         resultado="CONVERSA_INTERESSE"))
        self.assertEqual(ab.retornos_pendentes().count(), 0)

    def test_pedir_nao_contatar_bloqueia(self):
        ab.registrar_abordagem(Abordagem(empresa=self.b, usuario=self.user, canal="TELEFONE",
                                         resultado="CONVERSA_NAO_CONTATAR"))
        self.b.refresh_from_db()
        self.assertTrue(self.b.nao_contatar)

    def test_lista_nao_inclui_abordadas_nem_conta_como_contato(self):
        ab.marcar_nao_contatar(self.c, self.user, "pediu")
        ab.registrar_abordagem(Abordagem(empresa=self.a, usuario=self.user, canal="EMAIL", resultado="SEM_RESPOSTA"))
        lista = ab.gerar_lista("Teste", Empresa.objects.all(), self.user)  # mesmo passando todas
        self.assertEqual(list(lista.itens.values_list("empresa__salesforce_id", flat=True)), ["B"])
        self.assertIn("B", self._novas())  # estar na lista não é contato
        self.assertEqual(Abordagem.objects.filter(empresa=self.b).count(), 0)


class TelasTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ana", password="x", is_staff=True)
        self.client.force_login(self.user)
        importar_clientes([conta("A", CNPJ_A), conta("B", "")])
        importar_compras([item("N1", "A", "2026-05-02", 100)], desde=date(2026, 4, 1))
        self.a = Empresa.objects.get(salesforce_id="A")

    def test_telas_respondem(self):
        imp = Importacao.objects.first()
        lista = ab.gerar_lista("L", Empresa.objects.all(), self.user)
        for url in [reverse("carteira:visao_geral"), reverse("carteira:empresas"),
                    reverse("carteira:empresas") + "?situacao_compra=COMPROU&uf=SP&tempo_desconhecido=on",
                    reverse("carteira:novas_abordagens"), reverse("carteira:empresa_detalhe", args=[self.a.pk]),
                    reverse("carteira:registrar_abordagem", args=[self.a.pk]), reverse("carteira:retornos"),
                    reverse("carteira:importacoes"), reverse("carteira:importacao_detalhe", args=[imp.pk]),
                    reverse("carteira:revisao"), reverse("carteira:listas"),
                    reverse("carteira:lista_detalhe", args=[lista.pk]), reverse("carteira:lista_csv", args=[lista.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_visualizar_nao_conta_como_contato(self):
        self.client.get(reverse("carteira:empresa_detalhe", args=[self.a.pk]))
        self.assertEqual(Abordagem.objects.count(), 0)
        self.assertTrue(ab.novas_abordagens().filter(pk=self.a.pk).exists())

    def test_registrar_abordagem_pela_tela(self):
        r = self.client.post(reverse("carteira:registrar_abordagem", args=[self.a.pk]), {
            "data_hora": timezone.localtime().strftime("%Y-%m-%dT%H:%M"), "canal": "TELEFONE",
            "resultado": "CONVERSA_RETORNAR", "data_retorno": (date.today() + timedelta(days=1)).isoformat(),
            "proxima_acao": "Enviar proposta", "concluir_pendentes": "on", "sem_cnpj": "on",
        })
        self.assertEqual(r.status_code, 302)
        a = Abordagem.objects.get()
        self.assertEqual(a.usuario, self.user)
        self.assertContains(self.client.get(reverse("carteira:retornos")), self.a.nome)

    def test_filtro_situacao_contato_e_requer_login(self):
        r = self.client.get(reverse("carteira:empresas") + "?situacao_contato=NUNCA_ABORDADA")
        self.assertContains(r, self.a.nome)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("carteira:empresas")).status_code, 302)
