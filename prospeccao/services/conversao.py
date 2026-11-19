"""Lead que virou cliente (venda contada na captação) sai da base de trabalho do Insight Sales.

A oportunidade passa a JA_CLIENTE ("passou a constar como cliente"): some de "A procurar" e "Procurados", mas as
vendas e os pontos continuam contando no painel e na disputa (eles leem as empresas da prospecção, qualquer status).
Roda depois de cada sincronização com o Salesforce e ao abrir a tela do Insight Sales.
"""
from prospeccao.models import Oportunidade


def marcar_convertidos():
    """Retorna quantos leads passaram a cliente agora."""
    from prospeccao.services import bi_captacao as bi
    vendas = bi.vendas_por_nf(bi.empresas_prospeccao())
    ids = {linha["empresa_id"] for linha in vendas["linhas"]}
    if not ids:
        return 0
    return (Oportunidade.objects.filter(empresa_id__in=ids, tipo=Oportunidade.Tipo.NOVA_EXTERNA,
                                        status__in=[Oportunidade.Status.NOVA, Oportunidade.Status.LIBERADA])
            .update(status=Oportunidade.Status.JA_CLIENTE, em_atendimento_por=None, em_atendimento_desde=None))
