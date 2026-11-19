"""Sincronização de hora em hora: o comando chama o Salesforce só para notas recentes, e a busca não relê
a carteira quando ela foi sincronizada há pouco."""
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from prospeccao.services import cruzamento as cz


class SincronizacaoAutomaticaTests(TestCase):
    def test_comando_sincroniza_notas_recentes(self):
        with mock.patch("carteira.management.commands.sincronizar_automatico.call_command") as cc:
            call_command("sincronizar_automatico", "--dias", "10")
        args = cc.call_args[0]
        self.assertEqual(args[0], "sync_salesforce")
        self.assertIn("--compras", args)
        self.assertIn("--clientes", args)
        self.assertEqual(args[args.index("--ultimos-dias") + 1], "10")

    def test_busca_nao_rele_salesforce_com_carteira_fresca(self):
        recente = timezone.now() - timedelta(minutes=20)
        with mock.patch.object(cz, "ultima_sincronizacao_clientes", return_value=recente), \
                mock.patch("sys.argv", ["manage.py", "runserver"]), \
                mock.patch("carteira.services.salesforce.conectar") as conectar:
            ok, aviso = cz.tentar_atualizar_carteira()
        self.assertTrue(ok)
        self.assertIn("atualização automática", aviso)
        conectar.assert_not_called()

    def test_busca_sincroniza_quando_automacao_parou(self):
        antiga = timezone.now() - timedelta(hours=5)
        with mock.patch.object(cz, "ultima_sincronizacao_clientes", return_value=antiga), \
                mock.patch("sys.argv", ["manage.py", "runserver"]), \
                mock.patch("carteira.services.salesforce.conectar", side_effect=RuntimeError("sem rede")) as conectar:
            cz.tentar_atualizar_carteira()
        conectar.assert_called()
