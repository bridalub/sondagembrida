"""Retomada de fonte limitada (HTTP 429): grava a próxima tentativa e o worker a executa."""
from datetime import timedelta

from django.utils import timezone

MAX_TENTATIVAS = 5


def agendar(fonte, chave, erro="", retry_after=None, empresa=None):
    from prospeccao.models import Fonte, RetomadaFonte

    if not chave:
        return None
    agora = timezone.now()
    aberta = RetomadaFonte.objects.filter(fonte=fonte, chave=chave, concluida_em__isnull=True).first()
    tentativas = (aberta.tentativas if aberta else 0) + 1
    if tentativas > MAX_TENTATIVAS:
        if aberta:
            aberta.ultimo_erro = (erro or "")[:300]
            aberta.save(update_fields=["ultimo_erro"])
        return aberta
    espera = int(retry_after) if retry_after else min(30 * (2 ** (tentativas - 1)), 900)
    quando = agora + timedelta(seconds=max(int(espera), 1))
    cadastro = Fonte.objects.filter(codigo=fonte).first()
    intervalo = float(getattr(cadastro, "intervalo_minimo_segundos", 1) or 1)
    outra = (RetomadaFonte.objects.filter(fonte=fonte, concluida_em__isnull=True)
             .exclude(chave=chave).order_by("-disponivel_em").first())
    if outra and outra.disponivel_em + timedelta(seconds=intervalo) > quando:
        quando = outra.disponivel_em + timedelta(seconds=intervalo)
    if aberta:
        aberta.disponivel_em = quando
        aberta.tentativas = tentativas
        aberta.ultimo_erro = (erro or "")[:300]
        if empresa is not None:
            aberta.empresa = empresa
        aberta.save()
        return aberta
    return RetomadaFonte.objects.create(
        fonte=fonte, chave=str(chave)[:80], empresa=empresa, disponivel_em=quando,
        tentativas=tentativas, ultimo_erro=(erro or "")[:300])


def processar_devidas(limite=5):
    """Executa retomadas vencidas, no máximo uma por fonte neste ciclo. Não marca empresa como inativa."""
    from prospeccao.models import RetomadaFonte
    from prospeccao.services.oportunidades import RegraViolada, verificar_situacao_cadastral

    agora = timezone.now()
    feitas = 0
    fontes = set()
    pendentes = RetomadaFonte.objects.filter(
        concluida_em__isnull=True, disponivel_em__lte=agora).select_related("empresa").order_by("disponivel_em")
    for item in pendentes[:50]:
        if feitas >= limite or item.fonte in fontes:
            continue
        fontes.add(item.fonte)
        if item.fonte == "google_serpapi" and str(item.chave).startswith("velocidade:"):
            from prospeccao.models import Execucao
            from prospeccao.services.fila import enfileirar
            bruto = str(item.chave).split(":", 1)[-1]
            ex = Execucao.objects.filter(pk=bruto).select_related("pesquisa").first() if bruto.isdigit() else None
            if ex is not None and ex.status not in Execucao.ATIVOS:
                enfileirar(ex.pesquisa, Execucao.Gatilho.AGENDADA, parametros={
                    "retomada_de": ex.pk,
                    "motivo": "retomada após limite de velocidade da SerpApi",
                })
            if ex is None or ex.status not in Execucao.ATIVOS:
                item.concluida_em = timezone.now()
                item.save(update_fields=["concluida_em"])
                feitas += 1
            continue
        if item.fonte != "brasilapi_cnpj" or not item.empresa_id or not item.empresa.cnpj:
            continue
        try:
            verificar_situacao_cadastral(item.empresa)
        except RegraViolada:
            # 429 já reagendou dentro da verificação. Outro erro adia, sem presumir situação cadastral.
            item.refresh_from_db()
            if item.tentativas < MAX_TENTATIVAS and item.disponivel_em <= agora:
                item.disponivel_em = agora + timedelta(minutes=30)
                item.tentativas += 1
                item.save(update_fields=["disponivel_em", "tentativas"])
            continue
        item.concluida_em = timezone.now()
        item.save(update_fields=["concluida_em"])
        feitas += 1
    return feitas
