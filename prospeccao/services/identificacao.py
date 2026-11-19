"""
Identificação de candidatos: qual empresa (CNPJ) corresponde a um resultado de descoberta.

Regras:
  - CNPJ informado pela fonte ou publicado no site é VERIFICADO na Receita (BrasilAPI) antes do vínculo:
    município e nome precisam bater; senão o candidato fica em CONFLITO para revisão humana.
  - Sem CNPJ, o candidato fica PENDENTE. Nome/telefone/domínio só geram SUGESTÕES de correspondência
    (nunca fusão automática).
  - O vínculo preserva o candidato (fonte, id externo, URL) para rastreabilidade.
"""
from collections import defaultdict

from django.db import IntegrityError, transaction
from django.utils import timezone

from carteira.models import CanalEmpresa, Empresa, StatusCNPJ
from carteira.services import cnpj as cnpj_srv
from carteira.services.normalizacao import (
    digitos_telefone, dominio_de_email, dominio_de_url, nome_para_comparacao, normalizar_texto, similaridade_nome,
)
from prospeccao.models import Candidato, Evidencia
from prospeccao.services.enriquecimento import aplicar_dados_cnpj, registrar_canal, registrar_dado


def registrar_resultado(r, fonte, execucao, pesquisa):
    """Cria ou atualiza o candidato (fonte, id_externo). Retorna (candidato, criado)."""
    agora = timezone.now()
    cand = Candidato.objects.filter(fonte=fonte, id_externo=r.id_externo).first()
    criado = cand is None
    if criado:
        cand = Candidato(fonte=fonte, id_externo=r.id_externo, primeira_execucao=execucao, primeira_coleta_em=agora)
    for campo, valor in (("nome", r.nome), ("endereco", r.endereco), ("cidade", r.cidade), ("uf", r.uf),
                         ("cep", r.cep), ("telefone", r.telefone), ("email", r.email), ("site", r.site),
                         ("atividade_fonte", r.atividade), ("url_referencia", r.url_referencia)):
        if valor:  # vazio na fonte não apaga o que já foi coletado
            setattr(cand, campo, valor)
    if r.dados:
        cand.dados = {**cand.dados, **r.dados}
    cand.dominio = dominio_de_url(cand.site) or dominio_de_email(cand.email) or cand.dominio
    cand.coletado_em, cand.ultima_execucao = agora, execucao
    if r.cnpj and not cand.cnpj_informado:
        cand.cnpj_informado = r.cnpj
    if r.cnpj and cand.status == Candidato.Status.PENDENTE:
        cand.cnpj_candidato, cand.metodo_identificacao = r.cnpj, "FONTE"
        cand.status = Candidato.Status.CNPJ_ENCONTRADO
    cand.save()
    if pesquisa:
        cand.pesquisas.add(pesquisa)
    return cand, criado


def avaliar_correspondencia(cand, dados):
    """(status, confianca, detalhe) comparando o candidato com os dados oficiais do CNPJ."""
    sim = max(similaridade_nome(cand.nome, dados.razao_social), similaridade_nome(cand.nome, dados.nome_fantasia))
    cidade_ok = bool(cand.cidade) and normalizar_texto(cand.cidade) == normalizar_texto(dados.municipio)
    uf_ok = not cand.uf or cand.uf.upper() == (dados.uf or "").upper()
    det = (f"Receita: {dados.razao_social} / {dados.nome_fantasia or '-'} – {dados.municipio}/{dados.uf}; "
           f"similaridade de nome {sim:.2f}; município {'confere' if cidade_ok else 'diferente/desconhecido'}")
    if uf_ok and cidade_ok and sim >= 0.6:
        return Candidato.Status.IDENTIFICADO, Candidato.Confianca.ALTA, det
    if uf_ok and (cidade_ok and sim >= 0.35 or sim >= 0.8):
        return Candidato.Status.IDENTIFICADO, Candidato.Confianca.MEDIA, det
    return Candidato.Status.CONFLITO, Candidato.Confianca.BAIXA, det


@transaction.atomic
def vincular(cand, cnpj, *, dados=None, fonte_enriquecimento="", usuario=None, metodo=None, data_referencia=None,
             rotulo_fonte=""):
    """Vincula o candidato à empresa do CNPJ (cria se não existir). Não funde por nome."""
    empresa = Empresa.objects.select_for_update().filter(cnpj=cnpj).first()
    if empresa is None:
        from carteira.models import empresa_por_cnpj
        empresa = empresa_por_cnpj(cnpj)  # CNPJ associado a uma empresa já conhecida: não duplica
    if empresa is None:
        try:
            with transaction.atomic():
                empresa = Empresa.objects.create(
                    cnpj=cnpj, cnpj_status=StatusCNPJ.VALIDO, cnpj_origem=cnpj, origem=Empresa.Origem.DESCOBERTA,
                    nome=(dados.nome_fantasia or dados.razao_social) if dados else cand.nome)
        except IntegrityError:
            empresa = Empresa.objects.select_for_update().get(cnpj=cnpj)
    agora = timezone.now()
    cand.empresa, cand.cnpj_candidato = empresa, cnpj
    cand.status = Candidato.Status.IDENTIFICADO
    cand.vinculado_em, cand.vinculado_por = agora, usuario
    if metodo:
        cand.metodo_identificacao = metodo
    cand.save()
    if dados:
        aplicar_dados_cnpj(empresa, dados, fonte_enriquecimento, data_referencia=data_referencia,
                           rotulo_fonte=rotulo_fonte)
    fonte = cand.fonte.codigo
    if fonte != "receita_cnpj":  # canais da base Receita já entram (como CADASTRAIS) via aplicar_dados_cnpj
        if cand.telefone:
            registrar_canal(empresa, CanalEmpresa.Tipo.TELEFONE, cand.telefone, fonte, cand.url_referencia, "INCERTO")
        if cand.email:
            registrar_canal(empresa, CanalEmpresa.Tipo.EMAIL, cand.email, fonte, cand.url_referencia)
    for email, url in (cand.dados.get("site_emails") or {}).items():
        registrar_canal(empresa, CanalEmpresa.Tipo.EMAIL, email, "site_publico", url)
    for tel, url in (cand.dados.get("site_telefones") or {}).items():
        registrar_canal(empresa, CanalEmpresa.Tipo.TELEFONE, tel, "site_publico", url, "COMERCIAL")
    if cand.site:
        registrar_canal(empresa, CanalEmpresa.Tipo.SITE, cand.site, fonte, cand.url_referencia, "COMERCIAL")
        registrar_dado(empresa, "site", cand.site, fonte, cand.url_referencia)
        if not empresa.site:
            empresa.site, empresa.dominio = cand.site[:300], cand.dominio
            empresa.save(update_fields=["site", "dominio", "atualizado_em"])
    Evidencia.objects.filter(candidato=cand, empresa__isnull=True).update(empresa=empresa)
    return empresa


# Palavras comuns demais para identificar uma empresa sozinhas
GENERICAS = {
    "AUTO", "AUTOS", "PECAS", "PECA", "AUTOPECAS", "TRANSPORTE", "TRANSPORTES", "TRANSPORTADORA", "FROTA", "LOGISTICA",
    "COMERCIO", "COMERCIAL", "SERVICOS", "SERVICO", "MECANICA", "OFICINA", "CENTER", "CENTRO", "AUTOMOTIVO",
    "AUTOMOTIVA", "DISTRIBUIDORA", "INDUSTRIA", "INDUSTRIAL", "BRASIL", "GRUPO", "EMPRESA", "POSTO", "MOTOS",
    "MOTO", "ELETRICA", "ELETRICO", "CAMINHOES", "ONIBUS", "LTDA", "EXPRESS", "EXPRESSO", "CARGAS", "SAO", "PAULO",
    "GUARULHOS", "MATERIAIS", "CONSTRUCAO", "FILIAL", "UNIDADE", "MATRIZ",
    # palavras de atividade comuns demais para identificar uma empresa (lição do piloto real)
    "METALURGICA", "METALURGICO", "ESTAMPARIA", "PLASTICOS", "PLASTICO", "EMBALAGENS", "COOPERATIVA", "TRABALHO",
    "ELETRO", "ADMINISTRACAO", "AGROPECUARIA", "AGRO", "PECUARIA", "PAISAGISMO", "SOLUCOES", "AMBIENTAL", "TEXTIL",
    "IMPORTACAO", "EXPORTACAO", "IMPORT", "EXPORT", "TECNOLOGIA", "ENGENHARIA", "CONSTRUCOES", "MAQUINAS",
    "EQUIPAMENTOS", "FERRAMENTAS", "PRODUTOS", "ALIMENTOS", "QUIMICA", "MINAS", "PAULISTA", "NACIONAL", "INTERNACIONAL",
    "HOLDING", "PARTICIPACOES", "EMPREENDIMENTOS", "REPRESENTACOES", "COMERCIO", "DISTRIBUICAO", "RODOVIARIO",
    "CARGAS", "MULTIMODAL", "OPERADOR", "ARMAZENS", "GERAIS",
}


def tokens_distintivos(nome):
    return {t for t in nome_para_comparacao(nome).split() if len(t) >= 3 and t not in GENERICAS}


class IndiceCarteira:
    """
    Índice em memória para SUGERIR correspondências de candidatos sem CNPJ (nunca fundir).
    Regras: telefone igual ou domínio próprio igual já sugerem; nome só sugere quando há palavra
    DISTINTIVA em comum, nome muito parecido E mesma cidade (palavra comum sozinha não basta).
    """

    def __init__(self):
        self.por_telefone, self.por_dominio = defaultdict(set), defaultdict(set)
        self.por_cidade = defaultdict(list)
        for e in Empresa.objects.values("id", "nome", "nome_fantasia", "telefone", "dominio", "email", "cidade"):
            t = digitos_telefone(e["telefone"])
            if t:
                self.por_telefone[t].add(e["id"])
            for d in (e["dominio"], dominio_de_email(e["email"])):
                if d:
                    self.por_dominio[d].add(e["id"])
            if e["cidade"]:
                self.por_cidade[normalizar_texto(e["cidade"])].append(
                    (e["id"], nome_para_comparacao(e["nome"]), nome_para_comparacao(e["nome_fantasia"])))
        for emp_id, tipo, valor in CanalEmpresa.objects.values_list("empresa_id", "tipo", "valor"):
            if tipo == CanalEmpresa.Tipo.TELEFONE:
                self.por_telefone[valor].add(emp_id)

    def sugerir(self, cand):
        motivos = defaultdict(list)
        t = digitos_telefone(cand.telefone)
        for i in self.por_telefone.get(t, ()):
            motivos[i].append("telefone igual")
        if cand.dominio:
            for i in self.por_dominio.get(cand.dominio, ()):
                motivos[i].append(f"domínio {cand.dominio}")
        alvo = nome_para_comparacao(cand.nome)
        dist_alvo = tokens_distintivos(cand.nome)
        if alvo and dist_alvo and cand.cidade:
            from difflib import SequenceMatcher
            for emp_id, n1, n2 in self.por_cidade.get(normalizar_texto(cand.cidade), []):
                for n in (n1, n2):
                    if not n:
                        continue
                    comuns = dist_alvo & {t for t in n.split() if len(t) >= 3 and t not in GENERICAS}
                    if comuns and (n == alvo or SequenceMatcher(None, n, alvo).ratio() >= 0.85):
                        motivos[emp_id].append(f"nome distintivo semelhante na mesma cidade ({', '.join(sorted(comuns))})")
                        break
        if not motivos:
            return []
        empresas = {e.pk: e for e in Empresa.objects.filter(pk__in=motivos)}
        return [{"empresa_id": i, "nome": empresas[i].nome, "cnpj": empresas[i].cnpj or "",
                 "salesforce": bool(empresas[i].salesforce_id), "motivos": sorted(set(m))}
                for i, m in motivos.items() if i in empresas][:10]


def cnpj_valido(valor):
    return cnpj_srv.analisar(valor).cnpj
