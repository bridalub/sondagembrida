"""
Conector de IDENTIFICAÇÃO/EVIDÊNCIA: site público da própria empresa.

Lê no máximo 3 páginas do mesmo domínio (início + contato/sobre), respeitando robots.txt e as
proteções de NavegadorSeguro. Extrai: CNPJs publicados (validados pelo DV), e-mails e telefones
publicados e trechos curtos de texto para as regras de evidência.
Armazenamos apenas trechos curtos (até 300 caracteres) com a URL, nunca a página inteira.
"""
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from carteira.services import cnpj as cnpj_srv
from carteira.services.normalizacao import dominio_de_url
from prospeccao.conectores.base import ClienteFonte
from prospeccao.conectores.http_seguro import EnderecoBloqueado, NavegadorSeguro

_RE_CNPJ = re.compile(r"(?<![\dA-Z])([0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?\d{2})(?![\dA-Z])")
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_RE_TEL = re.compile(r"\(?\b(\d{2})\)?\s*((?:9[\s.]?)?\d{4})[-.\s]?(\d{4})\b")
_LINKS_INTERESSE = re.compile(
    r"contato|fale|sobre|quem-somos|quemsomos|empresa|institucional|about|contact|"
    r"unidade|filia|atendimento|sac|departamento|onde-estamos|localiza|rodape|privacidade|"
    r"politica|termos|dados-da-empresa|cnpj", re.I)
_RE_EMAIL_FRACO = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{1,}")
_RE_LDJSON = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)
_RE_WHATS = re.compile(
    r"(?:wa\.me/|api\.whatsapp\.com/send/?\?[^\"'\s>]*phone=|whatsapp\.com/send/?\?[^\"'\s>]*phone=|"
    r"web\.whatsapp\.com/send/?\?[^\"'\s>]*phone=)\+?(\d{10,15})", re.I)
_RE_URL_WHATS = re.compile(
    r"https?://(?:wa\.me/\S+|api\.whatsapp\.com/send\?\S+|web\.whatsapp\.com/send\?\S+|whatsapp\.com/send\?\S+)",
    re.I)
_TEXTO_BOTAO = re.compile(r"or[cç]amento|atendimento|contato|whatsapp|fale\s+conosco", re.I)
_ROTULOS = re.compile(r"(compras|suprimentos|manuten[cç][aã]o|comercial|vendas|atendimento|sac|financeiro|fiscal|"
                      r"cobran[cç]a|rh|recursos humanos|log[ií]stica|frota|oficina|ouvidoria|diretoria)", re.I)
_ATRIBUTOS_LINK = ("href", "data-href", "data-url", "data-link", "data-whatsapp", "formaction")


def _href_do_elemento(attrs):
    dados = dict(attrs)
    for chave in _ATRIBUTOS_LINK:
        if dados.get(chave):
            return dados[chave]
    clique = dados.get("onclick") or ""
    achado = _RE_URL_WHATS.search(clique) or re.search(r"https?://[^'\"\s]+", clique)
    return achado.group(0) if achado else ""


class _Extrator(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.textos, self.links, self.ancoras, self._ignorar = [], [], [], 0
        self._href, self._buf = None, []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._ignorar += 1
        if tag in ("a", "button", "area"):
            href = _href_do_elemento(attrs)
            if href:
                self.links.append(href)
                self._href, self._buf = href, []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._ignorar:
            self._ignorar -= 1
        if tag in ("a", "button", "area") and self._href is not None:
            self.ancoras.append((self._href, " ".join(self._buf)[:80]))
            self._href, self._buf = None, []

    def handle_data(self, data):
        if self._href is not None and data.strip():
            self._buf.append(data.strip())
        if not self._ignorar and data.strip():
            self.textos.append(data.strip())


@dataclass
class ResultadoSite:
    url_inicial: str
    paginas: list = field(default_factory=list)  # [(url, texto)]
    cnpjs: dict = field(default_factory=dict)     # cnpj -> url onde apareceu
    emails: dict = field(default_factory=dict)
    telefones: dict = field(default_factory=dict)
    erros: list = field(default_factory=list)
    whatsapp: dict = field(default_factory=dict)  # número -> URL original do link publicado
    links_whatsapp: list = field(default_factory=list)  # link publicado sem número identificável
    rotulos: dict = field(default_factory=dict)   # valor -> rótulo de departamento escrito perto (até 60 caracteres antes)
    emails_revisar: dict = field(default_factory=dict)  # publicado com forma incompleta; não é corrigido
    diagnostico: list = field(default_factory=list)
    estruturado: dict = field(default_factory=dict)


def _rotulo(texto, inicio):
    trecho = texto[max(0, inicio - 60):inicio]
    m = list(_ROTULOS.finditer(trecho))
    return m[-1].group(1).lower() if m else ""


def _juntar_ldjson(html):
    """Nome, documento, telefone e endereço publicados em dados estruturados. O rodapé em texto continua."""
    extras = []
    for bruto in _RE_LDJSON.findall(html or ""):
        try:
            import json
            dado = json.loads(bruto)
        except Exception:
            continue
        pilha = dado if isinstance(dado, list) else [dado]
        while pilha:
            item = pilha.pop(0)
            if isinstance(item, list):
                pilha.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            for chave in ("@graph", "mainEntity", "department"):
                filho = item.get(chave)
                if isinstance(filho, list):
                    pilha.extend(filho)
                elif isinstance(filho, dict):
                    pilha.append(filho)
            for chave in ("name", "legalName", "taxID", "vatID", "telephone", "email"):
                valor = item.get(chave)
                if isinstance(valor, str) and valor.strip():
                    extras.append(valor.strip())
            endereco = item.get("address")
            if isinstance(endereco, dict):
                extras.append(" ".join(str(endereco.get(k) or "") for k in (
                    "streetAddress", "addressLocality", "addressRegion", "postalCode")))
            elif isinstance(endereco, str):
                extras.append(endereco)
    return " ".join(x for x in extras if x)


def classificar_leitura(html, texto, erro=""):
    """http, vazio, bloqueio, render ou ok. Conteúdo vazio não é empresa inexistente."""
    baixo = f"{erro or ''} {(html or '')[:1500]}".lower()
    if any(x in (erro or "") for x in ("HTTP 401", "HTTP 403", "HTTP 429")) or "captcha" in baixo:
        return "bloqueio"
    if (erro or "").startswith("HTTP"):
        return "http"
    if "cloudflare" in baixo and len(texto or "") < 400:
        return "bloqueio"
    if not (texto or "").strip():
        if (html or "").lower().count("<script") >= 3:
            return "render"
        return "vazio"
    if len(texto or "") < 180 and (html or "").lower().count("<script") >= 5:
        return "render"
    return "ok"


def emails_a_revisar(texto):
    """Forma publicada incompleta. O texto original é mantido, sem completar o domínio."""
    saida = []
    for m in _RE_EMAIL_FRACO.finditer(texto or ""):
        bruto = m.group(0)
        if _RE_EMAIL.fullmatch(bruto) or bruto.lower().endswith((".png", ".jpg", ".gif", ".webp", ".svg")):
            continue
        if bruto not in saida:
            saida.append(bruto)
    return saida


def telefones_do_texto(texto):
    """Telefones com DDD explícito. O 9 do celular pode vir separado, sem alterar os dígitos."""
    saida = []
    for ddd, p1, p2 in _RE_TEL.findall(texto or ""):
        if not (11 <= int(ddd) <= 99):
            continue
        num = re.sub(r"\D", "", f"{ddd}{p1}{p2}")
        if num not in saida:
            saida.append(num)
    return saida


def trecho_relevante(texto, cnpjs=()):
    """Trecho que preserva o CNPJ ou, na falta dele, o começo e o rodapé."""
    texto = texto or ""
    for cnpj in cnpjs or []:
        pos = texto.find(cnpj)
        if pos < 0 and len(cnpj) == 14:
            fmt = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
            pos = texto.find(fmt)
        if pos >= 0:
            return texto[max(0, pos - 220):pos + 180]
    if len(texto) <= 1200:
        return texto
    return texto[:600] + " … " + texto[-600:]


def numero_whatsapp(url):
    """Dígitos do link publicado, ou '' quando a URL não traz um número identificável."""
    m = _RE_WHATS.search(url or "")
    if not m:
        return ""
    bruto = re.sub(r"\D", "", m.group(1))
    if bruto.startswith("55") and len(bruto) > 11:
        bruto = bruto[2:]
    return bruto if 10 <= len(bruto) <= 13 else ""


def links_whatsapp_no_html(html):
    """URLs wa.me / api.whatsapp publicadas no HTML, inclusive em atributo ou script."""
    vistos, saida = set(), []
    for bruto in _RE_URL_WHATS.findall(html or ""):
        url = bruto.rstrip(").,;\"'")
        if url in vistos:
            continue
        vistos.add(url)
        saida.append(url)
    return saida


def extrair(html, url_base):
    ex = _Extrator()
    try:
        ex.feed(html)
    except Exception:
        pass
    texto = re.sub(r"\s+", " ", " ".join(ex.textos + [_juntar_ldjson(html)]))
    links, marcados = [], set()
    for href in list(ex.links) + links_whatsapp_no_html(html):
        absoluto = href if href.startswith("http") else urljoin(url_base, href)
        if "wa.me/" in absoluto.lower() or "whatsapp.com/" in absoluto.lower():
            if absoluto in marcados:
                continue
            marcados.add(absoluto)
            numero = numero_whatsapp(absoluto)
            if numero:
                texto += f" [whatsapp:{numero}|{absoluto}]"
            else:
                texto += f" [whatsapp_link:{absoluto}]"
            continue
        if href.startswith("mailto:"):
            texto += " " + href[7:].split("?")[0]
            continue
        if href.startswith("tel:"):
            texto += " " + href[4:]
            continue
        links.append(urljoin(url_base, href))
    return texto, links, ex.ancoras


class SitePublico:
    codigo = "site_publico"
    MAX_PAGINAS = 5

    def __init__(self, fonte):
        self.fonte = fonte
        self._nav = None

    def analisar(self, url, ctx):
        if self._nav is None:
            self._nav = NavegadorSeguro(ClienteFonte(self.fonte, ctx))
        if "://" not in url:
            url = "https://" + url
        res = ResultadoSite(url_inicial=url)
        dominio = dominio_de_url(url)
        fila, visitadas = [url], set()
        while fila and len(res.paginas) < self.MAX_PAGINAS:
            atual = fila.pop(0)
            if atual in visitadas:
                continue
            visitadas.add(atual)
            try:
                final, html = self._nav.obter(atual)
            except (EnderecoBloqueado, PermissionError, ConnectionError) as e:
                classe = classificar_leitura("", "", str(e))
                res.diagnostico.append({"url": atual, "classe": classe, "detalhe": str(e)[:160]})
                res.erros.append(f"{atual}: {classe}: {e}")
                if atual.startswith("https://") and atual == url and not res.paginas:
                    fila.insert(0, "http://" + atual[8:])  # alguns sites só respondem em http
                continue
            except Exception as e:  # SSL, timeout etc. (as exceções de limite sobem)
                from prospeccao.conectores.base import ExecucaoCancelada, FonteIndisponivel, OrcamentoEsgotado
                if isinstance(e, (OrcamentoEsgotado, ExecucaoCancelada)):
                    raise
                res.erros.append(f"{atual}: {type(e).__name__}: {str(e)[:150]}")
                if isinstance(e, FonteIndisponivel):
                    break
                continue
            texto, links, ancoras = extrair(html, final)
            classe = classificar_leitura(html, texto)
            res.diagnostico.append({"url": final, "classe": classe})
            if classe == "render" and "whatsapp:" not in texto and "CNPJ" not in texto.upper():
                res.erros.append(
                    f"{final}: conteúdo depende de renderização; o HTML recebido não traz a ficha nem o destino do botão")
            elif classe == "render":
                res.erros.append(f"{final}: parte da página depende de renderização; o que estava no HTML foi preservado")
            res.paginas.append((final, texto[:200_000]))
            for bruto in emails_a_revisar(texto):
                res.emails_revisar.setdefault(bruto, final)
            maiusc = texto.upper()
            for m in _RE_CNPJ.finditer(maiusc):
                bruto = m.group(1)
                formatado = "/" in bruto or bruto.count(".") >= 2
                if not formatado and "CNPJ" not in maiusc[max(0, m.start() - 30):m.start()]:
                    continue
                doc = cnpj_srv.analisar(bruto)
                if doc.cnpj:
                    res.cnpjs.setdefault(doc.cnpj, final)
            for m in _RE_EMAIL.finditer(texto):
                e = m.group(0)
                if not e.lower().endswith((".png", ".jpg", ".gif", ".webp", ".svg")):
                    res.emails.setdefault(e.lower(), final)
                    res.rotulos.setdefault(e.lower(), _rotulo(texto, m.start()))
            for num in telefones_do_texto(texto):
                res.telefones.setdefault(num, final)
                pos = texto.find(num[-4:])
                res.rotulos.setdefault(num, _rotulo(texto, pos if pos >= 0 else 0))
            for numero, url in re.findall(r"\[whatsapp:(\d{10,13})\|([^\]]+)\]", texto):
                res.whatsapp.setdefault(numero, url)
            for url in re.findall(r"\[whatsapp_link:([^\]]+)\]", texto):
                if url not in res.links_whatsapp:
                    res.links_whatsapp.append(url)
            if len(res.paginas) == 1:
                for link in links:
                    caminho = urlsplit(link).path or ""
                    texto_botao = next((t for h, t in ancoras if urljoin(final, h).split("#")[0] == link.split("#")[0]), "")
                    if dominio_de_url(link) == dominio and (
                            _LINKS_INTERESSE.search(caminho) or _TEXTO_BOTAO.search(texto_botao)) \
                            and link not in visitadas and len(fila) < 6:
                        fila.append(link.split("#")[0])
        return res
