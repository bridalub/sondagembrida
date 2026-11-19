"""
Base pública de CNPJ (dados abertos da Receita Federal) – SEPARADA da carteira Salesforce.

Estar nesta base NÃO significa ser cliente nem oportunidade aprovada: é apenas o cadastro público
de uma competência mensal (não é consulta em tempo real).

Versionamento: cada carga (competência + recorte) grava suas próprias linhas. Só a carga `ativa`
é usada pela prospecção; ela só é trocada quando a nova carga termina com sucesso.
"""
from django.db import models
from django.db.models import Q
from django.utils import timezone

SITUACOES = {"01": "NULA", "1": "NULA", "02": "ATIVA", "2": "ATIVA", "03": "SUSPENSA", "3": "SUSPENSA",
             "04": "INAPTA", "4": "INAPTA", "08": "BAIXADA", "8": "BAIXADA"}
PORTES = {"00": "NÃO INFORMADO", "01": "MICRO EMPRESA", "03": "EMPRESA DE PEQUENO PORTE", "05": "DEMAIS"}


class CargaReceita(models.Model):
    class Status(models.TextChoices):
        EXECUTANDO = "EXECUTANDO", "Executando"
        INTERROMPIDA = "INTERROMPIDA", "Interrompida (pode ser retomada)"
        FALHA = "FALHA", "Falha (pode ser retomada)"
        CONCLUIDA = "CONCLUIDA", "Concluída"
        SUBSTITUIDA = "SUBSTITUIDA", "Substituída por carga mais nova"

    competencia = models.CharField(max_length=7, help_text="AAAA-MM da pasta oficial")
    data_arquivos = models.DateField(null=True, blank=True, help_text="Data de publicação dos arquivos oficiais")
    filtro_ufs = models.JSONField(default=list)
    filtro_municipios = models.JSONField(default=list, help_text="[{codigo, nome}] (códigos da tabela da Receita)")
    descricao_filtro = models.CharField(max_length=255)
    origem = models.CharField(max_length=300)
    diretorio = models.CharField(max_length=500)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.EXECUTANDO)
    ativa = models.BooleanField(default=False)
    iniciada_em = models.DateTimeField(default=timezone.now)
    finalizada_em = models.DateTimeField(null=True, blank=True)
    heartbeat_em = models.DateTimeField(default=timezone.now)
    etapa = models.CharField(max_length=60, blank=True)
    linhas_lidas = models.BigIntegerField(default=0)
    linhas_malformadas = models.BigIntegerField(default=0)
    estabelecimentos = models.PositiveIntegerField(default=0)
    empresas_complementadas = models.PositiveIntegerField(default=0)
    cnpj_invalidos = models.PositiveIntegerField(default=0)
    mensagem = models.TextField(blank=True)

    class Meta:
        ordering = ["-iniciada_em"]
        constraints = [
            models.UniqueConstraint(fields=["ativa"], condition=Q(ativa=True), name="uma_carga_receita_ativa"),
            models.UniqueConstraint(fields=["status"], condition=Q(status="EXECUTANDO"),
                                    name="uma_carga_receita_executando"),
        ]
        verbose_name = "carga da base Receita"
        verbose_name_plural = "cargas da base Receita"

    def __str__(self):
        return f"Receita {self.competencia} – {self.descricao_filtro}"

    @classmethod
    def ativa_atual(cls):
        return cls.objects.filter(ativa=True).first()


class ArquivoCarga(models.Model):
    class Status(models.TextChoices):
        PENDENTE = "PENDENTE", "Pendente"
        BAIXANDO = "BAIXANDO", "Baixando"
        PROCESSANDO = "PROCESSANDO", "Processando"
        CONCLUIDO = "CONCLUIDO", "Concluído"
        FALHA = "FALHA", "Falha"

    carga = models.ForeignKey(CargaReceita, on_delete=models.CASCADE, related_name="arquivos")
    nome = models.CharField(max_length=60)
    tamanho_esperado = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDENTE)
    linhas_lidas = models.BigIntegerField(default=0)
    linhas_gravadas = models.PositiveIntegerField(default=0)
    iniciado_em = models.DateTimeField(null=True, blank=True)
    concluido_em = models.DateTimeField(null=True, blank=True)
    erro = models.TextField(blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["carga", "nome"], name="arquivo_carga_unico")]


class TabelaReceita(models.Model):
    """Tabelas de códigos (Municípios, CNAEs, Motivos, Naturezas) de cada competência."""

    competencia = models.CharField(max_length=7)
    tipo = models.CharField(max_length=12)
    codigo = models.CharField(max_length=10)
    descricao = models.CharField(max_length=255)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["competencia", "tipo", "codigo"], name="tabela_receita_unica")]
        indexes = [models.Index(fields=["competencia", "tipo", "descricao"])]


class EstabelecimentoReceita(models.Model):
    """Um estabelecimento (CNPJ completo) do recorte importado. Contatos são CADASTRAIS (não validados)."""

    carga = models.ForeignKey(CargaReceita, on_delete=models.CASCADE, related_name="estabelecimentos_set")
    arquivo = models.CharField(max_length=60, help_text="Arquivo de origem (para retomada)")
    cnpj = models.CharField(max_length=14)
    cnpj_basico = models.CharField(max_length=8)
    matriz = models.BooleanField(null=True)
    nome_fantasia = models.CharField(max_length=255, blank=True)
    razao_social = models.CharField(max_length=255, blank=True)
    natureza_codigo = models.CharField(max_length=4, blank=True)
    natureza = models.CharField(max_length=160, blank=True)
    porte = models.CharField(max_length=40, blank=True)
    situacao_codigo = models.CharField(max_length=2, blank=True)
    situacao = models.CharField(max_length=12, blank=True)
    data_situacao = models.DateField(null=True, blank=True)
    motivo_codigo = models.CharField(max_length=3, blank=True)
    motivo = models.CharField(max_length=200, blank=True)
    data_abertura = models.DateField(null=True, blank=True)
    cnae_principal = models.CharField(max_length=7, blank=True)
    cnae_descricao = models.CharField(max_length=255, blank=True)
    cnaes_secundarios = models.CharField(max_length=1000, blank=True, help_text="Códigos separados por espaço")
    tipo_logradouro = models.CharField(max_length=40, blank=True)
    logradouro = models.CharField(max_length=160, blank=True)
    numero = models.CharField(max_length=20, blank=True)
    complemento = models.CharField(max_length=160, blank=True)
    bairro = models.CharField(max_length=80, blank=True)
    cep = models.CharField(max_length=8, blank=True)
    uf = models.CharField(max_length=2, blank=True)
    municipio_codigo = models.CharField(max_length=4, blank=True)
    municipio = models.CharField(max_length=80, blank=True)
    telefone1 = models.CharField(max_length=14, blank=True)
    telefone2 = models.CharField(max_length=14, blank=True)
    email = models.CharField(max_length=160, blank=True)
    situacao_especial = models.CharField(max_length=60, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["carga", "cnpj"], name="estab_receita_unico")]
        indexes = [
            models.Index(fields=["carga", "uf", "municipio", "situacao_codigo"]),
            models.Index(fields=["carga", "cnpj_basico"]),
            models.Index(fields=["carga", "arquivo"]),
            models.Index(fields=["carga", "cnae_principal"]),
            models.Index(fields=["carga", "telefone1"]),
            models.Index(fields=["carga", "telefone2"]),
            models.Index(fields=["carga", "municipio_codigo"]),
            models.Index(fields=["carga", "email"]),
        ]
        verbose_name = "estabelecimento (base Receita)"
        verbose_name_plural = "estabelecimentos (base Receita)"

    def __str__(self):
        return f"{self.cnpj} {self.razao_social or self.nome_fantasia}"
