# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from datetime import datetime

from kaitk.erp.consumo import RegistroConsumo, normalizar_rota

DIA = datetime(2026, 9, 26, 10, 0, 0)


class TestConsumo(unittest.TestCase):
    def test_normalizar_rota(self):
        self.assertEqual(normalizar_rota("/processos/123/tarefas?x=1"), "/processos/{id}/tarefas")
        self.assertEqual(normalizar_rota("processos/9f8e7d6c-1a2b-3c4d-5e6f-7a8b9c0d1e2f"), "/processos/{id}")
        self.assertEqual(normalizar_rota("/Processos(55)/Tarefas"), "/Processos({id})/Tarefas")
        self.assertEqual(normalizar_rota("https://api.exemplo.com/v1/processos/5"), "/v1/processos/{id}")

    def test_cota_e_alerta(self):
        reg = RegistroConsumo(cota_diaria=10, relogio=lambda: DIA)
        self.addCleanup(reg.fechar)
        for _ in range(8):
            reg.registrar("GET", "/processos/1", 200, 12.5)
        r = reg.resumo()
        self.assertEqual(r["total"], 8)
        self.assertAlmostEqual(r["uso_da_cota"], 0.8)
        self.assertIn("80% da cota", reg.alerta())

    def test_alerta_de_429(self):
        reg = RegistroConsumo(relogio=lambda: DIA)
        self.addCleanup(reg.fechar)
        for status in (200, 200, 200, 429):
            reg.registrar("GET", "/x", status, 5)
        self.assertIn("25% das chamadas recusadas", reg.alerta())

    def test_custo_rateado_e_so_em_sucesso(self):
        reg = RegistroConsumo(custos={"/consulta/{id}": 0.5}, relogio=lambda: DIA)
        self.addCleanup(reg.fechar)
        reg.registrar("GET", "/consulta/1", 200, 10, etiquetas={"area": "civel"})
        reg.registrar("GET", "/consulta/2", 200, 10, etiquetas={"area": "civel"})
        reg.registrar("GET", "/consulta/3", 200, 10, etiquetas={"area": "trabalhista"})
        reg.registrar("GET", "/consulta/4", 429, 10, etiquetas={"area": "trabalhista"})
        r = reg.resumo()
        self.assertEqual(r["custo_total"], 1.5)
        self.assertEqual(r["custo_por_area"], {"civel": 1.0, "trabalhista": 0.5})

    def test_dia_separado(self):
        agora = {"t": DIA}
        reg = RegistroConsumo(relogio=lambda: agora["t"])
        self.addCleanup(reg.fechar)
        reg.registrar("GET", "/x", 200, 1)
        agora["t"] = datetime(2026, 9, 27, 9, 0, 0)
        reg.registrar("GET", "/x", 200, 1)
        self.assertEqual(reg.resumo(DIA)["total"], 1)

    def test_persiste_em_arquivo(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = os.path.join(pasta, "consumo.db")
            reg = RegistroConsumo(caminho, relogio=lambda: DIA)
            reg.registrar("POST", "/processos", 201, 30)
            reg.fechar()
            reg2 = RegistroConsumo(caminho, relogio=lambda: DIA)
            self.assertEqual(reg2.resumo()["total"], 1)
            reg2.fechar()


if __name__ == "__main__":
    unittest.main()
