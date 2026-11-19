"""Regras de identificação pela base da Receita (casos reais de 19/11/2026, dados simulados)."""
from types import SimpleNamespace as N

from django.test import TestCase

from carteira.models import Empresa
from prospeccao.services.descoberta_google import _rua, palavras_proprias
from prospeccao.services.identificacao_cnpj import sigla_bate


class RegrasNomeTests(TestCase):
    def test_palavras_proprias_sem_ramo_cidade_e_vitrine_na_ordem(self):
        self.assertEqual(palavras_proprias("Dias Bueno Usinagem"), ["DIAS", "BUENO"])
        self.assertEqual(palavras_proprias("Opala Terraplanagem Mauá"), ["OPALA"])
        self.assertEqual(palavras_proprias("Jadlog São Caetano"), ["JADLOG"])
        self.assertEqual(palavras_proprias("GMV Auto Peças (Loja virtual)"), ["GMV"])
        self.assertEqual(palavras_proprias("ARC Engenharia Construções"), ["ARC"])

    def test_rua_abreviada_igual_a_rua_por_extenso(self):
        self.assertEqual(_rua("R. José de França Dias, 63 - São José, São Caetano do Sul"),
                         _rua("RUA JOSE DE FRANCA DIAS 63"))
        self.assertEqual(_rua("Av. Goiás, 1000"), _rua("AVENIDA GOIAS, 1000"))

    def test_sigla_reconhece_e_nao_confunde_palavra_do_ramo(self):
        def bate(nome, razao):
            return sigla_bate(N(nome=nome), N(razao_social=razao, nome_fantasia=""))
        self.assertTrue(bate("G&M Transportes", "GRELUK E MENEZES TRANSPORTES RODOVIARIOS LTDA"))
        self.assertTrue(bate("W L O Indústria Mecânica", "WLO MANUTENCAO DE MAQUINAS LTDA"))
        self.assertTrue(bate("Auto Peças Fácil", "APF IMPORTS LTDA"))
        self.assertFalse(bate("RIBEIRÃO AUTO PEÇAS", "GMR COMERCIO DE PECAS E ACESSORIOS LTDA"))
        self.assertFalse(bate("Transportes Silva", "SP LOGISTICA LTDA"))


class IdentificacaoPelaBaseTests(TestCase):
    """O telefone do Google pode ser de outra empresa: o candidato do NOME também é avaliado."""

    def setUp(self):
        from carteira.services.cnpj import calcular_dv
        from receita.models import CargaReceita, EstabelecimentoReceita
        self.carga = CargaReceita.objects.create(competencia="2026-11", status="CONCLUIDA", ativa=True)

        def estab(base, razao, tel=""):
            c = base + calcular_dv(base)
            EstabelecimentoReceita.objects.create(
                carga=self.carga, arquivo="t", cnpj=c, cnpj_basico=c[:8], razao_social=razao, situacao_codigo="02",
                situacao="ATIVA", uf="SP", municipio_codigo="7077", municipio="SAO CAETANO DO SUL",
                logradouro="RUA A", telefone1=tel)
            return c
        self.vizinho = estab("433119380001", "OUTRA EMPRESA DO ESCRITORIO LTDA", tel="11968289068")
        self.certo = estab("062360830001", "MAZETO CONSTRUTORA LTDA")
        self.e = Empresa.objects.create(nome="Mazeto Construtora", cidade="São Caetano do Sul", uf="SP",
                                        origem=Empresa.Origem.DESCOBERTA)
        from carteira.models import CanalEmpresa
        CanalEmpresa.objects.create(empresa=self.e, tipo="TELEFONE", valor="11968289068",
                                    valor_normalizado="11968289068", origem_canal="PUBLICACAO",
                                    confianca_associacao="MEDIA")

    def test_unidades_lista_matriz_e_filiais_da_mesma_raiz(self):
        from carteira.services.cnpj import calcular_dv
        from receita.models import EstabelecimentoReceita
        from prospeccao.services.unidades import unidades
        raiz = self.certo[:8]
        matriz = raiz + "0001" + calcular_dv(raiz + "0001")
        EstabelecimentoReceita.objects.filter(cnpj=self.certo).delete()
        filial = raiz + "0002" + calcular_dv(raiz + "0002")
        for c, mun, tel in ((matriz, "SAO PAULO", "1133334444"), (filial, "SAO CAETANO DO SUL", "11968289068")):
            EstabelecimentoReceita.objects.create(
                carga=self.carga, arquivo="t", cnpj=c, cnpj_basico=raiz, razao_social="MAZETO CONSTRUTORA LTDA",
                situacao_codigo="02", situacao="ATIVA", uf="SP", municipio=mun, telefone1=tel)
        self.e.cnpj = filial
        self.e.save()
        us = unidades(self.e)
        self.assertEqual([u["cnpj"] for u in us], [filial, matriz])  # principal primeiro
        self.assertEqual([u["tipo"] for u in us], ["Filial", "Matriz"])
        self.assertEqual(us[1]["telefones"], ["(11) 3333-4444"])
        self.assertEqual(unidades(Empresa(nome="Sem CNPJ")), [])

    def test_nome_vence_telefone_de_outra_empresa(self):
        from unittest import mock
        from prospeccao.services.identificacao_cnpj import identificar_pela_base
        with mock.patch("receita.importador.tabela", return_value={"7077": "SAO CAETANO DO SUL"}):
            info = identificar_pela_base(self.e, None, None, permitir_consulta_complementar=False)
        self.e.refresh_from_db()
        self.assertIsNotNone(info)
        self.assertEqual(self.e.cnpj, self.certo)
