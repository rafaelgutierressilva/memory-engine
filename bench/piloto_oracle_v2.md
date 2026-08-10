# LongMemEval (oracle) — 10 perguntas

## Tabela 1 — acurácia por tipo de pergunta

| tipo | n | acertos | acurácia |
|---|---:|---:|---:|
| knowledge-update | 1 | 1 | 100% |
| multi-session | 2 | 0 | 0% |
| single-session-assistant | 1 | 0 | 0% |
| single-session-preference | 1 | 0 | 0% |
| single-session-user | 2 | 1 | 50% |
| temporal-reasoning | 3 | 1 | 33% |
| **geral** | **10** | **3** | **30%** |

## Tabela 2 — custo e eficiência

A tabela que ninguém publica: não o quanto acerta, mas o quanto custa acertar.

| métrica | valor | leitura |
|---|---:|---|
| trocas ingeridas | 117 | o histórico inteiro passou pelo portão |
| taxa de carimbo na ingestão | 29.1% | quanto virou episódio; o resto não custou nada de consolidação |
| taxa de fusão | 30.8% | confirmações que viraram confiança em vez de linha nova |
| trocas que nunca pagaram consolidação | 70.9% | a economia do purgatório versus um sistema eager |
| episódios consolidados / expirados | 31 / 3 | do que foi carimbado, quanto chegou a virar fato |
| chamadas ao juiz do portão | 90 | 77% das trocas |
| **autoinconsistência** | 2 (1.71% das trocas) | o assistente contradizendo a memória — sobe antes da acurácia cair |
| chamadas LLM totais | 189 |  |
| tokens de entrada (estimado) | 3.6M | piso medido de ~19k por chamada do `claude -p` |
| custo total | US$ 7.24 |  |
| **custo por pergunta** | US$ 0.724 |  |
| **custo por acerto** | US$ 2.413 | a métrica que importa: acurácia POR token gasto |
| tempo total | 36 min |  |
