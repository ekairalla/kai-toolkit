# -*- coding: utf-8 -*-
import unittest

from kaitk.erp.cliente_http import (ClienteERP, ErroERP, EscritaIncerta, TetoDePaginas, segundos_retry_after,
                                    token_por_client_credentials)
from kaitk.erp.consumo import RegistroConsumo
from testes.api_falsa import ApiFalsa


class Relogio:
    """Relogio manual: o teste avanca o tempo sem esperar de verdade."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestClienteHttp(unittest.TestCase):
    def setUp(self):
        self.api = ApiFalsa().__enter__()
        self.relogio = Relogio()
        self.esperas = []
        self.consumo = RegistroConsumo()
        self.cli = ClienteERP(self.api.url, token_por_client_credentials(self.api.url + "/oauth/token", "cli", "abc"),
                              consumo=self.consumo, relogio=self.relogio, dormir=self.esperas.append,
                              etiquetas={"area": "civel"})

    def tearDown(self):
        self.consumo.fechar()
        self.api.__exit__(None, None, None)

    def test_token_renovado_antes_de_vencer(self):
        self.api.estado.validade = 600
        self.cli.get("/processos/1")
        self.assertEqual(self.cli.token.renovacoes, 1)
        self.relogio.t += 400          # faltam 200 s: ainda fora da margem de 120 s
        self.cli.get("/processos/1")
        self.assertEqual(self.cli.token.renovacoes, 1)
        self.relogio.t += 100          # faltam 100 s: dentro da margem, renova antes de vencer
        self.cli.get("/processos/1")
        self.assertEqual(self.cli.token.renovacoes, 2)

    def test_401_renova_uma_vez_e_repete(self):
        self.cli.get("/processos/1")
        self.api.estado.token_minimo = 2   # o servidor passa a considerar o token 1 vencido
        r = self.cli.get("/processos/2")
        self.assertEqual(r.dados["id"], 2)
        self.assertEqual(self.cli.token.renovacoes, 2)

    def test_401_persistente_nao_vira_laco(self):
        self.api.estado.token_minimo = 999
        with self.assertRaises(ErroERP) as ctx:
            self.cli.get("/processos/1")
        self.assertEqual(ctx.exception.status, 401)

    def test_429_respeita_retry_after(self):
        self.api.estado.falhas_429 = 2
        self.api.estado.retry_after = "7"
        self.assertTrue(self.cli.get("/limitado").dados["ok"])
        self.assertEqual(self.esperas, [7.0, 7.0])

    def test_429_sem_fim_desiste_no_limite(self):
        self.api.estado.falhas_429 = 99
        with self.assertRaises(ErroERP) as ctx:
            self.cli.get("/limitado")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(len(self.esperas), self.cli.max_tentativas - 1)

    def test_503_em_leitura_repete_com_espera_exponencial(self):
        self.api.estado.falhas_503 = 3
        self.assertTrue(self.cli.get("/instavel").dados["ok"])
        self.assertEqual(self.esperas, [1.0, 2.0, 4.0])

    def test_500_em_escrita_nao_repete_e_avisa(self):
        self.api.estado.post_grava_e_falha = True
        antes = len(self.api.estado.processos)
        with self.assertRaises(EscritaIncerta) as ctx:
            self.cli.post("/processos", {"pasta": "NOVA"})
        self.assertIn("Confira no destino", str(ctx.exception))
        self.assertEqual(len(self.api.estado.processos), antes + 1, "a API gravou apesar do 500")
        posts = [c for c in self.api.estado.chamadas if c == ("POST", "/processos")]
        self.assertEqual(len(posts), 1, "escrita incerta nunca e repetida sozinha")

    def test_paginacao_por_skip(self):
        itens = list(self.cli.paginar("/processos", tamanho=100))
        self.assertEqual(len(itens), 250)
        self.assertEqual(len({i["id"] for i in itens}), 250)

    def test_paginacao_por_next_link(self):
        self.api.estado.usar_next_link = True
        self.assertEqual(len(list(self.cli.paginar("/processos", tamanho=40))), 250)

    def test_teto_de_paginas_falha_fechado(self):
        vistos = []
        with self.assertRaises(TetoDePaginas):
            for item in self.cli.paginar("/processos", tamanho=50, teto_paginas=3):
                vistos.append(item)
        self.assertEqual(len(vistos), 150)

    def test_teto_exato_nao_dispara(self):
        self.assertEqual(len(list(self.cli.paginar("/processos", tamanho=50, teto_paginas=5))), 250)

    def test_conferir_gravacao_pega_campo_descartado(self):
        r = self.cli.patch("/processos/1", {"fase": "recursal", "observacao": "urgente"})
        self.assertEqual(r.status, 204)
        divergencias = self.cli.conferir_gravacao("/processos/1", {"fase": "recursal", "observacao": "urgente"})
        self.assertEqual(divergencias, {"observacao": ("urgente", None)})

    def test_consumo_registra_cada_tentativa(self):
        self.api.estado.falhas_429 = 1
        self.cli.get("/limitado")
        self.cli.get("/processos/7")
        r = self.consumo.resumo()
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["por_status"], {429: 1, 200: 2})
        self.assertIn(("/processos/{id}", 1), r["rotas_mais_usadas"])

    def test_retry_after_em_data_http(self):
        self.assertEqual(segundos_retry_after("Wed, 21 Oct 2015 07:28:10 GMT", agora=lambda: 1445412480.0), 10.0)
        self.assertIsNone(segundos_retry_after("amanha"))


if __name__ == "__main__":
    unittest.main()
