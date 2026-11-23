"""Gestão de usuários do sistema (só ADMIN). Dois perfis: ADMIN e Inside Sales.

Não usa o /admin/ do Django. Usuário não é apagado (os contatos registrados ficam ligados a ele): é desativado.
Proteções: ninguém tira o próprio perfil ADMIN nem se desativa, e sempre sobra pelo menos um ADMIN ativo.
"""
from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model, password_validation, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from prospeccao import papeis

U = get_user_model()


def _so_admin(request):
    if not papeis.eh_adm(request.user):
        raise PermissionDenied


class SenhaMixin(forms.Form):
    senha1 = forms.CharField(label="Senha", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
                             strip=False)
    senha2 = forms.CharField(label="Confirme a senha", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
                             strip=False)

    def validar_senha(self, usuario=None):
        s1, s2 = self.cleaned_data.get("senha1"), self.cleaned_data.get("senha2")
        if s1 and s2 and s1 != s2:
            self.add_error("senha2", "As duas senhas não são iguais.")
        elif s1:
            try:
                password_validation.validate_password(s1, usuario)
            except forms.ValidationError as e:
                self.add_error("senha1", e)


class UsuarioForm(forms.Form):
    nome = forms.CharField(label="Nome", max_length=150)
    username = forms.RegexField(label="Login", max_length=150, regex=r"^[\w.@+-]+$",
                                help_text="Usado para entrar. Letras, números e . @ + - _ (sem espaço).",
                                error_messages={"invalid": "Use só letras, números e . @ + - _ (sem espaço)."})
    email = forms.EmailField(label="E-mail", required=False)
    perfil = forms.ChoiceField(label="Perfil", choices=papeis.PERFIS, widget=forms.RadioSelect)
    ativo = forms.BooleanField(label="Ativo (pode entrar no sistema)", required=False, initial=True)

    def __init__(self, *args, usuario=None, **kwargs):
        self.usuario = usuario
        super().__init__(*args, **kwargs)

    def clean_username(self):
        u = self.cleaned_data["username"].strip()
        qs = U.objects.filter(username__iexact=u)
        if self.usuario is not None:
            qs = qs.exclude(pk=self.usuario.pk)
        if qs.exists():
            raise forms.ValidationError("Já existe um usuário com este login.")
        return u


class NovoUsuarioForm(UsuarioForm, SenhaMixin):
    def clean(self):
        dados = super().clean()
        self.validar_senha(U(username=dados.get("username") or "", first_name=dados.get("nome") or "",
                             email=dados.get("email") or ""))
        return dados


class NovaSenhaForm(SenhaMixin):
    def __init__(self, *args, usuario=None, **kwargs):
        self.usuario = usuario
        super().__init__(*args, **kwargs)

    def clean(self):
        dados = super().clean()
        self.validar_senha(self.usuario)
        return dados


@login_required
def usuarios(request):
    _so_admin(request)
    q = (request.GET.get("q") or "").strip()
    perfil = request.GET.get("perfil") or ""
    situacao = request.GET.get("situacao") or "ativos"
    qs = (U.objects.annotate(n_contatos=Count("abordagens", distinct=True),
                             ult_contato=Max("abordagens__data_hora"))
          .prefetch_related("groups").order_by("-is_active", "first_name", "username"))
    if q:
        qs = qs.filter(Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
                       | Q(email__icontains=q))
    if situacao == "ativos":
        qs = qs.filter(is_active=True)
    elif situacao == "inativos":
        qs = qs.filter(is_active=False)
    lista = []
    for u in qs:
        u.perfil = papeis.papel(u) if u.is_active else ("ADM" if u.is_superuser or u.is_staff
                                                         or any(g.name == papeis.GRUPO_ADM for g in u.groups.all())
                                                         else "INSIDE")
        u.perfil_rotulo = papeis.ROTULOS[u.perfil]
        if perfil and u.perfil != perfil:
            continue
        lista.append(u)
    todos = U.objects.all()
    return render(request, "prospeccao/usuarios.html", {
        "usuarios": lista, "f": {"q": q, "perfil": perfil, "situacao": situacao},
        "totais": {"ativos": todos.filter(is_active=True).count(), "inativos": todos.filter(is_active=False).count(),
                   "admins": papeis.admins_ativos().count()},
    })


def _proteger(request, alvo, perfil, ativo):
    """Mensagem de erro se a mudança tiraria o acesso de quem está mexendo ou deixaria o sistema sem ADMIN."""
    if alvo.pk == request.user.pk and perfil != "ADM":
        return "Você não pode tirar o seu próprio perfil ADMIN (peça a outro ADMIN)."
    if alvo.pk == request.user.pk and not ativo:
        return "Você não pode desativar o seu próprio usuário."
    era_admin = papeis.eh_adm(alvo) and alvo.is_active
    if era_admin and (perfil != "ADM" or not ativo) and not papeis.admins_ativos(excluir=alvo).exists():
        return "O sistema precisa de pelo menos um ADMIN ativo."
    return ""


@login_required
def usuario_novo(request):
    _so_admin(request)
    form = NovoUsuarioForm(request.POST or None, initial={"perfil": "INSIDE", "ativo": True})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        u = U.objects.create_user(d["username"], email=d["email"], password=d["senha1"], first_name=d["nome"],
                                  is_active=d["ativo"])
        papeis.definir_perfil(u, d["perfil"])
        messages.success(request, f"Usuário {u.username} criado com o perfil {papeis.ROTULOS[d['perfil']]}.")
        return redirect("prospeccao:usuarios")
    return render(request, "prospeccao/usuario_form.html", {"form": form, "novo": True})


@login_required
def usuario_editar(request, pk):
    _so_admin(request)
    alvo = get_object_or_404(U, pk=pk)
    inicial = {"nome": alvo.get_full_name() or alvo.first_name, "username": alvo.username, "email": alvo.email,
               "perfil": "ADM" if papeis.eh_adm(alvo) else "INSIDE", "ativo": alvo.is_active}
    form = UsuarioForm(request.POST or None, initial=inicial, usuario=alvo)
    senha_form = NovaSenhaForm(usuario=alvo)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        erro = _proteger(request, alvo, d["perfil"], d["ativo"])
        if erro:
            form.add_error(None, erro)
        else:
            alvo.first_name, alvo.last_name = d["nome"][:150], ""
            alvo.username, alvo.email, alvo.is_active = d["username"], d["email"], d["ativo"]
            alvo.save(update_fields=["first_name", "last_name", "username", "email", "is_active"])
            papeis.definir_perfil(alvo, d["perfil"])
            messages.success(request, f"Usuário {alvo.username} atualizado.")
            return redirect("prospeccao:usuarios")
    return render(request, "prospeccao/usuario_form.html", {"form": form, "alvo": alvo, "senha_form": senha_form})


@login_required
@require_POST
def usuario_senha(request, pk):
    _so_admin(request)
    alvo = get_object_or_404(U, pk=pk)
    senha_form = NovaSenhaForm(request.POST, usuario=alvo)
    if senha_form.is_valid():
        alvo.set_password(senha_form.cleaned_data["senha1"])
        alvo.save(update_fields=["password"])
        if alvo.pk == request.user.pk:
            update_session_auth_hash(request, alvo)  # quem trocou a própria senha continua conectado
        messages.success(request, f"Senha de {alvo.username} alterada. Informe a nova senha à pessoa.")
        return redirect("prospeccao:usuarios")
    inicial = {"nome": alvo.get_full_name() or alvo.first_name, "username": alvo.username, "email": alvo.email,
               "perfil": "ADM" if papeis.eh_adm(alvo) else "INSIDE", "ativo": alvo.is_active}
    return render(request, "prospeccao/usuario_form.html", {
        "form": UsuarioForm(initial=inicial, usuario=alvo), "alvo": alvo, "senha_form": senha_form,
        "foco_senha": True})


@login_required
@require_POST
def usuario_ativo(request, pk):
    _so_admin(request)
    alvo = get_object_or_404(U, pk=pk)
    ativo = request.POST.get("ativo") == "1"
    erro = _proteger(request, alvo, "ADM" if papeis.eh_adm(alvo) else "INSIDE", ativo)
    if erro:
        messages.error(request, erro)
    else:
        U.objects.filter(pk=alvo.pk).update(is_active=ativo)
        messages.success(request, f"Usuário {alvo.username} {'reativado' if ativo else 'desativado'}."
                         + ("" if ativo else " Ele não entra mais no sistema; o histórico dele é mantido."))
    return redirect("prospeccao:usuarios")
