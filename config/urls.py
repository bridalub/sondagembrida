from pathlib import Path

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve

from django.http import QueryDict
from django.shortcuts import redirect


def _para_login_do_sistema(request):
    """O login do /admin/ do Django não é usado: vai para a tela de entrada do sistema (mesmo destino)."""
    destino = request.GET.get("next") or ""
    if destino.startswith("/admin/auth/user") or destino in ("/admin/", "/admin"):
        destino = "/gestao/usuarios/"
    q = QueryDict(mutable=True)
    if destino:
        q["next"] = destino
    return redirect("/entrar/" + (f"?{q.urlencode()}" if q else ""))


urlpatterns = [
    # Gestão de usuários e login são do sistema; o /admin/ do Django fica só para manutenção técnica.
    path("admin/login/", _para_login_do_sistema),
    re_path(r"^admin/auth/user/?$", lambda r: redirect("prospeccao:usuarios")),
    path("admin/", admin.site.urls),
    path("entrar/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"),
    path("sair/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("prospeccao.urls")),
    path("", include("carteira.urls")),
    re_path(r"^static/(?P<path>.*)$", serve, {
        "document_root": str(Path(__file__).resolve().parent.parent / "carteira" / "static"),
    }),
]
