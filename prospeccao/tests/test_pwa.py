"""PWA: arquivos públicos, manifesto do Sondagem BRIDA, service worker sem cache de páginas e botão no menu."""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from prospeccao import papeis


class PwaTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.adm = U.objects.create_user("adm", password="x")
        papeis.definir_perfil(self.adm, "ADM")
        self.inside = U.objects.create_user("ana", password="x")
        papeis.definir_perfil(self.inside, "INSIDE")

    def _publicos_sem_redirecionar(self):
        for url, tipo in (("/manifest.webmanifest", "application/manifest+json"),
                          ("/sw.js", "application/javascript"), ("/offline/", "text/html")):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertTrue(r["Content-Type"].startswith(tipo), url)

    def test_arquivos_publicos_para_anonimo_e_para_inside_sales(self):
        self._publicos_sem_redirecionar()
        self.client.force_login(self.inside)  # o middleware de papéis não pode mandar para a tela dele
        self._publicos_sem_redirecionar()

    def test_manifesto(self):
        m = json.loads(self.client.get("/manifest.webmanifest").content)
        self.assertEqual((m["name"], m["short_name"], m["id"], m["start_url"], m["scope"], m["display"]),
                         ("Sondagem BRIDA", "Sondagem", "/", "/", "/", "standalone"))
        tamanhos = {(i["sizes"], i["purpose"]) for i in m["icons"]}
        self.assertEqual(tamanhos, {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")})
        self.assertTrue(all("prospeccao" in i["src"] for i in m["icons"]))

    def test_service_worker_nao_guarda_paginas_nem_escrita(self):
        r = self.client.get("/sw.js")
        js = r.content.decode()
        self.assertEqual(r["Service-Worker-Allowed"], "/")
        self.assertIn("no-cache", r["Cache-Control"])
        self.assertIn('if (req.method !== "GET") return;', js)
        self.assertIn("PERMITIDOS.has(url.pathname)", js)
        precache = json.loads(js.split("const PRECACHE = ")[1].split(";\n")[0])
        self.assertTrue(precache and all(u.startswith("/static/") and u.endswith(".png") for u in precache))
        self.assertNotIn("c.put(", js)  # nada é guardado em tempo de navegação

    def test_botao_instalar_no_menu_e_nao_no_login(self):
        self.assertNotContains(self.client.get("/entrar/"), 'id="pwa-instalar"')
        for u in (self.adm, self.inside):
            self.client.force_login(u)
            html = self.client.get("/prospeccao/oportunidades/" if u is self.adm else "/inside-sales/", follow=True).content.decode()
            i_botao, i_rodape = html.index('id="pwa-instalar"'), html.index('class="nav-rodape"')
            self.assertLess(i_botao, i_rodape)  # acima do usuário e do Sair
            self.assertIn('rel="manifest"', html)
            self.client.logout()
