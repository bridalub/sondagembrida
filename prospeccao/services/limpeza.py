"""
Limpeza do sistema (Configurações, só gestores, confirmação digitada). DUAS operações separadas, escopo explícito:

1. Resultados da prospecção (padrão): empresas encontradas pelas pesquisas e ainda não contatadas, leads, contatos
   coletados, seleções, candidatas, decisões de revisão e execuções. A carteira Salesforce local NÃO é tocada.
2. Carteira Salesforce local (só se marcada explicitamente): contas, notas, leads do Salesforce, indicadores e
   histórico de sincronização. Só volta com nova sincronização (ou restauração de cópia).

Sempre mantém: logins, LEADS JÁ CONTATADOS (empresas com abordagem registrada e todo o seu histórico), a base da
Receita, as pesquisas/frentes/configurações e os registros de custo de IA/Google (para o teto continuar correto).

Antes de apagar, grava uma CÓPIA DE SEGURANÇA (JSON compactado); se a cópia falhar, nada é apagado.
"""
import gzip
import logging
import os
from datetime import datetime
from pathlib import Path

from django.core import management
from django.db import transaction

from carteira.models import (Abordagem, CompraCategoria, Contato, Empresa, Importacao, IndicadorCompra, ItemLista,
                             LeadSalesforce, NotaFiscal, OcorrenciaImportacao)
from prospeccao.models import Candidato, DecisaoRevisao, Execucao, Oportunidade, Pesquisa, SelecaoPesquisa

log = logging.getLogger(__name__)
CONFIRMACAO = "LIMPAR"


class LimpezaBloqueada(Exception):
    pass


def _mantidas():
    return set(Abordagem.objects.values_list("empresa_id", flat=True))


def pasta_backup():
    base = os.getenv("LIMPEZA_BACKUP_DIR") or os.path.join(os.getenv("LOCALAPPDATA", str(Path.home())),
                                                           "BridaProspeccao", "backups")
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(base)


def previa():
    m = _mantidas()
    prosp = Empresa.objects.filter(salesforce_id__isnull=True).exclude(pk__in=m)
    sf = Empresa.objects.filter(salesforce_id__isnull=False).exclude(pk__in=m)
    return {
        "leads_contatados_mantidos": len(m),
        # 1. prospecção
        "empresas_encontradas": prosp.count(),
        "oportunidades": Oportunidade.objects.filter(empresa__in=prosp).count(),
        "pessoas_de_contato": Contato.objects.filter(empresa__in=prosp).count(),
        "selecoes": SelecaoPesquisa.objects.exclude(empresa_id__in=m).count(),
        "candidatas": Candidato.objects.exclude(empresa_id__in=m).count(),
        "execucoes": Execucao.objects.count(),
        # 2. carteira Salesforce (só com escolha explícita)
        "contas_salesforce": sf.count(),
        "notas_fiscais": NotaFiscal.objects.exclude(empresa_id__in=m).count(),
        "leads_salesforce": LeadSalesforce.objects.count(),
        "sincronizacoes": Importacao.objects.count(),
        "pesquisas_mantidas": Pesquisa.objects.count(),
    }


def backup():
    """Cópia de segurança das tabelas afetadas (carteira + prospecção; a base Receita não muda)."""
    from prospeccao.services.prontidao import exigir_espaco
    try:
        exigir_espaco("a cópia de segurança")
    except RuntimeError as e:
        raise LimpezaBloqueada(f"{e} Nada foi apagado.")
    destino = pasta_backup() / f"antes-da-limpeza-{datetime.now():%Y%m%d-%H%M%S}.json.gz"
    with gzip.open(destino, "wt", encoding="utf-8") as f:
        management.call_command("dumpdata", "carteira", "prospeccao", "auth", indent=None, stdout=f)
    if destino.stat().st_size < 100:
        raise LimpezaBloqueada("Cópia de segurança vazia; nada foi apagado.")
    return destino


def limpar(usuario=None, fazer_backup=True, incluir_carteira=False):
    """Apaga os resultados da prospecção e, SÓ se incluir_carteira=True, também a carteira Salesforce local."""
    if Execucao.objects.filter(status__in=Execucao.ATIVOS).exists():
        raise LimpezaBloqueada("Há busca em andamento. Aguarde terminar (ou cancele) antes de limpar.")
    antes = previa()
    try:
        arquivo = backup() if fazer_backup else None
    except LimpezaBloqueada:
        raise
    except Exception as e:
        raise LimpezaBloqueada(f"Não foi possível gravar a cópia de segurança ({type(e).__name__}: {e}); "
                               f"nada foi apagado.")
    m = _mantidas()
    alvo = Empresa.objects.exclude(pk__in=m)
    if not incluir_carteira:
        alvo = alvo.filter(salesforce_id__isnull=True)
    with transaction.atomic():
        DecisaoRevisao.objects.exclude(oportunidade__empresa_id__in=m).exclude(candidato__empresa_id__in=m).delete()
        ItemLista.objects.filter(empresa__in=alvo).delete()
        SelecaoPesquisa.objects.exclude(empresa_id__in=m).delete()
        Oportunidade.objects.filter(empresa__in=alvo).delete()
        Candidato.objects.exclude(empresa_id__in=m).delete()
        Execucao.objects.all().delete()
        if incluir_carteira:
            NotaFiscal.objects.exclude(empresa_id__in=m).delete()
            CompraCategoria.objects.exclude(empresa_id__in=m).delete()
            IndicadorCompra.objects.exclude(empresa_id__in=m).delete()
            LeadSalesforce.objects.all().delete()
        Contato.objects.filter(empresa__in=alvo).delete()
        alvo.delete()
        if incluir_carteira:
            OcorrenciaImportacao.objects.all().delete()
            Importacao.objects.all().delete()  # sem histórico de sincronização: a tela avisa que não há carteira
        Pesquisa.objects.update(cursor={}, proxima_execucao_em=None)
    antes["backup"] = str(arquivo) if arquivo else ""
    antes["incluiu_carteira"] = incluir_carteira
    log.warning("Limpeza do sistema por %s (carteira=%s): %s", usuario, incluir_carteira, antes)
    return antes
