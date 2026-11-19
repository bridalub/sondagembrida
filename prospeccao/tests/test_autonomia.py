"""Autonomia da rodada: a identificação não depende de IA nem de leitura de páginas, a meta conta só lead completo e a
pesquisa continua sozinha quando para por limite interno com trabalho útil pendente. Dados isolados e simulados."""
from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from carteira.models import CanalEmpresa, Empresa
from prospeccao.conectores.base import ContextoExecucao, OrcamentoEsgotado
from prospeccao.models import ConsultaWeb, Execucao, FrenteComercial, Pesquisa, SelecaoPesquisa
from prospeccao.services import descoberta_google as dg
from prospeccao.services import meta
from prospeccao.services import pipeline
from prospeccao.tests.test_fluxo_identificacao import dados_de, doc

CNPJ = doc("554443330001")


def orc_falso():
    return SimpleNamespace(ultimo_registro=None, busca_bloqueada="", motivos_bloqueio=set(), ia_chamadas=0)


class _Base(TestCase):
    def setUp(self):
        self.frente = FrenteComercial.objects.create(nome="Frota", descricao="transporte")
        self.p = Pesquisa.objects.create(nome="Autonomia", modo="PLANEJADA", cidades="Mauá", uf="SP",
                                         quantidade_desejada=30, max_buscas_web_empresa=2)
        self.p.frentes.add(self.frente)
        self.hoje = timezone.localdate()

    def empresa(self, nome, **extra):
        return Empresa.objects.create(nome=nome, origem=Empresa.Origem.DESCOBERTA, cidade="Mauá", uf="SP", **extra)

    def telefone(self, e, valor="1145550000"):
        CanalEmpresa.objects.create(
            empresa=e, tipo=CanalEmpresa.Tipo.TELEFONE, valor=valor, valor_normalizado=valor,
            origem_canal=CanalEmpresa.Origem.PUBLICACAO, confianca_associacao=CanalEmpresa.Confianca.MEDIA)

    def sel(self, e, cnpj="SEM1", **extra):
        dados = dict(pesquisa=self.p, frente=self.frente, cnpj=cnpj, empresa=e, pontuacao=5,
                     fila=SelecaoPesquisa.Fila.BUSCAR_CONTATO, aderencia=SelecaoPesquisa.Aderencia.PROVAVEL,
                     aderencia_motivos=["atividade da frente"], motivos_selecao=["Google local"])
        dados.update(extra)
        return SelecaoPesquisa.objects.create(**dados)


class IdentificacaoSemIATests(_Base):
    def _busca_com_cnpj(self, e, chamadas):
        def busca_falsa(conector, texto, **kwargs):
            chamadas.append(texto)
            ConsultaWeb.objects.create(pesquisa=self.p, empresa=e, finalidade="COMPLEMENTAR",
                                       provedor="google_serpapi", consulta=texto, status="OK", resultados=[])
            return [{"titulo": "Transportes Horizonte", "trecho": f"Transportes Horizonte CNPJ {CNPJ} Mauá"}]
        return busca_falsa

    def test_ia_esgotada_nao_impede_busca_dirigida_de_cnpj(self):
        e = self.empresa("Transportes Horizonte")
        self.telefone(e)  # telefone encontrado NÃO encerra a identificação
        s = self.sel(e)
        cadastro = dados_de(CNPJ, "Transportes Horizonte Ltda", "Transportes Horizonte", "Mauá")
        carga = SimpleNamespace(competencia=f"{self.hoje.year}-{self.hoje.month:02d}")
        chamadas = []
        with mock.patch("prospeccao.services.orcamento.buscar", side_effect=self._busca_com_cnpj(e, chamadas)), \
                mock.patch("prospeccao.conectores.receita.consultar_local", return_value=(cadastro, carga)), \
                mock.patch("prospeccao.services.ia.status", return_value=(False, "limite de chamadas de IA")):
            r = dg.investigar_dirigido(e, s, self.p, object(), None, orc_falso(), ContextoExecucao(), max_buscas=2)
        e.refresh_from_db()
        self.assertEqual(len(chamadas), 1)
        self.assertIn("CNPJ", chamadas[0])
        self.assertEqual(e.cnpj, CNPJ)
        self.assertEqual(e.situacao_cadastral, "ATIVA")
        self.assertEqual(r["identificacao"]["acao"], "cnpj_vinculado")

    def test_limite_de_paginas_nao_derruba_a_identificacao(self):
        e = self.empresa("Transportes Horizonte")
        s = self.sel(e)
        cadastro = dados_de(CNPJ, "Transportes Horizonte Ltda", "Transportes Horizonte", "Mauá")
        chamadas = []
        with mock.patch("prospeccao.services.orcamento.buscar", side_effect=self._busca_com_cnpj(e, chamadas)), \
                mock.patch("prospeccao.conectores.receita.consultar_local", return_value=(cadastro, None)), \
                mock.patch("prospeccao.services.ia.status", return_value=(False, "")), \
                mock.patch.object(dg, "_urls_conhecidas", return_value=["https://horizonte.example/"]), \
                mock.patch.object(dg.ct, "_tentar_sites",
                                  side_effect=OrcamentoEsgotado("limite de consultas de 'site_publico' (160)")):
            r = dg.investigar_dirigido(e, s, self.p, object(), object(), orc_falso(), ContextoExecucao(),
                                       max_buscas=2)
        e.refresh_from_db()
        self.assertIn("limite", r["leitura"])
        self.assertEqual(len(chamadas), 1)
        self.assertEqual(e.cnpj, CNPJ)

    def test_variacoes_da_busca_dirigida_ate_o_limite_da_empresa(self):
        e = self.empresa("Transportes Sem Rastro")
        self.telefone(e, "1149990000")
        chamadas = []

        def vazia(conector, texto, **kwargs):
            chamadas.append(texto)
            ConsultaWeb.objects.create(empresa=e, finalidade="COMPLEMENTAR", provedor="google_serpapi",
                                       consulta=texto, status="OK", resultados=[])
            return []
        from prospeccao.services.identificacao_cnpj import completar_identificacao
        with mock.patch("prospeccao.services.orcamento.buscar", side_effect=vazia):
            info = completar_identificacao(e, None, busca=object(), orc=orc_falso(), max_buscas=2)
        self.assertEqual(len(chamadas), 2)
        self.assertIn("1149990000", chamadas[1])
        self.assertEqual(info["acao"], "pendente_busca")
        self.assertEqual(info["buscas_novas"], 2)


class MetaLeadCompletoTests(_Base):
    def test_candidata_sem_cnpj_nao_conta_na_meta(self):
        sem = self.empresa("Candidata Sem Cnpj")
        self.telefone(sem, "1141110000")
        self.sel(sem, cnpj="SEMX")
        ok = self.empresa("Lead Completo", cnpj=doc("665554440001"), situacao_cadastral="ATIVA",
                          situacao_cadastral_referencia=date.today())
        self.telefone(ok, "1142220000")
        self.sel(ok, cnpj=ok.cnpj)
        sem_contato = self.empresa("Ativa Sem Contato", cnpj=doc("665554440002"), situacao_cadastral="ATIVA",
                                   situacao_cadastral_referencia=date.today())
        self.sel(sem_contato, cnpj=sem_contato.cnpj)
        est = SimpleNamespace(sync=(True, timezone.now(), 24), conferencia=None)
        painel = meta.resumo(self.p, est)
        self.assertEqual(painel["novos_encontrados"], 3)
        self.assertEqual(painel["identificadas"], 2)
        self.assertEqual(painel["ativas_cruzadas"], 2)
        self.assertEqual(painel["leads_completos"], 1)
        self.assertEqual(painel["novos_validados"], 1)  # é este número que encerra a meta
        self.assertEqual(painel["pendentes_identificacao"], 1)
        self.assertEqual(painel["ativas_sem_contato"], 1)


class ContinuacaoAutomaticaTests(_Base):
    def execucao(self, motivo="tempo", **extra):
        return Execucao.objects.create(pesquisa=self.p, status=Execucao.Status.PARCIAL,
                                       progresso={"motivo": motivo, "painel": {"meta": 30, "leads_completos": 3}},
                                       **extra)

    def test_continua_quando_para_por_limite_com_pendencia_e_avanco(self):
        e = self.empresa("Pendente De Cnpj")
        self.telefone(e)
        self.sel(e)
        ex = self.execucao(parametros={"permitir_continuacao_em_teste": True})
        ConsultaWeb.objects.create(pesquisa=self.p, execucao=ex, finalidade="DESCOBERTA", provedor="google_serpapi",
                                   consulta="transportadora Mauá", status="OK", resultados=[])
        est = SimpleNamespace(motivo_fim="tempo")
        continuar, motivo = pipeline.avaliar_continuacao(ex, est, Execucao.Status.PARCIAL)
        self.assertTrue(continuar, motivo)
        pipeline.continuar_automaticamente(ex, est, Execucao.Status.PARCIAL)
        nova = Execucao.objects.exclude(pk=ex.pk).get(pesquisa=self.p)
        self.assertEqual(nova.status, Execucao.Status.PENDENTE)
        self.assertEqual(nova.parametros["retomada_de"], ex.pk)
        self.assertEqual(nova.parametros["continuacao"], 1)
        ex.refresh_from_db()
        self.assertIn("enfileirada automaticamente", ex.progresso["continuacao"])

    def test_sem_avanco_nao_repete_o_ciclo(self):
        e = self.empresa("Pendente De Cnpj")
        self.sel(e)
        ex = self.execucao()
        continuar, motivo = pipeline.avaliar_continuacao(ex, SimpleNamespace(), Execucao.Status.PARCIAL)
        self.assertFalse(continuar)
        self.assertIn("não avançou", motivo)

    def test_cancelamento_e_limite_de_continuacoes_param(self):
        e = self.empresa("Pendente De Cnpj")
        self.sel(e)
        ex = self.execucao(motivo="cancelamento")
        self.assertFalse(pipeline.avaliar_continuacao(ex, SimpleNamespace(), Execucao.Status.PARCIAL)[0])
        ex2 = self.execucao(parametros={"continuacao": pipeline.MAX_CONTINUACOES})
        self.assertFalse(pipeline.avaliar_continuacao(ex2, SimpleNamespace(), Execucao.Status.PARCIAL)[0])

    def test_buscas_possiveis_esgotadas_param(self):
        e = self.empresa("Pendente De Cnpj")
        self.sel(e)
        ex = self.execucao()
        ConsultaWeb.objects.create(pesquisa=self.p, execucao=ex, empresa=e, finalidade="COMPLEMENTAR",
                                   provedor="google_serpapi", consulta=f'"{e.nome}" "Mauá SP" CNPJ', status="OK")
        continuar, motivo = pipeline.avaliar_continuacao(ex, SimpleNamespace(), Execucao.Status.PARCIAL)
        self.assertFalse(continuar)
        self.assertIn("buscas dirigidas possíveis já foram feitas", motivo)


class RegrasCnpjTests(_Base):
    def test_telefone_nome_e_municipio_vinculam(self):
        from prospeccao.services.identificacao_cnpj import avaliar_associacao
        e = self.empresa("Terraplenagem Alzira Franco")
        self.telefone(e, "1144726154")
        d = dados_de(doc("031407150001"), "Terraplenagem Alzira Franco Ltda", "", "Mauá", telefones=["(11) 4472-6154"])
        self.assertEqual(avaliar_associacao(e, d)[0], "adotar")

    def test_nome_exato_e_municipio_vinculam_mesmo_com_palavras_do_ramo(self):
        from prospeccao.services.identificacao_cnpj import avaliar_associacao
        e = self.empresa("Top Locações e Terraplenagem")
        d = dados_de(doc("563942660001"), "TOP LOCACOES E TERRAPLENAGEM LTDA", "", "Mauá")
        self.assertEqual(avaliar_associacao(e, d)[0], "adotar")

    def test_cnpj_de_diretorio_em_outra_cidade_e_rejeitado(self):
        from prospeccao.services.identificacao_cnpj import avaliar_associacao
        e = self.empresa("Sperine Usinagem")
        d = dados_de(doc("045788930001"), "Guia Empresas Online Ltda", "", "São Paulo")
        self.assertEqual(avaliar_associacao(e, d)[0], "rejeitar")

    def test_mesmo_nome_em_outra_cidade_vincula_com_aviso_e_preserva_local(self):
        from prospeccao.services import dimensoes as dm
        from prospeccao.services.identificacao_cnpj import resolver_candidato
        e = self.empresa("Pro Ares Logística e Transporte", endereco="Av. Local, 10 - Mauá - SP")
        s = self.sel(e)
        c = doc("172252420001")
        d = dados_de(c, "Palt Logistica e Transporte Ltda", "Pro Ares Logistica", "São Paulo")
        carga = SimpleNamespace(competencia=f"{self.hoje.year}-{self.hoje.month:02d}")
        with mock.patch("prospeccao.services.identificacao_cnpj.filial_local", return_value=(None, None)), \
                mock.patch("prospeccao.conectores.receita.consultar_local", return_value=(d, carga)):
            info = resolver_candidato(e, s, c, d, carga, ContextoExecucao(), "teste")
        e.refresh_from_db()
        self.assertEqual(info["acao"], "cnpj_outra_cidade")
        self.assertEqual(e.cnpj, c)
        self.assertEqual(e.cidade, "Mauá")
        self.assertTrue(e.dados_coletados.filter(campo="cnpj_outra_cidade").exists())
        self.assertEqual(dm.classificacao_identificacao(e)["codigo"], "ativo_outra_cidade")
        self.assertIn("outra cidade", dm.classificacao_identificacao(e)["rotulo"])

    def test_outra_cidade_com_raiz_na_carteira_vira_conhecida(self):
        from prospeccao.services.identificacao_cnpj import resolver_candidato
        c = doc("998877660001")
        Empresa.objects.create(nome="MATRIZ CLIENTE LTDA", cnpj=doc("998877660002"),
                               salesforce_id="001CLIENTE", origem=Empresa.Origem.SALESFORCE, cidade="São Paulo", uf="SP")
        e = self.empresa("Matriz Cliente")
        s = self.sel(e)
        d = dados_de(c, "Matriz Cliente Ltda", "", "São Paulo")
        with mock.patch("prospeccao.services.identificacao_cnpj.filial_local", return_value=(None, None)):
            info = resolver_candidato(e, s, c, d, None, ContextoExecucao(), "teste")
        s.refresh_from_db()
        self.assertEqual(info["acao"], "conhecida_salesforce")
        self.assertEqual(s.fila, SelecaoPesquisa.Fila.CONHECIDA)


class MotivoFimTests(_Base):
    def test_cota_de_descoberta_com_tempo_esgotado_continua(self):
        e = self.empresa("Pendente De Cnpj")
        self.telefone(e)
        self.sel(e)
        ex = Execucao.objects.create(pesquisa=self.p, status=Execucao.Status.PARCIAL,
                                     progresso={"painel": {"meta": 25, "novos_validados": 2, "leads_completos": 2}},
                                     limites_atingidos=["tempo máximo da execução (900s)"])
        ConsultaWeb.objects.create(pesquisa=self.p, execucao=ex, finalidade="DESCOBERTA", provedor="google_serpapi",
                                   consulta="x", status="OK")
        est = SimpleNamespace(motivo_fim="cota_descoberta", limites=["tempo máximo da execução (900s)"])
        meta.definir_motivo(ex, est, Execucao.Status.PARCIAL)
        self.assertEqual(ex.progresso["motivo"], "tempo")
        self.assertTrue(pipeline.avaliar_continuacao(ex, est, Execucao.Status.PARCIAL)[0])

    def test_meta_falsa_nao_bloqueia(self):
        e = self.empresa("Pendente De Cnpj")
        self.sel(e)
        ex = Execucao.objects.create(pesquisa=self.p, status=Execucao.Status.PARCIAL,
                                     progresso={"painel": {"meta": 25, "novos_validados": 2, "leads_completos": 2}})
        est = SimpleNamespace(motivo_fim="meta", limites=[])
        meta.definir_motivo(ex, est, Execucao.Status.PARCIAL)
        self.assertNotEqual(ex.progresso["motivo"], "meta")


class TelefoneAntigoTests(_Base):
    def test_celular_sem_nono_digito_na_receita_bate(self):
        from prospeccao.services.identificacao_cnpj import avaliar_associacao
        e = self.empresa("Top Locações e Terraplenagem", endereco="Rua Nova, 5 - Mauá - SP")
        self.telefone(e, "11962501205")
        d = dados_de(doc("563942660001"), "TOP LOCACOES E TERRAPLENAGEM LTDA", "", "Mauá",
                     logradouro="Rua Antiga", telefones=["1162501205"])
        decisao, motivo = avaliar_associacao(e, d)
        self.assertEqual(decisao, "adotar")
        self.assertIn("mudança de endereço", motivo)


class PendentesPrimeiroTests(_Base):
    def test_uma_busca_por_empresa_antes_das_variacoes_e_sem_busca_primeiro(self):
        ja = self.empresa("Ja Buscada")
        self.sel(ja, cnpj="J1")
        ConsultaWeb.objects.create(pesquisa=self.p, empresa=ja, finalidade="COMPLEMENTAR",
                                   provedor="google_serpapi", consulta='"Ja Buscada" "Mauá" CNPJ', status="OK")
        novas = [self.empresa(f"Nova Pendente {i}") for i in range(3)]
        for i, e in enumerate(novas):
            self.sel(e, cnpj=f"N{i}")
        ordem = [s.empresa.nome for s in dg.pendentes_identificacao(self.p)]
        self.assertEqual(ordem[-1], "Ja Buscada")  # quem nunca teve busca dirigida vem antes
        chamadas = []

        def falso(empresa, sel, pesquisa, busca, leitor, orc, ctx, max_buscas=2, **kw):
            chamadas.append((empresa.nome, max_buscas))
            return {"leitura": {}}
        est = SimpleNamespace()
        ex = Execucao.objects.create(pesquisa=self.p, status=Execucao.Status.EXECUTANDO)
        with mock.patch.object(dg, "investigar_dirigido", side_effect=falso):
            n = dg.identificar_pendentes_primeiro(ex, ContextoExecucao(), est, object(), None, orc_falso(), self.p,
                                                  minimo=1)
        primeira = chamadas[:4]
        self.assertEqual({m for _, m in primeira}, {1})  # 1ª passada: uma busca por empresa
        self.assertEqual([nm for nm, _ in primeira][-1], "Ja Buscada")
        self.assertEqual({m for _, m in chamadas[4:]}, {self.p.max_buscas_web_empresa})  # 2ª: variações
        self.assertEqual(n, 8)


class BaseReceitaTests(_Base):
    def _carga(self):
        from receita.models import CargaReceita, EstabelecimentoReceita
        carga = CargaReceita.objects.create(competencia="2026-11", status="CONCLUIDA", ativa=True)
        c = doc("563942660001")
        EstabelecimentoReceita.objects.create(
            carga=carga, arquivo="t", cnpj=c, cnpj_basico=c[:8], razao_social="TOP LOCACOES E TERRAPLENAGEM LTDA",
            nome_fantasia="TOP LOCACOES", situacao_codigo="02", situacao="ATIVA", uf="SP", municipio_codigo="6689",
            municipio="MAUA", logradouro="ANTIGA", telefone1="1162501205")
        return carga, c

    def test_base_receita_identifica_antes_do_google_pelo_telefone_sem_nove(self):
        from prospeccao.services.identificacao_cnpj import completar_identificacao
        carga, c = self._carga()
        e = self.empresa("Top Locações e Terraplenagem")
        self.telefone(e, "11962501205")
        s = self.sel(e)
        d = dados_de(c, "TOP LOCACOES E TERRAPLENAGEM LTDA", "TOP LOCACOES", "Mauá", telefones=["1162501205"])
        google = []
        with mock.patch("prospeccao.conectores.receita.consultar_local", return_value=(d, carga)), \
                mock.patch("prospeccao.services.orcamento.buscar", side_effect=lambda *a, **k: google.append(a) or []):
            info = completar_identificacao(e, s, ContextoExecucao(), busca=object(), orc=orc_falso(), max_buscas=2)
        e.refresh_from_db()
        self.assertEqual(e.cnpj, c)
        self.assertEqual(info["acao"], "cnpj_vinculado")
        self.assertEqual(google, [])  # resolvido pela base, sem gastar a franquia

    def test_nao_e_empresa_sai_da_fila(self):
        e = self.empresa("Ponto de Uber/99 - Centro Ribeirão Pires")
        s = self.sel(e)
        self.assertNotIn(s.pk, [x.pk for x in dg.pendentes_identificacao(self.p)])
        s.refresh_from_db()
        self.assertEqual(s.fila, SelecaoPesquisa.Fila.FORA)
        self.assertTrue(dg.nao_e_empresa("Parada de ônibus Av. Brasil"))
        self.assertFalse(dg.nao_e_empresa("Ponto Certo Transportes"))
        self.assertFalse(dg.nao_e_empresa("99 Freios Auto Peças"))
