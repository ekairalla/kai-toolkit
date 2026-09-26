# -*- coding: utf-8 -*-
import json
import unittest

from kaitk.ia.avaliador import (BateriaIncompleta, GeradorComAvaliador, Orcamento, OrcamentoEstourado, Precos,
                                RespostaModelo, extrair_json, medir_gabarito, validar_esquema)

ESQUEMA = {"tipo": str, "prazo_dias": int}


def modelo_fixo(*respostas, tokens=(100, 20)):
    """Modelo falso: devolve as respostas em ordem e conta quantas vezes foi chamado."""
    fila = list(respostas)

    def modelo(sistema, prompt):
        modelo.chamadas += 1
        return RespostaModelo(fila.pop(0), *tokens)

    modelo.chamadas = 0
    return modelo


def pipeline(gerador, avaliador, **kw):
    return GeradorComAvaliador(
        gerador=gerador, avaliador=avaliador, esquema=ESQUEMA,
        prompt_gerador=lambda e: "Classifique: " + e,
        prompt_avaliador=lambda e, s: "Confira %s contra %s" % (json.dumps(s), e),
        precos_gerador=Precos(1.0, 2.0), precos_avaliador=Precos(0.5, 1.0), **kw)


APROVA = '{"veredito": "aprova", "motivo": "coerente"}'
REPROVA = '{"veredito": "reprova", "motivo": "prazo nao bate com o ato"}'


class TestAvaliador(unittest.TestCase):
    def test_aprovado_quando_os_dois_concordam(self):
        p = pipeline(modelo_fixo('{"tipo": "sentenca", "prazo_dias": 15}'), modelo_fixo(APROVA))
        d = p.processar("texto da publicacao")
        self.assertEqual(d.status, "aprovado")
        self.assertEqual(d.saida, {"tipo": "sentenca", "prazo_dias": 15})
        self.assertAlmostEqual(d.custo, (100 * 1.0 + 20 * 2.0) / 1000 + (100 * 0.5 + 20 * 1.0) / 1000)
        self.assertEqual([c["etapa"] for c in d.chamadas], ["gerador", "avaliador"])

    def test_reprovado_vai_para_revisao_com_motivo(self):
        d = pipeline(modelo_fixo('{"tipo": "despacho", "prazo_dias": 5}'), modelo_fixo(REPROVA)).processar("x")
        self.assertEqual(d.status, "revisao")
        self.assertIn("prazo nao bate", d.motivo)

    def test_fora_do_esquema_nao_gasta_avaliador(self):
        avaliador = modelo_fixo(APROVA)
        d = pipeline(modelo_fixo('{"tipo": "sentenca", "prazo_dias": "quinze"}'), avaliador).processar("x")
        self.assertEqual(d.status, "revisao")
        self.assertIn("prazo_dias", d.motivo)
        self.assertEqual(avaliador.chamadas, 0)

    def test_saida_so_pontuacao_e_barrada(self):
        avaliador = modelo_fixo(APROVA)
        p = pipeline(modelo_fixo('{"tipo": " / ", "prazo_dias": 0}'), avaliador, campos_obrigatorios_nao_vazios=["tipo"])
        d = p.processar("pdf so de imagem")
        self.assertEqual(d.status, "revisao")
        self.assertIn("OCR", d.motivo)
        self.assertEqual(avaliador.chamadas, 0)

    def test_avaliador_fora_do_formato_vai_para_revisao(self):
        d = pipeline(modelo_fixo('{"tipo": "sentenca", "prazo_dias": 15}'), modelo_fixo("parece ok")).processar("x")
        self.assertEqual(d.status, "revisao")

    def test_json_em_bloco_de_codigo(self):
        self.assertEqual(extrair_json('Resposta:\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertIsNone(extrair_json("sem json"))

    def test_validar_esquema(self):
        self.assertEqual(validar_esquema({"tipo": "x", "prazo_dias": 1}, ESQUEMA), [])
        self.assertEqual(len(validar_esquema({"tipo": 3}, ESQUEMA)), 2)
        self.assertEqual(validar_esquema([1], ESQUEMA), ["saida nao e um objeto JSON"])

    def test_orcamento_para_antes_de_estourar(self):
        orc = Orcamento(teto=0.3)
        respostas = ['{"tipo": "t", "prazo_dias": 1}', APROVA] * 5
        g = modelo_fixo(*respostas[0::2])
        a = modelo_fixo(*respostas[1::2])
        p = pipeline(g, a, orcamento=orc, estimativa_por_chamada=0.14)
        p.processar("1")
        with self.assertRaises(OrcamentoEstourado):
            p.processar("2")
        self.assertLessEqual(orc.gasto, orc.teto)

    def test_gabarito_conta_o_erro_que_passaria(self):
        g = modelo_fixo('{"tipo": "sentenca", "prazo_dias": 15}', '{"tipo": "despacho", "prazo_dias": 5}',
                        '{"tipo": "sentenca", "prazo_dias": 10}')
        a = modelo_fixo(APROVA, APROVA, REPROVA)
        casos = [("a", {"tipo": "sentenca", "prazo_dias": 15}),
                 ("b", {"tipo": "decisao", "prazo_dias": 5}),      # aprovado errado: o perigoso
                 ("c", {"tipo": "sentenca", "prazo_dias": 15})]    # reprovado: foi para revisao
        rel = medir_gabarito(pipeline(g, a), casos)
        self.assertEqual((rel.total, rel.executados), (3, 3))
        self.assertEqual((rel.aprovados_corretos, rel.aprovados_errados, rel.enviados_revisao), (1, 1, 1))
        self.assertAlmostEqual(rel.acerto_dos_aprovados, 0.5)

    def test_bateria_interrompida_nunca_devolve_relatorio(self):
        from kaitk.ia.avaliador import Decisao

        class ParaNoMeio:
            def __init__(self):
                self.n = 0

            def processar(self, entrada):
                self.n += 1
                if self.n == 2:
                    raise RuntimeError("provedor fora do ar")
                return Decisao("aprovado", {"x": 1}, "", 0.0)

        p = ParaNoMeio()
        with self.assertRaises(RuntimeError):
            medir_gabarito(p, [("a", {"x": 1}), ("b", {"x": 1}), ("c", {"x": 1})])
        self.assertEqual(p.n, 2)

    def test_gabarito_aceita_gerador_de_casos(self):
        g = modelo_fixo(*['{"tipo": "t", "prazo_dias": 1}'] * 3)
        a = modelo_fixo(APROVA, APROVA, APROVA)
        rel = medir_gabarito(pipeline(g, a), ((str(i), {"tipo": "t", "prazo_dias": 1}) for i in range(3)))
        self.assertEqual((rel.total, rel.executados, rel.aprovados_corretos), (3, 3, 3))
        self.assertTrue(issubclass(BateriaIncompleta, RuntimeError))


if __name__ == "__main__":
    unittest.main()
