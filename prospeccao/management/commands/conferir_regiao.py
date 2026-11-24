"""Confere, nos dados reais, a indicação de vendedor/coordenador pela região do Salesforce.

Uso: python manage.py conferir_regiao
Mostra quantas contas têm o tipo e o coordenador do vendedor (vindos da sincronização), os vendedores de rua e seus
coordenadores, e quantos leads do Inside Sales recebem indicação. Somente leitura.
"""
from collections import Counter

from django.core.management.base import BaseCommand

from carteira.models import Empresa
from prospeccao.services import regiao


class Command(BaseCommand):
    help = "Confere a indicação de vendedor/coordenador pela região (somente leitura)."

    def handle(self, *args, **opts):
        contas = Empresa.objects.filter(salesforce_id__isnull=False)
        total = contas.count()
        com_tipo = contas.exclude(tipo_vendedor="").count()
        com_coord = contas.exclude(coordenador_vendedor="").count()
        self.stdout.write(f"Contas do Salesforce: {total} · com tipo do vendedor: {com_tipo} · "
                          f"com coordenador do vendedor: {com_coord}")
        if total and com_coord == 0:
            self.stdout.write("  ATENÇÃO: rode a sincronização das contas para trazer tipo e coordenador do vendedor.")
        self.stdout.write(f"Time interno de apoio (fora da indicação): {', '.join(sorted(regiao.apoio())) or '—'}")
        ap = regiao.apoio()
        pares = Counter()
        for v, c, cv, t in contas.exclude(vendedor="").values_list("vendedor", "coordenador", "coordenador_vendedor",
                                                                   "tipo_vendedor"):
            coord = cv or c
            if regiao.de_rua(v, coord, ap):
                pares[(coord, v, t)] += 1
        self.stdout.write(f"Vendedores de rua: {len(pares)} (coordenador · vendedor · tipo · contas), 25 maiores:")
        for (c, v, t), n in pares.most_common(25):
            self.stdout.write(f"  {c or '—'} · {v} · {t or '—'} · {n}")
        m = regiao.mapa(forcar=True)
        from prospeccao.views_inside import base_inside
        bases, coords = Counter(), Counter()
        for o in base_inside().select_related("empresa"):
            r = regiao.da_empresa(o.empresa, o.escopo, m)
            bases["sem indicação" if not r else ("CEP" if r["base"].startswith("CEP") else "município")] += 1
            if r:
                coords[r["coordenador"]] += 1
        self.stdout.write("Leads do Inside Sales por base da indicação: " +
                          " · ".join(f"{k}: {v}" for k, v in bases.most_common()))
        self.stdout.write("Leads por coordenador indicado: " + " · ".join(f"{k}: {v}" for k, v in coords.most_common()))
