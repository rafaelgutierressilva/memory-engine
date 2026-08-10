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
| taxa de carimbo na ingestão | 27.4% | quanto virou episódio; o resto não custou nada de consolidação |
| taxa de fusão | 29.9% | confirmações que viraram confiança em vez de linha nova |
| trocas que nunca pagaram consolidação | 72.6% | a economia do purgatório versus um sistema eager |
| episódios consolidados / expirados | 21 / 11 | do que foi carimbado, quanto chegou a virar fato |
| chamadas ao juiz do portão | 99 | 85% das trocas |
| **autoinconsistência** | 1 (0.85% das trocas) | o assistente contradizendo a memória — sobe antes da acurácia cair |
| chamadas LLM totais | 169 |  |
| tokens de entrada (estimado) | 3.2M | piso medido de ~19k por chamada do `claude -p` |
| custo total | US$ 6.59 |  |
| **custo por pergunta** | US$ 0.659 |  |
| **custo por acerto** | US$ 2.195 | a métrica que importa: acurácia POR token gasto |
| tempo total | 34 min |  |
