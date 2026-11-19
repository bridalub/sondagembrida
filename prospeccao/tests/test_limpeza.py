"""Limpeza do sistema: mantém só logins, leads já contatados, pesquisas, Receita e custos; apaga o resto (com cópia)."""
import gzip
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from carteira.models import (Abordagem, CanalEmpresa, Contato, Empresa, Importacao, LeadSalesforce, NotaFiscal)
from prospeccao.models import Execucao, Oportunidade, Pesquisa, SelecaoPesquisa, UsoIA
from prospeccao.services import limpeza


class LimpezaTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict("os.environ", {"LIMPEZA_BACKUP_DIR": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        self.p = Pesquisa.objects.create(nome="P", modo="PLANEJADA", uf="SP", cidades="Mauá")
        self.ex = Execucao.objects.create(pesquisa=self.p, status="CONCLUIDA")
        self.web = Empresa.objects.create(nome="ACHADA NA WEB", origem="DESCOBERTA")
        CanalEmpresa.objects.create(empresa=self.web, tipo="TELEFONE", valor="1145470000", origem_canal="PUBLICACAO")
        Oportunidade.objects.create(empresa=self.web, tipo="NOVA_EXTERNA")
        SelecaoPesquisa.objects.create(pesquisa=self.p, cnpj="W1", empresa=self.web)
        Contato.objects.create(empresa=self.web, nome="Fulano")
        self.user = get_user_model().objects.create_user("g", password="x", is_staff=True)
        self.contatada = Empresa.objects.create(nome="JA ABORDADA", origem="DESCOBERTA")
        CanalEmpresa.objects.create(empresa=self.contatada, tipo="EMAIL", valor="a@b.com", origem_canal="PUBLICACAO")
        Abordagem.objects.create(empresa=self.contatada, usuario=self.user, data_hora=timezone.now(),
                                 **({"canal": "TELEFONE"} if hasattr(Abordagem, "canal") else {}))
        Oportunidade.objects.create(empresa=self.contatada, tipo="NOVA_EXTERNA")
        self.sf = Empresa.objects.create(nome="CLIENTE SF", salesforce_id="001X", origem="SALESFORCE")
        imp = Importacao.objects.create(tipo=Importacao._meta.get_field("tipo").choices[0][0])
        NotaFiscal.objects.create(**self._nota(self.sf, imp))
        LeadSalesforce.objects.create(**self._lead())
        UsoIA.objects.create(tipo="PLANO", provedor="openai", versao_prompt="v", pesquisa=self.p,
                             custo_estimado_usd=Decimal("0.01"))

    @staticmethod
    def _obrigatorios(model, extra):
        """Preenche campos obrigatórios sem default com valores neutros (o teste não depende deles)."""
        dados = dict(extra)
        for f in model._meta.concrete_fields:
            if f.name in dados or f.primary_key or f.null or f.has_default() or f.blank and f.get_internal_type() in (
                    "CharField", "TextField") or getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False):
                continue
            if f.is_relation:
                continue
            t = f.get_internal_type()
            dados[f.name] = ({"DecimalField": Decimal("1"), "DateField": timezone.localdate(),
                              "DateTimeField": timezone.now(), "BooleanField": False}.get(t)
                             or (1 if "Integer" in t else f.choices[0][0] if f.choices else "X1"))
        return dados

    def _nota(self, empresa, imp):
        return self._obrigatorios(NotaFiscal, {"empresa": empresa, "primeira_importacao": imp,
                                                "ultima_importacao": imp})

    def _lead(self):
        return self._obrigatorios(LeadSalesforce, {"salesforce_id": "00QX"})

    def test_padrao_apaga_prospeccao_e_preserva_carteira(self):
        pv = limpeza.previa()
        self.assertEqual((pv["leads_contatados_mantidos"], pv["empresas_encontradas"], pv["contas_salesforce"]), (1, 1, 1))
        self.assertTrue(Empresa.objects.filter(pk=self.web.pk).exists())  # prévia não apaga
        feito = limpeza.limpar(self.user)
        self.assertFalse(Empresa.objects.filter(pk=self.web.pk).exists())
        self.assertTrue(Empresa.objects.filter(pk=self.sf.pk).exists())  # carteira NÃO é efeito colateral
        self.assertTrue(NotaFiscal.objects.exists() and LeadSalesforce.objects.exists() and Importacao.objects.exists())
        self.assertTrue(self.contatada.canais.filter(valor="a@b.com").exists())
        self.assertEqual(Abordagem.objects.count(), 1)
        self.assertFalse(Execucao.objects.exists() or SelecaoPesquisa.objects.exists())
        self.assertEqual(UsoIA.objects.count(), 1)
        self.assertFalse(feito["incluiu_carteira"])

    def test_carteira_so_com_escolha_explicita(self):
        feito = limpeza.limpar(self.user, incluir_carteira=True)
        self.assertEqual(list(Empresa.objects.values_list("pk", flat=True)), [self.contatada.pk])
        self.assertFalse(NotaFiscal.objects.exists() or LeadSalesforce.objects.exists() or Importacao.objects.exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.assertTrue(Pesquisa.objects.filter(pk=self.p.pk).exists())
        copia = Path(feito["backup"])
        with gzip.open(copia, "rt", encoding="utf-8") as f:
            self.assertIn("CLIENTE SF", f.read())  # a cópia tem o que foi apagado

    def test_sem_copia_nao_apaga(self):
        with mock.patch.object(limpeza, "backup", side_effect=OSError("disco cheio")):
            with self.assertRaises(limpeza.LimpezaBloqueada):
                limpeza.limpar(self.user)
        self.assertEqual(Empresa.objects.count(), 3)

    def test_bloqueia_com_busca_em_andamento(self):
        Execucao.objects.create(pesquisa=self.p, status="EXECUTANDO")
        with self.assertRaises(limpeza.LimpezaBloqueada):
            limpeza.limpar(self.user)
        self.assertTrue(Empresa.objects.filter(pk=self.web.pk).exists())

    def test_tela_exige_confirmacao_digitada(self):
        self.client.force_login(self.user)
        url = reverse("prospeccao:limpar_resultados")
        self.assertContains(self.client.get(url), "Carteira Salesforce local (opcional, separado)")
        self.client.post(url, {"confirmacao": "sim"})
        self.assertTrue(Empresa.objects.filter(pk=self.web.pk).exists())
        self.client.post(url, {"confirmacao": "LIMPAR"})  # sem marcar a carteira
        self.assertFalse(Empresa.objects.filter(pk=self.web.pk).exists())
        self.assertTrue(Empresa.objects.filter(pk=self.sf.pk).exists())
