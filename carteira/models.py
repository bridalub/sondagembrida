from datetime import date

from django.conf import settings
from django.db import models
from django.utils import timezone

# ============================================================
# Enumerações


class StatusCNPJ(models.TextChoices):
    VALIDO = "VALIDO", "Válido"
    AUSENTE = "AUSENTE", "Ausente"
    INVALIDO = "INVALIDO", "Inválido"
    CPF = "CPF", "Parece CPF (pessoa física)"


class SituacaoCompra(models.TextChoices):
    COMPROU = "COMPROU", "Compra comprovada"
    SEM_COMPRA_PERIODO = "SEM_COMPRA_PERIODO", "Sem compra encontrada no período"
    NUNCA_COMPROU = "NUNCA_COMPROU", "Nunca comprou (cobertura completa)"
    INDISPONIVEL = "INDISPONIVEL", "Histórico insuficiente ou indisponível"


class SituacaoContato(models.TextChoices):
    NUNCA_ABORDADA = "NUNCA_ABORDADA", "Nunca abordada"
    TENTATIVA_SEM_RESPOSTA = "TENTATIVA_SEM_RESPOSTA", "Só tentativas, sem conversa"
    CONVERSA_REALIZADA = "CONVERSA_REALIZADA", "Conversa realizada"
    RETORNO_AGENDADO = "RETORNO_AGENDADO", "Retorno agendado"
    NAO_CONTATAR = "NAO_CONTATAR", "Não contatar"


class CanalAbordagem(models.TextChoices):
    TELEFONE = "TELEFONE", "Telefone"
    WHATSAPP = "WHATSAPP", "WhatsApp"
    EMAIL = "EMAIL", "E-mail"
    PRESENCIAL = "PRESENCIAL", "Visita presencial"
    OUTRO = "OUTRO", "Outro"


class ResultadoAbordagem(models.TextChoices):
    # Tentativas (não houve conversa)
    SEM_RESPOSTA = "SEM_RESPOSTA", "Tentativa – sem resposta"
    RECADO = "RECADO", "Tentativa – recado / caixa postal"
    CONTATO_INVALIDO = "CONTATO_INVALIDO", "Tentativa – contato inválido / não localizado"
    # Conversas efetivas
    CONVERSA_INTERESSE = "CONVERSA_INTERESSE", "Conversa – com interesse"
    CONVERSA_RETORNAR = "CONVERSA_RETORNAR", "Conversa – pediu retorno"
    CONVERSA_SEM_INTERESSE = "CONVERSA_SEM_INTERESSE", "Conversa – sem interesse"
    CONVERSA_NAO_CONTATAR = "CONVERSA_NAO_CONTATAR", "Conversa – pediu para não ser contatado"
    CONVERSA_ORCAMENTO = "CONVERSA_ORCAMENTO", "Conversa – orçamento enviado"
    CONVERSA_VENDA = "CONVERSA_VENDA", "Conversa – venda fechada"


RESULTADOS_COM_CONVERSA = {
    ResultadoAbordagem.CONVERSA_INTERESSE,
    ResultadoAbordagem.CONVERSA_RETORNAR,
    ResultadoAbordagem.CONVERSA_SEM_INTERESSE,
    ResultadoAbordagem.CONVERSA_NAO_CONTATAR,
    ResultadoAbordagem.CONVERSA_ORCAMENTO,
    ResultadoAbordagem.CONVERSA_VENDA,
}


# ============================================================
# Configuração


class ConfiguracaoAnalise(models.Model):
    """Parâmetros de análise editáveis no admin (registro único, pk=1)."""

    dias_inatividade = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Dias sem compra para considerar um comprador inativo. Vazio = não classificar inatividade.",
    )
    dias_sem_compra_captacao = models.PositiveIntegerField(
        default=90,
        help_text="Captação: cliente que já existe no Salesforce só conta como venda da prospecção se nunca comprou "
                  "ou se está há mais que estes dias sem comprar (padrão 90 dias). Cliente ativo fica com a carteira.",
    )
    nunca_comprou_habilitado = models.BooleanField(
        default=False,
        help_text=(
            "Permite classificar 'Nunca comprou' quando a cobertura de notas começa antes do cadastro "
            "da conta no Salesforce. Só ative após confirmar que as notas anteriores ao cadastro "
            "no Salesforce estão integradas (contas migradas do Protheus podem ter compras antigas)."
        ),
    )
    # --- Regras de abordagem definidas pelo gestor (vazio = sem restrição)
    dias_minimos_nova_tentativa = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Intervalo mínimo (dias) entre tentativas na mesma empresa. Gestores (staff) podem justificar exceção.",
    )
    max_tentativas_sem_resposta = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Após N tentativas sem conversa, novas tentativas exigem exceção de gestor.",
    )
    # --- Cruzamento com a carteira
    horas_validade_cruzamento = models.PositiveIntegerField(
        default=24,
        help_text="Idade máxima da última sincronização de clientes para liberar/exportar oportunidade sem "
                  "nova conferência direta no Salesforce.",
    )
    # --- Execução recorrente das pesquisas (desligada até o gestor ativar)
    dias_validade_situacao_cadastral = models.PositiveIntegerField(
        default=45,
        help_text="Idade máxima da situação cadastral para liberar abordagem. Mais antiga: consulta a fonte integrada "
                  "(BrasilAPI); se falhar, a liberação fica pendente (nunca presume ATIVA).",
    )
    agendamento_ativo = models.BooleanField(
        default=False,
        help_text="Quando ligado, o worker cria execuções para pesquisas ativas com frequência definida.",
    )

    class Meta:
        verbose_name = "configuração de análise"
        verbose_name_plural = "configuração de análise"

    def __str__(self):
        return "Configuração de análise"

    @classmethod
    def obter(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


# ============================================================
# Importações


class Importacao(models.Model):
    class Tipo(models.TextChoices):
        CLIENTES = "CLIENTES", "Clientes (Account)"
        COMPRAS = "COMPRAS", "Compras (notas fiscais)"
        CONTATOS = "CONTATOS", "Contatos (Contact)"
        LEADS = "LEADS", "Leads BRIDA (Lead)"

    class Status(models.TextChoices):
        EXECUTANDO = "EXECUTANDO", "Executando"
        SUCESSO = "SUCESSO", "Sucesso"
        FALHA = "FALHA", "Falha"

    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    origem = models.CharField(max_length=200, help_text="Sistema/instância de origem")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.EXECUTANDO)
    iniciado_em = models.DateTimeField(default=timezone.now)
    finalizado_em = models.DateTimeField(null=True, blank=True)
    executado_por = models.CharField(max_length=150, blank=True)
    parametros = models.JSONField(default=dict, blank=True)
    lidos = models.PositiveIntegerField(default=0)
    criados = models.PositiveIntegerField(default=0)
    atualizados = models.PositiveIntegerField(default=0)
    inalterados = models.PositiveIntegerField(default=0)
    rejeitados = models.PositiveIntegerField(default=0)
    inconsistentes = models.PositiveIntegerField(default=0)
    detalhes = models.JSONField(default=dict, blank=True)
    mensagem = models.TextField(blank=True)

    class Meta:
        ordering = ["-iniciado_em"]
        verbose_name = "importação"
        verbose_name_plural = "importações"

    def __str__(self):
        return f"{self.get_tipo_display()} em {timezone.localtime(self.iniciado_em):%d/%m/%Y %H:%M}"


class OcorrenciaImportacao(models.Model):
    class Tipo(models.TextChoices):
        REJEITADO = "REJEITADO", "Rejeitado (não importado)"
        INCONSISTENTE = "INCONSISTENTE", "Inconsistente (importado, precisa revisão)"
        AVISO = "AVISO", "Aviso"

    importacao = models.ForeignKey(Importacao, on_delete=models.CASCADE, related_name="ocorrencias")
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    id_origem = models.CharField(max_length=40, blank=True)
    cnpj_origem = models.CharField(max_length=40, blank=True)
    nome = models.CharField(max_length=255, blank=True)
    mensagem = models.TextField()
    empresa = models.ForeignKey("Empresa", null=True, blank=True, on_delete=models.SET_NULL, related_name="ocorrencias")

    class Meta:
        ordering = ["id"]
        verbose_name = "ocorrência de importação"
        verbose_name_plural = "ocorrências de importação"


# ============================================================
# Empresas


class Empresa(models.Model):
    class Origem(models.TextChoices):
        SALESFORCE = "SALESFORCE", "Salesforce"
        MANUAL = "MANUAL", "Manual"
        DESCOBERTA = "DESCOBERTA", "Descoberta externa"

    # Identificação. PK técnica = id (BigAutoField). CNPJ = chave de negócio.
    cnpj = models.CharField(
        max_length=14, unique=True, null=True, blank=True,
        help_text="CNPJ normalizado (14 caracteres, aceita alfanumérico). Vazio se ausente/inválido.",
    )
    cnpj_origem = models.CharField(max_length=40, blank=True, help_text="Valor bruto recebido da origem")
    cnpj_status = models.CharField(max_length=10, choices=StatusCNPJ.choices, default=StatusCNPJ.AUSENTE)
    salesforce_id = models.CharField(
        max_length=18, unique=True, null=True, blank=True,
        help_text="Account.Id principal (fonte dos dados cadastrais)",
    )
    origem = models.CharField(max_length=20, choices=Origem.choices, default=Origem.SALESFORCE)

    # Dados cadastrais vindos de Account
    nome = models.CharField("nome da conta", max_length=255, help_text="Account.Name")
    codigo_cliente = models.CharField("código do cliente", max_length=40, blank=True, help_text="Account.Code__c")
    id_customizado = models.CharField(max_length=60, blank=True, help_text="Account.CustomizedID__c")
    segmento = models.CharField(max_length=120, blank=True, help_text="Account.Segment__c")
    subsegmento = models.CharField(max_length=120, blank=True, help_text="Account.Subsegment__c")
    ramo = models.CharField(max_length=120, blank=True, help_text="Account.Metier__c")
    imbp = models.CharField("IMBP", max_length=120, blank=True, help_text="Account.IMBP__c")
    classificacao_consumo = models.CharField(
        "classificação consumo/indústria", max_length=120, blank=True, help_text="Account.ConsumptionClassification__c"
    )
    granel = models.BooleanField(null=True, blank=True, help_text="Account.IsGrainy__c")
    cidade = models.CharField(max_length=120, blank=True, help_text="Account.BillingCity__c")
    uf = models.CharField("UF", max_length=40, blank=True, help_text="Account.BillingState__c")
    cep = models.CharField("CEP", max_length=20, blank=True, help_text="Account.BillingPostalCode")
    cidade_entrega = models.CharField(max_length=120, blank=True, help_text="Account.ShippingCity__c")
    uf_entrega = models.CharField("UF de entrega", max_length=40, blank=True, help_text="Account.ShippingState__c")
    regiao = models.CharField("região", max_length=120, blank=True, help_text="Account.Zone__r.Name")
    segmento_normalizado = models.CharField(
        max_length=120, blank=True, db_index=True,
        help_text="Segmento padronizado pelo código (ex.: 'CONSTRUCAO' -> '602-CONSTRUCAO')",
    )
    ultima_compra_cadastro = models.DateField(
        "última compra (cadastro)", null=True, blank=True,
        help_text="Account.LastBuyDate__c – evidência SECUNDÁRIA de compra (pode estar desatualizada)",
    )
    vendedor = models.CharField(max_length=200, blank=True, help_text="Account.Seller__r.Name")
    coordenador = models.CharField(max_length=200, blank=True, help_text="Account.CoordinatorFormula__c")
    tipo_vendedor = models.CharField(max_length=60, blank=True, help_text="Account.Seller__r.Type__c (Varejo, Consumo / Industria)")
    coordenador_vendedor = models.CharField(
        max_length=200, blank=True,
        help_text="Account.Seller__r.UserCoordinatorSync__r.Name – coordenador do vendedor (pode diferir do dono da região)")
    situacao_protheus = models.CharField(max_length=60, blank=True, help_text="Account.ProtheusCustomerSituation__c")
    distribuidor = models.CharField(max_length=200, blank=True, help_text="Account.Distributor__r.Name")
    data_cadastro_origem = models.DateTimeField(
        "data de cadastro no Salesforce", null=True, blank=True,
        help_text="Account.CreatedDate – NÃO é data de abertura da empresa nem início como cliente",
    )

    # Campos desejados que a automação NÃO traz. Só são preenchidos por campo
    # opcional confirmado via describe (SF_ACCOUNT_CAMPOS_EXTRAS) ou manualmente.
    razao_social = models.CharField(max_length=255, blank=True)
    nome_fantasia = models.CharField(max_length=255, blank=True)
    cnae = models.CharField("CNAE", max_length=20, blank=True)
    data_abertura = models.DateField(null=True, blank=True, help_text="Data de abertura da empresa (CNPJ)")
    telefone = models.CharField(max_length=60, blank=True)
    email = models.EmailField("e-mail", max_length=254, blank=True)
    email_classificacao = models.CharField(max_length=20, blank=True, help_text="COMERCIAL/FINANCEIRO/FISCAL/INCERTO")

    # Dados de identificação externa (Receita via BrasilAPI, site etc.). Fonte/data em DadoColetado.
    atividade_principal = models.CharField(max_length=255, blank=True, help_text="Descrição do CNAE principal")
    cnaes_secundarios = models.JSONField(default=list, blank=True)
    situacao_cadastral = models.CharField(max_length=60, blank=True, help_text="Situação na Receita (ATIVA, BAIXADA...)")
    situacao_cadastral_data = models.DateField(null=True, blank=True)
    motivo_situacao_cadastral = models.CharField(max_length=200, blank=True)
    situacao_cadastral_referencia = models.DateField(
        null=True, blank=True,
        help_text="Data a que a situação cadastral se refere (competência da base ou data da consulta)")
    situacao_cadastral_fonte = models.CharField(max_length=120, blank=True)
    natureza_juridica = models.CharField(max_length=120, blank=True)
    porte = models.CharField(max_length=60, blank=True)
    endereco = models.CharField(max_length=255, blank=True)
    site = models.URLField(max_length=300, blank=True)
    dominio = models.CharField(max_length=200, blank=True, db_index=True)

    # Revisão / qualidade
    requer_revisao = models.BooleanField(default=False)
    motivo_revisao = models.TextField(blank=True)
    conflito_origem = models.TextField(blank=True, help_text="Último conflito detectado na importação")

    # Bloqueio de contato
    nao_contatar = models.BooleanField(default=False)
    nao_contatar_motivo = models.TextField(blank=True)
    nao_contatar_em = models.DateTimeField(null=True, blank=True)
    nao_contatar_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # Rastreabilidade da atualização
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)
    ultima_importacao = models.ForeignKey(
        Importacao, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    ultima_sincronizacao_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["nome"]
        indexes = [
            models.Index(fields=["segmento"]),
            models.Index(fields=["uf", "cidade"]),
            models.Index(fields=["codigo_cliente"]),
        ]

    def __str__(self):
        return f"{self.nome} ({self.cnpj_formatado or 'sem CNPJ'})"

    @property
    def cnpj_formatado(self):
        c = self.cnpj
        if not c or len(c) != 14:
            return ""
        return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"

    @property
    def raiz_cnpj(self):
        return self.cnpj[:8] if self.cnpj else ""

    @property
    def tipo_estabelecimento(self):
        if not self.cnpj:
            return ""
        return "Matriz" if self.cnpj[8:12] == "0001" else "Filial"

    @property
    def bloqueada_origem(self):
        """Bloqueio explícito vindo do Protheus/Salesforce (ProtheusCustomerSituation__c = 'Bloqueado')."""
        return self.situacao_protheus.strip().lower() == "bloqueado"

    @property
    def tempo_empresa_anos(self):
        """Tempo de empresa pela DATA DE ABERTURA (não pelo cadastro no Salesforce)."""
        if not self.data_abertura:
            return None
        hoje = date.today()
        anos = hoje.year - self.data_abertura.year
        if (hoje.month, hoje.day) < (self.data_abertura.month, self.data_abertura.day):
            anos -= 1
        return anos


class RegistroOrigem(models.Model):
    """
    Cada registro de origem (ex.: uma Account do Salesforce) vinculado a uma empresa.
    Uma empresa pode ter mais de uma Account quando o CNPJ está duplicado na origem.
    """

    sistema = models.CharField(max_length=40, default="SALESFORCE")
    objeto = models.CharField(max_length=60, default="Account")
    id_origem = models.CharField(max_length=40)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="registros_origem")
    principal = models.BooleanField(default=False)
    dados = models.JSONField(default=dict, blank=True, help_text="Último registro bruto recebido")
    primeiro_visto_em = models.DateTimeField(auto_now_add=True)
    ultimo_visto_em = models.DateTimeField(default=timezone.now)
    ultima_importacao = models.ForeignKey(Importacao, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["sistema", "objeto", "id_origem"], name="registro_origem_unico"),
        ]
        verbose_name = "registro de origem"
        verbose_name_plural = "registros de origem"

    def __str__(self):
        return f"{self.sistema}/{self.objeto}/{self.id_origem}"


class Contato(models.Model):
    """Pessoa de contato. Várias por empresa; o CNPJ NÃO identifica a pessoa."""

    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="contatos")
    nome = models.CharField(max_length=200)
    cargo = models.CharField(max_length=120, blank=True)
    cargo_confirmado = models.BooleanField(default=False, help_text="Cargo informado pela fonte ou confirmado em conversa")
    departamento = models.CharField(max_length=60, blank=True, help_text="Ex.: Compras, Manutenção, Frota, Financeiro")
    papel = models.CharField(
        max_length=120, blank=True,
        help_text="Ex.: 'Sócio-administrador (QSA Receita)'. Sócio NÃO é automaticamente comprador.",
    )
    telefone = models.CharField(max_length=60, blank=True)
    celular = models.CharField(max_length=60, blank=True)
    email = models.EmailField("e-mail", blank=True)
    observacoes = models.TextField(blank=True)
    ativo = models.BooleanField(default=True)
    origem = models.CharField(max_length=40, default="MANUAL")
    id_origem = models.CharField(max_length=40, blank=True, help_text="Ex.: Contact.Id")
    fonte_referencia = models.CharField(max_length=300, blank=True)
    coletado_em = models.DateTimeField(null=True, blank=True)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["nome"]
        constraints = [
            models.UniqueConstraint(fields=["origem", "id_origem"], condition=~models.Q(id_origem=""),
                                    name="contato_origem_unico"),
        ]

    def __str__(self):
        return f"{self.nome} – {self.empresa.nome}"


class CanalEmpresa(models.Model):
    """Telefones, e-mails e sites encontrados em fontes externas, com classificação e fonte."""

    class Tipo(models.TextChoices):
        TELEFONE = "TELEFONE", "Telefone"
        EMAIL = "EMAIL", "E-mail"
        SITE = "SITE", "Site"

    class Classificacao(models.TextChoices):  # FINALIDADE do canal
        COMPRAS = "COMPRAS", "Compras"
        MANUTENCAO = "MANUTENCAO", "Manutenção"
        COMERCIAL = "COMERCIAL", "Comercial / vendas"
        ATENDIMENTO = "ATENDIMENTO", "Atendimento / SAC"
        FINANCEIRO = "FINANCEIRO", "Financeiro"
        FISCAL = "FISCAL", "Fiscal / NF"
        CONTADOR = "CONTADOR", "Contabilidade / contador"
        INCERTO = "INCERTO", "Desconhecida"

    class Origem(models.TextChoices):
        CADASTRAL = "CADASTRAL", "Cadastral (Receita/Salesforce)"
        PUBLICACAO = "PUBLICACAO", "Publicado em fonte externa (site, mapa, busca)"
        HUMANO = "HUMANO", "Informado pela equipe"

    class Confianca(models.TextChoices):
        ALTA = "ALTA", "Alta – fonte da própria empresa confirmada pelo CNPJ"
        MEDIA = "MEDIA", "Média – fonte provável da empresa"
        BAIXA = "BAIXA", "Baixa – associação fraca/cadastral"
        CONFLITO = "CONFLITO", "Conflito – pode ser de terceiro/outro estabelecimento"

    class AplicaA(models.TextChoices):
        ESTABELECIMENTO = "ESTABELECIMENTO", "Este estabelecimento"
        EMPRESA = "EMPRESA", "Empresa (sem distinção de unidade)"
        MATRIZ = "MATRIZ", "Matriz (outro estabelecimento da mesma empresa)"
        GRUPO = "GRUPO", "Central do grupo"
        CONTADOR = "CONTADOR", "Contador / terceiro"
        DESCONHECIDO = "DESCONHECIDO", "Desconhecido"

    class Confirmacao(models.TextChoices):
        NAO_CONFIRMADO = "NAO_CONFIRMADO", "Não confirmado em contato"
        CONFERIDO = "CONFERIDO", "Conferido pela equipe (revisão de fonte, sem contato)"
        ATENDIDO = "ATENDIDO", "Confirmado: atendeu / respondeu"
        INCORRETO = "INCORRETO", "Incorreto / não pertence à empresa"

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="canais")
    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    valor = models.CharField(max_length=300)
    class Verificacao(models.TextChoices):
        CADASTRAL = "CADASTRAL", "Cadastral (Receita/Salesforce) – não validado"
        ENCONTRADO = "ENCONTRADO", "Encontrado em fonte externa – não confirmado"
        CONFIRMADO_FONTE = "CONFIRMADO_FONTE", "Confirmado: site publica o mesmo CNPJ"
        CONFIRMADO_HUMANO = "CONFIRMADO_HUMANO", "Confirmado em contato"
        CONFLITO = "CONFLITO", "Conflito (outro CNPJ/estabelecimento)"

    classificacao = models.CharField(max_length=12, choices=Classificacao.choices, default=Classificacao.INCERTO)
    verificacao = models.CharField(max_length=20, choices=Verificacao.choices, default=Verificacao.ENCONTRADO,
                                   help_text="Legado (resumo); as dimensões abaixo são as oficiais")
    valor_normalizado = models.CharField(max_length=300, blank=True)
    origem_canal = models.CharField(max_length=12, choices=Origem.choices, default=Origem.PUBLICACAO)
    confianca_associacao = models.CharField(max_length=10, choices=Confianca.choices, default=Confianca.BAIXA)
    evidencia_associacao = models.CharField(max_length=500, blank=True)
    aplica_a = models.CharField(max_length=16, choices=AplicaA.choices, default=AplicaA.DESCONHECIDO)
    departamento = models.CharField(max_length=120, blank=True, help_text="Só quando rotulado explicitamente na fonte")
    compartilhado = models.CharField(max_length=200, blank=True,
                                     help_text="Ex.: usado por N empresas de grupos diferentes (provável contador)")
    whatsapp_explicito = models.BooleanField(default=False, help_text="Link wa.me/api.whatsapp publicado pela fonte")
    verificacao_tecnica = models.JSONField(default=dict, blank=True, help_text="Formato, domínio/MX, DDD...")
    conferido_em = models.DateTimeField(null=True, blank=True)
    confirmacao_humana = models.CharField(max_length=16, choices=Confirmacao.choices, default=Confirmacao.NAO_CONFIRMADO)
    confirmado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="+")
    confirmado_em = models.DateTimeField(null=True, blank=True)
    restrito = models.BooleanField(default=False, help_text="Restrição de contato neste canal")
    restricao_motivo = models.CharField(max_length=255, blank=True)
    preferido = models.BooleanField(default=False, help_text="Preferência definida pela equipe")
    fonte = models.CharField(max_length=60)
    referencia = models.CharField(max_length=500, blank=True)
    primeira_coleta_em = models.DateTimeField(default=timezone.now)
    coletado_em = models.DateTimeField(default=timezone.now)
    corroboracoes = models.JSONField(default=list, blank=True,
                                     help_text="Outras fontes (domínios diferentes) onde o mesmo canal apareceu")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["empresa", "tipo", "valor"], name="canal_empresa_unico")]
        ordering = ["tipo", "valor"]

    NIVEIS = {"CONFIRMADO_EQUIPE": "Confirmado pela equipe", "CONFERIDO_EQUIPE": "Conferido pela equipe (sem contato)", "INCORRETO": "Incorreto (marcado pela equipe)",
              "INFORMADO_EQUIPE": "Informado pela equipe", "CORROBORADO": "Corroborado por outra fonte associada",
              "PUBLICADO": "Publicado em fonte associada à empresa", "CADASTRAL": "Cadastro – a conferir",
              "PISTA": "Diretório/busca – a conferir"}

    @property
    def corroborado(self):
        """Apareceu também em outra fonte (domínio diferente) associada à empresa com confiança MÉDIA/ALTA.
        Diretórios (BAIXA) copiam a Receita com frequência e NÃO corroboram."""
        return any(c.get("confianca") in ("ALTA", "MEDIA") for c in (self.corroboracoes or []))

    @property
    def nivel(self):
        if self.confirmacao_humana == self.Confirmacao.ATENDIDO:
            return "CONFIRMADO_EQUIPE"
        if self.confirmacao_humana == self.Confirmacao.INCORRETO:
            return "INCORRETO"
        if self.confirmacao_humana == self.Confirmacao.CONFERIDO:
            return "CONFERIDO_EQUIPE"
        if self.origem_canal == self.Origem.HUMANO:
            return "INFORMADO_EQUIPE"
        if self.corroborado:
            return "CORROBORADO"
        if self.origem_canal == self.Origem.CADASTRAL:
            return "CADASTRAL"
        if self.confianca_associacao in (self.Confianca.BAIXA, self.Confianca.CONFLITO):
            return "PISTA"
        return "PUBLICADO"

    @property
    def nivel_display(self):
        return self.NIVEIS[self.nivel]


class DadoColetado(models.Model):
    """Fonte e data de cada informação coletada de fontes externas (último valor por campo/fonte)."""

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="dados_coletados")
    campo = models.CharField(max_length=60)
    valor = models.TextField(blank=True)
    fonte = models.CharField(max_length=60)
    referencia = models.CharField(max_length=500, blank=True)
    primeira_coleta_em = models.DateTimeField(default=timezone.now)
    coletado_em = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["empresa", "campo", "fonte"], name="dado_coletado_unico")]
        ordering = ["campo", "fonte"]


class CnpjAssociado(models.Model):
    """Outros CNPJs ligados à mesma empresa (encontrado na pesquisa, confirmado pelo atendimento, outra unidade).
    O principal continua em Empresa.cnpj (o confirmado pelo atendimento, que liga às notas fiscais e ao Salesforce).
    Os associados servem para RECONHECER a empresa quando o CNPJ aparecer de novo numa pesquisa; CNPJ só encontrado
    (não confirmado) não conta para vendas."""

    class Origem(models.TextChoices):
        PESQUISA = "PESQUISA", "Encontrado na pesquisa"
        ATENDIMENTO = "ATENDIMENTO", "Confirmado pelo atendimento"

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="cnpjs_associados")
    cnpj = models.CharField(max_length=14, db_index=True)
    origem = models.CharField(max_length=12, choices=Origem.choices)
    observacao = models.CharField(max_length=300, blank=True)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["empresa", "cnpj"], name="cnpj_associado_unico")]
        ordering = ["criado_em"]
        verbose_name = "CNPJ associado"
        verbose_name_plural = "CNPJs associados"

    def __str__(self):
        return f"{self.cnpj} ({self.get_origem_display()})"

    @property
    def cnpj_formatado(self):
        c = self.cnpj
        return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else c


def empresa_por_cnpj(cnpj, qs=None, excluir=None):
    """Empresa pelo CNPJ principal OU por um CNPJ associado. `qs` restringe (ex.: só prospecção)."""
    if not cnpj:
        return None
    qs = Empresa.objects.all() if qs is None else qs
    if excluir is not None:
        qs = qs.exclude(pk=getattr(excluir, "pk", excluir))
    return qs.filter(cnpj=cnpj).first() or qs.filter(cnpjs_associados__cnpj=cnpj).first()


class LeadSalesforce(models.Model):
    """Leads BRIDA (somente leitura). Leads de outros distribuidores NÃO são importados."""

    salesforce_id = models.CharField(max_length=18, unique=True)
    cnpj = models.CharField(max_length=14, null=True, blank=True, db_index=True)
    empresa_nome = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=80, blank=True)
    convertido = models.BooleanField(default=False)
    conta_convertida_id = models.CharField(max_length=18, blank=True)
    distribuidor = models.CharField(max_length=200, blank=True)
    criado_em_origem = models.DateTimeField(null=True, blank=True)
    ultimo_visto_em = models.DateTimeField(default=timezone.now)
    ultima_importacao = models.ForeignKey(Importacao, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = "lead Salesforce (BRIDA)"
        verbose_name_plural = "leads Salesforce (BRIDA)"

    def __str__(self):
        return f"{self.empresa_nome} ({self.status})"


# ============================================================
# Compras


class NotaFiscal(models.Model):
    """
    Resumo por nota (não por item), para rastrear os indicadores sem importar
    todo o detalhe. Fonte: InvoiceItem__c agregado por Invoice__c.
    """

    class Tipo(models.TextChoices):
        VENDA = "VENDA", "Venda (Saída, CFOP de venda)"
        DEVOLUCAO = "DEVOLUCAO", "Devolução (Entrada)"

    id_origem = models.CharField(max_length=40, unique=True, help_text="Invoice__c.Id")
    numero = models.CharField(max_length=80, blank=True, help_text="Invoice__c.Name")
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    cliente_id_origem = models.CharField(max_length=40, blank=True, help_text="Invoice__c.Customer__r.Id (Account)")
    empresa = models.ForeignKey(Empresa, null=True, blank=True, on_delete=models.SET_NULL, related_name="notas")
    data_emissao = models.DateField(null=True, blank=True, help_text="Menor InvoiceItem__c.EmissionDate__c da nota")
    valor_total = models.DecimalField(max_digits=16, decimal_places=2, default=0, help_text="Soma de ValorTotalFormula__c")
    qtd_itens = models.PositiveIntegerField(default=0)
    categorias = models.JSONField(default=dict, blank=True, help_text="{Tipo produto (TypeofProdutct__c): valor}")
    presente_na_origem = models.BooleanField(
        default=True, help_text="Falso se a nota deixou de vir na origem (ex.: cancelada depois)."
    )
    primeira_importacao = models.ForeignKey(Importacao, null=True, on_delete=models.SET_NULL, related_name="+")
    ultima_importacao = models.ForeignKey(Importacao, null=True, on_delete=models.SET_NULL, related_name="+")
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-data_emissao"]
        indexes = [models.Index(fields=["empresa", "tipo", "presente_na_origem"])]
        verbose_name = "nota fiscal"
        verbose_name_plural = "notas fiscais"

    def __str__(self):
        return f"NF {self.numero} ({self.get_tipo_display()})"


class IndicadorCompra(models.Model):
    empresa = models.OneToOneField(Empresa, on_delete=models.CASCADE, related_name="indicador")
    situacao = models.CharField(max_length=30, choices=SituacaoCompra.choices, default=SituacaoCompra.INDISPONIVEL)
    primeira_compra = models.DateField(null=True, blank=True)
    ultima_compra = models.DateField(null=True, blank=True)
    qtd_compras = models.PositiveIntegerField(default=0, help_text="Notas de venda distintas")
    valor_compras = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    qtd_devolucoes = models.PositiveIntegerField(default=0)
    valor_devolvido = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    cobertura_inicio = models.DateField(null=True, blank=True)
    cobertura_fim = models.DateTimeField(null=True, blank=True)
    calculado_em = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "indicador de compra"
        verbose_name_plural = "indicadores de compra"


class CompraCategoria(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="categorias_compra")
    categoria = models.CharField(max_length=120)
    qtd_notas = models.PositiveIntegerField(default=0)
    valor = models.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["empresa", "categoria"], name="compra_categoria_unica")]
        ordering = ["-valor"]


# ============================================================
# Abordagens


class Abordagem(models.Model):
    """Histórico de contato. Nunca é tocado pelas importações."""

    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="abordagens")
    contato = models.ForeignKey(Contato, null=True, blank=True, on_delete=models.PROTECT, related_name="abordagens")
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="abordagens")
    data_hora = models.DateTimeField("data e hora", default=timezone.now)
    canal = models.CharField(max_length=20, choices=CanalAbordagem.choices)
    resultado = models.CharField(max_length=30, choices=ResultadoAbordagem.choices)
    houve_conversa = models.BooleanField(default=False, editable=False)
    observacoes = models.TextField("observações", blank=True)
    valor_orcamento = models.DecimalField("valor do orçamento (R$)", max_digits=12, decimal_places=2,
                                          null=True, blank=True)
    valor_venda = models.DecimalField("valor vendido (R$)", max_digits=12, decimal_places=2, null=True, blank=True)
    cnpj_confirmado = models.CharField(
        "CNPJ confirmado com o cliente", max_length=14, blank=True,
        help_text="Confirmado pelo atendimento na conversa; liga a venda às notas fiscais do Salesforce")
    anexo_orcamento = models.FileField(
        "orçamento (arquivo)", upload_to="orcamentos/%Y/%m/", blank=True, max_length=255,
        help_text="PDF, imagem ou planilha do orçamento enviado ao cliente (comprovante da gamificação)")

    class SituacaoCliente(models.TextChoices):
        NOVO = "NOVO", "Cliente novo (CNPJ fora do Salesforce)"
        SEM_COMPRA = "SEM_COMPRA", "Cadastrado no Salesforce, nunca comprou"
        INATIVO = "INATIVO", "Cadastrado, sem comprar há mais que o limite"
        ATIVO = "ATIVO", "Cliente ativo da carteira (não conta na captação)"

    # foto da situação do CNPJ confirmado NO MOMENTO da confirmação (define se a venda conta na captação)
    cliente_situacao = models.CharField(max_length=12, choices=SituacaoCliente.choices, blank=True)
    cliente_ultima_compra = models.DateField(null=True, blank=True,
                                             help_text="Última compra conhecida antes da confirmação do CNPJ")
    proxima_acao = models.CharField("próxima ação", max_length=255, blank=True)
    data_retorno = models.DateField("data de retorno", null=True, blank=True)
    retorno_concluido = models.BooleanField(default=False)
    retorno_concluido_em = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-data_hora"]
        indexes = [models.Index(fields=["data_retorno", "retorno_concluido"])]

    def save(self, *args, **kwargs):
        self.houve_conversa = self.resultado in RESULTADOS_COM_CONVERSA
        super().save(*args, **kwargs)

    @property
    def retorno_pendente(self):
        return bool(self.data_retorno) and not self.retorno_concluido

    @property
    def retorno_atrasado(self):
        return self.retorno_pendente and self.data_retorno < timezone.localdate()


# ============================================================
# Listas de abordagem (incluir numa lista NÃO é contatar)


class ListaAbordagem(models.Model):
    nome = models.CharField(max_length=200)
    criada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    criada_em = models.DateTimeField(auto_now_add=True)
    filtros = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-criada_em"]
        verbose_name = "lista de abordagem"
        verbose_name_plural = "listas de abordagem"

    def __str__(self):
        return self.nome


class ItemLista(models.Model):
    lista = models.ForeignKey(ListaAbordagem, on_delete=models.CASCADE, related_name="itens")
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="itens_lista")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["lista", "empresa"], name="item_lista_unico")]
