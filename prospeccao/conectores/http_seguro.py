"""
Acesso seguro a URLs externas (sites de empresas).

Proteções:
  - só http/https, portas 80/443, sem usuário/senha na URL;
  - o IP é validado NO MOMENTO DA CONEXÃO (impede DNS rebinding): bloqueia loopback, redes privadas,
    link-local (inclui 169.254.169.254, metadados de nuvem), multicast, reservados;
  - redirecionamentos seguidos manualmente (máx. 3), cada salto revalidado;
  - tamanho máximo da resposta e só conteúdo HTML/texto;
  - robots.txt respeitado (se proibir, a página não é lida).
O conteúdo obtido é DADO NÃO CONFIÁVEL: é apenas analisado por expressões regulares; nunca é
interpretado como instrução nem executado.
"""
import ipaddress
import socket
import threading
import urllib.robotparser
from urllib.parse import urljoin, urlsplit

import urllib3.util.connection as u3conn

PORTAS_PERMITIDAS = {80, 443}
TAMANHO_MAXIMO = 1_500_000
_local = threading.local()
_create_connection_original = u3conn.create_connection


class EnderecoBloqueado(Exception):
    pass


def ip_permitido(ip):
    ip = ipaddress.ip_address(ip)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local
                                 or ip.is_private or ip.is_unspecified)


def _create_connection_protegida(address, *args, **kwargs):
    if not getattr(_local, "ativo", False):
        return _create_connection_original(address, *args, **kwargs)
    host, port = address
    if port not in PORTAS_PERMITIDAS:
        raise EnderecoBloqueado(f"porta {port} não permitida")
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    validos = [sa[0] for *_, sa in infos if ip_permitido(sa[0])]
    if not validos or len(validos) != len(infos):
        raise EnderecoBloqueado(f"{host} resolve para endereço interno/não público")
    # conecta no IP já validado (o TLS continua verificando o nome do host)
    return _create_connection_original((validos[0], port), *args, **kwargs)


u3conn.create_connection = _create_connection_protegida


def validar_url(url):
    partes = urlsplit(url)
    if partes.scheme not in ("http", "https"):
        raise EnderecoBloqueado(f"esquema '{partes.scheme}' não permitido")
    if partes.username or partes.password:
        raise EnderecoBloqueado("URL com credenciais não é permitida")
    if not partes.hostname:
        raise EnderecoBloqueado("URL sem host")
    porta = partes.port or (443 if partes.scheme == "https" else 80)
    if porta not in PORTAS_PERMITIDAS:
        raise EnderecoBloqueado(f"porta {porta} não permitida")
    try:
        ipaddress.ip_address(partes.hostname)
        if not ip_permitido(partes.hostname):
            raise EnderecoBloqueado("IP interno/não público")
    except ValueError:
        pass  # é nome; validado na conexão
    return url


class NavegadorSeguro:
    def __init__(self, cliente_fonte):
        self.cliente = cliente_fonte  # ClienteFonte: limites, intervalo, retentativas, contagem
        self._robots = {}

    def _get(self, url):
        validar_url(url)
        _local.ativo = True
        try:
            resp = self.cliente.requisitar("GET", url, allow_redirects=False, stream=True,
                                           headers={"Accept": "text/html,text/plain;q=0.9"})
            dados = b""
            for bloco in resp.iter_content(65536):
                dados += bloco
                if len(dados) > TAMANHO_MAXIMO:
                    break
            resp.close()
            return resp, dados[:TAMANHO_MAXIMO]
        finally:
            _local.ativo = False

    def _robots_permite(self, url):
        p = urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                resp, dados = self._get(base + "/robots.txt")
                if resp.status_code in (401, 403):
                    rp.disallow_all = True
                elif resp.status_code >= 400 or resp.is_redirect:
                    rp.allow_all = True
                else:
                    rp.parse(dados.decode("utf-8", "replace").splitlines())
            except EnderecoBloqueado:
                raise
            except Exception:
                rp.allow_all = True  # robots inacessível: padrão do protocolo é permitir
            self._robots[base] = rp
        from prospeccao.conectores.base import USER_AGENT
        return self._robots[base].can_fetch(USER_AGENT, url)

    def obter(self, url, max_redirecionamentos=3):
        """Retorna (url_final, html_texto) ou levanta EnderecoBloqueado/PermissionError."""
        for _ in range(max_redirecionamentos + 1):
            if not self._robots_permite(url):
                raise PermissionError(f"robots.txt não permite {url}")
            resp, dados = self._get(url)
            if resp.is_redirect and resp.headers.get("Location"):
                url = urljoin(url, resp.headers["Location"])
                continue
            if resp.status_code >= 400:
                raise ConnectionError(f"HTTP {resp.status_code} em {url}")
            tipo = resp.headers.get("Content-Type", "")
            if tipo and not any(t in tipo for t in ("text/html", "text/plain", "application/xhtml")):
                raise ConnectionError(f"conteúdo não textual ({tipo})")
            if "charset=" in tipo.lower():
                return url, dados.decode(resp.encoding or "utf-8", "replace")
            try:
                return url, dados.decode("utf-8")
            except UnicodeDecodeError:
                return url, dados.decode("cp1252", "replace")
        raise ConnectionError("redirecionamentos demais")
