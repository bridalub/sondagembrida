"""
Classifica as oportunidades pelo escopo comercial (services/escopo.py). Nada é apagado e nenhum status é alterado:
"fora do escopo" só sai da lista padrão e não recebe gasto pago. Decisões do gestor (escopo_humano) são mantidas.

    python manage.py aplicar_escopo            # simula e mostra a distribuição
    python manage.py aplicar_escopo --gravar   # grava
"""
from collections import Counter

from django.core.management.base import BaseCommand

from prospeccao.models import Oportunidade
from prospeccao.services import escopo as esc


class Command(BaseCommand):
    help = "Classifica as oportunidades pelo escopo comercial (sem apagar nada)."

    def add_arguments(self, parser):
        parser.add_argument("--gravar", action="store_true")

    def handle(self, *args, **o):
        cont, mudou = Counter(), 0
        for op in Oportunidade.objects.select_related("empresa"):
            if op.escopo_humano:
                cont[f"{op.escopo} (gestor)"] += 1
                continue
            novo, motivo = esc.classificar(op.empresa)
            cont[novo] += 1
            if (op.escopo, op.escopo_motivo) != (novo, motivo[:255]):
                mudou += 1
                if o["gravar"]:
                    Oportunidade.objects.filter(pk=op.pk).update(escopo=novo, escopo_motivo=motivo[:255])
        for k, n in sorted(cont.items(), key=lambda x: -x[1]):
            self.stdout.write(f"  {esc.ROTULOS.get(k, k)}: {n}")
        self.stdout.write(f"{mudou} oportunidade(s) {'atualizada(s)' if o['gravar'] else 'mudariam (use --gravar)'}")
