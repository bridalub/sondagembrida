"""
Prospecção: pesquisas, fontes, execuções, candidatos externos, evidências e oportunidades.

Separação obrigatória:
  1. Qualidade/confiabilidade dos dados  -> Oportunidade.qualidade_dados (+ pendências)
  2. Evidências de possível aplicação     -> Evidencia (CONFIRMADO_FONTE / HIPOTESE / INSUFICIENTE)
  3. Prioridade operacional               -> Oportunidade.prioridade (+ critérios explicados)
Ausência de evidência = "aplicação a investigar", nunca "sem potencial".
"""
import os

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from carteira.models import Empresa


def linhas(texto):
    """Campo de texto 'um item por linha' -> lista sem vazios."""
    return [x.strip() for x in (texto or "").replace("\r", "").split("\n") if x.strip()]


# ============================================================
# Catálogo (somente categorias reais vendidas)


class CategoriaProduto(models.Model):
    nome = models.CharField(max_length=120, unique=True)
    origem = models.CharField(max_length=200, default="Salesforce InvoiceItem__c.TypeofProdutct__c")
    notas_com_venda = models.PositiveIntegerField(default=0)
    ativa = models.BooleanField(default=True)
    atualizada_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "categoria de produto"
        verbose_name_plural = "categorias de produto (catálogo)"

    def __str__(self):
        return self.nome


# ============================================================
# Fontes


class Fonte(models.Model):
    class Tipo(models.TextChoices):
        DESCOBERTA = "DESCOBERTA", "Descoberta"
        IDENTIFICACAO = "IDENTIFICACAO", "Identificação"
        ENRIQUECIMENTO = "ENRIQUECIMENTO", "Enriquecimento"
        IMPORTACAO = "IMPORTACAO", "Importação de arquivo"
        ANALISE = "ANALISE", "Análise por IA (opcional)"

    codigo = models.SlugField(max_length=40, unique=True)
    nome = models.CharField(max_length=120)
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    habilitada = models.BooleanField(default=True)
    endpoints = models.TextField(blank=True, help_text="Um por linha, em ordem de preferência")
    intervalo_minimo_segundos = models.FloatField(default=1.0, help_text="Espera mínima entre requisições")
    timeout_segundos = models.PositiveIntegerField(default=30)
    max_consultas_execucao = models.PositiveIntegerField(default=50)
    max_consultas_dia = models.PositiveIntegerField(null=True, blank=True)
    max_retentativas = models.PositiveSmallIntegerField(default=2)
    custo_por_consulta = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    variavel_credencial = models.CharField(
        max_length=80, blank=True, help_text="NOME da variável de ambiente com a credencial (o valor nunca é exibido)",
    )
    licenca = models.CharField(max_length=200, blank=True)
    termos_uso_url = models.URLField(blank=True)
    atribuicao = models.CharField(max_length=200, blank=True)
    dados_permitidos = models.TextField(blank=True, help_text="O que pode ser armazenado desta fonte")
    observacoes = models.TextField(blank=True)

    class Meta:
        ordering = ["tipo", "nome"]

    def __str__(self):
        return self.nome

    @property
    def lista_endpoints(self):
        return linhas(self.endpoints)

    @property
    def credencial_configurada(self):
        return True if not self.variavel_credencial else bool(os.getenv(self.variavel_credencial))


class UsoFonte(models.Model):
    fonte = models.ForeignKey(Fonte, on_delete=models.CASCADE, related_name="usos")
    dia = models.DateField(default=timezone.localdate)
    consultas = models.PositiveIntegerField(default=0)
    erros = models.PositiveIntegerField(default=0)
    custo = models.DecimalField(max_digits=12, decimal_places=4, default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["fonte", "dia"], name="uso_fonte_dia_unico")]


# ============================================================
# Exclusões comerciais explícitas (definidas pelo gestor)


class ExclusaoComercial(models.Model):
    class Tipo(models.TextChoices):
        TERMO = "TERMO", "Termo no nome/atividade"
        CNAE = "CNAE", "CNAE (prefixo)"
        CNPJ_RAIZ = "CNPJ_RAIZ", "Raiz de CNPJ (8 dígitos)"

    tipo = models.CharField(max_length=12, choices=Tipo.choices)
    valor = models.CharField(max_length=120)
    motivo = models.CharField(max_length=255)
    ativa = models.BooleanField(default=True)
    criada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    criada_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "exclusão comercial"
        verbose_name_plural = "exclusões comerciais"

    def __str__(self):
        return f"{self.get_tipo_display()}: {self.valor}"


# ============================================================
# Frentes comerciais (editáveis pelo gestor) e planos de pesquisa


class FrenteComercial(models.Model):
    nome = models.CharField(max_length=120, unique=True)
    descricao = models.TextField(help_text="O que se busca nesta frente, em linguagem natural")
    cnaes_referencia = models.TextField(blank=True, help_text="Prefixos de CNAE de referência (um por linha); "
                                                              "conferidos na tabela oficial ao montar o plano")
    termos_operacao = models.TextField(blank=True, help_text="Termos de operação no nome/site (um por linha)")
    sinais_operacionais = models.TextField(blank=True, help_text="Sinais a investigar (frota, oficina própria...)")
    contextos_negativos = models.TextField(blank=True, help_text="Contextos que NÃO comprovam operação própria "
                                                                 "(financiamento, seguro, venda para...)")
    aplicacoes = models.ManyToManyField(CategoriaProduto, blank=True)
    ativa = models.BooleanField(default=True)
    atualizada_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "frente comercial"
        verbose_name_plural = "frentes comerciais"

    def __str__(self):
        return self.nome


# ============================================================
# Pesquisas e execuções


class Pesquisa(models.Model):
    class Modo(models.TextChoices):
        SIMPLES = "SIMPLES", "Simples (filtros diretos)"
        PLANEJADA = "PLANEJADA", "Planejada (objetivo + frentes + plano + investigação)"

    class IdadeDesconhecida(models.TextChoices):
        SINALIZAR = "SINALIZAR", "Manter e sinalizar 'tempo de empresa desconhecido'"
        REVISAR = "REVISAR", "Manter e enviar para revisão"

    nome = models.CharField(max_length=150)
    descricao = models.TextField(blank=True)
    modo = models.CharField(max_length=10, choices=Modo.choices, default=Modo.SIMPLES)
    objetivo = models.TextField(blank=True, help_text="Objetivo da busca em linguagem natural (definido pelo gestor)")
    frentes = models.ManyToManyField(FrenteComercial, blank=True, related_name="pesquisas")
    aplicacoes = models.ManyToManyField(CategoriaProduto, blank=True, related_name="pesquisas",
                                        help_text="Produtos/aplicações de interesse (opcional)")
    criterios_obrigatorios = models.TextField(blank=True, help_text="Um por linha (ex.: situação ATIVA; ter telefone)")
    preferencias = models.TextField(blank=True, help_text="Preferências de priorização (NÃO excluem ninguém)")
    quantidade_desejada = models.PositiveIntegerField(
        default=10, help_text="Total de novos leads da pesquisa (1 a 100), somando frentes e municípios")
    permitir_exploratoria = models.BooleanField(
        default=True, help_text="Permite selecionar para abordagem empresas com aderência provável (sem evidência forte)")
    plano_ativo = models.ForeignKey("PlanoPesquisa", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # --- limites da inteligência (IA + busca web) por execução
    max_buscas_web_empresa = models.PositiveIntegerField(default=3, help_text="Consultas web por empresa investigada")
    max_buscas_descoberta = models.PositiveIntegerField(default=6, help_text="Consultas web de descoberta por frente")
    max_paginas_site = models.PositiveIntegerField(default=5, help_text="Páginas lidas por site")
    ia_max_chamadas = models.PositiveIntegerField(default=40, help_text="Chamadas de IA por execução")
    ia_max_tokens_saida = models.PositiveIntegerField(default=6000, help_text="Tokens de saída por chamada de IA")
    teto_custo_usd = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Teto financeiro (IA + busca web) por execução. VAZIO = nenhuma chamada paga é feita.")
    validade_analise_ia_dias = models.PositiveIntegerField(default=30, help_text="Reaproveita análises de IA mais novas")
    segmentos = models.TextField(blank=True, help_text="Um por linha. Pode ser um segmento que não existe na carteira.")
    termos_busca = models.TextField(blank=True, help_text="Termos procurados no nome das empresas (um por linha)")
    termos_operacao = models.TextField(
        blank=True, help_text="Termos de operação a procurar nos sites (frota, máquinas, manutenção...), um por linha",
    )
    cnaes_filtro = models.TextField(
        blank=True, help_text="Base Receita: prefixos de CNAE (principal ou secundário), um por linha. Vazio = todas as atividades.",
    )
    situacoes_receita = models.CharField(
        max_length=80, default="ATIVA",
        help_text="Base Receita: situações cadastrais a trazer (ATIVA, SUSPENSA, INAPTA, BAIXADA, NULA), separadas por vírgula",
    )
    cursor = models.JSONField(default=dict, blank=True, help_text="Posição do lote (base Receita) para continuar")
    filtros_osm = models.TextField(
        blank=True, help_text="Filtros de categoria OpenStreetMap chave=valor, um por linha (ex.: shop=car_repair)",
    )
    uf = models.CharField("UF", max_length=2, default="SP")
    cidades = models.TextField(help_text="Uma cidade por linha (limite administrativo do município)")
    fontes = models.ManyToManyField(Fonte, related_name="pesquisas", limit_choices_to={"tipo": Fonte.Tipo.DESCOBERTA})
    limite_resultados = models.PositiveIntegerField(default=200, help_text="Máximo de resultados por execução")
    limite_consultas = models.PositiveIntegerField(default=20, help_text="Máximo de consultas à fonte de descoberta")
    identificar_por_site = models.BooleanField(default=True, help_text="Procurar CNPJ e evidências no site público")
    max_sites_execucao = models.PositiveIntegerField(default=40)
    enriquecer_por_cnpj = models.BooleanField(default=True)
    max_duracao_segundos = models.PositiveIntegerField(default=900)
    frequencia_horas = models.PositiveIntegerField(
        null=True, blank=True, help_text="Vazio = só execução manual. Exige agendamento ativo na configuração.",
    )
    validade_dados_dias = models.PositiveIntegerField(
        default=30, help_text="Após este prazo, dados de enriquecimento são consultados novamente",
    )
    idade_min_anos = models.PositiveIntegerField(null=True, blank=True, help_text="Tempo de empresa mínimo (abertura)")
    idade_max_anos = models.PositiveIntegerField(null=True, blank=True)
    idade_desconhecida = models.CharField(max_length=10, choices=IdadeDesconhecida.choices,
                                          default=IdadeDesconhecida.SINALIZAR)
    exclusoes_termos = models.TextField(blank=True, help_text="Exclusões comerciais desta pesquisa (termos, um por linha)")
    exclusoes_cnaes = models.TextField(blank=True, help_text="Prefixos de CNAE excluídos nesta pesquisa (um por linha)")
    ativa = models.BooleanField(default=True)
    proxima_execucao_em = models.DateTimeField(null=True, blank=True)
    criada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    criada_em = models.DateTimeField(auto_now_add=True)
    atualizada_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["nome"]

    def __str__(self):
        return self.nome


class Execucao(models.Model):
    class Status(models.TextChoices):
        PENDENTE = "PENDENTE", "Na fila"
        EXECUTANDO = "EXECUTANDO", "Executando"
        CONCLUIDA = "CONCLUIDA", "Concluída"
        PARCIAL = "PARCIAL", "Concluída com limites (parcial)"
        INTERROMPIDA = "INTERROMPIDA", "Interrompida"
        CANCELADA = "CANCELADA", "Cancelada"
        FALHA = "FALHA", "Falha"
        BLOQUEADA = "BLOQUEADA", "Bloqueada por configuração"

    class Gatilho(models.TextChoices):
        MANUAL = "MANUAL", "Manual (tela)"
        COMANDO = "COMANDO", "Comando"
        AGENDADA = "AGENDADA", "Agendada"
        CSV = "CSV", "Importação CSV"

    ATIVOS = (Status.PENDENTE, Status.EXECUTANDO)

    pesquisa = models.ForeignKey(Pesquisa, on_delete=models.PROTECT, related_name="execucoes")
    plano = models.ForeignKey("PlanoPesquisa", null=True, blank=True, on_delete=models.SET_NULL, related_name="execucoes")
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.PENDENTE)
    gatilho = models.CharField(max_length=10, choices=Gatilho.choices, default=Gatilho.MANUAL)
    solicitada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="+")
    parametros = models.JSONField(default=dict, blank=True)
    criada_em = models.DateTimeField(auto_now_add=True)
    iniciada_em = models.DateTimeField(null=True, blank=True)
    finalizada_em = models.DateTimeField(null=True, blank=True)
    heartbeat_em = models.DateTimeField(null=True, blank=True)
    cancelamento_solicitado = models.BooleanField(default=False)
    pausa_solicitada = models.BooleanField(default=False)
    etapa = models.CharField(max_length=60, blank=True)
    progresso = models.JSONField(default=dict, blank=True)
    consultas = models.JSONField(default=dict, blank=True, help_text="{fonte: nº de consultas}")
    custo = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    resultados = models.PositiveIntegerField(default=0)
    candidatos_novos = models.PositiveIntegerField(default=0)
    candidatos_existentes = models.PositiveIntegerField(default=0)
    identificados = models.PositiveIntegerField(default=0)
    pendentes = models.PositiveIntegerField(default=0)
    conhecidos_carteira = models.PositiveIntegerField(default=0)
    oportunidades_novas = models.PositiveIntegerField(default=0, help_text="Novas empresas EXTERNAS (não conhecidas)")
    complementados = models.PositiveIntegerField(default=0, help_text="Empresas complementadas pela internet")
    ia_analises = models.PositiveIntegerField(default=0, help_text="Análises de IA efetivamente executadas")
    erros = models.PositiveIntegerField(default=0)
    completa = models.BooleanField(default=False, help_text="Todas as etapas rodaram sem cortes por limite ou falha")
    limites_atingidos = models.JSONField(default=list, blank=True)
    mensagem = models.TextField(blank=True)

    class Meta:
        ordering = ["-criada_em"]
        constraints = [
            models.UniqueConstraint(fields=["pesquisa"], condition=Q(status__in=["PENDENTE", "EXECUTANDO"]),
                                    name="uma_execucao_ativa_por_pesquisa"),
        ]

    def __str__(self):
        return f"Execução {self.pk} – {self.pesquisa}"

    @property
    def ativa(self):
        return self.status in self.ATIVOS


class LogExecucao(models.Model):
    class Nivel(models.TextChoices):
        INFO = "INFO", "Info"
        AVISO = "AVISO", "Aviso"
        ERRO = "ERRO", "Erro"

    execucao = models.ForeignKey(Execucao, on_delete=models.CASCADE, related_name="logs")
    nivel = models.CharField(max_length=5, choices=Nivel.choices, default=Nivel.INFO)
    fonte = models.CharField(max_length=40, blank=True)
    mensagem = models.TextField()
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


# ============================================================
# Candidatos (resultados externos ainda não confirmados)


class Candidato(models.Model):
    class Status(models.TextChoices):
        PENDENTE = "PENDENTE", "Identificação pendente (sem CNPJ)"
        CNPJ_ENCONTRADO = "CNPJ_ENCONTRADO", "CNPJ encontrado, aguardando verificação"
        IDENTIFICADO = "IDENTIFICADO", "Identificado (CNPJ verificado)"
        CONFLITO = "CONFLITO", "Conflito de identificação"
        EXCLUIDO = "EXCLUIDO", "Excluído por regra comercial"
        DESCARTADO = "DESCARTADO", "Descartado na revisão"

    class Confianca(models.TextChoices):
        ALTA = "ALTA", "Alta"
        MEDIA = "MEDIA", "Média"
        BAIXA = "BAIXA", "Baixa"

    fonte = models.ForeignKey(Fonte, on_delete=models.PROTECT, related_name="candidatos")
    id_externo = models.CharField(max_length=120)
    nome = models.CharField(max_length=255)
    endereco = models.CharField(max_length=255, blank=True)
    cidade = models.CharField(max_length=120, blank=True)
    uf = models.CharField(max_length=2, blank=True)
    cep = models.CharField(max_length=10, blank=True)
    telefone = models.CharField(max_length=60, blank=True)
    email = models.CharField(max_length=254, blank=True)
    site = models.URLField(max_length=300, blank=True)
    dominio = models.CharField(max_length=200, blank=True, db_index=True)
    atividade_fonte = models.CharField(max_length=500, blank=True, help_text="Categoria/descrição informada pela fonte")
    cnpj_informado = models.CharField(max_length=14, blank=True, help_text="CNPJ fornecido pela própria fonte")
    dados = models.JSONField(default=dict, blank=True, help_text="Somente campos permitidos pela fonte")
    url_referencia = models.URLField(max_length=500, blank=True)
    pesquisas = models.ManyToManyField(Pesquisa, related_name="candidatos", blank=True)
    primeira_execucao = models.ForeignKey(Execucao, null=True, on_delete=models.SET_NULL, related_name="candidatos_criados")
    ultima_execucao = models.ForeignKey(Execucao, null=True, on_delete=models.SET_NULL, related_name="+")
    primeira_coleta_em = models.DateTimeField(default=timezone.now)
    coletado_em = models.DateTimeField(default=timezone.now)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDENTE)
    cnpj_candidato = models.CharField(max_length=14, blank=True)
    metodo_identificacao = models.CharField(max_length=40, blank=True)
    confianca = models.CharField(max_length=5, choices=Confianca.choices, blank=True)
    detalhe_identificacao = models.TextField(blank=True)
    site_verificado_em = models.DateTimeField(null=True, blank=True)
    sugestoes = models.JSONField(default=list, blank=True, help_text="Possíveis correspondências (nunca fundidas sozinhas)")
    empresa = models.ForeignKey(Empresa, null=True, blank=True, on_delete=models.PROTECT, related_name="candidatos")
    vinculado_em = models.DateTimeField(null=True, blank=True)
    vinculado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="+")
    motivo_exclusao = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["fonte", "id_externo"], name="candidato_fonte_unico")]
        indexes = [models.Index(fields=["cnpj_candidato"], name="candidato_cnpj_idx")]
        ordering = ["nome"]

    def __str__(self):
        return f"{self.nome} ({self.fonte.codigo})"


# ============================================================
# Evidências e regras


class RegraEvidencia(models.Model):
    class Alvo(models.TextChoices):
        TEXTO_SITE = "TEXTO_SITE", "Texto do site público"
        NOME = "NOME", "Nome da empresa"
        ATIVIDADE_FONTE = "ATIVIDADE_FONTE", "Categoria/atividade informada pela fonte"
        CNAE = "CNAE", "Código CNAE (prefixo)"
        CNAE_DESCRICAO = "CNAE_DESCRICAO", "Descrição do CNAE"

    nome = models.CharField(max_length=150)
    ativa = models.BooleanField(default=True)
    alvo = models.CharField(max_length=16, choices=Alvo.choices)
    padroes = models.TextField(help_text="Um por linha. Palavras/expressões (sem acento/maiúsculas) ou prefixos de CNAE.")
    tipo_evidencia = models.CharField(max_length=20, choices=[
        ("FROTA", "Frota / veículos"), ("MAQUINAS", "Máquinas / equipamentos"), ("MANUTENCAO", "Manutenção"),
        ("ATIVIDADE", "Atividade operacional"), ("REVENDA", "Revenda / serviço automotivo"),
    ])
    descricao = models.CharField(max_length=255, help_text="Texto da evidência (ex.: 'Operação com caminhões')")
    aplicacoes = models.ManyToManyField(CategoriaProduto, blank=True, help_text="Somente categorias do catálogo real")
    pergunta_sugerida = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["alvo", "nome"]
        verbose_name = "regra de evidência"
        verbose_name_plural = "regras de evidência"

    def __str__(self):
        return self.nome


class Evidencia(models.Model):
    class Classificacao(models.TextChoices):
        CONFIRMADO_FONTE = "CONFIRMADO_FONTE", "Informado pela fonte (sujeito a erro)"
        HIPOTESE = "HIPOTESE", "Hipótese"
        INSUFICIENTE = "INSUFICIENTE", "Informação insuficiente"

    empresa = models.ForeignKey(Empresa, null=True, blank=True, on_delete=models.CASCADE, related_name="evidencias")
    candidato = models.ForeignKey(Candidato, null=True, blank=True, on_delete=models.CASCADE, related_name="evidencias")
    tipo = models.CharField(max_length=20)
    descricao = models.CharField(max_length=255)
    classificacao = models.CharField(max_length=16, choices=Classificacao.choices)
    aplicacoes = models.ManyToManyField(CategoriaProduto, blank=True, related_name="+")
    fonte = models.CharField(max_length=60)
    url = models.URLField(max_length=500, blank=True)
    trecho = models.CharField(max_length=300, blank=True, help_text="Trecho curto verificável")
    regra = models.ForeignKey(RegraEvidencia, null=True, blank=True, on_delete=models.SET_NULL)
    gerada_por = models.CharField(max_length=10, default="REGRA", help_text="REGRA / IA / HUMANO")
    chave = models.CharField(max_length=64, unique=True, help_text="Evita evidências duplicadas")
    coletada_em = models.DateTimeField(default=timezone.now)
    ativa = models.BooleanField(default=True)

    class Meta:
        ordering = ["classificacao", "-coletada_em"]


# ============================================================
# Oportunidades e decisões


class Oportunidade(models.Model):
    class Tipo(models.TextChoices):
        NOVA_EXTERNA = "NOVA_EXTERNA", "Nova oportunidade externa"
        LEAD_CONHECIDO = "LEAD_CONHECIDO", "Lead conhecido (Salesforce)"
        CADASTRO_SEM_COMPRA = "CADASTRO_SEM_COMPRA", "Cadastro no Salesforce sem compra comprovada"

    class Status(models.TextChoices):
        NOVA = "NOVA", "Nova (a revisar)"
        LIBERADA = "LIBERADA", "Liberada para abordagem"
        DESCARTADA = "DESCARTADA", "Descartada pelo gestor"
        BLOQUEADA = "BLOQUEADA", "Bloqueada / não contatar"
        JA_CLIENTE = "JA_CLIENTE", "Passou a constar como cliente"
        FORA_FILA = "FORA_FILA", "Fora da fila (situação cadastral baixada/nula)"
        FORA_REGIAO = "FORA_REGIAO", "Fora da região da pesquisa (endereço/telefone da fonte)"

    class Prioridade(models.TextChoices):
        ALTA = "ALTA", "Alta"
        MEDIA = "MEDIA", "Média"
        INVESTIGAR = "INVESTIGAR", "Aplicação a investigar"
        VERIFICAR_CADASTRO = "VERIFICAR_CADASTRO", "Verificar situação cadastral"

    class Qualidade(models.TextChoices):
        COMPLETA = "COMPLETA", "Completa"
        PARCIAL = "PARCIAL", "Parcial"
        INSUFICIENTE = "INSUFICIENTE", "Insuficiente"

    empresa = models.OneToOneField(Empresa, on_delete=models.PROTECT, related_name="oportunidade")
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NOVA)
    status_definido_por_humano = models.BooleanField(default=False)
    situacao_carteira = models.CharField(max_length=30, blank=True)
    cruzamento_detalhe = models.TextField(blank=True)
    cruzado_em = models.DateTimeField(null=True, blank=True)
    cruzamento_base = models.CharField(max_length=20, blank=True, help_text="SINCRONIZACAO ou CONSULTA_DIRETA")
    prioridade = models.CharField(max_length=20, choices=Prioridade.choices, default=Prioridade.INVESTIGAR)
    prioridade_criterios = models.JSONField(default=list, blank=True)
    prioridade_manual = models.BooleanField(default=False)
    prioridade_motivo_manual = models.CharField(max_length=255, blank=True)
    qualidade_dados = models.CharField(max_length=12, choices=Qualidade.choices, default=Qualidade.INSUFICIENTE)
    pendencias_dados = models.JSONField(default=list, blank=True)
    resumo = models.TextField(blank=True)
    perguntas = models.JSONField(default=list, blank=True)
    alertas = models.JSONField(default=list, blank=True, help_text="Ex.: fora do critério de tempo, mesma raiz de cliente")
    resumo_ia = models.TextField(blank=True, help_text="Somente quando a IA foi efetivamente executada")
    perguntas_ia = models.JSONField(default=list, blank=True)
    ia_modelo = models.CharField(max_length=60, blank=True)
    ia_em = models.DateTimeField(null=True, blank=True)
    pesquisas = models.ManyToManyField(Pesquisa, related_name="oportunidades", blank=True)
    responsavel = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="oportunidades")
    atribuida_em = models.DateTimeField(null=True, blank=True)
    criada_em = models.DateTimeField(auto_now_add=True)
    atualizada_em = models.DateTimeField(auto_now=True)
    liberada_em = models.DateTimeField(null=True, blank=True)
    liberada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    motivo_descarte = models.CharField(max_length=255, blank=True)
    escopo = models.CharField(max_length=16, blank=True, db_index=True, choices=[
        ("FROTA", "Frota própria"), ("INDUSTRIA", "Indústria com máquinas"),
        ("AGRO_MAQUINAS", "Agro, construção e máquinas pesadas"), ("REVENDA_OFICINA", "Revenda e oficinas"),
        ("REVISAR", "Revisar (possível concorrente / evidência)"), ("FORA", "Fora do escopo")],
        help_text="Escopo comercial (services/escopo.py). FORA não é apagada: sai da lista padrão e não recebe gasto")
    escopo_motivo = models.CharField(max_length=255, blank=True)
    escopo_humano = models.BooleanField(default=False, help_text="Escopo definido pelo gestor (não é recalculado)")
    em_atendimento_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Quem está atendendo agora (trava: outro atendente não registra contato enquanto vale)")
    em_atendimento_desde = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-criada_em"]

    def __str__(self):
        return f"Oportunidade {self.empresa.nome}"


class PlanoPesquisa(models.Model):
    """Plano estruturado (versionado) que orienta a seleção. Origem: IA, modelo das frentes ou edição do gestor."""

    class Origem(models.TextChoices):
        IA = "IA", "Proposto pela IA"
        FRENTES = "FRENTES", "Montado a partir das frentes (regras, sem IA)"
        MANUAL = "MANUAL", "Ajustado pelo gestor"

    pesquisa = models.ForeignKey(Pesquisa, on_delete=models.CASCADE, related_name="planos")
    versao = models.PositiveIntegerField()
    origem = models.CharField(max_length=8, choices=Origem.choices)
    conteudo = models.JSONField(help_text="interpretacao, por_frente{...}, consultas_externas, ambiguidades, ...")
    cnaes_rejeitados = models.JSONField(default=list, blank=True, help_text="Códigos propostos que não existem na tabela")
    uso_ia = models.ForeignKey("UsoIA", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    criado_em = models.DateTimeField(auto_now_add=True)
    observacao = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-versao"]
        constraints = [models.UniqueConstraint(fields=["pesquisa", "versao"], name="plano_versao_unica")]

    def __str__(self):
        return f"Plano v{self.versao} ({self.get_origem_display()})"


class SelecaoPesquisa(models.Model):
    """Lista comercial de UMA pesquisa: cada empresa considerada, por frente, com motivos e fila."""

    class Aderencia(models.TextChoices):
        SUSTENTADA = "SUSTENTADA", "Aderência sustentada por evidências"
        PROVAVEL = "PROVAVEL", "Aderência provável pela atividade (a confirmar)"
        INSUFICIENTE = "INSUFICIENTE", "Informação insuficiente"
        FORA = "FORA", "Fora do objetivo desta pesquisa"

    class Fila(models.TextChoices):
        SELECIONADA = "SELECIONADA", "Com contato encontrado (fonte da empresa)"
        CONTATO_CADASTRAL = "CONTATO_CADASTRAL", "Com contato encontrado (cadastro ou diretório, a conferir)"
        BUSCAR_CONTATO = "BUSCAR_CONTATO", "Sem contato direto encontrado"
        INVESTIGAR = "INVESTIGAR", "Investigar operação"
        REVISAR_IDENTIDADE = "REVISAR_IDENTIDADE", "Revisar identidade / situação / conflito"
        FORA = "FORA", "Fora do objetivo desta pesquisa"
        CONHECIDA = "CONHECIDA", "Conhecida no Salesforce"
        JA_PESQUISADA = "JA_PESQUISADA", "Já pesquisada (regra antiga; recalculada)"
        RETORNO = "RETORNO", "Contato efetivo – retorno agendado"
        JA_ABORDADA = "JA_ABORDADA", "Contato efetivo – em acompanhamento"
        NAO_CONTATAR = "NAO_CONTATAR", "Não contatar / bloqueada"
        AGUARDANDO = "AGUARDANDO", "Considerada, aguardando investigação"
        LOCALIZACAO_PENDENTE = "LOCALIZACAO_PENDE", "Localização pendente (município não comprovado pela fonte)"

    pesquisa = models.ForeignKey(Pesquisa, on_delete=models.CASCADE, related_name="selecoes")
    frente = models.ForeignKey(FrenteComercial, null=True, blank=True, on_delete=models.SET_NULL, related_name="selecoes")
    cnpj = models.CharField(max_length=14)
    empresa = models.ForeignKey(Empresa, null=True, blank=True, on_delete=models.PROTECT, related_name="selecoes")
    plano = models.ForeignKey(PlanoPesquisa, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    pontuacao = models.FloatField(default=0)
    motivos_selecao = models.JSONField(default=list, blank=True)
    aderencia = models.CharField(max_length=12, choices=Aderencia.choices, default=Aderencia.INSUFICIENTE)
    aderencia_motivos = models.JSONField(default=list, blank=True)
    fila = models.CharField(max_length=20, choices=Fila.choices, default=Fila.AGUARDANDO)
    fila_motivos = models.JSONField(default=list, blank=True)
    investigada_em = models.DateTimeField(null=True, blank=True)
    decisao_humana = models.CharField(max_length=10, blank=True, help_text="INCLUIR / RETIRAR / CORRIGIR")
    aderencia_humana = models.CharField(max_length=12, choices=Aderencia.choices, blank=True)
    decisao_motivo = models.CharField(max_length=255, blank=True)
    decisao_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    decisao_em = models.DateTimeField(null=True, blank=True)
    primeira_execucao = models.ForeignKey(Execucao, null=True, on_delete=models.SET_NULL, related_name="+")
    ultima_execucao = models.ForeignKey(Execucao, null=True, on_delete=models.SET_NULL, related_name="+")
    criada_em = models.DateTimeField(auto_now_add=True)
    atualizada_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-pontuacao"]
        constraints = [models.UniqueConstraint(fields=["pesquisa", "frente", "cnpj"], name="selecao_unica")]
        indexes = [models.Index(fields=["pesquisa", "fila"])]

    @property
    def aderencia_efetiva(self):
        return self.aderencia_humana or self.aderencia


class ConsultaWeb(models.Model):
    """Registro de cada consulta de busca web (descoberta ou complementação)."""

    pesquisa = models.ForeignKey(Pesquisa, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    execucao = models.ForeignKey(Execucao, null=True, blank=True, on_delete=models.SET_NULL, related_name="consultas_web")
    empresa = models.ForeignKey(Empresa, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    finalidade = models.CharField(max_length=20, help_text="DESCOBERTA / CONTATO")
    provedor = models.CharField(max_length=30)
    consulta = models.CharField(max_length=300)
    status = models.CharField(max_length=20, help_text="OK / FALHA / NAO_CONFIGURADA / SEM_ORCAMENTO")
    resultados = models.JSONField(default=list, blank=True, help_text="[{url, titulo, dominio, descartado?}]")
    erro = models.TextField(blank=True)
    custo_estimado_usd = models.DecimalField(max_digits=8, decimal_places=5, default=0,
                                             help_text="Custo a partir do consumo MEDIDO (ou máximo de 1 tentativa se não medido)")
    custo_reservado_usd = models.DecimalField(max_digits=8, decimal_places=5, default=0,
                                              help_text="Custo máximo reservado antes da chamada")
    modelo = models.CharField(max_length=60, blank=True)
    tokens_entrada = models.PositiveIntegerField(default=0)
    tokens_saida = models.PositiveIntegerField(default=0)
    chamadas_ferramenta = models.PositiveIntegerField(default=0, help_text="Ações de pesquisa web cobradas")
    fontes = models.JSONField(default=list, blank=True,
                              help_text="Lista COMPLETA de URLs consultadas (web_search_call.action.sources + citações)")
    acoes = models.JSONField(default=list, blank=True,
                             help_text="Ações executadas pela ferramenta: [{tipo, consulta/url, fontes}]")
    analise = models.JSONField(default=dict, blank=True,
                               help_text="Análise do retorno pela IA: {faltando, proximas_consultas, justificativa, "
                                         "motivo (por que ESTA consulta foi feita)}")
    duracao_ms = models.PositiveIntegerField(default=0)
    criada_em = models.DateTimeField(auto_now_add=True, help_text="Data da coleta")

    class Meta:
        ordering = ["-criada_em"]


class ReservaCusto(models.Model):
    """Custo MÁXIMO reservado por uma chamada paga em andamento. Fica no banco para que execuções concorrentes
    (worker, comando, servidor) enxerguem as reservas umas das outras; é apagada depois que o custo medido é gravado.
    Reservas antigas (processo interrompido) deixam de contar após orcamento.VALIDADE_RESERVA."""

    pesquisa = models.ForeignKey(Pesquisa, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    tipo = models.CharField(max_length=20)
    valor_usd = models.DecimalField(max_digits=8, decimal_places=5)
    criada_em = models.DateTimeField(auto_now_add=True)


class UsoIA(models.Model):
    """Rastreio de cada chamada de IA (ou tentativa)."""

    tipo = models.CharField(max_length=20, help_text="PLANO / INVESTIGACAO")
    provedor = models.CharField(max_length=30)
    modelo = models.CharField(max_length=60, blank=True)
    versao_prompt = models.CharField(max_length=20)
    pesquisa = models.ForeignKey(Pesquisa, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    plano = models.ForeignKey(PlanoPesquisa, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    empresa = models.ForeignKey(Empresa, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    materiais = models.JSONField(default=list, blank=True, help_text="URLs/trechos enviados (referências)")
    resultado = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, help_text="OK / FALHA / RECUSA / NAO_CONFIGURADA")
    erro = models.TextField(blank=True)
    tokens_entrada = models.PositiveIntegerField(default=0)
    tokens_saida = models.PositiveIntegerField(default=0)
    duracao_ms = models.PositiveIntegerField(default=0)
    custo_estimado_usd = models.DecimalField(max_digits=10, decimal_places=5, default=0)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em"]
        verbose_name = "uso de IA"
        verbose_name_plural = "usos de IA"


class RetomadaFonte(models.Model):
    """Consulta adiada por limite de acesso (HTTP 429). O worker executa quando disponivel_em chega."""

    fonte = models.CharField(max_length=40, db_index=True)
    chave = models.CharField(max_length=80)
    empresa = models.ForeignKey("carteira.Empresa", null=True, blank=True, on_delete=models.CASCADE,
                                related_name="retomadas_fonte")
    disponivel_em = models.DateTimeField(db_index=True)
    tentativas = models.PositiveSmallIntegerField(default=1)
    ultimo_erro = models.CharField(max_length=300, blank=True)
    concluida_em = models.DateTimeField(null=True, blank=True)
    criada_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["disponivel_em"]


class DecisaoRevisao(models.Model):
    """Registro imutável das decisões humanas (vínculo, descarte, liberação, atribuição, prioridade)."""

    tipo = models.CharField(max_length=40)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    criada_em = models.DateTimeField(auto_now_add=True)
    candidato = models.ForeignKey(Candidato, null=True, blank=True, on_delete=models.PROTECT, related_name="decisoes")
    oportunidade = models.ForeignKey(Oportunidade, null=True, blank=True, on_delete=models.PROTECT, related_name="decisoes")
    motivo = models.TextField(blank=True)
    dados = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-criada_em"]
