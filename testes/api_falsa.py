# -*- coding: utf-8 -*-
"""API de ERP falsa, local, que reproduz os defeitos que um cliente de verdade precisa aguentar.

- /oauth/token emite tokens numerados; tokens abaixo de `token_minimo` recebem 401 (vencidos);
- /processos lista com $top/$skip, ou com @odata.nextLink se `usar_next_link`;
- /limitado responde 429 com Retry-After nas primeiras `falhas_429` chamadas;
- /instavel responde 503 nas primeiras `falhas_503` chamadas;
- POST /processos GRAVA e, se `post_grava_e_falha`, responde 500 mesmo assim;
- PATCH /processos/{id} responde 204 e descarta em silencio os campos de `campos_ignorados`.
"""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Estado:
    def __init__(self):
        self.tokens_emitidos = 0
        self.token_minimo = 1
        self.validade = 3600
        self.processos = {i: {"id": i, "pasta": "P%04d" % i, "fase": "inicial"} for i in range(1, 251)}
        self.usar_next_link = False
        self.falhas_429 = 0
        self.retry_after = "2"
        self.falhas_503 = 0
        self.post_grava_e_falha = False
        self.campos_ignorados = {"observacao"}
        self.chamadas = []


class _Manipulador(BaseHTTPRequestHandler):
    estado = None  # preenchido pela fabrica

    def log_message(self, *args):  # silencio nos testes
        pass

    def _responder(self, status, corpo=None, cab=None):
        dados = json.dumps(corpo).encode() if corpo is not None else b""
        self.send_response(status)
        for k, v in (cab or {}).items():
            self.send_header(k, v)
        if dados:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def _corpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _autorizado(self):
        cab = self.headers.get("Authorization", "")
        try:
            numero = int(cab.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return False
        return numero >= self.estado.token_minimo

    def _rota(self):
        partes = urllib.parse.urlsplit(self.path)
        return partes.path, dict(urllib.parse.parse_qsl(partes.query))

    def _tratar(self, metodo):
        e = self.estado
        caminho, q = self._rota()
        e.chamadas.append((metodo, caminho))
        if caminho == "/oauth/token" and metodo == "POST":
            form = dict(urllib.parse.parse_qsl(self._corpo().decode()))
            if form.get("grant_type") != "client_credentials":
                return self._responder(400, {"erro": "grant_type"})
            e.tokens_emitidos += 1
            return self._responder(200, {"access_token": "tok-%d" % e.tokens_emitidos, "expires_in": e.validade})
        if not self._autorizado():
            return self._responder(401, {"erro": "token invalido ou vencido"})
        if caminho == "/limitado":
            if e.falhas_429 > 0:
                e.falhas_429 -= 1
                return self._responder(429, {"erro": "limite"}, {"Retry-After": e.retry_after})
            return self._responder(200, {"ok": True})
        if caminho == "/instavel":
            if e.falhas_503 > 0:
                e.falhas_503 -= 1
                return self._responder(503, {"erro": "indisponivel"})
            return self._responder(200, {"ok": True})
        if caminho == "/processos" and metodo == "GET":
            itens = [e.processos[k] for k in sorted(e.processos)]
            top, skip = int(q.get("$top", 100)), int(q.get("$skip", 0))
            pagina = itens[skip:skip + top]
            corpo = {"value": pagina}
            if e.usar_next_link and skip + top < len(itens):
                host = "http://%s:%d" % self.server.server_address[:2]
                corpo["@odata.nextLink"] = host + "/processos?$top=%d&$skip=%d" % (top, skip + top)
            return self._responder(200, corpo)
        if caminho == "/processos" and metodo == "POST":
            novo = json.loads(self._corpo() or b"{}")
            novo["id"] = max(e.processos) + 1
            e.processos[novo["id"]] = novo
            if e.post_grava_e_falha:
                return self._responder(500, {"erro": "falha interna"})
            return self._responder(201, novo)
        if caminho.startswith("/processos/"):
            try:
                pid = int(caminho.rsplit("/", 1)[1])
            except ValueError:
                return self._responder(404, {"erro": "nao encontrado"})
            if pid not in e.processos:
                return self._responder(404, {"erro": "nao encontrado"})
            if metodo == "GET":
                return self._responder(200, e.processos[pid])
            if metodo == "PATCH":
                mudancas = json.loads(self._corpo() or b"{}")
                for k, v in mudancas.items():
                    if k not in e.campos_ignorados:
                        e.processos[pid][k] = v
                return self._responder(204)
        return self._responder(404, {"erro": "rota inexistente"})

    def do_GET(self):
        self._tratar("GET")

    def do_POST(self):
        self._tratar("POST")

    def do_PATCH(self):
        self._tratar("PATCH")


class ApiFalsa:
    """Uso: with ApiFalsa() as api: api.url, api.estado."""

    def __init__(self):
        self.estado = Estado()
        manipulador = type("Manipulador", (_Manipulador,), {"estado": self.estado})
        self._servidor = ThreadingHTTPServer(("127.0.0.1", 0), manipulador)
        self.url = "http://127.0.0.1:%d" % self._servidor.server_address[1]
        self._thread = threading.Thread(target=self._servidor.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._servidor.shutdown()
        self._servidor.server_close()
