"""Fluxo Google → CNPJ → situação cadastral → Salesforce, com dados isolados."""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from carteira.models import (
    Abordagem, CanalAbordagem, CanalEmpresa, Empresa, Importacao, ResultadoAbordagem,
)
from carteira.services.cnpj import calcular_dv
from prospeccao.conectores.brasilapi import DadosCNPJ
from prospeccao.conectores.busca_web import GoogleSerpApi
from prospeccao.models import ConsultaWeb, Execucao, Fonte, FrenteComercial, Oportunidade, Pesquisa, SelecaoPesquisa, UsoFonte
from prospeccao.services import cruzamento as cz
from prospeccao.services import dimensoes as dm
from prospeccao.services import descoberta_google as dg
from prospeccao.services.identificacao_cnpj import avaliar_associacao, completar_identificacao
from prospeccao.services.limpeza import limpar
from prospeccao.services.selecao import informativos, reencontrada
from prospeccao.views import fila_primeira_abordagem, filtrar_oportunidades, indicadores_pesquisa


def doc(base12):
    return base12 + calcular_dv(base12)


def dados_de(cnpj, razao, fantasia, municipio, situacao="ATIVA", logradouro="Rua das Flores", telefones=None):
    return DadosCNPJ(
        cnpj=cnpj, razao_social=razao, nome_fantasia=fantasia, situacao_cadastral=situacao,
        situacao_cadastral_data="2024-03-01", municipio=municipio, uf="SP", logradouro=logradouro,
        telefones=telefones or [], email="")


class _ApoioFluxo:
    """Apoio dos testes. Não é TestCase, para a regressão não reexecutar o fluxo inteiro."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("vend", password="x")
        self.hoje = timezone.localdate()
        Importacao.objects.create(
            tipo=Importacao.Tipo.CLIENTES, origem="teste", status=Importacao.Status.SUCESSO,
            finalizado_em=timezone.now(), executado_por="teste")

    def empresa(self, nome, cidade="Mauá", **extra):
        return Empresa.objects.create(nome=nome, origem=Empresa.Origem.DESCOBERTA, cidade=cidade, uf="SP", **extra)

    def oportunidade(self, empresa, **extra):
        dados = {"tipo": Oportunidade.Tipo.NOVA_EXTERNA, "status": Oportunidade.Status.LIBERADA}
        dados.update(extra)
        return Oportunidade.objects.create(empresa=empresa, **dados)

    def pesquisa(self, cidades="Mauá\nSanto André\nSão Caetano do Sul"):
        return Pesquisa.objects.create(nome="ABC", cidades=cidades, uf="SP", quantidade_desejada=6)

    def frente(self):
        return FrenteComercial.objects.create(nome="Transportadoras frota", descricao="transporte")


class FluxoIdentificacaoTests(_ApoioFluxo, TestCase):
    def test_sem_cnpj_continua_visivel_e_nao_validada(self):
        e = self.empresa("Oficina sem documento")
        op = self.oportunidade(e, status=Oportunidade.Status.NOVA)
        ident = dm.classificacao_identificacao(e)
        self.assertEqual(ident["codigo"], "nao_identificado")
        self.assertNotIn("inválido", ident["rotulo"])
        lista, qs = filtrar_oportunidades({"etapa": "completar_cnpj"}, self.user)
        self.assertIn(op.pk, list(qs.values_list("pk", flat=True)))
        _, validados = filtrar_oportunidades({"etapa": "validado"}, self.user)
        self.assertNotIn(op.pk, list(validados.values_list("pk", flat=True)))

    def test_telefone_nao_encerra_identificacao(self):
        e = self.empresa("Transportes Novos Ares", endereco="Av. B, 99 - Guarulhos - SP")
        CanalEmpresa.objects.create(
            empresa=e, tipo=CanalEmpresa.Tipo.TELEFONE, valor="(11) 2555-0000", valor_normalizado="1125550000",
            origem_canal=CanalEmpresa.Origem.PUBLICACAO, confianca_associacao=CanalEmpresa.Confianca.ALTA)
        ConsultaWeb.objects.create(
            empresa=e, finalidade="DESCOBERTA", provedor="google_serpapi", consulta="transportadora Mauá SP",
            status="OK", resultados=[{"titulo": "Transportes Novos Ares", "trecho": "telefone (11) 2555-0000",
                                      "tipo": "LOCAL"}])
        info = completar_identificacao(e, None, max_buscas=0)
        e.refresh_from_db()
        self.assertFalse(e.cnpj)
        self.assertEqual(info["acao"], "pendente_sem_busca")
        self.assertEqual(dm.classificacao_identificacao(e)["codigo"], "nao_identificado")
        self.assertTrue(e.dados_coletados.filter(campo="motivo_encerramento_identificacao").exists())

    def test_nome_fantasia_diferente_da_razao_vincula(self):
        cnpj = doc("112223330001")
        e = self.empresa("Oficina do Zeca")
        carga = SimpleNamespace(competencia=f"{self.hoje.year}-{self.hoje.month:02d}")
        cadastro = dados_de(cnpj, "Zeca Comercio de Pecas Ltda", "Oficina do Zeca", "Mauá")
        with mock.patch("prospeccao.conectores.receita.consultar_local", return_value=(cadastro, carga)):
            decisao, _ = avaliar_associacao(e, cadastro)
            self.assertEqual(decisao, "adotar")
            ConsultaWeb.objects.create(
                empresa=e, finalidade="DESCOBERTA", provedor="google_serpapi", consulta="autopeças Mauá SP",
                status="OK", resultados=[{"titulo": "Oficina do Zeca", "trecho": f"CNPJ {cnpj}", "tipo": "ORGANICO"}])
            info = completar_identificacao(e, None, max_buscas=0)
        e.refresh_from_db()
        self.assertEqual(info["acao"], "cnpj_vinculado")
        self.assertEqual(e.cnpj, cnpj)
        self.assertEqual(e.situacao_cadastral, "ATIVA")
        self.assertEqual(e.situacao_cadastral_data, date(2024, 3, 1))
        self.assertEqual(e.situacao_cadastral_referencia, date(self.hoje.year, self.hoje.month, 1))
        self.assertNotEqual(e.situacao_cadastral_data, e.situacao_cadastral_referencia)

    def test_cnpj_de_outra_cidade_com_mesmo_nome_vincula_com_aviso(self):
        # decisão do gestor (18/11/2026): a pesquisa achou a empresa aqui; o CNPJ do mesmo nome em outra cidade
        # é registrado como "CNPJ localizado em outra cidade", sem sumir como não identificada
        cnpj = doc("112223330002")
        e = self.empresa("Oficina do Zeca", endereco="Rua das Flores, 10, Mauá - SP")
        cadastro = dados_de(cnpj, "Oficina do Zeca Ltda", "Oficina do Zeca", "Santo André", logradouro="Rua das Palmeiras")
        decisao, motivo = avaliar_associacao(e, cadastro)
        self.assertEqual(decisao, "outra_cidade")
        self.assertIn("Santo André", motivo)
        carga = SimpleNamespace(competencia=f"{self.hoje.year}-{self.hoje.month:02d}")
        with mock.patch("prospeccao.conectores.receita.consultar_local", return_value=(cadastro, carga)),                 mock.patch("prospeccao.services.identificacao_cnpj.filial_local", return_value=(None, None)):
            ConsultaWeb.objects.create(
                empresa=e, finalidade="DESCOBERTA", provedor="google_serpapi", consulta="oficina Mauá",
                status="OK", resultados=[{"trecho": f"CNPJ {cnpj}", "titulo": "Oficina do Zeca"}])
            info = completar_identificacao(e, None, max_buscas=0)
        e.refresh_from_db()
        self.assertEqual(e.cnpj, cnpj)
        self.assertEqual(e.cidade, "Mauá")
        self.assertEqual(info["acao"], "cnpj_outra_cidade")
        self.assertEqual(dm.classificacao_identificacao(e)["codigo"], "ativo_outra_cidade")
        self.assertTrue(e.dados_coletados.filter(campo="cnpj_outra_cidade", valor__startswith=cnpj).exists())

    def test_cnpj_posterior_revela_cliente_salesforce(self):
        cnpj = doc("334445550001")
        cliente = self.empresa(
            "Cliente Antigo", cnpj=cnpj, salesforce_id="001000000000001",
            ultima_compra_cadastro=date(2025, 1, 10), cidade="Mauá")
        nova = self.empresa("Oficina do Zeca")
        op = self.oportunidade(nova)
        p = self.pesquisa()
        frente = self.frente()
        sel = SelecaoPesquisa.objects.create(pesquisa=p, frente=frente, cnpj="PENDENTE000001", empresa=nova)
        cadastro = dados_de(cnpj, "Cliente Antigo Ltda", "Oficina do Zeca", "Mauá")
        with mock.patch("prospeccao.conectores.receita.consultar_local", return_value=(cadastro, None)):
            ConsultaWeb.objects.create(
                empresa=nova, finalidade="DESCOBERTA", provedor="google_serpapi", consulta="oficina Mauá",
                status="OK", resultados=[{"trecho": f"CNPJ {cnpj}", "titulo": "Oficina do Zeca"}])
            info = completar_identificacao(nova, sel, max_buscas=0)
        nova.refresh_from_db()
        op.refresh_from_db()
        sel.refresh_from_db()
        self.assertFalse(nova.cnpj)
        self.assertEqual(cliente.cnpj, cnpj)
        self.assertEqual(info["acao"], "conhecida_salesforce")
        self.assertEqual(op.status, Oportunidade.Status.JA_CLIENTE)
        self.assertEqual(sel.fila, SelecaoPesquisa.Fila.CONHECIDA)
        self.assertEqual(dm.classificacao_salesforce(nova, op)["codigo"], "conhecida")

    def test_ativo_e_consulta_indisponivel(self):
        ativo = self.empresa(
            "Ativa", cnpj=doc("445556660001"), situacao_cadastral="ATIVA",
            situacao_cadastral_referencia=self.hoje, situacao_cadastral_fonte="Receita Federal – base local",
            situacao_cadastral_data=date(2024, 3, 1))
        self.assertEqual(dm.classificacao_identificacao(ativo)["codigo"], "ativo")
        pendente = self.empresa("Pendencia", cnpj=doc("556667770001"))
        with mock.patch(
                "prospeccao.services.oportunidades.verificar_situacao_cadastral",
                side_effect=RuntimeError("fonte fora")):
            info = completar_identificacao(pendente, None, max_buscas=0)
        pendente.refresh_from_db()
        self.assertEqual(info["acao"], "revisto")
        self.assertEqual(pendente.situacao_cadastral, "")
        self.assertEqual(dm.classificacao_identificacao(pendente)["codigo"], "situacao_pendente")
        self.assertTrue(pendente.dados_coletados.filter(campo="verificacao_situacao", valor__startswith="FALHA").exists())
        self.oportunidade(ativo)
        op_pendente = Oportunidade.objects.filter(empresa=pendente).first() or self.oportunidade(pendente)
        op_pendente.status = Oportunidade.Status.NOVA
        op_pendente.tipo = Oportunidade.Tipo.NOVA_EXTERNA
        op_pendente.save()
        _, validados = filtrar_oportunidades({"etapa": "validado"}, self.user)
        _, conferir = filtrar_oportunidades({"etapa": "conferir_cadastro"}, self.user)
        self.assertEqual(list(validados.values_list("empresa__nome", flat=True)), ["Ativa"])
        self.assertIn(pendente.pk, list(conferir.values_list("empresa_id", flat=True)))

    def test_contatos_alternativos_permanecem(self):
        e = self.empresa("Dois telefones")
        for valor, norm in (("(11) 2400-1111", "1124001111"), ("(11) 98888-2222", "11988882222")):
            CanalEmpresa.objects.create(
                empresa=e, tipo=CanalEmpresa.Tipo.TELEFONE, valor=valor, valor_normalizado=norm,
                origem_canal=CanalEmpresa.Origem.PUBLICACAO, confianca_associacao=CanalEmpresa.Confianca.MEDIA)
        completar_identificacao(e, None, max_buscas=0)
        self.assertEqual(e.canais.filter(tipo=CanalEmpresa.Tipo.TELEFONE).count(), 2)

    def test_reencontrada_sem_contato_efetivo_permanece_disponivel(self):
        e = self.empresa("Ja vista")
        p1, p2 = self.pesquisa(), self.pesquisa()
        frente = self.frente()
        SelecaoPesquisa.objects.create(pesquisa=p1, frente=frente, cnpj="SEM1", empresa=e, fila=SelecaoPesquisa.Fila.BUSCAR_CONTATO)
        SelecaoPesquisa.objects.create(pesquisa=p2, frente=frente, cnpj="SEM1", empresa=e, fila=SelecaoPesquisa.Fila.BUSCAR_CONTATO)
        op = self.oportunidade(e)
        op.pesquisas.add(p2)
        self.assertTrue(reencontrada(e, p2))
        self.assertIn("Encontrada anteriormente", " ".join(informativos(e, p2)))
        _, qs = filtrar_oportunidades({"etapa": "disponiveis", "pesquisa": str(p2.pk)}, self.user)
        self.assertIn(op.pk, list(qs.values_list("pk", flat=True)))

    def test_tentativa_sem_resposta_nao_sai_da_fila(self):
        e = self.empresa("Sem resposta")
        op = self.oportunidade(e)
        Abordagem.objects.create(
            empresa=e, usuario=self.user, canal=CanalAbordagem.TELEFONE, resultado=ResultadoAbordagem.SEM_RESPOSTA)
        self.assertEqual(dm.classificacao_contato(e, op, abordagens=list(e.abordagens.all()))["codigo"], "tentativa")
        self.assertIn(op.pk, list(fila_primeira_abordagem().values_list("pk", flat=True)))

    def test_retomada_nao_repete_consulta_concluida(self):
        e = self.empresa("Retomada")
        consulta = f'"{e.nome}" "{e.cidade}" CNPJ'
        chamadas = []

        def busca_falsa(conector, texto, **kwargs):
            chamadas.append(texto)
            ConsultaWeb.objects.create(
                empresa=e, finalidade="COMPLEMENTAR", provedor="google_serpapi", consulta=texto, status="OK",
                resultados=[])
            return []

        orc = SimpleNamespace(ultimo_registro=None)
        with mock.patch("prospeccao.services.orcamento.buscar", side_effect=busca_falsa):
            primeira = completar_identificacao(e, None, busca=object(), orc=orc, max_buscas=1)
            segunda = completar_identificacao(e, None, busca=object(), orc=orc, max_buscas=1)
        self.assertEqual(primeira["buscas_novas"], 1)
        self.assertEqual(chamadas, [consulta])
        self.assertEqual(segunda["buscas_novas"], 0)
        self.assertIn("já feita", segunda["motivo"])

    def test_indicador_lista_e_exportacao_usam_o_mesmo_recorte(self):
        p = self.pesquisa()
        frente = self.frente()
        validada = self.empresa(
            "Validada Export", cnpj=doc("667778880001"), situacao_cadastral="ATIVA",
            situacao_cadastral_referencia=self.hoje, situacao_cadastral_fonte="Receita Federal – base local")
        pendente = self.empresa("Pendente Export")
        conhecida = self.empresa("Conhecida Export", cnpj=doc("778889990001"), salesforce_id="001000000000009")
        for e in (validada, pendente, conhecida):
            tipo = Oportunidade.Tipo.CADASTRO_SEM_COMPRA if e.salesforce_id else Oportunidade.Tipo.NOVA_EXTERNA
            op = self.oportunidade(e, tipo=tipo, status=Oportunidade.Status.NOVA)
            op.pesquisas.add(p)
            SelecaoPesquisa.objects.create(
                pesquisa=p, frente=frente, cnpj=(e.cnpj or "SEM")[:14], empresa=e,
                fila=SelecaoPesquisa.Fila.CONHECIDA if e.salesforce_id else SelecaoPesquisa.Fila.BUSCAR_CONTATO)
        CanalEmpresa.objects.create(
            empresa=validada, tipo=CanalEmpresa.Tipo.TELEFONE, valor="1133334444", valor_normalizado="1133334444",
            origem_canal=CanalEmpresa.Origem.PUBLICACAO, confianca_associacao=CanalEmpresa.Confianca.ALTA)
        ind = indicadores_pesquisa(p, self.user)
        _, lista = filtrar_oportunidades({"pesquisa": str(p.pk), "etapa": "validado"}, self.user)
        self.assertEqual(ind["validado"], lista.count())
        self.assertEqual(list(lista.values_list("empresa__nome", flat=True)), ["Validada Export"])
        self.user.is_staff = True  # lista e exportação de leads: papel ADM
        self.user.save()
        self.client.force_login(self.user)
        resp = self.client.get(f"/prospeccao/oportunidades/exportar/?pesquisa={p.pk}&etapa=validado")
        corpo = resp.content.decode("utf-8-sig")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Validada Export", corpo)
        self.assertNotIn("Pendente Export", corpo)
        self.assertNotIn("Conhecida Export", corpo)
        self.assertIn("CNPJ ativo", corpo)
        self.assertIn("Identificação", corpo)
        _, conhecidas = filtrar_oportunidades({"etapa": "conhecidas"}, self.user)
        self.assertIn(conhecida.pk, list(conhecidas.values_list("empresa_id", flat=True)))
        self.assertNotIn(conhecida.pk, list(lista.values_list("empresa_id", flat=True)))
        linhas = dm.contagens([validada.pk, pendente.pk, conhecida.pk])
        self.assertEqual(linhas["cadastro_validado"], 1)
        self.assertEqual(linhas["cadastro_validado_com_contato"], 1)
        self.assertEqual(linhas["cnpj_nao_identificado"], 1)
        self.assertEqual(linhas["conhecidas_salesforce"], 1)

    def test_cidade_pesquisada_diferente_do_municipio_identificado(self):
        cid, uf = dg.cidade_uf_do_endereco("Av. Papa João XXIII - Parque Sao Vicente, Mauá - SP, 09370-800")
        self.assertEqual((cid, uf), ("MAUA", "SP"))
        self.assertIsNone(dg.cidade_citada("empresa ddd 11", ["Mauá", "Santo André"]))
        self.assertEqual(dg.cidade_citada("transportadora São Caetano contato", ["São Caetano do Sul", "Mauá"]),
                         "São Caetano do Sul")

    def test_retomada_nao_diz_que_nenhuma_cidade_foi_pesquisada(self):
        texto = dg.texto_cidades_pesquisadas({}, 4)
        self.assertNotIn("nenhuma", texto)
        self.assertIn("4 consulta", texto)
        self.assertEqual(dg.texto_cidades_pesquisadas({}, 0), "nenhuma")

    def test_prioridade_de_cidades_do_usuario(self):
        p = self.pesquisa()
        frente = self.frente()
        consultas = dg.consultas_frente(frente, {"consultas_descoberta": ["transportadora frota própria"]}, p, deslocamento=5)
        self.assertTrue(consultas)
        self.assertIn("Mauá", consultas[0])
        self.assertTrue(all("frota própria" not in q.lower() and "frota propria" not in q.lower() for q in consultas))
        cidades = []
        for q in consultas:
            if "Mauá" in q and "Mauá" not in cidades:
                cidades.append("Mauá")
            elif "Santo André" in q and "Santo André" not in cidades:
                cidades.append("Santo André")
            elif "São Caetano" in q and "São Caetano" not in cidades:
                cidades.append("São Caetano")
        self.assertEqual(cidades[:3], ["Mauá", "Santo André", "São Caetano"])

    def test_limpar_prospeccao_nao_apaga_carteira(self):
        carteira = self.empresa("Conta Salesforce", cnpj=doc("889990000001"), salesforce_id="001000000000010")
        prospect = self.empresa("So prospeccao")
        self.oportunidade(prospect)
        imp = Importacao.objects.get(tipo=Importacao.Tipo.CLIENTES)
        limpar(usuario=self.user, fazer_backup=False)
        self.assertTrue(Empresa.objects.filter(pk=carteira.pk, salesforce_id="001000000000010").exists())
        self.assertFalse(Empresa.objects.filter(pk=prospect.pk).exists())
        self.assertTrue(Importacao.objects.filter(pk=imp.pk, status=Importacao.Status.SUCESSO).exists())

    def test_falha_tecnica_nao_e_zero_empresas(self):
        bruto = {
            "search_information": {"local_results_state": 'Showing results for type: "place" instead of type: "search"'},
            "place_results": {"title": "Transportadora Mauá", "address": "Av. Papa João XXIII, Mauá - SP",
                              "phone": "(11) 4544-1000", "type": ["Empresa de transporte rodoviário"],
                              "website": "http://www.tmmaua.com.br/home/"},
        }
        saida = GoogleSerpApi._extrair(bruto, "local")
        self.assertEqual(len(saida), 1)
        self.assertEqual(saida[0]["nome_citado"], "Transportadora Mauá")
        self.assertEqual(saida[0]["modalidade"], "local/lugar")
        vazio = GoogleSerpApi._analise_extracao({"search_information": {}}, [], "local")
        self.assertTrue(vazio["inconclusivo"])
        trecho = open(dg.__file__, encoding="utf-8").read()
        self.assertIn("Não é ausência de empresas", trecho)

    def test_sync_falha_parcial_e_ausente(self):
        quando = timezone.now() - timedelta(days=3)
        Importacao.objects.filter(tipo=Importacao.Tipo.CLIENTES).update(finalizado_em=quando)
        a = self.empresa("Conta A", cnpj=doc("101010100001"), salesforce_id="001000000000011")
        b = self.empresa("Conta B", cnpj=doc("202020200001"), salesforce_id="001000000000012")
        with mock.patch("carteira.services.salesforce.conectar", side_effect=RuntimeError("login")):
            ok, aviso = cz.tentar_atualizar_carteira(permitir_rede=True)
        self.assertFalse(ok)
        self.assertIn(timezone.localtime(quando).strftime("%d/%m/%Y"), aviso)
        self.assertTrue(Empresa.objects.filter(pk=a.pk).exists())
        with mock.patch("carteira.services.salesforce.conectar", return_value=object()), \
                mock.patch("carteira.services.salesforce.buscar_accounts", return_value=([{"Id": "x"}], "q")):
            ok_parcial, aviso_parcial = cz.tentar_atualizar_carteira(permitir_rede=True)
        self.assertFalse(ok_parcial)
        self.assertIn("parcial", aviso_parcial)
        self.assertTrue(Empresa.objects.filter(pk=b.pk, salesforce_id="001000000000012").exists())
        Importacao.objects.all().delete()
        Empresa.objects.filter(salesforce_id__isnull=False).update(salesforce_id=None)
        with mock.patch("carteira.services.salesforce.conectar", side_effect=RuntimeError("login")):
            ok_sem, aviso_sem = cz.tentar_atualizar_carteira(permitir_rede=True)
        self.assertFalse(ok_sem)
        self.assertIn("Cruzamento pendente", aviso_sem)
        solta = self.empresa("Ainda sem documento")
        self.assertEqual(dm.classificacao_salesforce(solta)["codigo"], "cruzamento_pendente")

    def test_formato_invalido_nao_e_cnpj_ausente(self):
        e = self.empresa("Numero ruim")
        from prospeccao.services.enriquecimento import registrar_dado
        registrar_dado(e, "cnpj_informado_invalido", "11222333000100", "fonte", "")
        rotulo = dm.classificacao_identificacao(e)["rotulo"]
        self.assertIn("formato inválido", rotulo)
        op = self.oportunidade(e, status=Oportunidade.Status.NOVA)
        _, completar = filtrar_oportunidades({"etapa": "completar_cnpj"}, self.user)
        _, conferir = filtrar_oportunidades({"etapa": "conferir_cadastro"}, self.user)
        self.assertNotIn(op.pk, list(completar.values_list("pk", flat=True)))
        self.assertIn(op.pk, list(conferir.values_list("pk", flat=True)))

    def test_retomada_nomeia_cidade_ja_pesquisada(self):
        pesquisa = self.pesquisa()
        anterior = Execucao.objects.create(pesquisa=pesquisa, status=Execucao.Status.INTERROMPIDA)
        ConsultaWeb.objects.create(
            pesquisa=pesquisa, execucao=anterior, finalidade="DESCOBERTA", provedor="google_serpapi",
            consulta="terraplenagem Mauá SP", status="OK", resultados=[{"_extraidas": []}],
            acoes=[{"tipo": "google:local", "inicio": 0}])
        atual = Execucao.objects.create(
            pesquisa=pesquisa, status=Execucao.Status.EXECUTANDO, parametros={"retomada_de": anterior.pk})
        logs = []
        ctx = SimpleNamespace(log=lambda mensagem, nivel="INFO", fonte="": logs.append(mensagem), verificar=lambda: None)
        est = SimpleNamespace(parar=False, motivo_fim="", vistos_google=set(), consultas_norm=set())
        dg.descobrir(atual, ctx, est, [], None, None, None, None, None, None, None, None, 1)
        texto = " ".join(logs)
        self.assertIn("Mauá", texto)
        self.assertNotIn("nenhuma", texto)
        atual.refresh_from_db()
        self.assertIn("Mauá", atual.progresso["cidades"]["pesquisadas"])

    def test_correspondencia_so_por_nome_permanece_visivel(self):
        pesquisa = self.pesquisa(cidades="Mauá")
        frente = self.frente()
        execucao = Execucao.objects.create(pesquisa=pesquisa, status=Execucao.Status.EXECUTANDO)
        self.empresa("Pedreira Horizonte Ltda", cidade="Mauá", salesforce_id="001000000000099")
        item = {
            "nome_citado": "Pedreira Horizonte", "titulo": "Pedreira Horizonte", "tipo": "LOCAL",
            "endereco_citado": "Rua das Pedras, 10 - Mauá - SP, 09370-100",
            "telefones": [], "emails": [], "trecho": "Pedreira Horizonte",
        }
        est = SimpleNamespace(vistos_google=set(), ids_execucao=[])
        with mock.patch.object(dg, "identificar_na_receita", return_value=(None, "")):
            status = dg.processar_item(
                item, "pedreira Mauá SP", execucao, SimpleNamespace(log=lambda *a, **k: None), est,
                frente, None, None, None, {}, {}, set(), None, "Mauá")
        criada = Empresa.objects.get(nome="Pedreira Horizonte", salesforce_id__isnull=True)
        sel = SelecaoPesquisa.objects.get(empresa=criada)
        self.assertEqual(sel.fila, SelecaoPesquisa.Fila.REVISAR_IDENTIDADE)
        self.assertIn("conferência separada", status)
        self.assertIn(str(criada.pk), status)
        op = Oportunidade.objects.get(empresa=criada)
        op.pesquisas.add(pesquisa)
        _, conferir = filtrar_oportunidades({"etapa": "conferir_cadastro", "pesquisa": str(pesquisa.pk)}, self.user)
        self.assertIn(op.pk, list(conferir.values_list("pk", flat=True)))

    def test_cnpj_ausente_na_fonte_nao_confirma_situacao_antiga(self):
        from prospeccao.services.enriquecimento import registrar_dado
        e = self.empresa(
            "Base antiga", cnpj=doc("667778880001"), situacao_cadastral="ATIVA",
            situacao_cadastral_data=date(2019, 9, 25), situacao_cadastral_referencia=date(2026, 10, 1),
            situacao_cadastral_fonte="Receita Federal – dados abertos do CNPJ – competência 2026-10")
        registrar_dado(e, "verificacao_situacao", "CNPJ não encontrado na consulta", "brasilapi", "")
        registrar_dado(e, "verificacao_situacao", "FALHA: HTTP 429 Too Many Requests", "brasilapi_rechecagem", "")
        ident = dm.classificacao_identificacao(e)
        self.assertEqual(ident["codigo"], "situacao_pendente")
        self.assertNotIn("CNPJ ativo", ident["rotulo"])
        self.assertIn("não tem este CNPJ", ident["detalhe"])
        self.assertIn("competência da base", ident["detalhe"])

    def test_lote_nao_mexe_no_limite_diario(self):
        from prospeccao.services.lote_identificacao import abrir_lote, reservar_vaga
        fonte, _ = Fonte.objects.get_or_create(
            codigo="google_serpapi", defaults={"nome": "Google", "tipo": Fonte.Tipo.DESCOBERTA, "max_consultas_dia": 20})
        fonte.max_consultas_dia = 20
        fonte.save(update_fields=["max_consultas_dia"])
        UsoFonte.objects.update_or_create(fonte=fonte, dia=timezone.localdate(), defaults={"consultas": 28, "erros": 0})
        pesquisa = self.pesquisa()
        ex = abrir_lote(pesquisa, 53, "auditoria do estouro")
        self.assertTrue(reservar_vaga(ex))
        fonte.refresh_from_db()
        uso = UsoFonte.objects.get(fonte=fonte, dia=timezone.localdate())
        self.assertEqual(fonte.max_consultas_dia, 20)
        self.assertEqual(uso.consultas, 29)
        self.assertEqual(ex.parametros["lote_serpapi_identificacao"]["usadas"], 1)
        self.assertEqual(ex.parametros["lote_serpapi_identificacao"]["maximo"], 53)

    def test_http_429_nao_e_cnpj_inexistente(self):
        from prospeccao.conectores.base import FonteIndisponivel
        from prospeccao.conectores.brasilapi import BrasilAPICNPJ
        api = BrasilAPICNPJ(SimpleNamespace(lista_endpoints=["https://brasilapi.com.br/api/cnpj/v1/"]))
        api._cliente = mock.Mock()
        api._cliente.requisitar.return_value = SimpleNamespace(status_code=429, headers={"Retry-After": "3"})
        with self.assertRaises(FonteIndisponivel) as ctx:
            api.consultar("11222333000181", SimpleNamespace(log=lambda *a, **k: None))
        self.assertIn("429", str(ctx.exception))
        self.assertIn("não é CNPJ inexistente", str(ctx.exception))

    def test_conversa_com_situacao_pendente(self):
        from carteira.services.abordagens import registrar_abordagem
        e = self.empresa("Pendencia de cadastro", cnpj=doc("778889990001"))
        self.oportunidade(e, status=Oportunidade.Status.NOVA)
        abordagem = registrar_abordagem(Abordagem(
            empresa=e, usuario=self.user, canal="TELEFONE", resultado=ResultadoAbordagem.SEM_RESPOSTA))
        self.assertEqual(abordagem.empresa_id, e.pk)
        self.assertFalse(e.situacao_cadastral)

    def test_lista_conhecidas_mostra_as_mesmas_do_indicador(self):
        from prospeccao.services.lote_identificacao import exibir_conhecidas
        pesquisa = self.pesquisa()
        frente = self.frente()
        empresa = self.empresa("Lourenco Terraplenagem", cnpj=doc("889990000001"), salesforce_id="001000000000077")
        SelecaoPesquisa.objects.create(
            pesquisa=pesquisa, frente=frente, cnpj=empresa.cnpj, empresa=empresa,
            fila=SelecaoPesquisa.Fila.CONHECIDA, motivos_selecao=["já na carteira"])
        self.assertEqual(exibir_conhecidas(pesquisa), 1)
        op = Oportunidade.objects.get(empresa=empresa)
        self.assertEqual(op.status, Oportunidade.Status.JA_CLIENTE)
        self.assertNotEqual(op.tipo, Oportunidade.Tipo.NOVA_EXTERNA)
        _, conhecidas = filtrar_oportunidades({"etapa": "conhecidas", "pesquisa": str(pesquisa.pk)}, self.user)
        _, disponiveis = filtrar_oportunidades({"etapa": "disponiveis", "pesquisa": str(pesquisa.pk)}, self.user)
        self.assertIn(op.pk, list(conhecidas.values_list("pk", flat=True)))
        self.assertNotIn(op.pk, list(disponiveis.values_list("pk", flat=True)))
        ind = indicadores_pesquisa(pesquisa, self.user)
        self.assertEqual(ind["conhecidas"], 1)
        self.assertEqual(ind["conhecidas_lista"], 1)

    def test_extrator_le_organico_painel_e_lugar(self):
        cnpj = doc("494446930001")
        fmt = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
        bruto = {
            "organic_results": [{
                "position": 1, "title": "Transvx Transportes", "link": "https://www.transvx.com.br/",
                "snippet": f"CNPJ {fmt} contato@transvx.com.br (11) 97068-3136",
            }],
            "knowledge_graph": {
                "title": "Transportadora Transvx", "address": "Av. Exemplo, 10 - Santo André - SP",
                "phone": "(11) 4444-0000", "website": "https://www.transvx.com.br/",
            },
            "place_results": {"title": "Deposito Isolado", "address": "Rua B, 2", "phone": "(11) 3333-0000"},
        }
        saida = GoogleSerpApi._extrair(bruto, "organico")
        org = next(r for r in saida if r["modalidade"] == "organico")
        self.assertEqual(org["cnpj_citado"], cnpj)
        self.assertIn("contato@transvx.com.br", org["emails"])
        self.assertTrue(org["telefones"])
        self.assertTrue(any(r["modalidade"] == "knowledge_graph" and r["nome_citado"] == "Transportadora Transvx" for r in saida))
        self.assertTrue(any(r["titulo"] == "Deposito Isolado" for r in saida))

    def test_cnpj_do_site_vincula_com_cadastro_e_429_nao_vincula(self):
        from carteira.models import DadoColetado
        from prospeccao.services.identificacao_cnpj import aproveitar_cnpj_publicado
        cnpj = doc("494446930001")
        e = self.empresa("Transportadora Transvx", cidade="Santo André", endereco="Av. Engenheiro, 126")
        CanalEmpresa.objects.create(
            empresa=e, tipo=CanalEmpresa.Tipo.SITE, valor="https://www.transvx.com.br/",
            valor_normalizado="transvx.com.br", origem_canal=CanalEmpresa.Origem.PUBLICACAO,
            confianca_associacao=CanalEmpresa.Confianca.MEDIA)
        DadoColetado.objects.create(
            empresa=e, campo="cnpj_publicado_no_site", fonte="site_publico", valor=cnpj,
            referencia="https://www.transvx.com.br/")
        dados = dados_de(cnpj, "TRANSVX TRANSPORTES LTDA", "TRANSVX", "Santo Andre", logradouro="Avenida Engenheiro 126")
        with mock.patch("prospeccao.conectores.receita.consultar_local", return_value=(None, None)), \
                mock.patch("prospeccao.services.identificacao_cnpj._consultar_cadastral", return_value=("ok", dados)):
            info = aproveitar_cnpj_publicado(e, None, consultar=True)
        e.refresh_from_db()
        self.assertEqual(info["acao"], "cnpj_vinculado")
        self.assertEqual(e.cnpj, cnpj)
        self.assertEqual(e.razao_social, "TRANSVX TRANSPORTES LTDA")
        e2 = self.empresa("Transportadora Segura", cidade="Santo André")
        CanalEmpresa.objects.create(
            empresa=e2, tipo=CanalEmpresa.Tipo.SITE, valor="https://www.seguratrans.com.br/",
            valor_normalizado="seguratrans.com.br", origem_canal=CanalEmpresa.Origem.PUBLICACAO,
            confianca_associacao=CanalEmpresa.Confianca.MEDIA)
        DadoColetado.objects.create(
            empresa=e2, campo="cnpj_publicado_no_site", fonte="site_publico", valor=doc("112223330001"),
            referencia="https://www.seguratrans.com.br/")
        with mock.patch("prospeccao.conectores.receita.consultar_local", return_value=(None, None)), \
                mock.patch("prospeccao.services.identificacao_cnpj._consultar_cadastral",
                           return_value=("indisponivel", "HTTP 429 limite de acesso")):
            info2 = aproveitar_cnpj_publicado(e2, None, consultar=True)
        e2.refresh_from_db()
        self.assertEqual(info2["acao"], "cnpj_vinculado")
        self.assertEqual(e2.cnpj, doc("112223330001"))
        self.assertFalse(e2.situacao_cadastral)
        nota = e2.dados_coletados.get(campo="proxima_acao").valor
        self.assertIn("429", nota)
        self.assertIn("não é inexistente", nota)

    def test_nome_so_de_atividade_nao_revincula_cnpj_rejeitado(self):
        from carteira.models import DadoColetado
        from prospeccao.services.identificacao_cnpj import _cnpj_unico_do_site
        e = self.empresa("Autopeças", cidade="São Caetano do Sul", endereco="R. Amazonas, 1380")
        DadoColetado.objects.create(
            empresa=e, campo="cnpj_candidato_rejeitado", fonte="revisao_identificacao", valor="14310170010140")
        DadoColetado.objects.create(
            empresa=e, campo="cnpj_publicado_no_site", fonte="site_publico", valor="14310170010140",
            referencia="https://www.autozone.com.br/")
        self.assertIsNone(_cnpj_unico_do_site(e))

    def test_http_e_https_viram_um_site(self):
        from prospeccao.services.identificacao_cnpj import consolidar_sites
        e = self.empresa("Transportadora Transvx")
        CanalEmpresa.objects.create(
            empresa=e, tipo=CanalEmpresa.Tipo.SITE, valor="http://www.transvx.com.br/",
            valor_normalizado="transvx.com.br", origem_canal=CanalEmpresa.Origem.PUBLICACAO,
            confianca_associacao=CanalEmpresa.Confianca.BAIXA)
        CanalEmpresa.objects.create(
            empresa=e, tipo=CanalEmpresa.Tipo.SITE, valor="https://www.transvx.com.br/",
            valor_normalizado="transvx.com.br", origem_canal=CanalEmpresa.Origem.PUBLICACAO,
            confianca_associacao=CanalEmpresa.Confianca.MEDIA)
        consolidar_sites(e)
        sites = list(e.canais.filter(tipo=CanalEmpresa.Tipo.SITE))
        self.assertEqual(len(sites), 1)
        self.assertTrue(sites[0].valor.startswith("https://"))
        self.assertTrue(any(c.get("url", "").startswith("http://") for c in sites[0].corroboracoes))

    def test_numero_invalido_na_busca_nao_vira_formato_invalido(self):
        e = self.empresa("Empresa Distinta Nome")
        ConsultaWeb.objects.create(
            empresa=e, finalidade="COMPLEMENTAR", provedor="google_serpapi", status="OK",
            consulta="Empresa Distinta Nome Mauá CNPJ",
            resultados=[{"tipo": "ORGANICO", "titulo": "Empresa Distinta Nome",
                         "trecho": "CNPJ 60.251.215/0001-97", "cnpj_citado": ""}])
        completar_identificacao(e, None, max_buscas=0, permitir_consulta_complementar=False)
        self.assertFalse(e.dados_coletados.filter(campo="cnpj_informado_invalido").exists())

    def test_telefone_compartilhado_sem_cnpj_nao_interrompe(self):
        from prospeccao.services.contatos import registrar
        dona = self.empresa("Dona do telefone", cnpj=doc("303030300001"))
        sem = self.empresa("Ainda sem documento")
        CanalEmpresa.objects.create(
            empresa=sem, tipo=CanalEmpresa.Tipo.TELEFONE, valor="11988887777",
            valor_normalizado="11988887777", origem_canal=CanalEmpresa.Origem.PUBLICACAO,
            confianca_associacao=CanalEmpresa.Confianca.MEDIA)
        canal = registrar(
            dona, CanalEmpresa.Tipo.TELEFONE, "11988887777", fonte="consulta_salva",
            origem=CanalEmpresa.Origem.PUBLICACAO, confianca=CanalEmpresa.Confianca.BAIXA,
            aplica_a=CanalEmpresa.AplicaA.DESCONHECIDO, evidencia="visto na resposta salva")
        self.assertIsNotNone(canal)
        self.assertEqual(dona.canais.filter(tipo="TELEFONE").count(), 1)
        self.assertEqual(sem.canais.filter(tipo="TELEFONE").count(), 1)
        self.assertNotIn("None", canal.compartilhado or "")
        self.assertIn("possível relação", canal.compartilhado or "")

    def test_telefone_compartilhado_com_nome_vazio_nao_interrompe(self):
        from prospeccao.services.contatos import registrar
        outra = self.empresa("", cnpj=None)
        outra.nome = ""
        outra.save(update_fields=["nome"])
        CanalEmpresa.objects.create(
            empresa=outra, tipo=CanalEmpresa.Tipo.TELEFONE, valor="11977776666",
            valor_normalizado="11977776666", origem_canal=CanalEmpresa.Origem.PUBLICACAO,
            confianca_associacao=CanalEmpresa.Confianca.MEDIA)
        dona = self.empresa("Dona com nome", cnpj=doc("404040400001"))
        canal = registrar(
            dona, CanalEmpresa.Tipo.TELEFONE, "11977776666", fonte="resultado_salvo",
            origem=CanalEmpresa.Origem.PUBLICACAO, confianca=CanalEmpresa.Confianca.BAIXA,
            aplica_a=CanalEmpresa.AplicaA.DESCONHECIDO)
        self.assertIsNotNone(canal)
        self.assertNotIn("None", canal.compartilhado or "")
        self.assertIn(f"cadastro {outra.pk}", canal.compartilhado or "")
        self.assertEqual(outra.canais.filter(tipo="TELEFONE").count(), 1)

    def test_pagina_de_diretorio_nao_leva_contato_de_outra_empresa(self):
        from prospeccao.conectores.site import ResultadoSite
        from prospeccao.services import contatos as ct
        alfa = self.empresa("Transportadora Alfa")
        texto = (
            "Transportadora Alfa telefone (11) 91111-0000. "
            "Transportadora Beta telefone (11) 92222-0000."
        )
        cnpjs = [doc("505050500001"), doc("606060600001")]
        aceitos, rejeitados, _, _ = ct.selecionar_contatos_do_texto(
            alfa, texto, ["11911110000", "11922220000"], [], cnpjs, "listagem.example")
        self.assertEqual(aceitos, ["11911110000"])
        self.assertEqual(rejeitados, ["11922220000"])
        pagina = ResultadoSite(url_inicial="https://listagem.example/alfa")
        pagina.paginas = [("https://listagem.example/alfa", texto)]
        pagina.telefones = {"11911110000": pagina.url_inicial, "11922220000": pagina.url_inicial}
        pagina.cnpjs = {c: pagina.url_inicial for c in cnpjs}

        class Leitor:
            def analisar(self, url, ctx):
                return pagina

        with mock.patch("prospeccao.services.contatos.associar_site", return_value=(
                CanalEmpresa.Confianca.MEDIA, CanalEmpresa.AplicaA.EMPRESA, "nome no trecho", True)):
            ct._tentar_sites(alfa, Leitor(), None, [("https://listagem.example/alfa", "teste")],
                             {"site": None, "paginas": [], "tentativas": [], "nomes_alt": []}, {})
        tels = set(alfa.canais.filter(tipo="TELEFONE").values_list("valor", flat=True))
        self.assertIn("11911110000", tels)
        self.assertNotIn("11922220000", tels)
        self.assertTrue(alfa.dados_coletados.filter(campo="contatos_rejeitados_pagina").exists())

    def test_homonima_em_outra_cidade_nao_recebe_contato_nem_cnpj(self):
        from carteira.models import DadoColetado
        p = self.pesquisa()
        frente = self.frente()
        maua = self.empresa("Elite Guindastes Especiais", cidade="Mauá")
        andre = self.empresa("Elite Guindastes Especiais", cidade="Santo André")
        cnpj = doc("707070700001")
        ConsultaWeb.objects.create(
            pesquisa=p, empresa=maua, finalidade="COMPLEMENTAR", provedor="google_serpapi", status="OK",
            consulta="Elite Guindastes Santo André SP",
            resultados=[{
                "tipo": "LOCAL", "titulo": "Elite Guindastes Especiais", "nome_citado": "Elite Guindastes Especiais",
                "endereco_citado": "Rua das Flores, 10 - Santo André - SP, 09000-000",
                "telefones": ["(11) 93333-4444"], "emails": [],
                "cnpj_citado": cnpj, "trecho": f"CNPJ {cnpj}",
            }])
        DadoColetado.objects.create(empresa=maua, campo="cnpj_candidato", fonte="consulta 1", valor=cnpj)
        sel_m = SelecaoPesquisa.objects.create(pesquisa=p, frente=frente, cnpj="HOMON1", empresa=maua)
        sel_a = SelecaoPesquisa.objects.create(pesquisa=p, frente=frente, cnpj="HOMON2", empresa=andre)
        completar_identificacao(maua, sel_m, max_buscas=0, permitir_consulta_complementar=False)
        completar_identificacao(andre, sel_a, max_buscas=0, permitir_consulta_complementar=False)
        maua.refresh_from_db()
        self.assertFalse(maua.canais.filter(valor="11933334444").exists())
        self.assertFalse(maua.cnpj)
        self.assertFalse(maua.dados_coletados.filter(campo="cnpj_candidato", valor=cnpj).exists())
        self.assertTrue(andre.canais.filter(valor="11933334444").exists())

    def test_resposta_rica_aproveita_so_o_trecho_da_empresa(self):
        p = self.pesquisa()
        frente = self.frente()
        e = self.empresa("Transporte Norte Sul", cidade="Santo André",
                         endereco="Av. Engenheiro, 126 - Santo André - SP, 09220-305")
        ConsultaWeb.objects.create(
            pesquisa=p, empresa=e, finalidade="DESCOBERTA", provedor="google_serpapi", status="OK",
            consulta="transportadora Santo André SP",
            resultados=[
                {"tipo": "LOCAL", "titulo": "Outra Carga Ltda", "nome_citado": "Outra Carga Ltda",
                 "endereco_citado": "Rua B, 2 - Santo André - SP", "telefones": ["(11) 90000-1111"],
                 "emails": [], "modalidade": "local"},
                {"tipo": "LOCAL", "titulo": "Transporte Norte Sul", "nome_citado": "Transporte Norte Sul",
                 "endereco_citado": "Av. Engenheiro, 126 - Santo André - SP, 09220-305",
                 "telefones": ["(11) 97068-3136"], "emails": [],
                 "atividade": "Empresa de transporte rodoviário", "modalidade": "local/lugar",
                 "cnpj_citado": ""},
                {"tipo": "ORGANICO", "titulo": "VX Transportes", "nome_citado": "VX Transportes",
                 "trecho": "CNPJ 18.064.448/0004-87", "telefones": ["(11) 95555-6666"], "diretorio": False},
            ])
        sel = SelecaoPesquisa.objects.create(pesquisa=p, frente=frente, cnpj="RICA1", empresa=e)
        completar_identificacao(e, sel, max_buscas=0, permitir_consulta_complementar=False)
        e.refresh_from_db()
        self.assertEqual(e.cep, "09220305")
        valores = set(e.canais.values_list("valor", flat=True))
        self.assertIn("11970683136", valores)
        self.assertNotIn("11900001111", valores)
        self.assertNotIn("11955556666", valores)
        self.assertTrue(e.dados_coletados.filter(campo="atividade_publicada").exists())
        self.assertFalse(e.dados_coletados.filter(campo="cnpj_candidato").exists())

    def test_sintese_so_grava_valor_literal_e_nao_situacao(self):
        from prospeccao.services.descoberta_google import aplicar_sintese, validar_sintese
        e = self.empresa("Transportadora Transvx", cidade="Santo André")
        refs = {"c113:5": "Transportadora Transvx (11) 97068-3136 Av. Engenheiro Santo André"}
        bruto = {"sintese": [
            {"campo": "telefone", "valor": "11970683136", "ref": "c113:5", "associacao": "aceita", "motivo": "no trecho"},
            {"campo": "telefone", "valor": "11900000000", "ref": "c113:5", "associacao": "aceita", "motivo": "inventado"},
            {"campo": "situacao_cadastral", "valor": "ATIVA", "ref": "c113:5", "associacao": "aceita", "motivo": "chute"},
            {"campo": "razao_social", "valor": "TRANSVX LTDA", "ref": "c999:0", "associacao": "aceita", "motivo": "sem fonte"},
        ]}
        aceitas, recusadas = validar_sintese(bruto, refs)
        aplicar_sintese(e, aceitas)
        self.assertTrue(e.canais.filter(valor="11970683136").exists())
        self.assertFalse(e.canais.filter(valor="11900000000").exists())
        self.assertFalse(e.situacao_cadastral)
        self.assertFalse(e.razao_social)
        self.assertTrue(any("situacao" in (r.get("campo") or "") for r in recusadas))

    def test_falha_isolada_nao_derruba_as_outras(self):
        from prospeccao.services.selecao import reprocessar_respostas_salvas
        p = self.pesquisa()
        frente = self.frente()
        a = self.empresa("Empresa Alfa Isolada")
        b = self.empresa("Empresa Beta Isolada")
        SelecaoPesquisa.objects.create(pesquisa=p, frente=frente, cnpj="ISO1", empresa=a)
        SelecaoPesquisa.objects.create(pesquisa=p, frente=frente, cnpj="ISO2", empresa=b)

        def lado(empresa, *args, **kwargs):
            if empresa.pk == a.pk:
                raise RuntimeError("falha desta empresa")
            return {"identificacao": {"acao": "pendente", "motivo": "seguiu"}, "novos_canais": 0}

        with mock.patch("prospeccao.services.descoberta_google.investigar_dirigido", lado):
            saida = reprocessar_respostas_salvas(p)
        self.assertEqual(len(saida), 2)
        self.assertIn("falha", [x["acao"] for x in saida])
        self.assertIn("pendente", [x["acao"] for x in saida])

    def test_retomada_nao_duplica_contato(self):
        p = self.pesquisa()
        frente = self.frente()
        e = self.empresa("Transporte Norte Sul", cidade="Santo André")
        ConsultaWeb.objects.create(
            pesquisa=p, empresa=e, finalidade="COMPLEMENTAR", provedor="google_serpapi", status="OK",
            consulta="Transporte Norte Sul Santo André",
            resultados=[{"tipo": "LOCAL", "titulo": "Transporte Norte Sul", "nome_citado": "Transporte Norte Sul",
                         "endereco_citado": "Rua A, 1 - Santo André - SP", "telefones": ["(11) 98888-7777"]}])
        sel = SelecaoPesquisa.objects.create(pesquisa=p, frente=frente, cnpj="RET1", empresa=e)
        completar_identificacao(e, sel, max_buscas=0, permitir_consulta_complementar=False)
        completar_identificacao(e, sel, max_buscas=0, permitir_consulta_complementar=False)
        self.assertEqual(e.canais.filter(tipo="TELEFONE").count(), 1)

    def test_conhecida_fica_fora_da_lista_externa(self):
        p = self.pesquisa()
        frente = self.frente()
        ext = self.empresa("Nova Externa Visivel")
        conhecida = self.empresa("Conta Ja Nossa", cnpj=doc("808080800001"), salesforce_id="001NOVA")
        op_ext = self.oportunidade(ext, status=Oportunidade.Status.LIBERADA)
        op_con = self.oportunidade(conhecida, tipo=Oportunidade.Tipo.CADASTRO_SEM_COMPRA,
                                   status=Oportunidade.Status.JA_CLIENTE)
        op_ext.pesquisas.add(p)
        op_con.pesquisas.add(p)
        SelecaoPesquisa.objects.create(pesquisa=p, frente=frente, cnpj="EXT1", empresa=ext,
                                       fila=SelecaoPesquisa.Fila.BUSCAR_CONTATO)
        SelecaoPesquisa.objects.create(pesquisa=p, frente=frente, cnpj=conhecida.cnpj, empresa=conhecida,
                                       fila=SelecaoPesquisa.Fila.CONHECIDA)
        _, externas = filtrar_oportunidades({"etapa": "disponiveis", "pesquisa": str(p.pk)}, self.user)
        _, lista_conhecidas = filtrar_oportunidades({"etapa": "conhecidas", "pesquisa": str(p.pk)}, self.user)
        ids_ext = set(externas.values_list("empresa_id", flat=True))
        ids_con = set(lista_conhecidas.values_list("empresa_id", flat=True))
        self.assertIn(ext.pk, ids_ext)
        self.assertNotIn(conhecida.pk, ids_ext)
        self.assertIn(conhecida.pk, ids_con)
        ind = indicadores_pesquisa(p, self.user)
        self.assertEqual(ind["conhecidas_lista"], lista_conhecidas.count())

    def test_filial_de_outro_municipio_fica_como_outra_cidade(self):
        e = self.empresa("Oficina do Bairro", cidade="Mauá")
        dados = dados_de(doc("909090900001"), "OFICINA DO BAIRRO LTDA", "OFICINA DO BAIRRO", "Santo André")
        decisao, motivo = avaliar_associacao(e, dados)
        self.assertEqual(decisao, "outra_cidade")
        self.assertIn("Santo André", motivo)
        outra = dados_de(doc("909090900002"), "PADARIA CENTRAL LTDA", "", "Santo André")
        self.assertEqual(avaliar_associacao(e, outra)[0], "rejeitar")  # nome não bate: não é a empresa

    def test_ramo_igual_nao_vincula_cnpj_de_outra_empresa(self):
        e = self.empresa("Terraplanagem Saraiva ABC", cidade="Santo André")
        dados = dados_de(doc("111111110001"), "TOP TERRA TERRAPLANAGEM LTDA",
                         "TOP TERRA SERVICOS DE TERRAPLANAGEM", "Santo André",
                         telefones=["11999990000"])
        from carteira.models import CanalEmpresa
        from prospeccao.services.contatos import registrar
        registrar(e, CanalEmpresa.Tipo.TELEFONE, "11999990000", fonte="google_local", url="",
                  origem=CanalEmpresa.Origem.PUBLICACAO, confianca=CanalEmpresa.Confianca.MEDIA,
                  aplica_a=CanalEmpresa.AplicaA.EMPRESA)
        decisao, _motivo = avaliar_associacao(e, dados)
        self.assertEqual(decisao, "ambiguo")

    def test_palavra_de_ramo_nao_pega_contato_de_outra_empresa(self):
        from prospeccao.services.identificacao_cnpj import item_refere_empresa, recuperar_contatos_salvos
        e = self.empresa("MAC TERRAPLENAGEM", cidade="Santo André")
        p = self.pesquisa()
        sel = SelecaoPesquisa.objects.create(
            pesquisa=p, frente=self.frente(), cnpj="MAC1", empresa=e, fila=SelecaoPesquisa.Fila.BUSCAR_CONTATO)
        ConsultaWeb.objects.create(
            empresa=e, pesquisa=p, finalidade="COMPLEMENTAR", provedor="google_serpapi", status="OK",
            consulta="terraplenagem Santo André",
            resultados=[{"tipo": "ORGANICO", "titulo": "Top Terraplenagem", "nome_citado": "Top Terraplenagem",
                         "emails": ["contato@topterraplenagem.com.br"], "url": "https://topterraplenagem.com.br/",
                         "dominio": "topterraplenagem.com.br"}])
        self.assertFalse(item_refere_empresa(e, {"titulo": "Top Terraplenagem", "nome_citado": "Top Terraplenagem",
                                                 "dominio": "topterraplenagem.com.br"}))
        recuperar_contatos_salvos(e, p.pk)
        self.assertFalse(e.canais.filter(valor__icontains="topterraplenagem").exists())
        sel.refresh_from_db()


def _fmt(cnpj):
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


class CasosReaisTests(_ApoioFluxo, TestCase):
    def test_rodape_preserva_cnpj_e_nao_some_antes_da_extracao(self):
        from prospeccao.conectores.site import extrair, trecho_relevante
        cnpj = doc("494446930001")
        html = (
            "<html><body><header>Transvx</header><main>Transporte rodoviário.</main>"
            f"<footer>TRANSVX TRANSPORTES LTDA CNPJ {_fmt(cnpj)} (11) 97068-3136</footer>"
            "<script type=\"application/ld+json\">"
            "{\"@type\":\"LocalBusiness\",\"name\":\"Transvx\",\"taxID\":\"" + cnpj + "\"}"
            "</script></body></html>"
        )
        texto, _links, _ancoras = extrair(html, "https://www.transvx.com.br/")
        self.assertIn(cnpj, texto.replace(".", "").replace("/", "").replace("-", ""))
        self.assertIn("97068", texto)
        trecho = trecho_relevante(texto, [cnpj])
        self.assertIn("CNPJ", trecho)

    def test_visao_do_google_nao_confirma_cadastro_e_page_token_e_segunda_requisicao(self):
        cnpj = doc("494446930001")
        bruto = {"organic_results": [{"title": "Transvx", "link": "https://www.transvx.com.br/", "snippet": "site"}],
                 "ai_overview": {"text_blocks": [{"type": "paragraph", "snippet": f"A Transvx publica CNPJ {_fmt(cnpj)}",
                                                  "reference_indexes": [0]}],
                                 "references": [{"index": 0, "title": "Transvx", "link": "https://www.transvx.com.br/",
                                                 "snippet": "rodapé"}]}}
        saida = GoogleSerpApi._extrair(bruto, "organico")
        visoes = [r for r in saida if r["tipo"] == "AI_OVERVIEW"]
        self.assertGreaterEqual(len(visoes), 2)
        self.assertTrue(all(r["confirmacao_cadastral"] is False for r in visoes))
        self.assertTrue(any(r["cnpj_citado"] == cnpj for r in visoes))
        fonte, _ = Fonte.objects.get_or_create(
            codigo="google_serpapi", defaults={"nome": "Google", "tipo": "DESCOBERTA", "habilitada": True})
        api = GoogleSerpApi(fonte)
        respostas = [
            {"search_metadata": {"id": "req-busca", "status": "Success"},
             "organic_results": [{"title": "Opala", "link": "https://opala.example/", "snippet": "telefone"}],
             "ai_overview": {"page_token": "tok-curto"}},
            {"search_metadata": {"id": "req-visao", "status": "Success"},
             "ai_overview": {"text_blocks": [{"snippet": "Opala Terraplanagem atende a região", "reference_indexes": [0]}],
                             "references": [{"index": 0, "title": "Opala", "link": "https://opala.example/",
                                             "snippet": "endereço"}]}},
        ]

        def requisitar(params, ctx):
            return respostas.pop(0)

        with mock.patch.dict("os.environ", {"SERPAPI_API_KEY": "segredo-teste"}), \
                mock.patch.object(api, "_requisitar", side_effect=requisitar):
            api.buscar("terraplenagem Mauá", None, contexto={"modalidade": "organico"})
        self.assertEqual(api.ultimo["acoes"], 2)
        self.assertEqual(api.ultimo["custo"], api.preco() * 2)
        ids = [a.get("request_id") for a in api.ultimo["acoes_detalhe"]]
        self.assertEqual(ids, ["req-busca", "req-visao"])
        self.assertNotIn("segredo-teste", str(api.ultimo))
        self.assertNotIn("tok-curto", str(api.ultimo["acoes_detalhe"]))

    def test_bj_diesel_vincula_o_cnpj_do_site_e_recusa_o_parecido(self):
        from carteira.models import DadoColetado
        from prospeccao.services.identificacao_cnpj import aproveitar_cnpj_publicado, avaliar_associacao
        certo = doc("375040670001")
        parecido = doc("375040680001")
        e = self.empresa("Mecânica BJ Diesel", cidade="Mauá", endereco="Rua das Flores, 10")
        CanalEmpresa.objects.create(
            empresa=e, tipo=CanalEmpresa.Tipo.SITE, valor="https://www.mecanicabjdiesel.com.br/",
            valor_normalizado="mecanicabjdiesel.com.br", origem_canal=CanalEmpresa.Origem.PUBLICACAO,
            confianca_associacao=CanalEmpresa.Confianca.MEDIA)
        DadoColetado.objects.create(
            empresa=e, campo="cnpj_publicado_no_site", fonte="site_publico", valor=certo,
            referencia="https://www.mecanicabjdiesel.com.br/")
        cadastro = dados_de(certo, "B J DIESEL MECANICA LTDA", "BJ DIESEL", "Mauá", logradouro="Rua das Flores, 10")
        outro = dados_de(parecido, "BJ DIESEL PECAS LTDA", "BJ DIESEL PECAS", "Mauá", logradouro="Avenida Brasil, 500")
        self.assertEqual(avaliar_associacao(e, outro)[0], "ambiguo")
        with mock.patch("prospeccao.conectores.receita.consultar_local", return_value=(cadastro, None)), \
                mock.patch("prospeccao.services.identificacao_cnpj._consultar_cadastral", return_value=("ok", cadastro)):
            info = aproveitar_cnpj_publicado(e, None, consultar=True)
        e.refresh_from_db()
        self.assertEqual(info["acao"], "cnpj_vinculado")
        self.assertEqual(e.cnpj, certo)
        self.assertNotEqual(e.cnpj, parecido)

    def test_piqueno_preserva_email_publicado_sem_corrigir(self):
        from prospeccao.conectores.site import emails_a_revisar, extrair
        html = "<footer>Piqueno Centro Automotivo contato@piqueno.com.b</footer>"
        texto, _, _ = extrair(html, "https://piqueno.example/")
        revisar = emails_a_revisar(texto)
        self.assertEqual(revisar, ["contato@piqueno.com.b"])
        self.assertNotIn("contato@piqueno.com.br", revisar)
        cnpj = doc("149778850001")
        e = self.empresa("Piqueno Centro Automotivo", cidade="Mauá", endereco="Rua José Sanches Parra, 16")
        cadastro = dados_de(cnpj, "DAVID RODRIGUES DIAS", "PIQUENO CENTRO AUTOMOTIVO", "Mauá",
                            logradouro="Rua José Sanches Parra, 16")
        decisao, _motivo = avaliar_associacao(e, cadastro)
        self.assertEqual(decisao, "adotar")

    def test_nove_separado_preserva_os_digitos_do_celular(self):
        from prospeccao.conectores.site import telefones_do_texto
        self.assertEqual(telefones_do_texto("(11) 9 4030 4775"), ["11940304775"])
        self.assertEqual(telefones_do_texto("(11) 2400-1112"), ["1124001112"])

    def test_palavra_frota_nao_traz_telefone_de_outra_empresa(self):
        from prospeccao.services.identificacao_cnpj import item_refere_empresa
        e = self.empresa("Rápido Frota")
        self.assertFalse(item_refere_empresa(e, {
            "tipo": "LOCAL", "titulo": "Frota Sem Endereço Transportes",
            "nome_citado": "Frota Sem Endereço Transportes", "telefones": ["(11) 2999-1234"]}))

    def test_endereco_em_mogi_nao_vira_maua_nem_e_descartado(self):
        decisao, motivo, nota = dg.avaliacao_regional(
            "Rua das Industrias, 100 - Mogi das Cruzes - SP", "SP", ["Mauá", "Santo André"])
        self.assertEqual(decisao, "manter")
        self.assertEqual(motivo, "")
        self.assertIn("Mogi Das Cruzes", nota)
        self.assertNotIn("Mauá", nota)
        self.assertEqual(dg.cidade_persistida("MOGI DAS CRUZES", ["Mauá"]), "Mogi Das Cruzes")
        fora, motivo_fora, _ = dg.avaliacao_regional(
            "R. X, 229 - Jardim Mauá, Novo Hamburgo - RS", "SP", ["Mauá"])
        self.assertEqual(fora, "descartar")
        self.assertIn("Novo Hamburgo", motivo_fora)

    def test_opala_com_telefone_continua_pendente_de_cnpj(self):
        e = self.empresa("Opala Terraplanagem", endereco="Rua das Flores, 10 - Mauá - SP")
        CanalEmpresa.objects.create(
            empresa=e, tipo=CanalEmpresa.Tipo.TELEFONE, valor="11988880000", valor_normalizado="11988880000",
            origem_canal=CanalEmpresa.Origem.PUBLICACAO, confianca_associacao=CanalEmpresa.Confianca.MEDIA)
        info = completar_identificacao(e, None, max_buscas=0)
        e.refresh_from_db()
        self.assertFalse(e.cnpj)
        self.assertEqual(info["acao"], "pendente_sem_busca")
        self.assertIn("CNPJ não identificado", info["motivo"])
        self.assertEqual(e.canais.filter(tipo="TELEFONE").count(), 1)

    def test_telefone_anterior_nao_e_substituido(self):
        from prospeccao.services.contatos import registrar
        e = self.empresa("Metaltezza")
        registrar(e, CanalEmpresa.Tipo.TELEFONE, "1144441111", fonte="site_publico",
                  origem=CanalEmpresa.Origem.PUBLICACAO, confianca=CanalEmpresa.Confianca.MEDIA,
                  aplica_a=CanalEmpresa.AplicaA.EMPRESA, evidencia="contato anterior")
        registrar(e, CanalEmpresa.Tipo.TELEFONE, "1144442222", fonte="site_publico",
                  origem=CanalEmpresa.Origem.PUBLICACAO, confianca=CanalEmpresa.Confianca.MEDIA,
                  aplica_a=CanalEmpresa.AplicaA.EMPRESA, evidencia="contato da página de contato")
        numeros = set(e.canais.filter(tipo="TELEFONE").values_list("valor", flat=True))
        self.assertEqual(numeros, {"1144441111", "1144442222"})

    def test_botao_orcamento_e_link_sem_numero(self):
        from prospeccao.conectores.site import extrair
        html = (
            '<a href="https://wa.me/5511999990001">Orçamento</a>'
            '<button data-href="https://api.whatsapp.com/send?phone=5511999990002">Atendimento</button>'
            '<a href="https://wa.me/message/ABC123">WhatsApp</a>'
            '<footer>Empresa Exemplo CNPJ 11.222.333/0001-81</footer>'
        )
        texto, links, ancoras = extrair(html, "https://exemplo.test/")
        self.assertIn("[whatsapp:11999990001|https://wa.me/5511999990001]", texto)
        self.assertIn("[whatsapp:11999990002|https://api.whatsapp.com/send?phone=5511999990002]", texto)
        self.assertIn("[whatsapp_link:https://wa.me/message/ABC123]", texto)
        self.assertIn("11.222.333/0001-81", texto)
        self.assertTrue(any("Orçamento" in t for _, t in ancoras))
        self.assertFalse(any("11999990001" in link for link in links))

    def test_google_local_grava_whatsapp_uma_vez_e_nao_apaga(self):
        from prospeccao.conectores.busca_web import GoogleSerpApi
        from prospeccao.services.contatos import registrar
        item = GoogleSerpApi._local({
            "title": "Oficina Exemplo", "phone": "(11) 99999-0001",
            "links": {"website": "https://exemplo.test/", "whatsapp": "https://wa.me/5511999990001"},
        }, "local", "visto no resultado local")
        self.assertEqual(item["whatsapp"][0]["numero"], "11999990001")
        self.assertEqual(item["whatsapp"][0]["url"], "https://wa.me/5511999990001")
        e = self.empresa("Oficina Exemplo")
        dg.registrar_contatos_item(e, item, "oficina Mauá")
        canal = e.canais.get(tipo="TELEFONE")
        self.assertTrue(canal.whatsapp_explicito)
        self.assertEqual(e.canais.filter(tipo="TELEFONE").count(), 1)
        registrar(e, CanalEmpresa.Tipo.TELEFONE, "11999990001", fonte="google_local",
                  origem=CanalEmpresa.Origem.PUBLICACAO, confianca=CanalEmpresa.Confianca.MEDIA,
                  aplica_a=CanalEmpresa.AplicaA.EMPRESA, evidencia="nova importação sem o link", whatsapp=False)
        canal.refresh_from_db()
        self.assertTrue(canal.whatsapp_explicito)
        self.assertIn("wa.me/5511999990001", canal.referencia)

    def test_diretorio_nao_leva_telefone_de_outra_empresa(self):
        from prospeccao.services.contatos import selecionar_contatos_do_texto
        e = self.empresa("Opala Terraplanagem", cidade="Mauá")
        texto = "Opala Terraplanagem (11) 98888-0001 Top Terraplenagem (11) 97777-0002"
        aceitos, rejeitados, _, _ = selecionar_contatos_do_texto(
            e, texto, ["11988880001", "11977770002"], [], ["11222333000181", "44555666000100"], "guiamais.com.br")
        self.assertEqual(aceitos, ["11988880001"])
        self.assertEqual(rejeitados, ["11977770002"])

    def test_provedor_parcial_permanece_desligado(self):
        from prospeccao.conectores.identidade_parcial import ProvedorIdentidadeParcial
        with mock.patch.dict("os.environ", {"ECONODATA_API_KEY": ""}):
            resposta = ProvedorIdentidadeParcial().identificar(nome="Opala Terraplanagem", telefone="11988880000")
        self.assertFalse(resposta["habilitado"])
        self.assertEqual(resposta["candidatos"], [])
