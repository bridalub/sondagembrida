from django.core.management.base import BaseCommand

from carteira.services.indicadores import recalcular_indicadores, vincular_notas_pendentes


class Command(BaseCommand):
    help = "Revincula notas pendentes e recalcula os indicadores de compra (sem acessar o Salesforce)."

    def handle(self, *args, **opts):
        n = vincular_notas_pendentes()
        recalcular_indicadores()
        self.stdout.write(self.style.SUCCESS(f"Indicadores recalculados. Notas vinculadas agora: {n}."))
