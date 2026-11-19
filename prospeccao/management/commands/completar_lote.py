"""Completa WhatsApp, CNPJ e situação de um lote já encontrado, sem descoberta nova."""
import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from prospeccao.models import Execucao, Pesquisa
from prospeccao.services import completar_lote as lote

REFERENCIAS = (
    "https://www.mecanicabjdiesel.com.br/",
    "https://piquenocentroautomotivo.com.br/",
    "https://metaltezza.com.br/index.php/contato/",
    "https://www.transvx.com.br/",
)


class Command(BaseCommand):
    help = "Reprocessa o lote já descoberto: arquivo pago, sites e identificação, sem busca Google nova."

    def add_arguments(self, parser):
        parser.add_argument("--pesquisa", type=int, default=1)
        parser.add_argument("--sem-ia", action="store_true")
        parser.add_argument("--sem-referencias", action="store_true")

    def handle(self, *args, **opts):
        pesquisa = Pesquisa.objects.get(pk=opts["pesquisa"])
        selecoes = lote.empresas_do_lote(pesquisa)
        antes = lote.foto(selecoes)
        self.stdout.write("ANTES " + json.dumps(antes, ensure_ascii=False))
        self.stdout.flush()
        ex = lote.abrir_execucao(pesquisa)
        try:
            arquivo = lote.recuperar_arquivo_pago(pesquisa, selecoes)
            from prospeccao.conectores.base import ContextoExecucao
            sites = lote.reler_sites(selecoes, ContextoExecucao(ex))
            motivos = lote.identificar_sem_busca(selecoes)
            ia = {} if opts["sem_ia"] else lote.sintetizar(selecoes, pesquisa, ex)
            referencias = [] if opts["sem_referencias"] else lote.conferir_publicacoes(REFERENCIAS)
            depois = lote.foto(selecoes)
        except Exception:
            ex.status = Execucao.Status.FALHA
            ex.finalizada_em = timezone.now()
            ex.mensagem = "Complementação interrompida por falha. A descoberta anterior foi preservada."
            ex.save(update_fields=["status", "finalizada_em", "mensagem"])
            raise
        ex.status = Execucao.Status.PARCIAL
        ex.finalizada_em = timezone.now()
        ex.mensagem = (
            "Complementação do lote já encontrado, sem descoberta nova. "
            f"Arquivo pago: {arquivo.get('arquivos', 0)}. Sites relidos: {sites.get('lidos', 0)}."
        )
        ex.progresso = {"arquivo": arquivo, "sites": sites, "identificacao": motivos, "ia": ia}
        ex.save(update_fields=["status", "finalizada_em", "mensagem", "progresso"])
        self.stdout.write(json.dumps({
            "execucao": ex.pk, "empresas": len(selecoes), "antes": antes, "depois": depois,
            "arquivo": arquivo, "sites": sites, "identificacao": motivos, "ia": ia,
            "referencias": referencias,
        }, ensure_ascii=False, default=str))
