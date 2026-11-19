"""
Indicadores consolidados de compra e classificação da situação de compra.

Evidência de compra: nota fiscal de SAÍDA, não cancelada, com CFOP de venda
(mesma regra do relatório VENDAS da automação). Pedido/oportunidade criados
NÃO contam como compra.

Situação de compra:
  INDISPONIVEL        – nenhuma sincronização de compras concluída, ou empresa sem
                        vínculo com o Salesforce (as notas não têm como ser ligadas).
  COMPROU             – ao menos uma nota de venda presente na origem.
  NUNCA_COMPROU       – sem nota, a cobertura começa antes do cadastro da conta no
                        Salesforce E a configuração 'nunca_comprou_habilitado' está ativa.
  SEM_COMPRA_PERIODO  – sem nota dentro da cobertura, mas a empresa pode ter comprado antes.
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Max, Min, Sum
from django.utils import timezone

from carteira.models import (
    CompraCategoria, ConfiguracaoAnalise, Empresa, Importacao, IndicadorCompra, NotaFiscal,
    RegistroOrigem, SituacaoCompra,
)


def cobertura_compras():
    """(inicio: date, fim: datetime) das sincronizações de venda concluídas, ou None."""
    inicio, fim = None, None
    for imp in Importacao.objects.filter(tipo=Importacao.Tipo.COMPRAS, status=Importacao.Status.SUCESSO):
        if NotaFiscal.Tipo.VENDA not in (imp.parametros or {}).get("tipos", []):
            continue
        try:
            d = date.fromisoformat(imp.parametros["desde"])
        except (KeyError, ValueError, TypeError):
            continue
        inicio = d if inicio is None or d < inicio else inicio
        fim = imp.finalizado_em if fim is None or (imp.finalizado_em and imp.finalizado_em > fim) else fim
    return (inicio, fim) if inicio else None


def vincular_notas_pendentes():
    mapa = dict(RegistroOrigem.objects.filter(objeto="Account").values_list("id_origem", "empresa_id"))
    total = 0
    pendentes = NotaFiscal.objects.filter(empresa__isnull=True).exclude(cliente_id_origem="")
    for nota in pendentes.only("id", "cliente_id_origem"):
        emp = mapa.get(nota.cliente_id_origem)
        if emp:
            NotaFiscal.objects.filter(pk=nota.pk).update(empresa_id=emp)
            total += 1
    return total


def classificar(*, qtd_compras, tem_vinculo_origem, data_cadastro_origem, cobertura, nunca_habilitado):
    if cobertura is None or not tem_vinculo_origem:
        return SituacaoCompra.INDISPONIVEL
    if qtd_compras > 0:
        return SituacaoCompra.COMPROU
    if nunca_habilitado and data_cadastro_origem and cobertura[0] <= timezone.localdate(data_cadastro_origem):
        return SituacaoCompra.NUNCA_COMPROU
    return SituacaoCompra.SEM_COMPRA_PERIODO


def recalcular_indicadores():
    cfg = ConfiguracaoAnalise.obter()
    cobertura = cobertura_compras()
    agora = timezone.now()

    base = NotaFiscal.objects.filter(presente_na_origem=True, empresa__isnull=False)
    vendas = {r["empresa_id"]: r for r in base.filter(tipo=NotaFiscal.Tipo.VENDA).values("empresa_id").annotate(
        qtd=Count("id"), valor=Sum("valor_total"), prim=Min("data_emissao"), ult=Max("data_emissao"))}
    devol = {r["empresa_id"]: r for r in base.filter(tipo=NotaFiscal.Tipo.DEVOLUCAO).values("empresa_id").annotate(
        qtd=Count("id"), valor=Sum("valor_total"))}

    categorias = defaultdict(lambda: defaultdict(lambda: [0, Decimal("0")]))
    for emp_id, cats in base.filter(tipo=NotaFiscal.Tipo.VENDA).values_list("empresa_id", "categorias"):
        for cat, valor in (cats or {}).items():
            acc = categorias[emp_id][cat[:120]]
            acc[0] += 1
            acc[1] += Decimal(valor)

    existentes = {i.empresa_id: i for i in IndicadorCompra.objects.all()}
    criar, atualizar = [], []
    for emp in Empresa.objects.values("id", "salesforce_id", "data_cadastro_origem"):
        v = vendas.get(emp["id"], {})
        d = devol.get(emp["id"], {})
        campos = dict(
            situacao=classificar(
                qtd_compras=v.get("qtd", 0), tem_vinculo_origem=bool(emp["salesforce_id"]),
                data_cadastro_origem=emp["data_cadastro_origem"], cobertura=cobertura,
                nunca_habilitado=cfg.nunca_comprou_habilitado,
            ),
            primeira_compra=v.get("prim"), ultima_compra=v.get("ult"), qtd_compras=v.get("qtd", 0),
            valor_compras=v.get("valor") or 0, qtd_devolucoes=d.get("qtd", 0), valor_devolvido=d.get("valor") or 0,
            cobertura_inicio=cobertura[0] if cobertura else None, cobertura_fim=cobertura[1] if cobertura else None,
            calculado_em=agora,
        )
        ind = existentes.get(emp["id"])
        if ind is None:
            criar.append(IndicadorCompra(empresa_id=emp["id"], **campos))
        else:
            for k, val in campos.items():
                setattr(ind, k, val)
            atualizar.append(ind)
    IndicadorCompra.objects.bulk_create(criar, batch_size=1000)
    if atualizar:
        IndicadorCompra.objects.bulk_update(atualizar, list(campos.keys()), batch_size=1000)

    CompraCategoria.objects.all().delete()
    CompraCategoria.objects.bulk_create(
        [CompraCategoria(empresa_id=e, categoria=c, qtd_notas=q, valor=val)
         for e, cats in categorias.items() for c, (q, val) in cats.items()],
        batch_size=1000,
    )


def data_limite_inatividade(dias=None):
    """Data antes da qual a última compra caracteriza inatividade. None se não configurado."""
    if dias is None:
        dias = ConfiguracaoAnalise.obter().dias_inatividade
    if not dias:
        return None
    return timezone.localdate() - timedelta(days=dias)
