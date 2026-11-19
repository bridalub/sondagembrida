from django.contrib import admin

from carteira import models as m


@admin.register(m.ConfiguracaoAnalise)
class ConfiguracaoAdmin(admin.ModelAdmin):
    list_display = ["__str__", "dias_inatividade", "nunca_comprou_habilitado"]

    def has_add_permission(self, request):
        return not m.ConfiguracaoAnalise.objects.exists()


class ContatoInline(admin.TabularInline):
    model = m.Contato
    extra = 0


class RegistroOrigemInline(admin.TabularInline):
    model = m.RegistroOrigem
    extra = 0
    readonly_fields = ["sistema", "objeto", "id_origem", "principal", "ultimo_visto_em", "ultima_importacao"]
    exclude = ["dados"]


@admin.register(m.Empresa)
class EmpresaAdmin(admin.ModelAdmin):
    list_display = ["nome", "cnpj", "cnpj_status", "segmento", "cidade", "uf", "requer_revisao", "nao_contatar"]
    list_filter = ["cnpj_status", "requer_revisao", "nao_contatar", "uf", "segmento"]
    search_fields = ["nome", "cnpj", "codigo_cliente", "salesforce_id"]
    readonly_fields = ["salesforce_id", "cnpj_origem", "ultima_importacao", "ultima_sincronizacao_em",
                       "criado_em", "atualizado_em"]
    inlines = [ContatoInline, RegistroOrigemInline]


@admin.register(m.Abordagem)
class AbordagemAdmin(admin.ModelAdmin):
    list_display = ["empresa", "data_hora", "usuario", "canal", "resultado", "houve_conversa", "data_retorno",
                    "retorno_concluido"]
    list_filter = ["canal", "resultado", "houve_conversa", "retorno_concluido"]
    search_fields = ["empresa__nome", "empresa__cnpj"]
    raw_id_fields = ["empresa", "contato"]


class OcorrenciaInline(admin.TabularInline):
    model = m.OcorrenciaImportacao
    extra = 0
    can_delete = False
    readonly_fields = ["tipo", "id_origem", "cnpj_origem", "nome", "mensagem", "empresa"]


@admin.register(m.Importacao)
class ImportacaoAdmin(admin.ModelAdmin):
    list_display = ["__str__", "status", "lidos", "criados", "atualizados", "inalterados", "rejeitados",
                    "inconsistentes"]
    list_filter = ["tipo", "status"]
    inlines = [OcorrenciaInline]


for modelo in (m.Contato, m.NotaFiscal, m.IndicadorCompra, m.CompraCategoria, m.ListaAbordagem, m.RegistroOrigem):
    admin.site.register(modelo)


from carteira.models import CnpjAssociado  # noqa: E402


@admin.register(CnpjAssociado)
class CnpjAssociadoAdmin(admin.ModelAdmin):
    list_display = ("cnpj", "empresa", "origem", "criado_por", "criado_em")
    list_filter = ("origem",)
    search_fields = ("cnpj", "empresa__nome", "empresa__cnpj")
    raw_id_fields = ("empresa",)
