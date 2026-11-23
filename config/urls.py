from pathlib import Path

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve

from django.http import QueryDict
from django.shortcuts import redirect

from config import pwa


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
    # PWA: manifesto e service worker na raiz (escopo do site inteiro) e página offline.
    path("manifest.webmanifest", pwa.manifesto, name="pwa_manifesto"),
    path("sw.js", pwa.service_worker, name="pwa_sw"),
    path("offline/", pwa.offline, name="pwa_offline"),
    # O perfil se chamava Insight Sales: links e favoritos antigos seguem funcionando.
    re_path(r"^insight-sales/(?P<resto>.*)$",
            lambda r, resto: redirect(f"/inside-sales/{resto}" + (f"?{r.META['QUERY_STRING']}" if r.META.get("QUERY_STRING") else ""),
                                      permanent=True), name="legado_insight"),
    path("", include("prospeccao.urls")),
    path("", include("carteira.urls")),
    re_path(r"^static/(?P<path>.*)$", serve, {
        "document_root": str(Path(__file__).resolve().parent.parent / "carteira" / "static"),
    }),
]
