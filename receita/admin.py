from django.contrib import admin

from receita.models import ArquivoCarga, CargaReceita, EstabelecimentoReceita


class ArquivoInline(admin.TabularInline):
    model = ArquivoCarga
    extra = 0
    readonly_fields = ["nome", "status", "linhas_lidas", "linhas_gravadas", "iniciado_em", "concluido_em", "erro"]


@admin.register(CargaReceita)
class CargaAdmin(admin.ModelAdmin):
    list_display = ["pk", "competencia", "descricao_filtro", "status", "ativa", "estabelecimentos", "iniciada_em"]
    inlines = [ArquivoInline]


@admin.register(EstabelecimentoReceita)
class EstabAdmin(admin.ModelAdmin):
    list_display = ["cnpj", "razao_social", "nome_fantasia", "municipio", "situacao", "cnae_principal"]
    list_filter = ["situacao", "uf"]
    search_fields = ["cnpj", "razao_social", "nome_fantasia"]
