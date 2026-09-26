# -*- coding: utf-8 -*-
"""IA com avaliador independente: um modelo faz o trabalho, outro confere, e a divergencia vai para uma pessoa.

Independente de fornecedor: um "modelo" e qualquer funcao que recebe (sistema, prompt) e devolve
RespostaModelo(texto, tokens_entrada, tokens_saida). Voce liga o provedor que quiser por fora.

Protecoes que vieram de erro real:
- saida fora do esquema e reprovada sem gastar o avaliador;
- saida vazia ou so pontuacao (documento sem texto) e reprovada, nunca gravada;
- orcamento com teto: o lote para antes de passar do limite;
- gabarito conta quantos casos RODARAM; bateria que termina antes e diz "tudo certo" e falha.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class RespostaModelo:
    texto: str
    tokens_entrada: int = 0
    tokens_saida: int = 0


@dataclass(frozen=True)
class Precos:
    """Preco por mil tokens, na moeda que voce usar."""
    entrada_por_mil: float = 0.0
    saida_por_mil: float = 0.0

    def custo(self, r):
        return (r.tokens_entrada * self.entrada_por_mil + r.tokens_saida * self.saida_por_mil) / 1000


class OrcamentoEstourado(RuntimeError):
    pass


class Orcamento:
    def __init__(self, teto):
        self.teto = float(teto)
        self.gasto = 0.0

    def conferir(self, proximo_estimado=0.0):
        if self.gasto + proximo_estimado > self.teto:
            raise OrcamentoEstourado("gasto %.4f + proxima chamada %.4f passaria do teto %.4f"
                                     % (self.gasto, proximo_estimado, self.teto))

    def somar(self, valor):
        self.gasto += valor


@dataclass
class Decisao:
    status: str                 # "aprovado" ou "revisao"
    saida: dict | None
    motivo: str
    custo: float
    chamadas: list = field(default_factory=list)


def validar_esquema(obj, esquema):
    """esquema = {campo: tipo ou tupla de tipos}. Devolve a lista de problemas (vazia = valido)."""
    if not isinstance(obj, dict):
        return ["saida nao e um objeto JSON"]
    problemas = []
    for campo, tipo in esquema.items():
        if campo not in obj:
            problemas.append("falta o campo '%s'" % campo)
        elif not isinstance(obj[campo], tipo):
            problemas.append("campo '%s' com tipo %s" % (campo, type(obj[campo]).__name__))
    return problemas


_SO_PONTUACAO = re.compile(r"^[\s\W_]*$")


def _vazio(valor):
    return valor is None or (isinstance(valor, str) and _SO_PONTUACAO.match(valor) is not None)


def extrair_json(texto):
    """Aceita JSON puro ou dentro de bloco de codigo. None se nao houver JSON valido."""
    t = texto.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except ValueError:
        return None


class GeradorComAvaliador:
    def __init__(self, *, gerador, avaliador, prompt_gerador, prompt_avaliador, esquema,
                 sistema_gerador="", sistema_avaliador="", precos_gerador=Precos(), precos_avaliador=Precos(),
                 orcamento=None, campos_obrigatorios_nao_vazios=(), estimativa_por_chamada=0.0):
        """`prompt_gerador(entrada) -> str`; `prompt_avaliador(entrada, saida_dict) -> str`.

        O avaliador deve responder JSON {"veredito": "aprova" | "reprova", "motivo": "..."}.
        """
        self.gerador = gerador
        self.avaliador = avaliador
        self.prompt_gerador = prompt_gerador
        self.prompt_avaliador = prompt_avaliador
        self.esquema = esquema
        self.sistema_gerador = sistema_gerador
        self.sistema_avaliador = sistema_avaliador
        self.precos_gerador = precos_gerador
        self.precos_avaliador = precos_avaliador
        self.orcamento = orcamento
        self.nao_vazios = tuple(campos_obrigatorios_nao_vazios)
        self.estimativa = estimativa_por_chamada

    def _chamar(self, nome, modelo, sistema, prompt, precos, registro):
        if self.orcamento:
            self.orcamento.conferir(self.estimativa)
        r = modelo(sistema, prompt)
        custo = precos.custo(r)
        if self.orcamento:
            self.orcamento.somar(custo)
        registro.append({"etapa": nome, "tokens_entrada": r.tokens_entrada, "tokens_saida": r.tokens_saida,
                         "custo": custo})
        return r, custo

    def processar(self, entrada):
        registro, custo = [], 0.0
        r, c = self._chamar("gerador", self.gerador, self.sistema_gerador, self.prompt_gerador(entrada),
                            self.precos_gerador, registro)
        custo += c
        saida = extrair_json(r.texto)
        problemas = validar_esquema(saida, self.esquema)
        if problemas:
            return Decisao("revisao", None, "saida fora do esquema: " + "; ".join(problemas), custo, registro)
        vazios = [f for f in self.nao_vazios if _vazio(saida.get(f))]
        if vazios:
            return Decisao("revisao", saida, "campo vazio ou so pontuacao: " + ", ".join(vazios)
                           + " (documento sem texto? passe por OCR)", custo, registro)
        r, c = self._chamar("avaliador", self.avaliador, self.sistema_avaliador,
                            self.prompt_avaliador(entrada, saida), self.precos_avaliador, registro)
        custo += c
        veredito = extrair_json(r.texto)
        if not isinstance(veredito, dict) or veredito.get("veredito") not in ("aprova", "reprova"):
            return Decisao("revisao", saida, "avaliador respondeu fora do formato", custo, registro)
        if veredito["veredito"] == "reprova":
            return Decisao("revisao", saida, "avaliador reprovou: " + str(veredito.get("motivo", "")), custo,
                           registro)
        return Decisao("aprovado", saida, str(veredito.get("motivo", "")), custo, registro)


@dataclass
class RelatorioGabarito:
    total: int
    executados: int
    aprovados_corretos: int
    aprovados_errados: int      # o numero que importa: erro que passaria sem ninguem ver
    enviados_revisao: int
    custo: float

    @property
    def acerto_dos_aprovados(self):
        aprovados = self.aprovados_corretos + self.aprovados_errados
        return self.aprovados_corretos / aprovados if aprovados else None


class BateriaIncompleta(RuntimeError):
    pass


def medir_gabarito(pipeline, casos, comparar=lambda saida, esperado: saida == esperado):
    """Roda o pipeline em casos com resposta conhecida. `casos` = lista de (entrada, esperado)."""
    casos = list(casos)
    corretos = errados = revisao = executados = 0
    custo = 0.0
    for entrada, esperado in casos:
        d = pipeline.processar(entrada)
        executados += 1
        custo += d.custo
        if d.status == "revisao":
            revisao += 1
        elif comparar(d.saida, esperado):
            corretos += 1
        else:
            errados += 1
    if executados != len(casos):
        raise BateriaIncompleta("rodaram %d de %d casos" % (executados, len(casos)))
    return RelatorioGabarito(len(casos), executados, corretos, errados, revisao, round(custo, 6))
