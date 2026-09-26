# kai-toolkit

Ferramentas da **KAI Legal Ops** para automação jurídica: falar com a API do ERP sem perder dado,
medir o consumo e usar IA com conferência independente. Só biblioteca padrão do Python (3.10+).

| Módulo | O que resolve |
|---|---|
| `kaitk/erp/cliente_http.py` | Cliente HTTP para API de ERP jurídico com os cuidados de produção. |
| `kaitk/erp/consumo.py` | Registro de cada chamada: cota diária, 429, rotas mais usadas e custo por área. |
| `kaitk/ia/avaliador.py` | Um modelo faz o trabalho, outro confere; divergência vai para uma pessoa. |

## Cliente do ERP

```python
import os
from kaitk.erp.cliente_http import ClienteERP, token_por_client_credentials
from kaitk.erp.consumo import RegistroConsumo

obter = token_por_client_credentials(os.environ["ERP_URL_TOKEN"], os.environ["ERP_CLIENTE"],
                                     os.environ["ERP_SEGREDO"])
with RegistroConsumo("consumo.db", cota_diaria=10000) as consumo:
    erp = ClienteERP(os.environ["ERP_URL"], obter, consumo=consumo, etiquetas={"area": "civel"})
    for processo in erp.paginar("/processos", params={"$filter": "status eq 1"}):
        ...
    divergencias = erp.conferir_gravacao("/processos/42", {"fase": "recursal"})
    print(consumo.alerta())
```

| Situação real | Comportamento |
|---|---|
| Execução longa com token curto | Renova antes de vencer (margem de 120 s) e, se ainda vier 401, renova uma vez e repete. |
| 429, limite de requisições | Espera o `Retry-After` (segundos ou data HTTP); sem ele, espera exponencial com teto. |
| 5xx em leitura | Repete com espera exponencial até o limite de tentativas. |
| 5xx em escrita (POST, PATCH) | **Não repete.** A API pode ter gravado e respondido erro: sobe `EscritaIncerta` para conferir no destino. |
| Listagem maior que o esperado | Passar do teto de páginas é erro (`TetoDePaginas`), não lista cortada que parece completa. |
| Atualização que responde 204 e descarta campo | `conferir_gravacao` lê de volta e aponta o campo que não gravou. |
| Conta do ERP no fim do mês | Toda tentativa vira linha no registro de consumo, com rota normalizada e etiqueta de área. |

A credencial nunca fica no código: `obter_token` recebe o que vier do ambiente ou do cofre.

## IA com avaliador independente

```python
from kaitk.ia.avaliador import GeradorComAvaliador, Orcamento, Precos, medir_gabarito

pipeline = GeradorComAvaliador(
    gerador=meu_modelo, avaliador=outro_modelo,          # funcoes (sistema, prompt) -> RespostaModelo
    prompt_gerador=lambda texto: "Classifique a publicação: " + texto,
    prompt_avaliador=lambda texto, saida: f"A classificação {saida} está correta para: {texto}?",
    esquema={"tipo": str, "prazo_dias": int},
    campos_obrigatorios_nao_vazios=["tipo"],
    precos_gerador=Precos(0.5, 1.5), precos_avaliador=Precos(0.1, 0.4),
    orcamento=Orcamento(teto=20.0), estimativa_por_chamada=0.02)

decisao = pipeline.processar(texto)          # status "aprovado" ou "revisao", com motivo e custo
relatorio = medir_gabarito(pipeline, casos)  # casos = [(entrada, resposta_esperada), ...]
```

- Saída fora do esquema, vazia ou só com pontuação vai para revisão **sem gastar o avaliador**.
- O orçamento para o lote antes de passar do teto.
- O gabarito separa **aprovado errado** (o erro que passaria sem ninguém ver) de enviado para revisão.
- Independente de fornecedor: o modelo é qualquer função que você liga por fora.

## Testes

```bash
python -m unittest discover -s testes -t .
```

Os testes rodam contra `testes/api_falsa.py`, uma API local que reproduz os defeitos que um cliente
de produção precisa aguentar: token que vence, 429 com `Retry-After`, 503 intermitente, escrita que
grava e responde 500, atualização que descarta campo e listagem paginada por `$skip` ou `nextLink`.

---

Código escrito do zero para a KAI Legal Ops, com dado fictício. Todo commit passa pelo gate de
sigilo em `ferramentas/checa_sigilo.py`.
