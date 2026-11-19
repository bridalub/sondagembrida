"""Normalizações compartilhadas (carteira e prospecção)."""
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlsplit

DOMINIOS_GENERICOS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.br", "uol.com.br", "bol.com.br",
    "terra.com.br", "ig.com.br", "live.com", "msn.com", "icloud.com", "globo.com", "r7.com", "hotmail.com.br",
    "outlook.com.br", "zipmail.com.br",
}

_SUFIXOS = r"\b(LTDA|LIMITADA|S\.?A|S/A|EIRELI|ME|EPP|MEI|CIA|COMERCIO|COM|IND|INDUSTRIA|SERVICOS|E|DE|DA|DO|DOS|DAS)\b"


def sem_acentos(texto):
    return "".join(c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c))


def normalizar_texto(texto):
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", sem_acentos(texto).upper())).strip()


def nome_para_comparacao(nome):
    return re.sub(r"\s+", " ", re.sub(_SUFIXOS, " ", normalizar_texto(nome))).strip()


def similaridade_nome(a, b):
    a, b = nome_para_comparacao(a), nome_para_comparacao(b)
    if not a or not b:
        return 0.0
    if a == b or (len(a) >= 5 and (a in b or b in a)):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def digitos_telefone(valor):
    """Últimos 10/11 dígitos (DDD + número), sem DDI."""
    d = re.sub(r"\D", "", valor or "")
    if d.startswith("55") and len(d) > 11:
        d = d[2:]
    d = d.lstrip("0")
    return d if 10 <= len(d) <= 11 else ""


def dominio_de_url(url):
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def dominio_de_email(email):
    if not email or "@" not in email:
        return ""
    d = email.rsplit("@", 1)[1].lower().strip()
    return "" if d in DOMINIOS_GENERICOS else d


# Procura a palavra em qualquer parte do nome do e-mail (ex.: br.dsc.fiscal@...), separada por . _ -
_S = r"(?:^|[._\-])"
_EMAIL_FISCAL = re.compile(_S + r"(nf|nfe|nf-e|nfs|nfse|xml|fiscal|danfe|notafiscal|nota\.?fiscal)(?:$|[._\-\d])", re.I)
_EMAIL_CONTADOR = re.compile(_S + r"(contab|contabil|contabilidade|contador|contadora|escritorio)(?:$|[._\-\d])", re.I)
_EMAIL_FINANCEIRO = re.compile(_S + r"(financeiro|financ|cobranca|cobrança|contas|pagar|receber|faturamento|boleto)"
                               r"(?:$|[._\-\d])", re.I)
_EMAIL_COMERCIAL = re.compile(_S + r"(comercial|vendas|venda|compras|compra|contato|atendimento|sac|orcamento|orçamento)"
                              r"(?:$|[._\-\d])", re.I)


def classificar_email(email):
    """Classificação só pelo prefixo; sem evidência clara fica INCERTO."""
    local = (email or "").split("@")[0]
    if _EMAIL_CONTADOR.search(local):
        return "CONTADOR"
    if _EMAIL_FISCAL.search(local):
        return "FISCAL"
    if _EMAIL_FINANCEIRO.search(local):
        return "FINANCEIRO"
    if _EMAIL_COMERCIAL.search(local):
        return "COMERCIAL"
    return "INCERTO"


_COD_SEG = re.compile(r"^(\d{3})\s*-\s*(.+)$")


def normalizar_segmento(valor, codificados=None):
    """
    '509-OFICINAS/MOTOPEÇAS' -> igual; 'CONSTRUCAO' -> '602-CONSTRUCAO' se houver um segmento
    codificado com o mesmo nome em `codificados` (lista de valores com código). Sem correspondência, mantém.
    """
    v = (valor or "").strip()
    if not v or _COD_SEG.match(v):
        return v
    alvo = normalizar_texto(v)
    for c in codificados or []:
        m = _COD_SEG.match(c)
        if m and normalizar_texto(m.group(2)) == alvo:
            return c
    return v
