"""PWA: manifesto, service worker (servido na raiz para valer em todo o site) e página offline.

O service worker não guarda páginas do sistema (têm dados do usuário e token CSRF): navegação vai sempre à rede e,
sem conexão, mostra a página offline. Só os arquivos estáticos (ícones, logos) ficam em cache.
"""
import json

from django.http import HttpResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.cache import cache_control

# Troque ao mudar o service worker ou os arquivos pré-carregados: o navegador descarta o cache antigo.
VERSAO = "brida-pwa-v1"

TEMA = "#21275A"


@cache_control(max_age=3600, public=True)
def manifesto(request):
    dados = {
        "name": "Brida · Prospecção",
        "short_name": "Brida",
        "description": "Prospecção, Inside Sales e carteira da Brida Distribuidora.",
        "lang": "pt-BR",
        "dir": "ltr",
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "theme_color": TEMA,
        "background_color": "#ffffff",
        "icons": [
            {"src": static("brida/pwa/icone-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("brida/pwa/icone-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static("brida/pwa/icone-maskable-512.png"), "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    return HttpResponse(json.dumps(dados, ensure_ascii=False), content_type="application/manifest+json; charset=utf-8")


SW = """/* Service worker da Brida (gerado por config/pwa.py) */
const VERSAO = %(versao)s;
const OFFLINE = %(offline)s;
const PRECACHE = %(precache)s;

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSAO).then((c) => c.addAll([OFFLINE, ...PRECACHE])).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((nomes) => Promise.all(nomes.filter((n) => n !== VERSAO).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Páginas: sempre da rede (dados do usuário); sem conexão, a página offline.
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }

  // Arquivos estáticos: responde do cache e atualiza em segundo plano.
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.open(VERSAO).then((c) =>
        c.match(req).then((guardado) => {
          const rede = fetch(req).then((resp) => {
            if (resp && resp.ok) c.put(req, resp.clone());
            return resp;
          }).catch(() => guardado);
          return guardado || rede;
        })
      )
    );
  }
});
"""


@cache_control(no_cache=True)
def service_worker(request):
    precache = [static(p) for p in (
        "brida/pwa/icone-192.png", "brida/img/marca-b.png",
        "brida/img/logo-brida-negativo.svg", "brida/img/marca-b-negativo.svg", "brida/img/logo-brida.svg",
    )]
    corpo = SW % {"versao": json.dumps(VERSAO), "offline": json.dumps("/offline/"), "precache": json.dumps(precache)}
    resp = HttpResponse(corpo, content_type="application/javascript; charset=utf-8")
    resp["Service-Worker-Allowed"] = "/"
    return resp


def offline(request):
    return render(request, "pwa/offline.html", {"tema": TEMA})
