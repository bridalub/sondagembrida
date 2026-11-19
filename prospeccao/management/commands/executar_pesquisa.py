"""
Executa uma pesquisa agora, no terminal (útil para validação controlada).

    python manage.py executar_pesquisa <id_pesquisa>
    python manage.py executar_pesquisa <id_pesquisa> --csv arquivo.csv   # importação de arquivo (não é mineração)
"""
from dataclasses import asdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from prospeccao.conectores.csv_arquivo import ler_csv
from prospeccao.models import Execucao, Pesquisa
from prospeccao.services import fila
from prospeccao.services.pipeline import executar


class Command(BaseCommand):
    help = "Enfileira e executa imediatamente uma pesquisa de prospecção."

    def add_arguments(self, parser):
        parser.add_argument("pesquisa_id", type=int)
        parser.add_argument("--csv", help="Importar candidatos de um arquivo CSV em vez de descobrir")
        parser.add_argument("--somente-investigar", action="store_true",
                            help="Pesquisa planejada: não seleciona novos; (re)investiga os que aguardam")

    def handle(self, *args, **opts):
        try:
            p = Pesquisa.objects.get(pk=opts["pesquisa_id"])
        except Pesquisa.DoesNotExist:
            raise CommandError("Pesquisa não encontrada")
        gatilho, params = Execucao.Gatilho.COMANDO, {"somente_investigar": opts["somente_investigar"]}
        if opts["csv"]:
            caminho = Path(opts["csv"])
            resultados, erros = ler_csv(caminho.read_bytes(), caminho.name)
            gatilho, params = Execucao.Gatilho.CSV, {"arquivo": caminho.name, "resultados": [asdict(r) for r in resultados],
                                                     "erros_leitura": erros[:200]}
        ex, nova = fila.enfileirar(p, gatilho, parametros=params)
        if not nova:
            raise CommandError(f"Já existe execução ativa ({ex.pk}, {ex.get_status_display()}) para esta pesquisa")
        self.stdout.write(f"Execução {ex.pk} iniciada...")
        executar(ex.pk)
        ex.refresh_from_db()
        self.stdout.write(self.style.SUCCESS(
            f"{ex.get_status_display()} (completa={ex.completa}): resultados={ex.resultados} novos={ex.candidatos_novos} "
            f"existentes={ex.candidatos_existentes} identificados={ex.identificados} pendentes={ex.pendentes} "
            f"conhecidos_carteira={ex.conhecidos_carteira} oportunidades_novas={ex.oportunidades_novas} "
            f"consultas={ex.consultas} erros={ex.erros}"))
        for lim in ex.limites_atingidos:
            self.stdout.write(self.style.WARNING(f"  limite: {lim}"))
        if ex.mensagem:
            self.stdout.write(self.style.WARNING(f"  {ex.mensagem}"))
