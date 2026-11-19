from decimal import Decimal
import re

from django import template
from django.urls import reverse
from django.utils.html import format_html

register = template.Library()


@register.filter
def nd(valor):
    """Mostra 'desconhecido' para dado ausente, sem inventar valor."""
    if valor is None or valor == "":
        return format_html('<span class="nd">desconhecido</span>')
    return valor


@register.filter
def brl(valor):
    if valor is None:
        return ""
    v = Decimal(valor).quantize(Decimal("0.01"))
    inteiro, dec = f"{v:,.2f}".split(".")
    return f"R$ {inteiro.replace(',', '.')},{dec}"


@register.filter
def rotulo(chave, mapa):
    return mapa.get(chave, chave)


@register.filter
def tel_href(valor):
    d = re.sub(r"[^\d+]", "", str(valor or ""))
    return f"tel:{d}" if d else ""


@register.filter
def wa_href(valor):
    d = re.sub(r"\D", "", str(valor or ""))
    if d.startswith("0"):
        d = d.lstrip("0")
    if d and not d.startswith("55") and len(d) <= 11:
        d = "55" + d
    return f"https://wa.me/{d}" if d else ""


@register.filter
def url_externa(valor):
    """Só aceita http(s); acrescenta https:// quando a fonte trouxe só o domínio."""
    v = str(valor or "").strip()
    if not v:
        return ""
    if v.lower().startswith(("http://", "https://")):
        return v
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", v, re.I):
        return "https://" + v
    return ""


# Nível do canal (CanalEmpresa.nivel) -> (classe, símbolo, rótulo curto). O rótulo longo vai no title.
NIVEL_VISUAL = {
    "CONFIRMADO_EQUIPE": ("n-equipe", "✓", "Confirmado pela equipe"),
    "CONFERIDO_EQUIPE": ("n-equipe", "✓", "Conferido pela equipe"),
    "INFORMADO_EQUIPE": ("n-equipe", "✓", "Informado pela equipe"),
    "CORROBORADO": ("n-corroborado", "✓✓", "Corroborado"),
    "PUBLICADO": ("n-publicado", "●", "Publicado"),
    "CADASTRAL": ("n-cadastral", "○", "Cadastral"),
    "PISTA": ("n-pista", "?", "Pista a conferir"),
    "INCORRETO": ("n-incorreto", "✕", "Incorreto"),
}
NIVEL_DESCRICAO = {
    "CONFIRMADO_EQUIPE": "Alguém da equipe falou por este contato.",
    "CONFERIDO_EQUIPE": "A equipe conferiu a fonte, sem ter falado por este contato.",
    "INFORMADO_EQUIPE": "Contato informado por alguém da equipe.",
    "CORROBORADO": "Apareceu em mais de uma fonte associada à empresa.",
    "PUBLICADO": "Publicado em site ou fonte associada à empresa; ainda não confirmado em contato.",
    "CADASTRAL": "Veio do cadastro (Receita/Salesforce) e não foi validado; pode ser do contador.",
    "PISTA": "Citado em diretório ou busca; a identidade ainda precisa ser conferida.",
    "INCORRETO": "Marcado como incorreto pela equipe.",
}


@register.simple_tag
def nivel_canal(nivel):
    if not nivel:
        return format_html('<span class="nivel n-pendente" title="{}"><span class="ic" aria-hidden="true">–</span>{}</span>',
                           "Nenhum contato encontrado ainda.", "Pendente")
    classe, simbolo, texto = NIVEL_VISUAL.get(nivel, ("n-pendente", "•", nivel))
    return format_html('<span class="nivel {}" title="{}"><span class="ic" aria-hidden="true">{}</span>{}</span>',
                       classe, NIVEL_DESCRICAO.get(nivel, texto), simbolo, texto)


EVIDENCIA_VISUAL = {
    "CONFIRMADO_FONTE": ("ok", "Informado pela fonte"),
    "HIPOTESE": ("aviso", "Hipótese a confirmar"),
    "INSUFICIENTE": ("contorno", "A investigar"),
}


@register.simple_tag
def selo_evidencia(ev):
    """Selo da evidência. Origem IA é sempre marcada como hipótese (não é fato)."""
    if ev is None:
        return format_html('<span class="tag contorno">{}</span>', "A investigar")
    classe, texto = EVIDENCIA_VISUAL.get(ev.classificacao, ("contorno", ev.get_classificacao_display()))
    if getattr(ev, "gerada_por", "") == "IA":
        classe, texto = "aviso", "Hipótese da IA"
    return format_html('<span class="tag {}" title="{}">{}</span>', classe, ev.get_classificacao_display(), texto)


@register.filter
def descricao_nivel(nivel):
    return NIVEL_DESCRICAO.get(nivel, "")


@register.filter
def url_empresa(empresa):
    """Detalhe certo da empresa: tela de prospecção quando há oportunidade; senão, cadastro da carteira."""
    if empresa is None:
        return ""
    try:
        tem_op = empresa.oportunidade is not None
    except Exception:  # RelatedObjectDoesNotExist
        tem_op = False
    if tem_op:
        return reverse("prospeccao:empresa", args=[empresa.pk])
    return reverse("carteira:empresa_detalhe", args=[empresa.pk])


@register.simple_tag(takes_context=True)
def qs_troca(context, **mudancas):
    """Querystring atual trocando/removendo parâmetros (valor vazio remove). Sempre zera a página."""
    q = context["request"].GET.copy()
    q.pop("page", None)
    for k, v in mudancas.items():
        if v in (None, ""):
            q.pop(k, None)
        else:
            q[k] = v
    s = q.urlencode()
    return f"?{s}" if s else "?"


@register.simple_tag
def pagina_href(querystring, numero):
    """Link de paginação sem `?&page=` quando não há outros filtros."""
    s = (querystring or "").strip()
    return f"?{s}&page={numero}" if s else f"?page={numero}"
