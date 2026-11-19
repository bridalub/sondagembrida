"""
Fila de execuções no próprio banco (sem Redis/Celery).

- enfileirar(): a restrição única 'uma execução ativa por pesquisa' impede execução duplicada.
- worker (manage.py prospeccao_worker) pega a próxima PENDENTE; o UPDATE condicional em
  pipeline.executar garante que só um processo execute cada item.
- recuperar_travadas(): execuções sem heartbeat viram INTERROMPIDA (dados já gravados ficam).
- agendar_devidas(): só cria execuções recorrentes se ConfiguracaoAnalise.agendamento_ativo = True.
"""
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from carteira.models import ConfiguracaoAnalise
from prospeccao.models import Execucao, Pesquisa


def enfileirar(pesquisa, gatilho=Execucao.Gatilho.MANUAL, usuario=None, parametros=None):
    ativa = Execucao.objects.filter(pesquisa=pesquisa, status__in=Execucao.ATIVOS).first()
    if ativa:
        return ativa, False
    parametros = dict(parametros or {})
    ultima = Execucao.objects.filter(pesquisa=pesquisa).exclude(status=Execucao.Status.BLOQUEADA).order_by("-pk").first()
    if ultima is not None and ultima.status == Execucao.Status.INTERROMPIDA:
        # retomada: vinculada à anterior; consultas já feitas e o orçamento acumulado continuam contando
        parametros.setdefault("retomada_de", ultima.pk)
    try:
        with transaction.atomic():
            return Execucao.objects.create(pesquisa=pesquisa, gatilho=gatilho, solicitada_por=usuario,
                                           parametros=parametros), True
    except IntegrityError:
        return Execucao.objects.filter(pesquisa=pesquisa, status__in=Execucao.ATIVOS).first(), False


def recuperar_travadas(minutos_sem_sinal=10):
    limite = timezone.now() - timedelta(minutes=minutos_sem_sinal)
    return Execucao.objects.filter(status=Execucao.Status.EXECUTANDO, heartbeat_em__lt=limite).update(
        status=Execucao.Status.INTERROMPIDA, finalizada_em=timezone.now(), completa=False,
        mensagem="Sem sinal do processo (interrompida). Resultados já gravados foram mantidos; "
                 "execute novamente para continuar.")


def agendar_devidas():
    if not ConfiguracaoAnalise.obter().agendamento_ativo:
        return []
    agora, criadas = timezone.now(), []
    for p in Pesquisa.objects.filter(ativa=True, frequencia_horas__isnull=False):
        if p.proxima_execucao_em and p.proxima_execucao_em > agora:
            continue
        ex, nova = enfileirar(p, Execucao.Gatilho.AGENDADA)
        if nova:
            criadas.append(ex)
        Pesquisa.objects.filter(pk=p.pk).update(proxima_execucao_em=agora + timedelta(hours=p.frequencia_horas))
    return criadas


def processar_proxima():
    from prospeccao.services.pipeline import executar
    ex = Execucao.objects.filter(status=Execucao.Status.PENDENTE).order_by("criada_em").first()
    return executar(ex.pk) if ex else None


def disparar_worker():
    """Inicia um worker em segundo plano (não bloqueia a requisição web). Controlado por settings."""
    if not getattr(settings, "PROSPECCAO_DISPARAR_WORKER", False):
        return False
    manage = Path(settings.BASE_DIR) / "manage.py"
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, str(manage), "prospeccao_worker", "--uma-vez"], cwd=str(settings.BASE_DIR),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)
    return True
