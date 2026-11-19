"""Falha de CSRF tratada sem página técnica.

O caso comum é um formulário de login antigo (aberto antes de outro login, ou reenviado com F5 na tela de erro):
a chave do formulário venceu. Em vez do erro 403 em inglês, a tela de login é recarregada com uma chave nova e um
aviso. Nos demais formulários, uma página curta explica e oferece voltar. Nenhuma ação é executada.
"""
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme


def falha_csrf(request, reason=""):
    login = reverse("login")
    if request.path == login:
        proximo = request.GET.get("next") or ""
        destino = login + "?expirou=1"
        if proximo and url_has_allowed_host_and_scheme(proximo, allowed_hosts={request.get_host()}):
            from urllib.parse import quote
            destino += "&next=" + quote(proximo)
        return HttpResponseRedirect(destino)
    return render(request, "csrf_falha.html", {"voltar": request.META.get("HTTP_REFERER") or "/"}, status=403)
