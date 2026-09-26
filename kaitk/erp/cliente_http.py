# -*- coding: utf-8 -*-
"""Cliente HTTP para API de ERP juridico, com os cuidados que a pratica ensinou.

- Token renovado ANTES de vencer (margem configuravel) e de novo, uma vez, se a API responder 401.
  Execucao longa com token curto e a causa classica de caso gravado pela metade.
- 429 respeita o Retry-After (segundos ou data HTTP); sem ele, espera exponencial com teto.
- 5xx em leitura (GET, PUT, DELETE) e repetido. 5xx em escrita nao idempotente (POST, PATCH) NAO e
  repetido: a API pode ter gravado e respondido erro. Sobe EscritaIncerta para quem chamou conferir.
- Paginacao com teto: passar do teto e erro, nao corte silencioso da lista.
- `conferir_gravacao` le o registro de volta e aponta campo que a API aceitou e descartou calada.
- Cada chamada (inclusive as repetidas) vira linha no registro de consumo, se houver um.

So biblioteca padrao. Credencial nunca e escrita no codigo: vem de quem chama (variavel de ambiente,
cofre), pela funcao `obter_token`.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

IDEMPOTENTES = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})


class ErroERP(Exception):
    def __init__(self, mensagem, status=None, corpo=None):
        super().__init__(mensagem)
        self.status = status
        self.corpo = corpo


class EscritaIncerta(ErroERP):
    """Escrita respondeu erro de servidor. Pode ter gravado: confira no destino antes de repetir."""


class TetoDePaginas(ErroERP):
    """A listagem passou do teto de paginas. Falha fechado em vez de devolver lista cortada."""


@dataclass
class Resposta:
    status: int
    dados: object
    cabecalhos: dict = field(default_factory=dict)


class Token:
    """Guarda o token e renova quando falta menos que `margem` segundos para vencer.

    `obter` e uma funcao sem argumentos que devolve (token, validade_em_segundos).
    """

    def __init__(self, obter, margem=120.0, relogio=time.monotonic):
        self._obter = obter
        self._margem = margem
        self._relogio = relogio
        self._valor = None
        self._vence_em = 0.0
        self.renovacoes = 0

    def valor(self):
        if self._valor is None or self._relogio() >= self._vence_em - self._margem:
            self.renovar()
        return self._valor

    def renovar(self):
        valor, validade = self._obter()
        if not valor:
            raise ErroERP("obter_token devolveu token vazio")
        self._valor = valor
        self._vence_em = self._relogio() + float(validade)
        self.renovacoes += 1
        return self._valor


def token_por_client_credentials(url_token, cliente_id, segredo, escopo=None, timeout=30):
    """Funcao `obter` para o fluxo OAuth client credentials. Passe o segredo lido do ambiente ou do cofre."""

    def obter():
        form = {"grant_type": "client_credentials", "client_id": cliente_id, "client_secret": segredo}
        if escopo:
            form["scope"] = escopo
        req = urllib.request.Request(url_token, data=urllib.parse.urlencode(form).encode(), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            dados = json.loads(resp.read())
        return dados["access_token"], dados.get("expires_in", 3600)

    return obter


def segundos_retry_after(valor, agora=time.time):
    """Converte o cabecalho Retry-After (segundos ou data HTTP) em segundos de espera. None se invalido."""
    if valor is None:
        return None
    valor = str(valor).strip()
    if valor.isdigit():
        return float(valor)
    try:
        return max(0.0, parsedate_to_datetime(valor).timestamp() - agora())
    except (TypeError, ValueError, IndexError):
        return None


class ClienteERP:
    def __init__(self, base_url, obter_token, *, consumo=None, etiquetas=None, max_tentativas=5,
                 espera_base=1.0, espera_maxima=60.0, teto_paginas=150, margem_token=120.0, timeout=30,
                 relogio=time.monotonic, dormir=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.token = Token(obter_token, margem=margem_token, relogio=relogio)
        self.consumo = consumo
        self.etiquetas = dict(etiquetas or {})
        self.max_tentativas = max_tentativas
        self.espera_base = espera_base
        self.espera_maxima = espera_maxima
        self.teto_paginas = teto_paginas
        self.timeout = timeout
        self._relogio = relogio
        self._dormir = dormir

    # ------------------------------------------------------------ baixo nivel
    def _url(self, caminho, params):
        url = caminho if caminho.startswith(("http://", "https://")) else self.base_url + "/" + caminho.lstrip("/")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, safe="$@")
        return url

    def _http(self, metodo, url, corpo):
        dados = json.dumps(corpo).encode() if corpo is not None else None
        cab = {"Authorization": "Bearer " + self.token.valor(), "Accept": "application/json"}
        if dados is not None:
            cab["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=dados, method=metodo, headers=cab)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def _espera(self, tentativa, retry_after=None):
        segundos = segundos_retry_after(retry_after)
        if segundos is None:
            segundos = self.espera_base * (2 ** (tentativa - 1))
        return min(segundos, self.espera_maxima)

    @staticmethod
    def _decodificar(bruto):
        if not bruto:
            return None
        try:
            return json.loads(bruto)
        except ValueError:
            return bruto.decode("utf-8", "replace")

    # ------------------------------------------------------------ chamada
    def requisitar(self, metodo, caminho, *, params=None, corpo=None, etiquetas=None):
        metodo = metodo.upper()
        url = self._url(caminho, params)
        tentativa, renovou = 0, False
        while True:
            tentativa += 1
            inicio = self._relogio()
            status, bruto, cab = self._http(metodo, url, corpo)
            if self.consumo is not None:
                self.consumo.registrar(metodo, caminho, status, (self._relogio() - inicio) * 1000, tentativa,
                                       dict(self.etiquetas, **(etiquetas or {})))
            if status == 401 and not renovou:
                self.token.renovar()
                renovou = True
                continue
            if status == 429 and tentativa < self.max_tentativas:
                self._dormir(self._espera(tentativa, cab.get("Retry-After")))
                continue
            if status >= 500:
                if metodo not in IDEMPOTENTES:
                    raise EscritaIncerta("%s %s respondeu %d: pode ter gravado. Confira no destino antes de repetir."
                                         % (metodo, caminho, status), status, self._decodificar(bruto))
                if tentativa < self.max_tentativas:
                    self._dormir(self._espera(tentativa))
                    continue
            if status >= 400:
                raise ErroERP("%s %s respondeu %d" % (metodo, caminho, status), status, self._decodificar(bruto))
            return Resposta(status, self._decodificar(bruto), cab)

    def get(self, caminho, **kw):
        return self.requisitar("GET", caminho, **kw)

    def post(self, caminho, corpo, **kw):
        return self.requisitar("POST", caminho, corpo=corpo, **kw)

    def patch(self, caminho, corpo, **kw):
        return self.requisitar("PATCH", caminho, corpo=corpo, **kw)

    # ------------------------------------------------------------ listas e conferencia
    def paginar(self, caminho, *, params=None, tamanho=100, chave_itens="value", teto_paginas=None):
        """Percorre uma listagem no estilo OData ($top/$skip e @odata.nextLink), item a item.

        Se ainda houver pagina depois do teto, levanta TetoDePaginas: melhor parar e avisar do que
        entregar uma lista cortada que parece completa.
        """
        teto = teto_paginas or self.teto_paginas
        proximo, pulo, paginas = None, 0, 0
        while True:
            if proximo:
                resp = self.get(proximo)
            else:
                p = dict(params or {})
                p.update({"$top": tamanho, "$skip": pulo})
                resp = self.get(caminho, params=p)
            paginas += 1
            dados = resp.dados or {}
            itens = dados.get(chave_itens, []) if isinstance(dados, dict) else []
            yield from itens
            proximo = dados.get("@odata.nextLink") if isinstance(dados, dict) else None
            tem_mais = bool(proximo) or len(itens) >= tamanho
            if not tem_mais:
                return
            if paginas >= teto:
                # pagina cheia no teto nao prova que ha mais: sem nextLink, sonda um item adiante
                if not proximo:
                    p = dict(params or {})
                    p.update({"$top": 1, "$skip": pulo + tamanho})
                    sonda = self.get(caminho, params=p).dados or {}
                    if not (isinstance(sonda, dict) and sonda.get(chave_itens)):
                        return
                raise TetoDePaginas("%s passou de %d paginas; aumente o teto se o volume for esperado"
                                    % (caminho, teto))
            if not proximo:
                pulo += tamanho

    def conferir_gravacao(self, caminho, esperado):
        """Le o registro e devolve {campo: (esperado, lido)} do que nao bate. Vazio = gravou tudo."""
        lido = self.get(caminho).dados or {}
        return {c: (v, lido.get(c)) for c, v in esperado.items() if lido.get(c) != v}
