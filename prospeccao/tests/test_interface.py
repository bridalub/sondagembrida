"""
Testes da interface reformulada (dados SINTÉTICOS, sem rede).

Garantem que a reorganização das telas não muda regras comerciais:
- salvar pesquisa não cria execução (nenhuma chamada paga);
- indicadores da visão geral abrem exatamente os registros contados;
- abrir empresa, listar e filtrar não registram abordagem;
- filtros antigos (?status=, ?abordagem=) continuam funcionando;
- redirecionamentos pós-ação só aceitam endereços deste site.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from carteira.models import Abordagem, CanalEmpresa, Empresa, Importacao
from carteira.services import cnpj as cnpj_srv
from prospeccao.models import Execucao, Fonte, Oportunidade, Pesquisa
from prospeccao.views import filtrar_oportunidades


def cnpj(base12):
    return base12 + cnpj_srv.calcular_dv(base12)


class InterfaceTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.vend = U.objects.create_user("vend", password="x")
        self.outro = U.objects.create_user("outro", password="x")
        self.gestor = U.objects.create_user("gestor", password="x", is_staff=True)
        agora = timezone.now()
        Importacao.objects.create(tipo=Importacao.Tipo.CLIENTES, origem="teste", status=Importacao.Status.SUCESSO,
                                  iniciado_em=agora, finalizado_em=agora)
        self.ops = {}
        dados = [("pronta", "111222330001", "LIBERADA", "FROTA", True),
                 ("nova", "222333440001", "NOVA", "FROTA", False),
                 ("abordada", "333444550001", "LIBERADA", "INDUSTRIA", True),
                 ("fora", "444555660001", "LIBERADA", "FORA", True),
                 ("semcontato", "555666770001", "NOVA", "FROTA", False)]
        for chave, base, st, esc, com_tel in dados:
            e = Empresa.objects.create(cnpj=cnpj(base), cnpj_status="VALIDO", origem="DESCOBERTA", nome=f"EMPRESA {chave.upper()} LTDA",
                                       cidade="Guarulhos", uf="SP", situacao_cadastral="ATIVA",
                                       telefone="(11) 2400-0000" if com_tel else "")
            self.ops[chave] = Oportunidade.objects.create(empresa=e, tipo=Oportunidade.Tipo.NOVA_EXTERNA, status=st, escopo=esc)
        CanalEmpresa.objects.create(empresa=self.ops["nova"].empresa, tipo="EMAIL", valor="compras@nova.example",
                                    origem_canal="PUBLICACAO", confianca_associacao="ALTA", fonte="site")
        Abordagem.objects.create(empresa=self.ops["abordada"].empresa, usuario=self.vend, canal="TELEFONE",
                                 resultado="CONVERSA_INTERESSE")  # contato efetivo
        self.client.force_login(self.vend)

    def _ids(self, **params):
        q = QueryDict(mutable=True)
        q.update(params)
        return set(filtrar_oportunidades(q, self.vend)[1].values_list("pk", flat=True))

    def test_etapas_e_filtros_antigos(self):
        o = self.ops
        self.assertEqual(self._ids(etapa="primeira"), {o["pronta"].pk})  # FORA fica oculta por padrão
        self.assertEqual(self._ids(etapa="conferir"), {o["nova"].pk, o["semcontato"].pk})
        self.assertEqual(self._ids(etapa="andamento"), {o["abordada"].pk})
        self.assertEqual(self._ids(contato="sem"), {o["semcontato"].pk})
        self.assertEqual(self._ids(contato="email"), {o["nova"].pk})
        self.assertIn(o["fora"].pk, self._ids(etapa="todas", escopo="todos"))
        # links antigos continuam válidos
        self.assertEqual(self._ids(abordagem="primeira"), {o["pronta"].pk})
        self.assertEqual(self._ids(status="NOVA"), {o["nova"].pk, o["semcontato"].pk})

    def test_indicadores_da_visao_geral_batem_com_as_listas(self):
        self.client.force_login(self.gestor)  # visão geral e listas: papel ADM
        r = self.client.get(reverse("prospeccao:visao_geral"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["n_primeira"], len(self._ids(etapa="primeira")))
        self.assertEqual(r.context["n_sem_contato"], len(self._ids(contato="sem")))
        self.assertEqual(r.context["n_conferir"], len(self._ids(etapa="conferir", tipo="NOVA_EXTERNA")))
        lista = self.client.get(reverse("prospeccao:oportunidades") + "?etapa=primeira")
        self.assertEqual(lista.context["total"], r.context["n_primeira"])

    def test_navegar_nao_registra_abordagem(self):
        self.client.force_login(self.gestor)  # telas de gestão: papel ADM
        antes = Abordagem.objects.count()
        e = self.ops["pronta"].empresa
        for url in [reverse("prospeccao:oportunidades"), reverse("prospeccao:oportunidades") + "?etapa=primeira&cidade=Guarulhos",
                    reverse("prospeccao:empresa", args=[e.pk]), reverse("carteira:registrar_abordagem", args=[e.pk]),
                    reverse("carteira:retornos"), reverse("prospeccao:visao_geral"), reverse("prospeccao:revisao"),
                    reverse("prospeccao:integracoes"), reverse("prospeccao:parametros")]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(Abordagem.objects.count(), antes)

    def test_voltar_da_empresa_preserva_filtros(self):
        self.client.force_login(self.gestor)  # filtros de "Meus leads": papel ADM
        self.client.get(reverse("prospeccao:oportunidades") + "?etapa=primeira&cidade=Guarulhos&page=1")
        r = self.client.get(reverse("prospeccao:empresa", args=[self.ops["pronta"].empresa.pk]))
        self.assertIn("etapa=primeira", r.context["voltar_url"])
        self.assertIn("cidade=Guarulhos", r.context["voltar_url"])

    def test_salvar_pesquisa_nao_executa(self):
        self.client.force_login(self.gestor)
        fonte = Fonte.objects.filter(codigo="osm_overpass").first() or Fonte.objects.filter(tipo=Fonte.Tipo.DESCOBERTA).first()
        r = self.client.post(reverse("prospeccao:pesquisa_nova"), {
            "nome": "Coleta – Mogi", "modo": "SIMPLES", "uf": "sp", "cidades": "Mogi das Cruzes", "fontes": [fonte.pk],
            "termos_busca": "coleta", "situacoes_ui": "1", "situacoes_sel": ["ATIVA", "SUSPENSA"], "ativa": "on",
            "idade_desconhecida": "SINALIZAR"})
        p = Pesquisa.objects.get(nome="Coleta – Mogi")
        self.assertRedirects(r, reverse("prospeccao:pesquisa_detalhe", args=[p.pk]))
        self.assertEqual(Execucao.objects.count(), 0)
        self.assertEqual((p.uf, p.situacoes_receita, p.limite_resultados, p.teto_custo_usd), ("SP", "ATIVA,SUSPENSA", 200, None))

    def test_erro_em_campo_avancado_aparece(self):
        self.client.force_login(self.gestor)
        r = self.client.post(reverse("prospeccao:pesquisa_nova"), {
            "nome": "X", "modo": "PLANEJADA", "uf": "SP", "cidades": "Guarulhos", "situacoes_ui": "1",
            "max_sites_execucao": "-1", "idade_desconhecida": "SINALIZAR"})
        self.assertEqual(r.status_code, 200)
        form = r.context["form"]
        self.assertTrue(form.erro_avancado and form.erro_objetivo and form.erro_regiao)
        self.assertContains(r, "erro-resumo")
        self.assertEqual(Pesquisa.objects.count(), 0)

    def test_abordagem_volta_para_a_empresa_e_recusa_destino_externo(self):
        e = self.ops["pronta"].empresa
        base = {"data_hora": timezone.localtime().strftime("%Y-%m-%dT%H:%M"), "canal": "TELEFONE",
                "resultado": "CONVERSA_RETORNAR", "data_retorno": (date.today() + timedelta(days=2)).isoformat(),
                "concluir_pendentes": "on", "sem_cnpj": "on"}
        r = self.client.post(reverse("carteira:registrar_abordagem", args=[e.pk]), {**base, "proximo": "https://exemplo.invalido/"})
        self.assertRedirects(r, reverse("prospeccao:empresa", args=[e.pk]), fetch_redirect_response=False)
        self.client.force_login(self.gestor)  # tela de retornos: papel ADM
        r = self.client.get(reverse("carteira:retornos"))
        self.assertContains(r, e.nome)

    def test_responsavel_de_outro_nao_mostra_registrar(self):
        op = self.ops["pronta"]
        op.responsavel = self.outro
        op.save()
        r = self.client.get(reverse("prospeccao:empresa", args=[op.empresa.pk]))
        self.assertFalse(r.context["pode_abordar"])
        self.assertNotContains(r, reverse("carteira:registrar_abordagem", args=[op.empresa.pk]))

    def test_login_usa_logo_oficial(self):
        self.client.logout()
        r = self.client.get(reverse("login"))
        self.assertContains(r, "brida/img/logo-brida.svg")
        self.assertNotContains(r, "logo-brida.png")
