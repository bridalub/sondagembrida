"""Trava de atendimento: um lead só é atendido por uma pessoa de cada vez.

- Quem clica em "Atender" (ou abre o registro de contato) assume o lead; os outros veem "Em atendimento por …".
- A trava sai sozinha ao registrar o contato, ao clicar em "Liberar" ou depois de PROSPECCAO_ATENDIMENTO_MIN
  minutos sem registro (padrão 30), para nenhum lead ficar preso se a pessoa fechar a tela.
- O ADM pode liberar a trava de qualquer pessoa.
"""
import os
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from prospeccao.models import Oportunidade


class AtendimentoOcupado(Exception):
    def __init__(self, op):
        self.op = op
        quem = op.em_atendimento_por
        nome = (quem.get_full_name() or quem.get_username()) if quem else "outra pessoa"
        hora = timezone.localtime(op.em_atendimento_desde).strftime("%H:%M") if op.em_atendimento_desde else ""
        super().__init__(f"Este lead está em atendimento por {nome}" + (f" desde {hora}" if hora else "")
                         + ". Escolha outro lead ou aguarde a liberação.")


class LeadDeOutro(AtendimentoOcupado):
    """O lead já tem dono (quem fez o primeiro atendimento ou recebeu do ADMIN)."""

    def __init__(self, op):
        self.op = op
        dono = op.responsavel
        nome = (dono.get_full_name() or dono.get_username()) if dono else "outra pessoa"
        Exception.__init__(self, f"Este lead é de {nome}. Escolha outro lead; só um ADMIN pode transferi-lo.")


def dono_de_outro(op, user):
    """True quando o lead tem dono e não é `user` (ADMIN pode atender qualquer lead)."""
    from prospeccao.papeis import eh_adm
    return bool(op.responsavel_id) and op.responsavel_id != user.pk and not eh_adm(user)


def minutos():
    try:
        return max(1, int(os.getenv("PROSPECCAO_ATENDIMENTO_MIN") or 30))
    except ValueError:
        return 30


def limite_validade():
    return timezone.now() - timedelta(minutes=minutos())


def vigente(op):
    """Usuário que está atendendo agora, ou None (sem trava ou trava vencida)."""
    if not op or not op.em_atendimento_por_id or not op.em_atendimento_desde:
        return None
    if op.em_atendimento_desde < limite_validade():
        return None
    return op.em_atendimento_por


def oportunidade_da_empresa(empresa):
    return Oportunidade.objects.filter(empresa=empresa).select_related("em_atendimento_por").first()


def assumir(op, user):
    """Assume (ou renova) o atendimento. Levanta AtendimentoOcupado se outra pessoa estiver atendendo."""
    with transaction.atomic():
        atual = (Oportunidade.objects.select_for_update(of=("self",))
                 .select_related("em_atendimento_por", "responsavel").get(pk=op.pk))
        if dono_de_outro(atual, user):
            raise LeadDeOutro(atual)
        quem = vigente(atual)
        if quem is not None and quem.pk != user.pk:
            raise AtendimentoOcupado(atual)
        Oportunidade.objects.filter(pk=op.pk).update(em_atendimento_por=user, em_atendimento_desde=timezone.now())
    op.em_atendimento_por, op.em_atendimento_desde = user, timezone.now()
    return op


def liberar(op, user=None, forcar=False):
    """Tira a trava. Sem `forcar`, só quem está atendendo libera (ou trava vencida)."""
    with transaction.atomic():
        atual = Oportunidade.objects.select_for_update().get(pk=op.pk)
        quem = vigente(atual)
        if quem is not None and user is not None and quem.pk != user.pk and not forcar:
            raise AtendimentoOcupado(atual)
        Oportunidade.objects.filter(pk=op.pk).update(em_atendimento_por=None, em_atendimento_desde=None)
    op.em_atendimento_por, op.em_atendimento_desde = None, None
    return op
