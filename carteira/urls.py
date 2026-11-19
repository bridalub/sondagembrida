from django.urls import path

from carteira import views

app_name = "carteira"

urlpatterns = [
    path("carteira/", views.visao_geral, name="visao_geral"),
    path("empresas/", views.empresas, name="empresas"),
    path("novas-abordagens/", views.novas_abordagens, name="novas_abordagens"),
    path("empresas/<int:pk>/", views.empresa_detalhe, name="empresa_detalhe"),
    path("empresas/<int:pk>/abordagem/", views.registrar_abordagem, name="registrar_abordagem"),
    path("empresas/<int:pk>/contato/", views.adicionar_contato, name="adicionar_contato"),
    path("empresas/<int:pk>/nao-contatar/", views.nao_contatar, name="nao_contatar"),
    path("retornos/", views.retornos, name="retornos"),
    path("abordagens/<int:pk>/concluir-retorno/", views.concluir_retorno, name="concluir_retorno"),
    path("abordagens/<int:pk>/orcamento/", views.anexo_orcamento, name="anexo_orcamento"),
    path("importacoes/", views.importacoes, name="importacoes"),
    path("importacoes/<int:pk>/", views.importacao_detalhe, name="importacao_detalhe"),
    path("revisao/", views.revisao, name="revisao"),
    path("listas/", views.listas, name="listas"),
    path("listas/<int:pk>/", views.lista_detalhe, name="lista_detalhe"),
    path("listas/<int:pk>/csv/", views.lista_csv, name="lista_csv"),
]
