"""PWA do Sondagem BRIDA: manifesto, service worker (na raiz, escopo do site inteiro) e página offline.

Objetivo: permitir instalar o sistema como aplicativo. Não há operação offline.
- O service worker guarda em cache SÓ a lista fechada de PRECACHE (ícones e a página "Sem conexão").
- Páginas vão sempre à rede (têm dados de clientes e token CSRF); sem conexão, mostra a página offline.
- Requisições de escrita (POST etc.), buscas, exportações e qualquer outro arquivo não são interceptados.
- A versão do cache é calculada pelo conteúdo dos arquivos guardados + o código do service worker: mudou algo,
  o navegador instala a versão nova e apaga os caches antigos DESTE aplicativo (prefixo próprio).
"""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from django.contrib.staticfiles import finders
from django.http import HttpResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.cache import cache_control

NOME = "Sondagem BRIDA"
NOME_CURTO = "Sondagem"
TEMA = "#21275A"
PREFIXO_CACHE = "sondagem-brida-pwa-"
PREFIXOS_ANTIGOS = ("brida-pwa-",)  # versão anterior deste mesmo aplicativo

ICONES = {
    "192": "brida/pwa/prospeccao-192.png",
    "512": "brida/pwa/prospeccao-512.png",
    "maskable": "brida/pwa/prospeccao-maskable-512.png",
    "apple": "brida/pwa/prospeccao-apple-180.png",
    "favicon": "brida/pwa/prospeccao-32.png",
}
# Arquivos estáticos que o service worker pode guardar (nada além disso).
PRECACHE_ESTATICOS = (ICONES["192"], ICONES["512"], ICONES["favicon"], "brida/img/marca-b.png")
OFFLINE_URL = "/offline/"


@cache_control(max_age=3600, public=True)
def manifesto(request):
    dados = {
        "name": NOME,
        "short_name": NOME_CURTO,
        "description": "Prospecção de novos clientes, Inside Sales e carteira da BRIDA.",
        "lang": "pt-BR",
        "dir": "ltr",
        "id": "/",  # identificador estável: não mudar, senão o navegador trata como outro aplicativo
        "start_url": "/",  # com sessão abre o sistema; sem sessão o próprio site leva ao login
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "theme_color": TEMA,
        "background_color": "#ffffff",
        "categories": ["business", "productivity"],
        "icons": [
            {"src": static(ICONES["192"]), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static(ICONES["512"]), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static(ICONES["maskable"]), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        # permite ao Chrome/Edge informar (navigator.getInstalledRelatedApps) se este app já está instalado
        "related_applications": [{"platform": "webapp", "url": request.build_absolute_uri("/manifest.webmanifest")}],
        "prefer_related_applications": False,
    }
    return HttpResponse(json.dumps(dados, ensure_ascii=False), content_type="application/manifest+json; charset=utf-8")


SW = """/* Service worker do Sondagem BRIDA (gerado por config/pwa.py). Só instalação; sem operação offline. */
const VERSAO = %(versao)s;
const PREFIXO = %(prefixo)s;
const ANTIGOS = %(antigos)s;
const OFFLINE = %(offline)s;
const PRECACHE = %(precache)s;
const PERMITIDOS = new Set(PRECACHE);

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(VERSAO)
      .then((c) => c.addAll([OFFLINE, ...PRECACHE].map((u) => new Request(u, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  // apaga só caches deste aplicativo que não sejam a versão atual
  e.waitUntil(
    caches.keys()
      .then((nomes) => Promise.all(nomes
        .filter((n) => n !== VERSAO && (n.startsWith(PREFIXO) || ANTIGOS.some((p) => n.startsWith(p))))
        .map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;               // escrita nunca passa pelo service worker
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (req.mode === "navigate") {                   // páginas: sempre da rede; sem conexão, aviso para reconectar
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }
  if (PERMITIDOS.has(url.pathname)) {              // só os arquivos da lista fechada
    e.respondWith(caches.open(VERSAO).then((c) => c.match(url.pathname).then((r) => r || fetch(req))));
  }
  // todo o resto (CSS, JS, buscas, exportações, anexos) segue direto para a rede, sem cache
});
"""


def _offline_template():
    return Path(__file__).resolve().parent.parent / "carteira" / "templates" / "pwa" / "offline.html"


@lru_cache(maxsize=1)
def _versao():
    h = hashlib.sha256(SW.encode())
    for caminho in PRECACHE_ESTATICOS:
        achado = finders.find(caminho)
        h.update(caminho.encode())
        if achado:
            h.update(Path(achado).read_bytes())
    h.update(_offline_template().read_bytes())
    return PREFIXO_CACHE + h.hexdigest()[:12]


@cache_control(no_cache=True, max_age=0)
def service_worker(request):
    corpo = SW % {
        "versao": json.dumps(_versao()),
        "prefixo": json.dumps(PREFIXO_CACHE),
        "antigos": json.dumps(list(PREFIXOS_ANTIGOS)),
        "offline": json.dumps(OFFLINE_URL),
        "precache": json.dumps([static(p) for p in PRECACHE_ESTATICOS]),
    }
    resp = HttpResponse(corpo, content_type="application/javascript; charset=utf-8")
    resp["Service-Worker-Allowed"] = "/"
    return resp


@cache_control(no_cache=True, max_age=0)
def offline(request):
    return render(request, "pwa/offline.html", {"tema": TEMA, "nome": NOME, "icone": ICONES["192"]})
