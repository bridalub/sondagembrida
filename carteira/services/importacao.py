"""
Importação idempotente de clientes (Account) e compras (notas fiscais).

Regras principais:
  - Empresa é localizada primeiro pelo Account.Id já vinculado (RegistroOrigem),
    depois pelo CNPJ normalizado. NUNCA por semelhança de nome.
  - Conta sem CNPJ válido vira empresa própria sinalizada para revisão.
  - Campo vazio na origem não apaga valor existente (não há regra explícita de limpeza).
  - Abordagens, contatos e "não contatar" nunca são tocados pela importação.
  - Os dados cadastrais vêm apenas da Account principal; Accounts extras com o
    mesmo CNPJ ficam registradas e a empresa é sinalizada para revisão.
"""
import logging
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from carteira.models import (
    Empresa, Importacao, NotaFiscal, OcorrenciaImportacao, RegistroOrigem, StatusCNPJ,
)
from carteira.services import cnpj as cnpj_srv
from carteira.services import conversao as cv

logger = logging.getLogger("carteira")

SISTEMA = "SALESFORCE"
OBJ_ACCOUNT = "Account"

# Campos cadastrais que a importação pode atualizar.
CAMPOS_CADASTRAIS = [
    "nome", "razao_social", "nome_fantasia", "codigo_cliente", "id_customizado", "segmento",
    "segmento_normalizado", "subsegmento", "ramo", "imbp",
    "classificacao_consumo", "granel", "cidade", "uf", "cep", "cidade_entrega", "uf_entrega",
    "regiao", "vendedor", "coordenador", "tipo_vendedor", "coordenador_vendedor", "situacao_protheus", "distribuidor", "data_cadastro_origem",
    "telefone", "email", "email_classificacao", "ultima_compra_cadastro",
    # opcionais (só chegam se confirmados via describe)
    "cnae", "data_abertura",
]
# Campos que podem ser limpos quando vierem vazios na origem. Nenhum por padrão: só entra
# aqui com regra explícita do negócio.
CAMPOS_LIMPAVEIS = set()


class _DryRun(Exception):
    pass


@dataclass
class _Ctx:
    imp: Importacao
    inconsistentes_ids: set
    vazios_mantidos: int = 0

    def ocorrencia(self, tipo, mensagem, id_origem="", cnpj_origem="", nome="", empresa=None):
        OcorrenciaImportacao.objects.create(
            importacao=self.imp, tipo=tipo, mensagem=mensagem, id_origem=id_origem or "",
            cnpj_origem=(cnpj_origem or "")[:40], nome=(nome or "")[:255], empresa=empresa,
        )
        if tipo == OcorrenciaImportacao.Tipo.INCONSISTENTE:
            self.inconsistentes_ids.add(id_origem or f"sem-id-{len(self.inconsistentes_ids)}")


def mapear_account(rec, extras=None, segmentos_codificados=None):
    from carteira.services.normalizacao import classificar_email, normalizar_segmento

    seller = rec.get("Seller__r") or {}
    dist = rec.get("Distributor__r") or {}
    zona = rec.get("Zone__r") or {}
    email = cv.texto(rec.get("Email__c")).lower()
    d = {
        "nome": cv.texto(rec.get("Name")),
        # Diagnóstico: Name é a razão social (71% com sufixo societário)
        "razao_social": cv.texto(rec.get("Name")),
        "nome_fantasia": cv.texto(rec.get("FantasyName__c")),
        "telefone": cv.texto(rec.get("Phone")),
        "email": email if "@" in email else "",
        "email_classificacao": classificar_email(email) if "@" in email else "",
        "ultima_compra_cadastro": cv.data(rec.get("LastBuyDate__c")),
        "segmento_normalizado": normalizar_segmento(cv.texto(rec.get("Segment__c")), segmentos_codificados),
        "codigo_cliente": cv.texto(rec.get("Code__c")),
        "id_customizado": cv.texto(rec.get("CustomizedID__c")),
        "segmento": cv.texto(rec.get("Segment__c")),
        "subsegmento": cv.texto(rec.get("Subsegment__c")),
        "ramo": cv.texto(rec.get("Metier__c")),
        "imbp": cv.texto(rec.get("IMBP__c")),
        "classificacao_consumo": cv.texto(rec.get("ConsumptionClassification__c")),
        "granel": cv.booleano(rec.get("IsGrainy__c")),
        "cidade": cv.texto(rec.get("BillingCity__c")),
        "uf": cv.texto(rec.get("BillingState__c")).upper(),
        "cep": cv.texto(rec.get("BillingPostalCode")),
        "cidade_entrega": cv.texto(rec.get("ShippingCity__c")),
        "uf_entrega": cv.texto(rec.get("ShippingState__c")).upper(),
        "regiao": cv.texto(zona.get("Name")),
        "vendedor": cv.texto(seller.get("Name")),
        "coordenador": cv.texto(rec.get("CoordinatorFormula__c")),
        "tipo_vendedor": cv.texto(seller.get("Type__c")),
        "coordenador_vendedor": cv.texto((seller.get("UserCoordinatorSync__r") or {}).get("Name")),
        "situacao_protheus": cv.texto(rec.get("ProtheusCustomerSituation__c")),
        "distribuidor": cv.texto(dist.get("Name")),
        "data_cadastro_origem": cv.data_hora(rec.get("CreatedDate")),
    }
    for attr, campo in (extras or {}).items():
        bruto = rec.get(campo)
        d[attr] = cv.data(bruto) if attr == "data_abertura" else cv.texto(bruto)
    return d


def _aplicar(empresa, dados, ctx):
    """Aplica somente valores não vazios. Retorna lista de campos alterados."""
    alterados = []
    for campo in CAMPOS_CADASTRAIS:
        if campo not in dados:
            continue
        novo = dados[campo]
        atual = getattr(empresa, campo)
        if cv.vazio(novo) and campo not in CAMPOS_LIMPAVEIS:
            if not cv.vazio(atual):
                ctx.vazios_mantidos += 1
            continue
        if campo == "email" and "@" not in novo:
            continue
        if novo != atual:
            setattr(empresa, campo, novo)
            alterados.append(campo)
    return alterados


def atualizar_revisao(empresa):
    motivos = []
    if empresa.cnpj_status != StatusCNPJ.VALIDO:
        motivos.append(f"CNPJ {empresa.get_cnpj_status_display().lower()}")
    qtd = empresa.registros_origem.filter(sistema=SISTEMA, objeto=OBJ_ACCOUNT).count()
    if qtd > 1:
        motivos.append(f"CNPJ duplicado no Salesforce ({qtd} contas)")
    if empresa.conflito_origem:
        motivos.append(empresa.conflito_origem)
    motivo = "; ".join(motivos)
    if empresa.motivo_revisao != motivo or empresa.requer_revisao != bool(motivos):
        Empresa.objects.filter(pk=empresa.pk).update(motivo_revisao=motivo, requer_revisao=bool(motivos))


def _processar_account(rec, ctx, extras, segmentos_codificados=None):
    T = OcorrenciaImportacao.Tipo
    sf_id = cv.texto(rec.get("Id"))
    nome = cv.texto(rec.get("Name"))
    cnpj_bruto = cv.texto(rec.get("CNPJ__c"))
    if not sf_id:
        ctx.ocorrencia(T.REJEITADO, "Registro sem Account.Id", cnpj_origem=cnpj_bruto, nome=nome)
        return "rejeitado", None
    if not nome:
        ctx.ocorrencia(T.REJEITADO, "Registro sem nome (Account.Name)", id_origem=sf_id, cnpj_origem=cnpj_bruto)
        return "rejeitado", None

    dados = mapear_account(rec, extras, segmentos_codificados)
    doc = cnpj_srv.analisar(cnpj_bruto)
    agora = timezone.now()

    registro = (
        RegistroOrigem.objects.select_related("empresa")
        .filter(sistema=SISTEMA, objeto=OBJ_ACCOUNT, id_origem=sf_id).first()
    )
    empresa, criada, principal, conflito = None, False, True, ""

    if registro:
        empresa = registro.empresa
        principal = registro.principal
        if principal and doc.cnpj and doc.cnpj != empresa.cnpj:
            outra = Empresa.objects.filter(cnpj=doc.cnpj).exclude(pk=empresa.pk).first()
            if empresa.cnpj:
                conflito = (f"CNPJ da conta mudou na origem ({empresa.cnpj} -> {doc.cnpj}); "
                            "mantido o anterior até revisão")
            elif outra:
                conflito = f"CNPJ {doc.cnpj} já pertence a outra empresa (id {outra.pk})"
            else:
                empresa.cnpj, empresa.cnpj_status = doc.cnpj, StatusCNPJ.VALIDO
        elif principal and not doc.cnpj and empresa.cnpj:
            conflito = f"CNPJ veio vazio/inválido na origem ('{cnpj_bruto}'); mantido {empresa.cnpj}"
    elif doc.cnpj:
        empresa = Empresa.objects.filter(cnpj=doc.cnpj).first()
        if empresa:
            if empresa.salesforce_id and empresa.salesforce_id != sf_id:
                principal = False
                ctx.ocorrencia(
                    T.INCONSISTENTE,
                    f"CNPJ {doc.cnpj} duplicado no Salesforce: conta {sf_id} vinculada à empresa da conta "
                    f"{empresa.salesforce_id}. Dados cadastrais mantidos da conta principal.",
                    id_origem=sf_id, cnpj_origem=cnpj_bruto, nome=nome, empresa=empresa,
                )
            else:
                empresa.salesforce_id = sf_id  # empresa cadastrada manualmente com mesmo CNPJ
        else:
            empresa = Empresa(cnpj=doc.cnpj, cnpj_status=StatusCNPJ.VALIDO, salesforce_id=sf_id)
            criada = True
    else:
        empresa = Empresa(cnpj=None, cnpj_status=doc.status, salesforce_id=sf_id)
        criada = True

    alterados = []
    if principal:
        if not empresa.cnpj:
            empresa.cnpj_status = doc.status
        if cnpj_bruto:
            empresa.cnpj_origem = cnpj_bruto[:40]
        alterados = _aplicar(empresa, dados, ctx)
        empresa.conflito_origem = conflito
        empresa.origem = Empresa.Origem.SALESFORCE if criada else empresa.origem
    empresa.ultima_importacao = ctx.imp
    empresa.ultima_sincronizacao_em = agora
    empresa.save()

    if registro is None:
        registro = RegistroOrigem(sistema=SISTEMA, objeto=OBJ_ACCOUNT, id_origem=sf_id, empresa=empresa,
                                  principal=principal)
    registro.dados = cv.sem_attributes(rec)
    registro.ultimo_visto_em = agora
    registro.ultima_importacao = ctx.imp
    registro.save()

    if doc.status != StatusCNPJ.VALIDO and not empresa.cnpj:
        ctx.ocorrencia(T.INCONSISTENTE, f"Sem CNPJ válido ({doc.aviso or doc.status}); sinalizado para revisão. "
                       "Não associado a outras empresas por nome.",
                       id_origem=sf_id, cnpj_origem=cnpj_bruto, nome=nome, empresa=empresa)
    elif doc.aviso:
        ctx.ocorrencia(T.AVISO, doc.aviso, id_origem=sf_id, cnpj_origem=cnpj_bruto, nome=nome, empresa=empresa)
    if conflito:
        ctx.ocorrencia(T.INCONSISTENTE, conflito, id_origem=sf_id, cnpj_origem=cnpj_bruto, nome=nome, empresa=empresa)

    atualizar_revisao(empresa)
    if criada:
        return "criado", empresa
    return ("atualizado" if alterados else "inalterado"), empresa


def _finalizar(imp, contagem, ctx, detalhes):
    imp.lidos = contagem["lidos"]
    imp.criados = contagem["criado"]
    imp.atualizados = contagem["atualizado"]
    imp.inalterados = contagem["inalterado"]
    imp.rejeitados = contagem["rejeitado"]
    imp.inconsistentes = len(ctx.inconsistentes_ids)
    imp.detalhes = {**imp.detalhes, **detalhes, "campos_vazios_mantidos": ctx.vazios_mantidos}
    imp.status = Importacao.Status.SUCESSO
    imp.finalizado_em = timezone.now()
    imp.save()


def _executar(tipo, origem, parametros, executado_por, dry_run, corpo, apos=None):
    """
    Cria a Importacao e roda `corpo(imp, ctx, contagem)`; `apos()` roda depois de a
    importação estar marcada como SUCESSO (ex.: recalcular indicadores com a cobertura nova).
    Em dry-run desfaz tudo ao final.
    """
    resultado = {}

    def rodar():
        imp = Importacao.objects.create(tipo=tipo, origem=origem, parametros=parametros,
                                        executado_por=executado_por)
        resultado["imp"] = imp
        ctx = _Ctx(imp=imp, inconsistentes_ids=set())
        contagem = defaultdict(int)
        try:
            detalhes = corpo(imp, ctx, contagem) or {}
        except Exception as e:
            imp.status = Importacao.Status.FALHA
            imp.mensagem = f"{type(e).__name__}: {e}"
            imp.finalizado_em = timezone.now()
            imp.save()
            raise
        _finalizar(imp, contagem, ctx, detalhes)
        if apos:
            apos()

    if dry_run:
        try:
            with transaction.atomic():
                rodar()
                resultado["imp"].ocorrencias_simuladas = list(resultado["imp"].ocorrencias.all())
                raise _DryRun
        except _DryRun:
            pass
        resultado["imp"].mensagem = "SIMULAÇÃO (dry-run): nada foi gravado."
    else:
        rodar()
    return resultado["imp"]


# ============================================================
# CLIENTES


def importar_clientes(registros, *, origem="Salesforce", parametros=None, extras=None,
                      executado_por="", dry_run=False, recalcular=True):
    from carteira.services.indicadores import recalcular_indicadores, vincular_notas_pendentes

    import re as _re
    registros = list(registros)
    segmentos_codificados = sorted({cv.texto(r.get("Segment__c")) for r in registros
                                    if _re.match(r"^\d{3}-", cv.texto(r.get("Segment__c")))})

    def corpo(imp, ctx, contagem):
        for rec in registros:
            contagem["lidos"] += 1
            try:
                with transaction.atomic():
                    acao, _ = _processar_account(rec, ctx, extras, segmentos_codificados)
            except Exception as e:  # um registro ruim não derruba a importação
                logger.exception("Erro ao importar Account %s", rec.get("Id"))
                acao = "rejeitado"
                ctx.ocorrencia(OcorrenciaImportacao.Tipo.REJEITADO, f"Erro: {type(e).__name__}: {e}",
                               id_origem=cv.texto(rec.get("Id")), cnpj_origem=cv.texto(rec.get("CNPJ__c")),
                               nome=cv.texto(rec.get("Name")))
            contagem[acao] += 1
        vinculadas = vincular_notas_pendentes()
        return {"notas_vinculadas_apos_importacao": vinculadas}

    return _executar(Importacao.Tipo.CLIENTES, origem, parametros or {}, executado_por, dry_run, corpo,
                     apos=recalcular_indicadores if recalcular else None)


# ============================================================
# CONTATOS (Contact) – somente contas BRIDA já importadas


def importar_contatos(registros, *, origem="Salesforce", executado_por="", dry_run=False):
    from carteira.models import Contato

    def corpo(imp, ctx, contagem):
        T = OcorrenciaImportacao.Tipo
        empresa_por_account = dict(
            RegistroOrigem.objects.filter(sistema=SISTEMA, objeto=OBJ_ACCOUNT).values_list("id_origem", "empresa_id"))
        existentes = {c.id_origem: c for c in Contato.objects.filter(origem=SISTEMA).exclude(id_origem="")}
        agora = timezone.now()
        for rec in registros:
            contagem["lidos"] += 1
            cid, acc, nome = cv.texto(rec.get("Id")), cv.texto(rec.get("AccountId")), cv.texto(rec.get("Name"))
            empresa_id = empresa_por_account.get(acc)
            if not cid or not nome:
                contagem["rejeitado"] += 1
                ctx.ocorrencia(T.REJEITADO, "Contato sem Id ou nome", id_origem=cid)
                continue
            if not empresa_id:
                contagem["rejeitado"] += 1
                ctx.ocorrencia(T.REJEITADO, f"Conta {acc} do contato não foi importada", id_origem=cid, nome=nome)
                continue
            email = cv.texto(rec.get("Email")).lower()
            dados = {"nome": nome, "cargo": cv.texto(rec.get("Title")), "telefone": cv.texto(rec.get("Phone")),
                     "celular": cv.texto(rec.get("MobilePhone")), "email": email if "@" in email else ""}
            c = existentes.get(cid)
            if c is None:
                Contato.objects.create(empresa_id=empresa_id, origem=SISTEMA, id_origem=cid,
                                       cargo_confirmado=bool(dados["cargo"]), coletado_em=agora,
                                       fonte_referencia=f"Contact {cid}", **dados)
                contagem["criado"] += 1
                continue
            mudou = False
            for k, v in dados.items():  # vazio não apaga
                if v and getattr(c, k) != v:
                    setattr(c, k, v)
                    mudou = True
            if c.empresa_id != empresa_id:
                ctx.ocorrencia(T.AVISO, f"Contato mudou de conta na origem ({c.empresa_id} -> {empresa_id}); mantido "
                                        "vínculo anterior por causa do histórico", id_origem=cid, nome=nome)
            c.coletado_em = agora
            c.save()
            contagem["atualizado" if mudou else "inalterado"] += 1
        return {}

    return _executar(Importacao.Tipo.CONTATOS, origem, {}, executado_por, dry_run, corpo)


# ============================================================
# LEADS BRIDA (Lead) – usados só no cruzamento


def importar_leads(registros, *, origem="Salesforce", executado_por="", dry_run=False):
    from carteira.models import LeadSalesforce

    def corpo(imp, ctx, contagem):
        T = OcorrenciaImportacao.Tipo
        existentes = {lead.salesforce_id: lead for lead in LeadSalesforce.objects.all()}
        agora = timezone.now()
        for rec in registros:
            contagem["lidos"] += 1
            lid = cv.texto(rec.get("Id"))
            dist = cv.texto((rec.get("Distributor__r") or {}).get("Name"))
            if not lid:
                contagem["rejeitado"] += 1
                continue
            doc = cnpj_srv.analisar(rec.get("CNPJ__c"))
            if not doc.cnpj:
                ctx.ocorrencia(T.INCONSISTENTE, f"Lead sem CNPJ válido ('{rec.get('CNPJ__c')}')", id_origem=lid,
                               nome=cv.texto(rec.get("Company")))
            dados = dict(cnpj=doc.cnpj, empresa_nome=cv.texto(rec.get("Company")), status=cv.texto(rec.get("Status")),
                         convertido=bool(rec.get("IsConverted")), conta_convertida_id=cv.texto(rec.get("ConvertedAccountId")),
                         distribuidor=dist, criado_em_origem=cv.data_hora(rec.get("CreatedDate")))
            lead = existentes.get(lid)
            if lead is None:
                LeadSalesforce.objects.create(salesforce_id=lid, ultimo_visto_em=agora, ultima_importacao=imp, **dados)
                contagem["criado"] += 1
                continue
            mudou = any(getattr(lead, k) != v for k, v in dados.items() if v not in (None, ""))
            for k, v in dados.items():
                if v not in (None, ""):
                    setattr(lead, k, v)
            lead.ultimo_visto_em, lead.ultima_importacao = agora, imp
            lead.save()
            contagem["atualizado" if mudou else "inalterado"] += 1
        return {}

    return _executar(Importacao.Tipo.LEADS, origem, {}, executado_por, dry_run, corpo)


# ============================================================
# COMPRAS (notas fiscais)

_TIPO_POR_SF = {"Saída": NotaFiscal.Tipo.VENDA, "Entrada": NotaFiscal.Tipo.DEVOLUCAO}


def _agrupar_itens(itens, ctx, contagem, tipos):
    T = OcorrenciaImportacao.Tipo
    notas = {}
    for it in itens:
        contagem["itens"] += 1
        inv = it.get("Invoice__r") or {}
        inv_id = cv.texto(inv.get("Id"))
        tipo = _TIPO_POR_SF.get(cv.texto(inv.get("Type__c")))
        if not inv_id or tipo not in tipos:
            contagem["itens_rejeitados"] += 1
            contagem["rejeitado"] += 1
            ctx.ocorrencia(T.REJEITADO, f"Item de nota sem nota/tipo reconhecido (tipo='{inv.get('Type__c')}')",
                           id_origem=cv.texto(it.get("Id")))
            continue
        n = notas.setdefault(inv_id, {
            "numero": cv.texto(inv.get("Name")), "tipo": tipo,
            "cliente": cv.texto((inv.get("Customer__r") or {}).get("Id")),
            "data": None, "valor": Decimal("0"), "itens": 0, "categorias": defaultdict(Decimal),
            "itens_sem_valor": 0,
        })
        n["itens"] += 1
        d = cv.data(it.get("EmissionDate__c"))
        if d and (n["data"] is None or d < n["data"]):
            n["data"] = d
        valor = cv.decimal(it.get("ValorTotalFormula__c"))
        if valor is None:
            n["itens_sem_valor"] += 1
            valor = Decimal("0")
        n["valor"] += valor
        n["categorias"][cv.texto(it.get("TypeofProdutct__c")) or "(sem tipo de produto)"] += valor
    return notas


def importar_compras(itens, *, desde, tipos=(NotaFiscal.Tipo.VENDA, NotaFiscal.Tipo.DEVOLUCAO),
                     origem="Salesforce", executado_por="", dry_run=False, consultas=None):
    from carteira.services.indicadores import recalcular_indicadores

    tipos = tuple(tipos)
    parametros = {"desde": desde.isoformat(), "tipos": list(tipos), "consultas": consultas or []}

    def corpo(imp, ctx, contagem):
        T = OcorrenciaImportacao.Tipo
        notas = _agrupar_itens(itens, ctx, contagem, tipos)
        contagem["lidos"] = len(notas)

        empresa_por_account = dict(
            RegistroOrigem.objects.filter(sistema=SISTEMA, objeto=OBJ_ACCOUNT).values_list("id_origem", "empresa_id")
        )
        existentes = {n.id_origem: n for n in NotaFiscal.objects.filter(id_origem__in=list(notas))}
        sem_empresa = defaultdict(int)

        for inv_id, n in notas.items():
            empresa_id = empresa_por_account.get(n["cliente"]) if n["cliente"] else None
            if not n["cliente"]:
                ctx.ocorrencia(T.INCONSISTENTE, f"Nota {n['numero']} sem cliente (Customer__c vazio)", id_origem=inv_id)
            elif not empresa_id:
                sem_empresa[n["cliente"]] += 1
            if n["data"] is None:
                ctx.ocorrencia(T.INCONSISTENTE, f"Nota {n['numero']} sem data de emissão", id_origem=inv_id)
            if n["itens_sem_valor"]:
                ctx.ocorrencia(T.AVISO, f"Nota {n['numero']}: {n['itens_sem_valor']} item(ns) sem ValorTotalFormula__c "
                               "(somado como 0)", id_origem=inv_id)

            valores = {
                "numero": n["numero"], "tipo": n["tipo"], "cliente_id_origem": n["cliente"],
                "empresa_id": empresa_id, "data_emissao": n["data"],
                "valor_total": n["valor"].quantize(Decimal("0.01")), "qtd_itens": n["itens"],
                "categorias": {k: str(v.quantize(Decimal("0.01"))) for k, v in n["categorias"].items()},
                "presente_na_origem": True,
            }
            nota = existentes.get(inv_id)
            if nota is None:
                NotaFiscal.objects.create(id_origem=inv_id, primeira_importacao=imp, ultima_importacao=imp, **valores)
                contagem["criado"] += 1
                continue
            # Não desvincula empresa se a conta não veio desta vez
            if valores["empresa_id"] is None:
                valores["empresa_id"] = nota.empresa_id
            mudou = any(getattr(nota, k) != v for k, v in valores.items())
            for k, v in valores.items():
                setattr(nota, k, v)
            nota.ultima_importacao = imp
            nota.save()
            contagem["atualizado" if mudou else "inalterado"] += 1

        for cliente, qtd in sem_empresa.items():
            ctx.ocorrencia(T.INCONSISTENTE,
                           f"{qtd} nota(s) de conta não importada (fora do filtro de clientes ou ainda não sincronizada). "
                           "Serão vinculadas quando a conta for importada.", id_origem=cliente)

        # Notas do período que não vieram mais na origem (ex.: canceladas depois): saem dos indicadores,
        # mas não são apagadas.
        ausentes = (NotaFiscal.objects.filter(tipo__in=tipos, data_emissao__gte=desde, presente_na_origem=True)
                    .exclude(ultima_importacao=imp).update(presente_na_origem=False))
        return {"itens_lidos": contagem["itens"], "itens_rejeitados": contagem["itens_rejeitados"],
                "notas_ausentes_na_origem": ausentes, "contas_sem_empresa": len(sem_empresa)}

    return _executar(Importacao.Tipo.COMPRAS, origem, parametros, executado_por, dry_run, corpo,
                     apos=recalcular_indicadores)
