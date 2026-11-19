"""Papéis de quem usa o sistema.

- ADMIN: vê e faz tudo (pesquisas, buscas, configurações, carteira, painel de captação, usuários).
  É ADMIN quem é superusuário, "membro da equipe" (is_staff) ou está no grupo "ADM".
- Insight Sales: menu Insight Sales, Minha performance e Acompanhar retornos (só os dele), mais o atendimento do
  lead (ficha da empresa e registro do contato). Qualquer usuário que não seja ADMIN é Insight Sales.

Os usuários são cadastrados pelo ADMIN na tela Gestão → Usuários (prospeccao/views_usuarios.py).
"""
from django.contrib import messages
from django.shortcuts import redirect

GRUPO_ADM = "ADM"
GRUPO_INSIGHT = "Insight Sales"

# Telas (nomes de rota) liberadas ao Insight Sales. O resto redireciona para a tela dele.
ROTAS_INSIGHT = {
    "prospeccao:insight_sales", "prospeccao:minha_performance", "prospeccao:atendimento", "prospeccao:desclassificar",
    "prospeccao:empresa", "prospeccao:oportunidade",
    "carteira:registrar_abordagem", "carteira:adicionar_contato", "carteira:concluir_retorno", "carteira:retornos",
    "carteira:anexo_orcamento",
    "carteira:empresas", "carteira:empresa_detalhe", "carteira:nao_contatar",  # Carteira Salesforce
    "login", "logout",
}


def eh_adm(user):
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user.groups.filter(name=GRUPO_ADM).exists()


def papel(user):
    if not getattr(user, "is_authenticated", False):
        return ""
    return "ADM" if eh_adm(user) else "INSIGHT"


PERFIS = [("ADM", "ADMIN"), ("INSIGHT", "Insight Sales")]
ROTULOS = dict(PERFIS)


def rotulo(user):
    return ROTULOS.get(papel(user), "")


def definir_perfil(user, perfil):
    """Os dois únicos perfis. ADMIN = acesso total; Insight Sales = só as telas dele. Grava grupo e flags juntos."""
    from django.contrib.auth.models import Group
    adm = perfil == "ADM"
    grupo = Group.objects.get_or_create(name=GRUPO_ADM if adm else GRUPO_INSIGHT)[0]
    type(user).objects.filter(pk=user.pk).update(is_staff=adm, is_superuser=adm)
    user.is_staff = user.is_superuser = adm
    user.groups.set([grupo])
    type(user).objects.filter(pk=user.pk).update(is_staff=adm, is_superuser=adm)  # depois do sinal do grupo


def admins_ativos(excluir=None):
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    qs = get_user_model().objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(is_staff=True) | Q(groups__name=GRUPO_ADM)).distinct()
    return qs.exclude(pk=excluir.pk) if excluir is not None else qs


def contexto(request):
    """Context processor: `papel` e `eh_adm` em todos os templates."""
    p = papel(getattr(request, "user", None))
    return {"papel": p, "eh_adm": p == "ADM", "papel_rotulo": ROTULOS.get(p, "")}


class PapelMiddleware:
    """Insight Sales só acessa as telas dele. Arquivos estáticos e o login passam sempre."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False) or eh_adm(user):
            return None
        if request.path.startswith("/static/"):
            return None
        rm = request.resolver_match
        nome = f"{rm.namespace}:{rm.url_name}" if rm and rm.namespace else (rm.url_name if rm else "")
        if nome in ROTAS_INSIGHT:
            return None
        if nome != "prospeccao:visao_geral":  # a página inicial só leva à tela dele, sem aviso
            messages.info(request, "Seu perfil é Insight Sales: leads, atendimento, sua performance e seus retornos.")
        return redirect("prospeccao:insight_sales")
