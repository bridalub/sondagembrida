"""
Diagnóstico comercial de uma pesquisa (só lê o banco; nenhuma chamada):

    python manage.py diagnostico_pesquisa 8

Por consulta: resultados recebidos, empresas identificadas, conhecidas, repetidas, fora da região/incertas, novas
pertinentes, com contato e ignoradas (com o motivo). No total: novos com contato direto, só com contato de terceiro,
distribuição por atividade, tempo até o primeiro lead com contato, custo por lead com contato e pendências que a
investigação realmente tentou resolver.
"""
from collections import Counter
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Min, Sum

from prospeccao.models import ConsultaWeb, Execucao, Oportunidade, Pesquisa, SelecaoPesquisa, UsoIA
from prospeccao.services import contatos as ct


def _classe(st):
    st = (st or "").lower()
    if not st:
        return "sem processamento"
    for chave, nome in (("cota", "ignorada: cota da frente"), ("conhecida", "conhecida"), ("repetida", "repetida"),
                        ("já pesquisada", "repetida"), ("fora", "fora da região/escopo/incerta"),
                        ("possível correspondência", "possível correspondência"), ("nova", "nova")):
        if chave in st:
            return nome
    return "outro: " + st[:40]


class Command(BaseCommand):
    help = "Diagnóstico por consulta e métricas comerciais de uma pesquisa (sem chamadas)."

    def add_arguments(self, parser):
        parser.add_argument("pesquisa_id", type=int)

    def handle(self, *args, **o):
        w = self.stdout.write
        p = Pesquisa.objects.get(pk=o["pesquisa_id"])
        w(f"Pesquisa {p.pk}: {p.nome}")
        w("Consulta | modalidade | recebidos | empresas | novas | c/contato | conhecidas | repetidas | fora/incerta | "
          "ignoradas")
        for c in ConsultaWeb.objects.filter(pesquisa=p, finalidade__in=["DESCOBERTA"], status="OK").order_by("pk"):
            res = [r for r in (c.resultados or []) if isinstance(r, dict) and "url" in r]
            ext = next((r["_extraidas"] for r in reversed(c.resultados or []) if isinstance(r, dict) and "_extraidas" in r), [])
            locais = [r for r in res if r.get("tipo") == "LOCAL"]
            itens = locais + [e for e in ext if e.get("anotacao") is not None]
            cl = Counter(_classe(i.get("anotacao")) for i in itens)
            mod = ((c.acoes or [{}])[0].get("tipo") or "").split(":")[-1]
            w(f"CW{c.pk} “{c.consulta[:48]}” | {mod} | {len(res)} | {len(itens)} | {cl['nova']} | "
              f"{sum(1 for i in itens if 'com contato' in (i.get('anotacao') or ''))} | {cl['conhecida']} | "
              f"{cl['repetida']} | {cl['fora da região/escopo/incerta']} | "
              f"{cl['ignorada: cota da frente'] + cl['sem processamento']}")
            org_ign = len([r for r in res if r.get("tipo") != "LOCAL"]) - len(ext)
            if org_ign > 0 and mod != "local":
                w(f"      {org_ign} resultado(s) orgânico(s) sem empresa extraída (listas, notícias ou páginas sem nome)")
        ops = Oportunidade.objects.filter(pesquisas=p, tipo="NOVA_EXTERNA").exclude(
            status__in=["FORA_REGIAO"]).select_related("empresa")
        direto = terc = sem = 0
        ativ = Counter()
        primeiro_contato = None
        for op in ops:
            e = op.empresa
            if ct.canais_utilizaveis(e).exists():
                direto += 1
                t = max(min(c.primeira_coleta_em for c in ct.canais_utilizaveis(e)), op.criada_em)
                primeiro_contato = t if primeiro_contato is None or t < primeiro_contato else primeiro_contato
            elif ct.canais_terceiros(e).exists():
                terc += 1
            else:
                sem += 1
            s = SelecaoPesquisa.objects.filter(pesquisa=p, empresa=e).first()
            m = next((x for x in (s.motivos_selecao if s else []) if str(x).startswith("Interesse comercial")), "")
            ativ[(m.split("“")[1].split("”")[0] if "“" in m else (s.frente.nome if s and s.frente else "?"))[:40]] += 1
        sels = SelecaoPesquisa.objects.filter(pesquisa=p)
        ex0 = Execucao.objects.filter(pesquisa=p).aggregate(m=Min("iniciada_em"))["m"]
        custo_ia = UsoIA.objects.filter(pesquisa=p).aggregate(s=Sum("custo_estimado_usd"))["s"] or Decimal("0")
        custo_busca = ConsultaWeb.objects.filter(pesquisa=p).aggregate(s=Sum("custo_estimado_usd"))["s"] or Decimal("0")
        buscas = ConsultaWeb.objects.filter(pesquisa=p, provedor="google_serpapi").count()
        w("")
        w(f"Novos leads (empresas): {ops.count()} | com contato direto: {direto} | só contato de terceiro: {terc} | "
          f"sem contato: {sem}")
        w(f"Conhecidas (Salesforce) excluídas: {sels.filter(fila='CONHECIDA').count()} | já pesquisadas: "
          f"{sels.filter(fila='JA_PESQUISADA').count()} | fora da região: "
          f"{Oportunidade.objects.filter(pesquisas=p, status='FORA_REGIAO').count()} | conferência separada: "
          f"{sels.filter(fila='REVISAR_IDENTIDADE', empresa__isnull=True).count()}")
        w("Distribuição por atividade: " + "; ".join(f"{k} {v}" for k, v in ativ.most_common()))
        if ex0 and primeiro_contato:
            w(f"Tempo até o primeiro lead com contato: {(primeiro_contato - ex0).total_seconds():.0f} s")
        w(f"Custo: IA US$ {custo_ia:.4f} + busca US$ {custo_busca:.4f} ({buscas} buscas Google na franquia) = "
          f"US$ {custo_ia + custo_busca:.4f}; por lead com contato direto: "
          f"US$ {((custo_ia + custo_busca) / direto) if direto else 0:.4f}")
        tent = UsoIA.objects.filter(pesquisa=p, tipo="DECISAO")
        acoes = Counter((u.resultado or {}).get("acao") for u in tent)
        uteis = sum(1 for u in tent if "contato(s) citado(s) registrado(s)" in str((u.resultado or {}).get("_resultado_da_acao"))
                    and not str((u.resultado or {}).get("_resultado_da_acao")).startswith("0") or
                    "identificado" in str((u.resultado or {}).get("_resultado_da_acao")))
        recus = sum(1 for u in tent if "recusada" in str((u.resultado or {}).get("_resultado_da_acao")))
        w(f"Pendências trabalhadas pela investigação: {tent.values('empresa').distinct().count()} empresa(s), "
          f"{tent.count()} decisão(ões) {dict(acoes)}; ações recusadas pelo programa: {recus}")
