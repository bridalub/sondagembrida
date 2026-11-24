"""Confere, nos dados reais, se o coordenador/vendedor pela região do Salesforce é confiável.

Uso: python manage.py conferir_regiao
Mostra quantos municípios têm um coordenador só, os municípios divididos e quantos leads do Inside Sales recebem
indicação (por município, por CEP ou nenhuma). Somente leitura.
"""
from collections import Counter

from django.core.management.base import BaseCommand

from prospeccao.services import regiao


class Command(BaseCommand):
    help = "Confere a indicação de coordenador/vendedor pela região (somente leitura)."

    def handle(self, *args, **opts):
        m = regiao.mapa(forcar=True)
        cidades = {k: v for k, v in m.items() if k[0] == "cidade"}
        if not cidades:
            self.stdout.write("Nenhuma conta do Salesforce com coordenador/vendedor e município. Rode a sincronização.")
            return
        unico = sum(1 for v in cidades.values() if v["coord_pct"] == 100)
        forte = sum(1 for v in cidades.values() if v["coord_pct"] >= 80)
        self.stdout.write(f"Municípios com contas do Salesforce: {len(cidades)}")
        self.stdout.write(f"  com um coordenador só: {unico} ({unico * 100 // len(cidades)}%)")
        self.stdout.write(f"  com coordenador predominante (>= 80% das contas): {forte} ({forte * 100 // len(cidades)}%)")
        divididos = sorted(((v["contas"], k, v) for k, v in cidades.items() if v["coord_pct"] < 80), reverse=True)[:15]
        if divididos:
            self.stdout.write("Municípios mais divididos (contas · cidade/UF · coordenador predominante %):")
            for n, k, v in divididos:
                self.stdout.write(f"  {n:>5} · {k[1]}/{k[2]} · {v['coordenador'] or '—'} ({v['coord_pct']}%)")
        from prospeccao.views_inside import base_inside
        leads = list(base_inside().select_related("empresa"))
        bases = Counter()
        for o in leads:
            r = regiao.da_empresa(o.empresa, m)
            bases["nenhuma" if not r else ("CEP" if r["base"].startswith("CEP") else "município")] += 1
        self.stdout.write(f"Leads do Inside Sales: {len(leads)} · indicação por município: {bases['município']} · "
                          f"por CEP: {bases['CEP']} · sem indicação: {bases['nenhuma']}")
