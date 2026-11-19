"""
Worker de prospecção (processa a fila de execuções no banco).

    python manage.py prospeccao_worker              # laço contínuo (ex.: serviço do Windows / NSSM)
    python manage.py prospeccao_worker --uma-vez    # processa o que houver e sai (Agendador de Tarefas)

A cada ciclo: marca como INTERROMPIDA execuções sem sinal; se ConfiguracaoAnalise.agendamento_ativo,
cria execuções devidas das pesquisas com frequência; executa as PENDENTES, uma por vez.
"""
import time

from django.core.management.base import BaseCommand

from prospeccao.services import fila


class Command(BaseCommand):
    help = "Processa a fila de execuções de prospecção."

    def add_arguments(self, parser):
        parser.add_argument("--uma-vez", action="store_true")
        parser.add_argument("--intervalo", type=int, default=30, help="Segundos entre verificações da fila")

    def handle(self, *args, **opts):
        while True:
            from prospeccao.services.retomada import processar_devidas
            retomadas = processar_devidas()
            if retomadas:
                self.stdout.write(f"{retomadas} consulta(s) adiada(s) retomada(s).")
            travadas = fila.recuperar_travadas()
            if travadas:
                self.stdout.write(f"{travadas} execução(ões) sem sinal marcadas como interrompidas.")
            for ex in fila.agendar_devidas():
                self.stdout.write(f"Agendada: execução {ex.pk} ({ex.pesquisa})")
            while True:
                ex = fila.processar_proxima()
                if ex is None:
                    break
                ex.refresh_from_db()
                self.stdout.write(f"Execução {ex.pk} ({ex.pesquisa}): {ex.get_status_display()} – "
                                  f"{ex.resultados} resultados, {ex.candidatos_novos} novos, "
                                  f"{ex.oportunidades_novas} oportunidades novas")
            if opts["uma_vez"]:
                return
            time.sleep(opts["intervalo"])
