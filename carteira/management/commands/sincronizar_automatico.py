"""
Sincronização automática com o Salesforce (somente leitura), para rodar de hora em hora.

    python manage.py sincronizar_automatico                 # clientes, contatos, leads e notas dos últimos 15 dias
    python manage.py sincronizar_automatico --dias 30       # janela de notas maior

- Agendamento: systemd timer ou cron na VPS (deploy/), Agendador de Tarefas no Windows (sincronizar_salesforce.cmd).
- Trava: se a rodada anterior ainda estiver em andamento, esta sai sem fazer nada (não empilha execuções).
- Com a carteira sincronizada há menos de PROSPECCAO_SYNC_JANELA_MIN minutos (padrão 70), o botão Buscar não lê o
  Salesforce: usa esta sincronização. Se a automação parar, a busca volta a sincronizar sozinha.
"""
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

TRAVA = 0x0B1DA5  # chave do pg_advisory_lock desta rotina


class Command(BaseCommand):
    help = "Sincroniza clientes, contatos, leads e notas recentes do Salesforce (para agendamento de hora em hora)."

    def add_arguments(self, parser):
        parser.add_argument("--dias", type=int, default=15, help="Janela de notas fiscais (padrão: 15 dias)")
        parser.add_argument("--sem-contatos", action="store_true", help="Não sincroniza contatos (mais rápido)")

    def _travar(self):
        if connection.vendor != "postgresql":
            return True
        with connection.cursor() as c:
            c.execute("SELECT pg_try_advisory_lock(%s)", [TRAVA])
            return bool(c.fetchone()[0])

    def _destravar(self):
        if connection.vendor == "postgresql":
            with connection.cursor() as c:
                c.execute("SELECT pg_advisory_unlock(%s)", [TRAVA])

    def handle(self, *args, **o):
        if not self._travar():
            self.stdout.write("Sincronização anterior ainda em andamento; esta rodada foi pulada.")
            return
        inicio = time.monotonic()
        try:
            partes = ["clientes", "leads", "compras"] + ([] if o["sem_contatos"] else ["contatos"])
            call_command("sync_salesforce", *[f"--{p}" for p in partes], "--ultimos-dias", str(o["dias"]),
                         stdout=self.stdout, stderr=self.stderr)
            from prospeccao.services.conversao import marcar_convertidos
            n = marcar_convertidos()  # lead com venda contada sai da base de trabalho do Inside Sales
            if n:
                self.stdout.write(f"{n} lead(s) passaram a cliente (venda confirmada por nota fiscal).")
        except CommandError as e:
            raise CommandError(f"Sincronização automática falhou: {e}")
        finally:
            self._destravar()
        self.stdout.write(self.style.SUCCESS(f"Sincronização automática concluída em {time.monotonic() - inicio:.0f} s."))
