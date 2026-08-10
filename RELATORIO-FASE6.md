# Fase 6 — LongMemEval

> **Piloto interrompido — tendência desfavorável, amostra insuficiente, causa
> estrutural medida.**

Este relatório não traz um número comparável com Zep, Mem0 ou Letta. Traz o que
foi medido antes de interrompermos, por que interrompemos, e o que precisaria
mudar para o número existir.

**Amostra:** 10 perguntas da variante `oracle` (histórico só com as sessões que
contêm a evidência) e 3 perguntas da variante `S` (48 sessões, ~122k tokens de
histórico por pergunta). O braço *eager* — a arquitetura do mercado reproduzida
no nosso corpo — **não chegou a rodar**.

**Gasto encerrado: US$ 48,97.**

---

## Tabela 1 — acurácia por tipo de pergunta

| tipo | oracle (n=10) | S, braço portão (n=3) |
|---|---:|---:|
| knowledge-update | 1/1 | 0/1 |
| single-session-user | 1/2 | — |
| temporal-reasoning | 1/3 | — |
| multi-session | 0/2 | 0/1 |
| single-session-assistant | 0/1 | 0/1 |
| single-session-preference | 0/1 | — |
| **geral** | **3/10 (30%)** | **0/3 (0%)** |

Publicados na variante S, para referência de ordem de grandeza: Mem0 49,0% ·
Zep 63,8% · Letta 83,2%. **A comparação não é válida** — juiz diferente, amostra
de 3 contra 500, e a nossa medição no S está incompleta. Serve só para dizer que
a distância é grande e a tendência, desfavorável.

## Tabela 2 — custo e eficiência

A tabela que o mercado não publica: não o quanto acerta, mas o quanto custa
acertar.

| métrica | oracle (n=10) | S, braço portão (n=3) |
|---|---:|---:|
| trocas ingeridas | 117 | 750 |
| viraram episódio | 29 (**24,8%**) | 119 (**15,9%**) |
| fusões | 40 (34,2%) | 489 (65,2%) |
| chamadas ao juiz do portão | 96 (82% das trocas) | 380 (51% das trocas) |
| fatos gerados | 39 | 151 |
| resgates episódicos | não instrumentado¹ | 6 em 3 respostas |
| cobertura média dos fatos | não instrumentado¹ | 0,45 |
| autoinconsistência | 0 (0,00%) | 5 (0,67%) |
| chamadas LLM | 173 | 674 |
| tokens de entrada² | 3,3M | 12,8M |
| custo total | US$ 6,92 | US$ 26,42 |
| custo por pergunta | US$ 0,69 | **US$ 8,81** |
| custo por acerto | US$ 2,31 | — (zero acertos) |
| tempo por pergunta | 3 min | **43 min** |

¹ A instrumentação de resgate episódico entrou depois da corrida do oracle.
² Estimado pelo piso medido de ~19k tokens de entrada por chamada do `claude -p`.

---

## Os três degraus de perda

A acurácia baixa não tem uma causa. Tem três, e as três são deliberadas.

```
   750 trocas ingeridas (S)
        │
        ▼
   ┌─────────────────────────────────────────────────┐
   │ DEGRAU 1 — o portão          descarta 84%       │  por desenho: CUSTO
   │ 631 trocas nunca viraram episódio               │
   └─────────────────────────────────────────────────┘
        │ 119 episódios
        ▼
   ┌─────────────────────────────────────────────────┐
   │ DEGRAU 2 — a destilação      descarta o          │  por desenho: DENSIDADE
   │ incidental do que passou (cobertura 0,45)        │
   └─────────────────────────────────────────────────┘
        │ 151 fatos
        ▼
   ┌─────────────────────────────────────────────────┐
   │ DEGRAU 3 — o decaimento      afunda o que        │  por desenho: RUÍDO
   │ ninguém usa                                      │
   └─────────────────────────────────────────────────┘
```

A recuperação híbrida (Fase 6) conserta o **degrau 2**: episódios consolidados
continuam buscáveis com peso baixo e respondem quando os fatos não cobrem — cada
resgate registrado como `resgate_episodico`, que é o medidor da perda da
destilação. Ela é governada pelo **degrau 3**: episódio cru que ninguém resgata
afunda, com meia-vida de 90 dias contra 30 dos fatos.

O **degrau 1 é o teto**, e não há ajuste de recuperação que o contorne. Num
benchmark que pergunta sobre detalhes uniformemente distribuídos pelas trocas, a
acurácia é limitada pela taxa de carimbo. A única forma de subir é desligar o
portão — que é desligar o produto.

Isso foi verificado, não deduzido: a mesma pergunta ("onde foi o casamento do meu
primo?") falhou por dois motivos diferentes em duas execuções. Numa, o portão
carimbou e a destilação perdeu a oração incidental. Na outra, **o portão nem
carimbou** — a mesma frase, julgada não-surpreendente dentro de uma conversa
sobre presente de aniversário.

---

## O que este benchmark mede vs. o que este motor otimiza

Esta é a seção que explica por que os números acima não condenam o motor — e
por que também não os desculpam.

**O LongMemEval mede recuperação verbatim de detalhes incidentais.** Por
construção: o haystack é montado com sessões de assuntos não relacionados e
distratores deliberados, e a pergunta mira um detalhe solto, muitas vezes uma
oração subordinada dentro de uma conversa sobre outra coisa. É um teste de
*arquivo*: você guardou aquele token, e consegue achá-lo de novo?

Os sistemas que pontuam alto não destilam — indexam texto bruto e recuperam
trechos. É a arquitetura certa para essa pergunta.

**Este motor otimiza memória operacional de regras e decisões.** "Quero os
relatórios em xlsx, exceto quando for para o investidor, e isso mudou dia 8 de
agosto." É um teste de *cartório*: você sabe o que vale hoje, desde quando, e
por quê?

As duas coisas são memória. Não são a mesma memória.

### A medição estrutural que separa as duas

Medido com a LLM desligada, custo zero, sobre 723 trocas reais do benchmark:
**99% das trocas cairiam no juiz. Só 1% resolve de graça.**

Investiguei se era idioma — as heurísticas são regex em português e o benchmark é
em inglês. Não é: escrevendo os equivalentes em inglês, os sinais duros aparecem
em apenas ~6% das trocas. A tradução levaria 99% para ~93%.

A causa é estrutural. A economia do portão vem da **fusão**, e fusão exige um
fato consolidado que a troca ecoe. O haystack não tem repetição — por desenho.
Não há o que fundir.

**O rótulo honesto: o benchmark é adversarial à tese da fusão, e conversas reais
não são.**

### A evidência do terreno certo

O `eval.py` é uma conversa de 20 trocas com a forma de uma conversa de verdade:
a pessoa estabelece preferências, confirma, corrige, muda de ideia, pede para
esquecer. Rodando do zero, com LLM real:

| | |
|---|---|
| taxa de carimbo | 39% (7/18) — dentro da faixa do roteiro |
| substituição temporal | 1, com `valid_to` fechado e histórico consultável |
| cascata de esquecimento | 2 episódios apagados, 2 cascatas completas |
| fatos sem proveniência | 0 |
| decisões sem razão registrada | 0 |
| custo | **US$ 0,87** |

Sete de sete checks verdes. E, mais importante que os checks: nesse terreno a
fusão **acontece** — a pessoa repete e confirma, então a camada 1 resolve de
graça, e o juiz só entra no que é genuinamente ambíguo. É a mesma máquina, com o
motor de custo funcionando como projetado.

Isso não é desculpa para 0/3 no S. É a delimitação do que foi construído: um
motor que troca recall verbatim por densidade, datação e custo. Quem precisa do
primeiro precisa de outra arquitetura — ou da híbrida, com o arquivo bruto
pesando bem mais do que 0,25.

---

## O que ficou como trabalho futuro

**O número comparável precisa de outra via de execução.** O `claude -p` tem piso
irredutível de ~19k tokens de entrada por chamada — medido, não estimado: testei
`--strict-mcp-config`, `--exclude-dynamic-system-prompt-sections` e system prompt
próprio, e o piso saiu de 25,5k para 19,0k e parou. Com 245 trocas por pergunta,
isso são US$ 8,81 e 43 minutos **por pergunta**, o que torna 500 perguntas
inviável e até 10 caro demais para o valor da informação.

A via correta é a **API em lote** (Message Batches), que não carrega o system
prompt do CLI, aceita concorrência e custa uma fração. Com ela, os dois braços —
portão e eager — sobre 30-50 perguntas ficam plausíveis, e aí o número passa a
significar alguma coisa. Fica registrado como trabalho futuro, não como dívida:
o que aprendemos aqui não muda com o número.

**O braço eager não rodou.** Ele está implementado, testado e pronto
(`--modo eager`): toda troca vira episódio, sem juiz, mesmo ritmo de sono. Sem
ele não existe o delta do portão em pontos e em dólares, que era o número mais
interessante da fase. Fica junto com o resto, para a via em lote.

---

## O que a Fase 6 entregou, apesar de interrompida

Dois bugs reais do motor, que só apareceram porque rodamos contra dados de
verdade em outro idioma:

1. **Fatos eram gravados sempre em português**, hardcoded no prompt de
   consolidação. Uma conversa em inglês virava memória correta e **invisível** —
   a busca casa palavra com palavra. Consertado; a pergunta que falhava passou a
   responder certo.
2. **O escopo da consolidação era só semântico** e mandava ignorar "o
   circunstancial". Um evento com lugar, data e pessoa era descartado. Agora o
   prompt pede as duas famílias, semântica e episódica.

E uma capacidade nova: **a recuperação híbrida**, com os três degraus
explicitados e o `resgate_episodico` como métrica de calibragem — que mede a
destilação falhando, não o fallback funcionando.
