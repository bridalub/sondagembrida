"""Reprocessa identificação cadastral das empresas já encontradas, sem descoberta nova.

Usa textos já salvos, a base local da Receita e, quando a situação está ausente ou vencida,
a consulta cadastral já integrada. A releitura do arquivo SerpApi só ocorre para buscas
locais já pagas que ficaram com zero estabelecimentos. Não aumenta teto nem abre pesquisa nova.
"""
import json
import shutil
import time
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import connection

from prospeccao.models import Pesquisa, SelecaoPesquisa
from prospeccao.services import dimensoes as dm
from prospeccao.services.descoberta_google import materializar_revisoes_sem_empresa, reidratar_lugares
from prospeccao.services.identificacao_cnpj import completar_identificacao


class Command(BaseCommand):
    help = "Reprocessa CNPJ e situação das empresas já encontradas em uma pesquisa."

    def add_arguments(self, parser):
        parser.add_argument("--pesquisa", type=int, default=11)
        parser.add_argument("--sem-arquivo", action="store_true")
        parser.add_argument("--limite-arquivo", type=int, default=8)

    def handle(self, *args, **opts):
        livre = shutil.disk_usage("C:\\").free
        with connection.cursor() as cur:
            cur.execute("SELECT pg_is_in_recovery(), pg_database_size(current_database())")
            recuperacao, tamanho = cur.fetchone()
        if recuperacao or livre < 2 * 1024 ** 3:
            self.stdout.write(json.dumps({
                "abortado": True,
                "motivo": "PostgreSQL em recuperação" if recuperacao else "disco com menos de 2 GB livres",
            }, ensure_ascii=False))
            return
        pesquisa = Pesquisa.objects.get(pk=opts["pesquisa"])
        from django.db.models import Sum
        from prospeccao.models import ConsultaWeb
        from prospeccao.services.cruzamento import ultima_sincronizacao_clientes
        gasto = ConsultaWeb.objects.filter(pesquisa=pesquisa).aggregate(
            estimado=Sum("custo_estimado_usd"), reservado=Sum("custo_reservado_usd"))
        self.stdout.write(json.dumps({
            "disco_livre_bytes": livre, "postgres_recuperacao": bool(recuperacao),
            "postgres_bytes": int(tamanho), "pesquisa": pesquisa.pk,
            "teto_custo_usd": str(pesquisa.teto_custo_usd),
            "limite_consultas": pesquisa.limite_consultas,
            "gasto_consultas": {k: str(v or 0) for k, v in gasto.items()},
            "carteira_em": str(ultima_sincronizacao_clientes() or ""),
        }, ensure_ascii=False))
        empresas = list(
            pesquisa.selecoes.exclude(empresa__isnull=True).select_related("empresa")
            .order_by("empresa_id").distinct("empresa_id"))
        ids = [s.empresa_id for s in empresas]
        antes = dm.contagens(ids)
        self.stdout.write("ANTES " + json.dumps(antes, ensure_ascii=False))
        self.stdout.flush()
        t0 = time.monotonic()
        materializadas = materializar_revisoes_sem_empresa(pesquisa)
        arquivo = {"pulado": True}
        if not opts["sem_arquivo"]:
            arquivo = reidratar_lugares(pesquisa, limite=opts["limite_arquivo"])
            ids = list(SelecaoPesquisa.objects.filter(pesquisa=pesquisa, empresa__isnull=False)
                       .order_by("empresa_id").values_list("empresa_id", flat=True).distinct())
        motivos = {}
        puladas_conhecidas = 0
        vistas = set()
        for sel in (SelecaoPesquisa.objects.filter(pesquisa=pesquisa, empresa__isnull=False)
                    .select_related("empresa").order_by("empresa_id", "pk")):
            if sel.empresa_id in vistas:
                continue
            vistas.add(sel.empresa_id)
            empresa = sel.empresa
            if empresa.salesforce_id or sel.fila == SelecaoPesquisa.Fila.CONHECIDA:
                puladas_conhecidas += 1
                continue
            info = completar_identificacao(empresa, sel, max_buscas=0, permitir_consulta_complementar=True)
            motivos[info.get("acao") or "sem_acao"] = motivos.get(info.get("acao") or "sem_acao", 0) + 1
        depois = dm.contagens(ids)
        self.stdout.write(json.dumps({
            "pesquisa": pesquisa.pk,
            "empresas": len(set(ids)),
            "antes": antes,
            "depois": depois,
            "motivos": motivos,
            "puladas_conhecidas": puladas_conhecidas,
            "materializadas": materializadas,
            "arquivo": arquivo,
            "segundos": round(time.monotonic() - t0, 1),
            "quando": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, default=str))
