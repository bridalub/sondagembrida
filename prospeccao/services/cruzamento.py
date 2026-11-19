"""
Cruzamento com a carteira (dados do Salesforce sincronizados) e conferência direta (somente leitura).

Situações (existir no Salesforce NÃO é o mesmo que ter comprado):
  CLIENTE_CARTEIRA     conta BRIDA com compra (NF no período ou data de última compra no cadastro)
  CADASTRO_SEM_COMPRA  conta BRIDA sem nenhuma evidência de compra
  LEAD_CONHECIDO       CNPJ presente nos leads BRIDA
  NOVA_EXTERNA         CNPJ verificado que não consta na carteira nem nos leads BRIDA
  IDENTIFICACAO_PENDENTE  candidato sem CNPJ
  BLOQUEADA            conta Bloqueada no Protheus/Salesforce
  NAO_CONTATAR         marcada como não contatar
"""
from datetime import timedelta

from django.db import models
from django.utils import timezone

from carteira.models import ConfiguracaoAnalise, Empresa, Importacao, LeadSalesforce, SituacaoCompra


class Situacao(models.TextChoices):
    CLIENTE_CARTEIRA = "CLIENTE_CARTEIRA", "Cliente da carteira"
    CADASTRO_SEM_COMPRA = "CADASTRO_SEM_COMPRA", "Cadastro no Salesforce sem compra comprovada"
    LEAD_CONHECIDO = "LEAD_CONHECIDO", "Lead conhecido (Salesforce BRIDA)"
    NOVA_EXTERNA = "NOVA_EXTERNA", "Nova oportunidade externa"
    IDENTIFICACAO_PENDENTE = "IDENTIFICACAO_PENDENTE", "Identificação pendente"
    BLOQUEADA = "BLOQUEADA", "Conta bloqueada (Protheus/Salesforce)"
    NAO_CONTATAR = "NAO_CONTATAR", "Não contatar"


def cache_cruzamento():
    """Carteira e leads locais, uma vez por execução. A comparação é por CNPJ normalizado."""
    leads = {l.cnpj: l for l in LeadSalesforce.objects.exclude(cnpj__isnull=True).exclude(cnpj="")}
    raizes = {}
    for nome, cnpj in (Empresa.objects.filter(salesforce_id__isnull=False).exclude(cnpj__isnull=True)
                       .exclude(cnpj="").values_list("nome", "cnpj")):
        raizes.setdefault(cnpj[:8], [])
        if len(raizes[cnpj[:8]]) < 4:
            raizes[cnpj[:8]].append(nome)
    return {"leads": leads, "raizes": raizes}


def situacao_empresa(empresa, cache=None):
    """(situacao, detalhe, alertas). cache evita uma consulta por empresa."""
    alertas = []
    if empresa.cnpj:
        if cache is not None:
            mesma_raiz = [n for n in cache.get("raizes", {}).get(empresa.cnpj[:8], []) if n != empresa.nome][:3]
        else:
            mesma_raiz = (Empresa.objects.filter(cnpj__startswith=empresa.cnpj[:8], salesforce_id__isnull=False)
                          .exclude(pk=empresa.pk).values_list("nome", flat=True)[:3])
        if mesma_raiz:
            alertas.append("Mesma raiz de CNPJ (grupo/matriz) já está na carteira: " + ", ".join(mesma_raiz))
    if empresa.nao_contatar:
        return Situacao.NAO_CONTATAR, f"Marcada como não contatar: {empresa.nao_contatar_motivo}", alertas
    if empresa.bloqueada_origem:
        return Situacao.BLOQUEADA, "Conta com situação 'Bloqueado' no Protheus/Salesforce", alertas
    if empresa.salesforce_id:
        ind = getattr(empresa, "indicador", None)
        if ind and ind.situacao == SituacaoCompra.COMPROU:
            return (Situacao.CLIENTE_CARTEIRA,
                    f"Compra comprovada por NF ({ind.qtd_compras} nota(s), última {ind.ultima_compra:%d/%m/%Y})", alertas)
        if empresa.ultima_compra_cadastro:
            return (Situacao.CLIENTE_CARTEIRA,
                    f"Última compra no cadastro em {empresa.ultima_compra_cadastro:%d/%m/%Y} (evidência secundária)", alertas)
        return Situacao.CADASTRO_SEM_COMPRA, f"Conta {empresa.salesforce_id} sem compra encontrada", alertas
    if empresa.cnpj:
        if cache is not None:
            lead = cache.get("leads", {}).get(empresa.cnpj)
        else:
            lead = LeadSalesforce.objects.filter(cnpj=empresa.cnpj).first()
        if lead:
            return Situacao.LEAD_CONHECIDO, f"Lead BRIDA {lead.salesforce_id} ({lead.status})", alertas
        return Situacao.NOVA_EXTERNA, "CNPJ não encontrado na carteira nem nos leads BRIDA sincronizados", alertas
    return Situacao.IDENTIFICACAO_PENDENTE, "Sem CNPJ confirmado", alertas


def ultima_sincronizacao_clientes():
    imp = (Importacao.objects.filter(tipo=Importacao.Tipo.CLIENTES, status=Importacao.Status.SUCESSO)
           .order_by("-finalizado_em").first())
    return imp.finalizado_em if imp else None


def cruzamento_atual():
    """(atual: bool, quando: datetime|None, validade_horas). atual é a janela fresca; quando basta para cruzar."""
    horas = ConfiguracaoAnalise.obter().horas_validade_cruzamento
    quando = ultima_sincronizacao_clientes()
    return bool(quando and timezone.now() - quando <= timedelta(hours=horas)), quando, horas


TIMEOUT_AUTENTICACAO_S = 20
TIMEOUT_LEITURA_S = 120
TENTATIVAS_AUTENTICACAO = 2


def reconciliar_selecoes():
    """Move para conhecidos quem passou a constar na carteira, sem apagar o histórico da seleção."""
    from prospeccao.models import SelecaoPesquisa
    n = 0
    qs = (SelecaoPesquisa.objects.filter(empresa__salesforce_id__isnull=False)
          .exclude(fila__in=[SelecaoPesquisa.Fila.CONHECIDA, SelecaoPesquisa.Fila.NAO_CONTATAR])
          .select_related("empresa"))
    for sel in qs.iterator():
        mot = list(sel.fila_motivos or [])
        nota = f"Reconciliado com a carteira local: conta {sel.empresa.salesforce_id}."
        if nota not in mot:
            mot.append(nota)
        sel.fila = SelecaoPesquisa.Fila.CONHECIDA
        sel.fila_motivos = mot
        sel.save(update_fields=["fila", "fila_motivos", "atualizada_em"])
        n += 1
    return n


def _aviso_base_local():
    _atual, quando, _horas = cruzamento_atual()
    n = Empresa.objects.filter(salesforce_id__isnull=False).count()
    if quando:
        return False, (f"Usando a carteira sincronizada em {timezone.localtime(quando).strftime('%d/%m/%Y %H:%M')} "
                       f"({n} contas).")
    if n:
        return False, (f"Usando {n} contas Salesforce já gravadas. A carga ainda não foi encerrada; "
                       "conta ainda não carregada não será tratada como ausência confirmada.")
    return False, ("Cruzamento pendente: não há carteira local utilizável. "
                   "Estes resultados não estão confirmados como novos.")


def tentar_atualizar_carteira(permitir_rede=None):
    """Uma leitura no início da pesquisa. Falha ou lote vazio não substitui a última base íntegra."""
    import socket
    import sys
    automatico = permitir_rede is None
    if permitir_rede is None:
        permitir_rede = "test" not in sys.argv
    if not permitir_rede:
        return _aviso_base_local()
    if automatico:
        # a sincronização de hora em hora (Agendador do Windows) mantém a carteira fresca: a busca não lê o Salesforce
        import os
        janela = int(os.getenv("PROSPECCAO_SYNC_JANELA_MIN", "70"))
        quando = ultima_sincronizacao_clientes()
        if quando and timezone.now() - quando <= timedelta(minutes=janela):
            return True, (f"Carteira sincronizada às {timezone.localtime(quando).strftime('%H:%M')} "
                          "pela atualização automática.")
    from carteira.services import importacao, salesforce
    ultimo = ""
    for _tentativa in range(TENTATIVAS_AUTENTICACAO):
        anterior = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(TIMEOUT_AUTENTICACAO_S)
            sf = salesforce.conectar()
            socket.setdefaulttimeout(TIMEOUT_LEITURA_S)
            registros, query = salesforce.buscar_accounts(sf, {})
            existentes = Empresa.objects.filter(salesforce_id__isnull=False).count()
            if not registros and existentes:
                return False, "Sincronização vazia; a carteira local anterior foi preservada."
            if existentes and 0 < len(registros) < existentes:
                _ok, aviso = _aviso_base_local()
                return False, ("Sincronização parcial não substitui a carteira completa. " + aviso)
            importacao.importar_clientes(
                registros, origem=f"Salesforce {sf.sf_instance}", executado_por="prospeccao",
                parametros={"consulta": query}, recalcular=False)
            try:
                leads, _consulta_leads = salesforce.buscar_leads(sf)
                if leads:
                    importacao.importar_leads(
                        leads, origem=f"Salesforce {sf.sf_instance}", executado_por="prospeccao")
            except Exception:
                pass
            reconciliar_selecoes()
            _ok, quando, _horas = cruzamento_atual()
            if quando:
                return True, f"Carteira atualizada em {timezone.localtime(quando).strftime('%d/%m/%Y %H:%M')}."
            return _aviso_base_local()
        except Exception as e:
            ultimo = type(e).__name__
        finally:
            socket.setdefaulttimeout(anterior)
    _ok, aviso = _aviso_base_local()
    if ultimo and "não há carteira" in aviso:
        aviso = f"{aviso} Tentativa de atualização: {ultimo}."
    return False, aviso


class SalesforceIndisponivel(Exception):
    pass


def conferir_direto(cnpjs):
    """Consulta (leitura) contas e leads BRIDA pelos CNPJs. Levanta SalesforceIndisponivel se não houver acesso."""
    from carteira.services import salesforce
    try:
        sf = salesforce.conectar()
        return salesforce.conferir_cnpjs(sf, list(cnpjs))
    except Exception as e:
        raise SalesforceIndisponivel(f"Conferência direta no Salesforce indisponível: {type(e).__name__}: {e}")
