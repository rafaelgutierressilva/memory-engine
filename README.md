# lembra

**Memória de longo prazo para agentes conversacionais.** Python puro + SQLite,
**zero dependências de terceiros**. A LLM de tudo é o próprio Claude, chamado
via `claude -p` (headless).

🇧🇷 Português (abaixo) · 🇺🇸 [English](#english) · 📖 [Glossário / Glossary](GLOSSARIO.md)

> O vocabulário do projeto é em português — *carimbo*, *purgatório*, *cartório*,
> *sono* — e continua em português na documentação em inglês. Cada palavra foi
> escolhida porque a alternativa em inglês descreveria outro comportamento. O
> [glossário](GLOSSARIO.md) explica cada escolha.

A ideia central: **a maioria das interações não merece virar memória.** Elas
confirmam o que já se sabe. Guardar tudo é caro, e — pior — enche a memória de
ruído que compete com o que importa. Então cada estágio tem um portão, e cada
portão existe para curar uma doença com nome.

---

## Arquitetura

```
                         ┌──────────── o caminho barato (a maioria) ────────────┐
                         │                                                      │
   interação ──▶ ┌───────────────┐  não surpreende   ┌──────────────┐           │
                 │ PORTÃO DE     │ ─────────────────▶│    FUSÃO     │───────────┘
                 │ SURPRESA      │                   │ confiança++  │
                 │               │                   └──────────────┘
                 │ camada 1      │ surpreende
                 │  heurísticas  │      │
                 │  (grátis)     │      ▼
                 │ camada 2      │  ┌─────────────────┐
                 │  juiz haiku   │  │   PURGATÓRIO    │  episódio cru, sem compressão
                 │  (inconclus.) │  │   (buffered)    │  status: buffered
                 └───────────────┘  └────────┬────────┘
                                             │
                     ┌───────────────────────┼───────────────────────┐
                     │ maturou               │ TTL sem maturar       │
                     ▼                       ▼                       │
              ┌─────────────┐         ┌─────────────┐                │
              │ CONSOLIDAÇÃO│         │  EXPIRADO   │                │
              │ (modelo caro)│        │ 30d, depois │                │
              └──────┬──────┘         │  purgado    │                │
                     │                └─────────────┘                │
                     ▼                                               │
              ┌─────────────┐   não sustentado    ┌──────────────┐   │
              │  AUDITOR    │ ───────────────────▶│  DESCARTADO  │   │
              │ (prompt     │                     └──────────────┘   │
              │  invertido) │                                        │
              └──────┬──────┘                                        │
                     │ verificado                                    │
                     ▼                                               │
              ┌───────────────────────────────────────┐              │
              │              FATOS                    │              │
              │  valid_from / valid_to / confiança    │              │
              │  + PROVENIÊNCIA (quais episódios)     │              │
              └──────────────────┬────────────────────┘              │
                                 │                                   │
                                 ▼                                   │
                  ┌──────────────────────────┐                       │
                  │      RECUPERAÇÃO         │◀──────────────────────┘
                  │ núcleo + top-k           │   episódios em buffer
                  │ BM25 × recência × conf.  │   também competem
                  └────────────┬─────────────┘
                               │ toda injeção conta como uso
                               └────────────▶ alimenta decaimento e maturação
```

### Os quatro estágios e seus portões

| estágio | portão | o que passa | doença curada |
|---|---|---|---|
| interação → episódio | **surpresa** | só o inesperado | custo |
| episódio → maduro | **purgatório** | só o que foi usado, repetido, ou é velho e surpreendente | consolidação ansiosa |
| maduro → fato | **auditoria** | só o que os episódios sustentam | envenenamento de memória |
| fato → prompt | **competição** | núcleo + 3-5 vencedores | lost-in-the-middle |

---

## Os cinco princípios, e onde eles moram no código

**1. Captura por surpresa** — `memory/gate.py`. Duas camadas: heurísticas duras
(correção do usuário, pedido de memória, contradição, falha de ferramenta,
assunto inédito) resolvem de graça; o juiz haiku só entra no inconclusivo.
Toda decisão registra **como** foi resolvida.

**2. Buffer transitório** — `memory/sleep.py`. Episódio carimbado espera. Só
consolida quem foi usado na recuperação, formou cluster de assunto, ou é velho
e muito surpreendente. O resto expira sem nunca custar consolidação.

**3. Consolidação com proveniência** — `memory/sleep.py`. Todo fato guarda os
IDs dos episódios de que foi destilado (`provenance`), e passa por uma segunda
chamada com o prompt invertido: *"liste o que os episódios NÃO sustentam"*.
Sem auditoria não há gravação — falha fecha a porta.

**4. Fatos temporais** — `valid_from` / `valid_to`. Atualizar nunca sobrescreve:
encerra a vigência do antigo e abre a do novo. A destilação declara um de
quatro **destinos** para cada fato proposto:

| destino | o que acontece com o fato antigo |
|---|---|
| `novo` | nada — assuntos diferentes |
| `substitui` | `valid_to` fechado (depois de um segundo parecer independente) |
| `condicional` | **nada** — a regra condicional convive com a geral |
| `excecao_isolada` | **nada** — e o desvio nem vira fato |

**5. Esquecimento em três formas** — fusão (`gate.py`), decaimento
(`retrieval.py`), deleção dirigida (`forget.py`).

**6. Recuperação parcimoniosa** — `memory/retrieval.py`. FTS5 do SQLite, sem
embeddings. Score = BM25 × recência × confiança. Fato com vigência encerrada
**não compete** — só reaparece em pergunta explicitamente temporal.

---

## Como usar

Um binário, subcomandos para tudo:

```bash
lembra chat -v                 # conversar com o Claude ATRAVÉS do motor
lembra stats                   # panorama
lembra list-facts --current    # só o que vale hoje
lembra inspect <id>            # fato + proveniência + cadeia temporal
lembra sleep                   # rodada de consolidação + decaimento
lembra sleep --dry-run         # quem maturaria, sem gastar nada
lembra gate "<troca>" -v       # o portão sobre uma troca avulsa
lembra gate-log                # o KPI: cada decisão e como foi resolvida
lembra calibrate               # o que girar no config, e por quê
lembra forget <ep_id>          # deleção dirigida + cascata
lembra eval --estimar          # a conta antes de rodar a regressão
lembra eval                    # a suíte ponta-a-ponta
lembra bench --variante oracle -n 10    # LongMemEval
```

No repositório, sem instalar: `./bin/lembra <subcomando>`. Os scripts antigos
(`memctl`, `chat`, `eval`, `runbench`) continuam funcionando como aliases finos.

Testes: `PYTHONPATH=. .venv/bin/python -m unittest discover -s tests`

---

## Os botões (`config.yaml`)

### `gate` — a fronteira entre "de graça" e "paga haiku"

| botão | padrão | o que faz |
|---|---|---|
| `surprise_threshold` | 6.0 | nota do juiz a partir da qual carimba |
| `heuristic_score` | 8.0 | nota atribuída quando uma heurística dura dispara |
| `merge_coverage_min` | 0.30 | quanto do fato a troca precisa ecoar para ser confirmação. **Também é o piso da fusão**: sem isso, a confiança sobe no fato "menos distante" |
| `merge_novelty_max` + `merge_novelty_min_tokens` | 0.30 / 3 | novidade que veta a fusão grátis — em proporção **e** em contagem absoluta |
| `novel_topic_max_overlap` | 0.10 | abaixo disso, "assunto inédito" |
| `judge_timeout_s` | 25 | orçamento do juiz. Estourou, a camada 1 decide sozinha e o estouro vai pro log |
| `merge_confidence_step` / `_ceiling` | 0.02 / 0.95 | quanto a confirmação soma, e o teto |

### `purgatory` — quem sai do buffer

| botão | padrão | o que faz |
|---|---|---|
| `ttl_days` | 7 | sem maturar até aqui, expira |
| `min_cluster_size` | 2 | episódios do mesmo assunto que maturam juntos |
| `cluster_similarity` | 0.30 | **dice** mínimo para "mesmo assunto" (não jaccard — ver aresta 6) |
| `high_surprise_score` | 8.0 | velho + muito surpreendente matura sozinho |
| `purge_expired_after_days` | 30 | só então sai do disco |

### `consolidation` — o caro

| botão | padrão | o que faz |
|---|---|---|
| `audit_enabled` | true | o auditor. Desligar = gravar sem verificação |
| `new_fact_confidence` | 0.70 | confiança inicial (+0.05 por episódio extra) |
| `supersede_coverage_min` | 0.35 | sobreposição para virar candidato a substituição |

### `retrieval` / `decay` — a competição e o esquecimento

| botão | padrão | o que faz |
|---|---|---|
| `top_k` | 4 | vencedores por turno |
| `core_max_lines` | 5 | teto do núcleo sempre-presente |
| `recency_half_life_days` | 45 | meia-vida do fator de recência na busca |
| `half_life_days` | 30 | meia-vida do score de decaimento |
| `sink_threshold` | 0.15 | abaixo disso o fato afunda (sai da busca, fica no disco) |

### `llm`

`enabled: false` roda o pipeline inteiro sem gastar token — **leia a aresta 1
antes de usar isso em sério.** `gate_model` (haiku, frequente),
`consolidation_model` (caro, raro), `chat_model`.

---

## Economia medida nesta máquina

| operação | custo | latência |
|---|---|---|
| decisão do portão por heurística | **US$ 0** | ~0,05s |
| decisão do portão com juiz haiku | US$ 0,013–0,023 | 10–16s |
| rodada de sono (destilar + auditar + desempatar) | US$ 0,19–0,26 | ~40s |
| turno de chat (resposta + portão) | US$ 0,075–0,10 | 4,5–22s |
| suíte `eval` completa (20 trocas + 2 sonos) | US$ 0,87–1,17 | ~8 min |

**O piso de ~19k tokens de entrada por chamada é irredutível** (overhead do
CLI). Encurtar o prompt não move o custo. Por isso o desenho inteiro aposta em
**não chamar**, não em chamar barato.

---

## Benchmark

Rodamos o LongMemEval e **interrompemos o piloto**: tendência desfavorável,
amostra insuficiente, causa estrutural medida. O relatório completo está em
[`RELATORIO-FASE6.md`](RELATORIO-FASE6.md) — com as duas tabelas, os três
degraus de perda, e a seção que importa: *o que este benchmark mede
(recuperação verbatim de incidentais) vs. o que este motor otimiza (memória
operacional de regras e decisões)*.

O resumo em uma linha: **o benchmark é adversarial à tese da fusão, e conversas
reais não são** — 99% das trocas do haystack cairiam no juiz, contra uma fração
muito menor numa conversa de verdade. O número comparável fica como trabalho
futuro, via API em lote, sem o piso de ~19k tokens do CLI.

---

## Arestas conhecidas

Descobertas durante o build, todas com teste de regressão. O caderno completo
está em [`NOTAS-ARESTAS.md`](NOTAS-ARESTAS.md); os cinco que mais importam:

### 1. O modo `--no-llm` erra para o lado da confiança (o mais traiçoeiro)

Sem o juiz, uma mudança **sutil** — sem "não", sem palavra nova suficiente — é
indistinguível de uma confirmação. Medido, a mesma troca nos dois modos:

```
com juiz:   CARIMBADO (surpresa 8.0: "xlsx contradiz 'nunca em planilha'")
--no-llm:   fundido -> fato #3 confiança 0.87→0.89
```

O fato errado ficou **mais confiante** por causa da evidência que o contradizia.
E não aparece como erro: sem exceção, sem log vermelho, o motor segue barato e
silencioso enquanto endurece em volta de uma crença já corrigida. É o oposto do
envenenamento por alucinação (que o auditor pega): aqui é envenenamento por
**omissão**, e nenhuma passada de verificação olha para ele.

O modo determinístico é para testar o encanamento sem queimar token — **não é um
modo de produção econômico.** Se for rodar assim, zere `merge_confidence_step`.

### 2. Prompt em forma de persona faz o modelo recusar

`"Você é o portão de surpresa... responda SOMENTE com JSON"` foi lido como
tentativa de jailbreak e recusado (1 em 3 chamadas, silenciosamente — voltava
prosa em vez de JSON). Enquadrar como **tarefa** resolveu: 3/3. Todo prompt
interno segue esse formato.

### 3. Agrupar por palavra-chave perde o mesmo assunto dito com outras palavras

"PDF é melhor porque leio no celular entre reuniões" não entrou no cluster dos
outros três episódios sobre formato de relatório: compartilha uma palavra só.
Uma preferência dita uma única vez, com vocabulário próprio, pode nunca virar
fato. É o caso que embeddings resolveriam — a Fase 7 hipotética.

### 4. Radicalizar para comparar ≠ radicalizar para buscar

O radical de "reuniões" é "reunião", que **não é prefixo** de "reuniões" — então
a busca por prefixo não encontrava o fato. O motor respondeu *"não tenho nada
registrado sobre duração de reuniões"* tendo dois fatos sobre isso. A consulta
agora leva as duas formas mais o radical comum.

### 5. O eval é o que acha os erros de calibragem — e achou três

A primeira execução da suíte falhou com **61% de taxa de carimbo**. As três
causas, todas reais:

- **A novidade contava as palavras do assistente.** "Obrigado, era isso por
  hoje" foi carimbado com novidade 1,0 — o vocabulário novo era todo da
  resposta ("janela de otimização", "criativo em rotação"). Sozinho, isso
  respondia por 3 dos 11 carimbos.
- **`min_content_tokens: 3` era baixo demais.** "Valeu, chegou certinho" tem
  exatamente 3 palavras de conteúdo, todas inéditas. Subiu para 5.
- **O roteiro estava mal rotulado.** Trocas marcadas como "rotineiras"
  reforçavam uma mudança que a memória ainda não tinha consolidado — o juiz deu
  8 e 9 de surpresa, e **estava certo**. A correção foi mover o sono para logo
  depois de cada mudança, não widen a faixa da asserção.

Depois: **39% (7/18), verde nos 7 checks, US$ 0,87.**

### 6. A atribuição de "qual fato foi contrariado" pelo portão é aproximada

O portão rotula pela sobreposição de palavras e às vezes aponta o fato errado.
Não afeta a decisão (o carimbo estava certo) e quem decide de fato qual fato
morre é o passo de destino do sono, que usa o modelo caro. O rótulo do portão é
uma pista, não um veredito.

### Uma propriedade que apareceu sozinha

O portão compara a troca **inteira** com os fatos — e a troca inclui a resposta
do assistente. Quando a recuperação falha e o assistente diz "não tenho nada
registrado", isso contradiz um fato vigente, e o portão carimba. Sai de graça um
**detector de falha de recuperação**. Esses carimbos têm razão própria
(`autoinconsistencia`) e taxa separada no `calibrate`: eles medem a saúde do
motor, não a novidade do usuário.

---

## Decisões de projeto que merecem defesa

**`provenance.episode_id` é `ON DELETE RESTRICT`, não CASCADE.** Um episódio não
consegue sair do disco enquanto sustentar um fato. O banco força o
reprocessamento a acontecer, em vez de confiar que o código lembre.

**Deletar o episódio que provou uma mudança reabre a vigência do fato antigo.**
Se o fato B substituiu A e depois B perde toda a proveniência, o `valid_to` de A
volta a ser NULL. Parece contrariar "nunca sobrescreva a história", mas aquele
`valid_to` não era história observada — era uma **conclusão** tirada de um
episódio que acabou de ser apagado. Mantê-lo deixaria o assunto sem nenhum fato
vigente: nem o novo (aposentado) nem o velho (fechado por uma razão que não
existe mais). O `valid_to` anterior fica no log de eventos, então a auditoria
não perde nada.

**Fato afundado sai da competição por filtro na consulta, não por remoção do
índice FTS.** Efeito idêntico, e elimina a chance de índice e verdade
discordarem.

---

<a name="english"></a>

# lembra — English

**Long-term memory for conversational agents.** Pure Python + SQLite, **zero
third-party dependencies**. Every LLM call goes to Claude itself, via `claude -p`
(headless).

The core idea: **most interactions do not deserve to become memory.** They
confirm what is already known. Storing everything is expensive and — worse — it
fills memory with noise that competes with what matters. So every stage has a
gate, and every gate exists to cure a named disease.

> The project's vocabulary stays in Portuguese — *carimbo* (stamp), *purgatório*
> (purgatory), *cartório* (registry), *sono* (sleep). Not decoration: each word
> was chosen because the obvious English alternative describes different
> behavior. See the [glossary](GLOSSARIO.md) for the reasoning behind each one.

## The four stages and their gates

| stage | gate | what gets through | disease cured |
|---|---|---|---|
| interaction → episode | **surpresa** (surprise) | only the unexpected | cost |
| episode → mature | **purgatório** (purgatory) | only what was used, repeated, or is old and surprising | eager consolidation |
| mature → fact | **auditor** | only what the episodes support | memory poisoning |
| fact → prompt | competition | core + 3-5 winners | lost-in-the-middle |

## Principles

1. **Capture by surprise, not by default.** Two layers: free heuristics decide
   most cases; a haiku judge runs only when they are inconclusive. Every
   decision logs **how** it was resolved.
2. **A transient buffer before consolidation.** Stamped episodes wait. Only
   those that were used in retrieval, formed a topic cluster, or are old and
   very surprising get the expensive pass. The rest expire silently.
3. **Consolidation with provenance.** Every fact stores the IDs of the episodes
   it was distilled from, and passes a second call with the prompt inverted:
   *"list what the episodes do NOT support."* No audit, no write.
4. **Temporal facts.** `valid_from` / `valid_to`. Updating never overwrites: it
   closes the old fact's validity and opens the new one's. Distillation declares
   one of four destinies per proposed fact: `novo`, `substitui`, `condicional`,
   `excecao_isolada`.
5. **Forgetting in three forms.** Merge (confirmations dissolve into a
   confidence counter), decay (unused facts sink out of search before leaving
   disk), and targeted deletion (deleting an episode reprocesses every fact
   derived from it).
6. **Parsimonious retrieval.** SQLite FTS5, no embeddings. Score = BM25 ×
   recency × confidence. Facts whose validity has ended **do not compete** —
   they return only for explicitly temporal questions. Consolidated episodes
   remain searchable as a low-weight raw archive.

## Install & use

```bash
pip install lembra          # coming in checkpoint 2
lembra chat -v
lembra sleep
lembra calibrate
```

Requires the `claude` CLI on PATH.

## Measured economics

| operation | cost | latency |
|---|---|---|
| gate decision, heuristic | **US$ 0** | ~0.05s |
| gate decision, haiku judge | US$ 0.013–0.023 | 10–16s |
| sleep round (distill + audit + tiebreak) | US$ 0.19–0.26 | ~40s |
| full chat turn | US$ 0.075–0.10 | 4.5–22s |

**The ~19k input-token floor per call is irreducible** (CLI overhead, measured).
Shortening prompts does not move it. Which is why the whole design bets on **not
calling** rather than on calling cheaply.

## Benchmark: we ran it, we lost where the thesis says to lose, we published the bill

We ran LongMemEval and **stopped the pilot**: unfavorable trend, insufficient
sample, structural cause measured. Full report in
[`RELATORIO-FASE6.md`](RELATORIO-FASE6.md) (Portuguese).

The one-line summary: **the benchmark is adversarial to the merge thesis, and
real conversations are not.** LongMemEval's haystack is built from unrelated
sessions with deliberate distractors — by construction, almost nothing confirms
anything, so there is nothing to merge. Measured on 723 real exchanges: 99% would
hit the judge, against a much smaller share in an actual conversation.

It also asks, by construction, about details incidental to the conversation's
purpose. Lossy distillation drops those — and lossy distillation is this engine's
value proposition. Systems that score high do not distill: they index raw text.

We report the number we got (30% on the oracle variant, 10 questions), the cost
(US$ 48.97 total), and why we stopped. The comparable number needs the batch API
rather than the CLI, and is registered as future work.
