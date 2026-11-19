from django.contrib import admin

from prospeccao import models as m


@admin.register(m.Fonte)
class FonteAdmin(admin.ModelAdmin):
    list_display = ["nome", "codigo", "tipo", "habilitada", "max_consultas_execucao", "max_consultas_dia",
                    "intervalo_minimo_segundos"]
    exclude = []  # nenhum segredo é armazenado: só o NOME da variável de ambiente


@admin.register(m.RegraEvidencia)
class RegraAdmin(admin.ModelAdmin):
    list_display = ["nome", "alvo", "tipo_evidencia", "ativa"]
    list_filter = ["alvo", "tipo_evidencia", "ativa"]
    filter_horizontal = ["aplicacoes"]


@admin.register(m.ExclusaoComercial)
class ExclusaoAdmin(admin.ModelAdmin):
    list_display = ["tipo", "valor", "motivo", "ativa", "criada_por", "criada_em"]

    def save_model(self, request, obj, form, change):
        if not obj.criada_por_id:
            obj.criada_por = request.user
        super().save_model(request, obj, form, change)


@admin.register(m.Pesquisa)
class PesquisaAdmin(admin.ModelAdmin):
    list_display = ["nome", "uf", "ativa", "frequencia_horas", "proxima_execucao_em", "teto_custo_usd"]


@admin.register(m.UsoIA)
class UsoIAAdmin(admin.ModelAdmin):
    list_display = ["criado_em", "tipo", "status", "modelo", "empresa", "tokens_entrada", "tokens_saida",
                    "custo_estimado_usd", "duracao_ms"]
    list_filter = ["tipo", "status", "modelo"]
    raw_id_fields = ["empresa", "pesquisa", "plano"]


@admin.register(m.ConsultaWeb)
class ConsultaWebAdmin(admin.ModelAdmin):
    list_display = ["criada_em", "finalidade", "status", "consulta", "empresa", "custo_estimado_usd", "duracao_ms"]
    list_filter = ["finalidade", "status", "provedor"]
    search_fields = ["consulta", "empresa__cnpj"]
    raw_id_fields = ["empresa", "execucao", "pesquisa"]


@admin.register(m.Execucao)
class ExecucaoAdmin(admin.ModelAdmin):
    list_display = ["pk", "pesquisa", "status", "gatilho", "criada_em", "resultados", "candidatos_novos", "completa"]
    list_filter = ["status", "gatilho"]


@admin.register(m.Candidato)
class CandidatoAdmin(admin.ModelAdmin):
    list_display = ["nome", "fonte", "cidade", "status", "cnpj_candidato", "empresa"]
    list_filter = ["status", "fonte"]
    search_fields = ["nome", "cnpj_candidato", "id_externo"]
    raw_id_fields = ["empresa"]


@admin.register(m.Oportunidade)
class OportunidadeAdmin(admin.ModelAdmin):
    list_display = ["empresa", "tipo", "status", "escopo", "prioridade", "responsavel", "criada_em"]
    list_filter = ["tipo", "status", "escopo", "escopo_humano", "prioridade"]
    search_fields = ["empresa__nome", "empresa__cnpj"]

    def save_model(self, request, obj, form, change):
        if "escopo" in form.changed_data:  # reclassificação pelo gestor: não é recalculada
            obj.escopo_humano = True
            from django.utils import timezone
            obj.escopo_motivo = f"definido por {request.user} em {timezone.localdate():%d/%m/%Y}"
        super().save_model(request, obj, form, change)
    raw_id_fields = ["empresa"]


@admin.register(m.FrenteComercial)
class FrenteComercialAdmin(admin.ModelAdmin):
    """Frentes são editáveis pelo gestor (docs/PROSPECCAO.md)."""
    list_display = ["nome", "ativa", "atualizada_em"]
    list_filter = ["ativa"]
    filter_horizontal = ["aplicacoes"]


for modelo in (m.CategoriaProduto, m.Evidencia, m.DecisaoRevisao, m.UsoFonte, m.LogExecucao):
    admin.site.register(modelo)
