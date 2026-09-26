# -*- coding: utf-8 -*-
"""Registro de consumo de API: cada chamada vira linha, com rota normalizada, status, tempo e etiquetas.

Responde as perguntas que aparecem quando a cota do ERP estoura ou a conta do fornecedor chega:
quanto da cota diaria foi usado, quais rotas consomem mais, quanto virou 429 e quanto custou, por area.
Guarda em SQLite (arquivo ou memoria), so biblioteca padrao.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

_ID_NUMERICO = re.compile(r"/\d+(?=/|$)")
_ID_GUID = re.compile(r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?=/|$)")
_ID_ODATA = re.compile(r"\(\s*[^)]*\)")


def normalizar_rota(rota):
    """/processos/123/tarefas?x=1 -> /processos/{id}/tarefas. Assim a rota agrupa, o id nao."""
    rota = rota.split("?", 1)[0]
    if "://" in rota:
        rota = "/" + rota.split("://", 1)[1].split("/", 1)[-1]
    rota = _ID_GUID.sub("/{id}", rota)
    rota = _ID_NUMERICO.sub("/{id}", rota)
    rota = _ID_ODATA.sub("({id})", rota)
    return "/" + rota.lstrip("/")


class RegistroConsumo:
    def __init__(self, caminho=":memory:", *, cota_diaria=None, custos=None, relogio=datetime.now):
        """`custos` mapeia rota normalizada para custo por chamada (ex.: consulta paga a provedor)."""
        self.cota_diaria = cota_diaria
        self.custos = dict(custos or {})
        self._relogio = relogio
        self._db = sqlite3.connect(caminho)
        self._db.execute("""CREATE TABLE IF NOT EXISTS chamadas (
            momento TEXT NOT NULL, dia TEXT NOT NULL, metodo TEXT NOT NULL, rota TEXT NOT NULL,
            status INTEGER NOT NULL, duracao_ms REAL NOT NULL, tentativa INTEGER NOT NULL,
            custo REAL NOT NULL, etiquetas TEXT NOT NULL)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_dia ON chamadas (dia)")

    def fechar(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fechar()

    def registrar(self, metodo, rota, status, duracao_ms, tentativa=1, etiquetas=None):
        agora = self._relogio()
        normal = normalizar_rota(rota)
        # custo so da chamada que deu certo: tentativa recusada por limite nao e cobrada pelo fornecedor
        custo = self.custos.get(normal, 0.0) if 200 <= status < 300 else 0.0
        self._db.execute("INSERT INTO chamadas VALUES (?,?,?,?,?,?,?,?,?)", (
            agora.isoformat(timespec="seconds"), agora.strftime("%Y%m%d"), metodo.upper(), normal, int(status),
            float(duracao_ms), int(tentativa), float(custo), json.dumps(etiquetas or {}, ensure_ascii=False)))
        self._db.commit()

    def _linhas(self, dia):
        return self._db.execute("SELECT rota, status, custo, etiquetas FROM chamadas WHERE dia = ?",
                                (dia.strftime("%Y%m%d"),)).fetchall()

    def resumo(self, dia=None, chave_rateio="area"):
        dia = dia or self._relogio()
        linhas = self._linhas(dia)
        total = len(linhas)
        por_status = Counter(s for _, s, _, _ in linhas)
        por_rota = Counter(r for r, _, _, _ in linhas)
        custo_por = defaultdict(float)
        for _, _, custo, et in linhas:
            if custo:
                custo_por[json.loads(et).get(chave_rateio, "sem " + chave_rateio)] += custo
        return {
            "total": total,
            "por_status": dict(por_status),
            "rotas_mais_usadas": por_rota.most_common(5),
            "taxa_429": (por_status.get(429, 0) / total) if total else 0.0,
            "uso_da_cota": (total / self.cota_diaria) if self.cota_diaria else None,
            "custo_total": round(sum(custo_por.values()), 2),
            "custo_por_" + chave_rateio: {k: round(v, 2) for k, v in sorted(custo_por.items())},
        }

    def alerta(self, dia=None, limiar=0.8):
        """Texto de alerta quando o uso passa do limiar da cota ou quando 429 passa de 5%. None se ok."""
        r = self.resumo(dia)
        avisos = []
        if r["uso_da_cota"] is not None and r["uso_da_cota"] >= limiar:
            avisos.append("consumo em %d%% da cota diaria (%d de %d chamadas)"
                          % (round(100 * r["uso_da_cota"]), r["total"], self.cota_diaria))
        if r["taxa_429"] > 0.05:
            avisos.append("%d%% das chamadas recusadas por limite (429)" % round(100 * r["taxa_429"]))
        return "; ".join(avisos) or None
