# Arestas conhecidas — caderno de bordo

Registro corrido do que apareceu durante o build. Vira a secao "arestas
conhecidas" do README na Fase 5. Cada item: o que e, por que acontece, e o que
fazer a respeito.

---

## 1. O modo `--no-llm` erra para o lado da confianca (o mais traicoeiro)

**O que e.** Com `llm.enabled: false`, o portao roda so a camada 1. Quando a
troca e uma mudanca *sutil* — sem "nao", sem "na verdade", sem palavra nova
suficiente — a camada 1 nao tem como distinguir contradicao de confirmacao, e o
fallback funde: soma confianca no fato que a troca estava justamente
contrariando.

**Medido na Fase 2**, mesma troca, dois modos:

```
com juiz:   CARIMBADO (surpresa 8.0: "xlsx contradiz 'nunca em planilha'")
--no-llm:   fundido -> fato #3 confianca 0.87->0.89
```

O fato errado ficou *mais* confiante por causa da evidencia que o contradizia.

**Por que e o pior modo de falha.** Ele nao aparece como erro. Nao ha excecao,
nao ha log vermelho, o motor segue rodando barato e silencioso — so que a
memoria vai endurecendo em volta de uma crenca que o usuario ja corrigiu. E o
oposto do envenenamento por alucinacao (que o auditor pega): aqui e
envenenamento por *omissao*, e nenhuma passada de verificacao olha para ele.

**O que fazer.** O modo deterministico e para testar o encanamento sem queimar
token — nao e um modo de producao economico. Se for rodar assim de proposito,
baixe `gate.merge_confidence_step` para perto de zero: sem juiz, e melhor a
confianca ficar parada do que subir na direcao errada.

---

## 2. Prompt em forma de persona faz o modelo recusar a tarefa

**O que e.** A primeira versao do prompt do juiz comecava com "Voce e o portao
de surpresa de um motor de memoria. Sua unica tarefa e... Responda SOMENTE com
JSON". O `claude -p` **recusou**, lendo aquilo como tentativa de jailbreak
(troca de persona + formato de saida imposto + "nao converse").

Falhou 1 vez em 3 na primeira medicao — silenciosamente, porque a resposta era
prosa educada em vez de JSON, e virava `resposta sem JSON reconhecivel`.

**O que fazer.** Enquadrar todo prompt interno como **descricao de tarefa**
("Tarefa de classificacao para um motor de memoria. Avalie..."), nunca como
adocao de persona. Com a reescrita, 3/3 responderam. Vale para o portao, a
consolidacao, o auditor e o desempate temporal. Ha uma nota no topo de
`JUDGE_PROMPT` avisando quem for reescrever.

---

## 3. Cold start: com memoria vazia, quase tudo parece inedito

**O que e.** O sinal `assunto_inedito` compara a troca com tudo que ja foi
armazenado. Num banco novo, isso e o conjunto vazio, entao toda troca carimba.
Nos testes, "qual o clima em Curitiba" virou episodio.

**O que fazer.** Nada no portao. A defesa certa e o purgatorio: episodio inutil
nao e usado, nao forma cluster, e expira sem nunca ter custado consolidacao. O
desperdicio de um carimbo e uma linha em disco; o desperdicio de uma
consolidacao ansiosa e uma chamada do modelo caro.

---

## 4. A latencia do juiz nao vem do modelo

**O que e.** Medido nesta maquina: 10,7s a 16,3s por chamada do juiz com
`--model haiku`, e US$ 0,013-0,017 por chamada. Uma chamada trivial ("responda
{"ok": true}") levou 2,3s e custou US$ 0,018. Ou seja: **o custo e a latencia
sao dominados pela partida do CLI e pelo system prompt do Claude Code**, nao
pelo tamanho do nosso prompt.

**O que fazer.** Encurtar prompt quase nao ajuda; o que ajuda e *nao chamar*.
Dai a camada 1 resolver a maioria de graca. `gate.judge_timeout_s` (padrao 25s)
garante que o portao nunca vire gargalo — estourou, a camada 1 decide e o
estouro fica logado como `resolucao: llm_timeout`.

---

## 5. O JSON as vezes vem embrulhado em lista

**O que e.** A mesma pergunta, com o mesmo prompt, as vezes volta `{...}` e as
vezes `[{...}]`. Nao e erro do modelo, e variacao de formatacao.

**O que fazer.** `llm.as_object()` desembrulha lista de um elemento. Tratar isso
como falha custaria uma decisao boa e ja paga.

---

## 6. Jaccard nao serve para agrupar assunto; dice serve

**O que e.** O agrupamento do purgatorio comecou com jaccard >= 0,25. Numa cena
real de 4 episodios sobre o mesmo assunto, o jaccard espremeu todos os pares
entre **0,06 e 0,29** — nao ha limiar que separe "mesmo assunto" de "assunto
diferente" nessa faixa. Trocado por **dice**, os mesmos pares se abriram: 0,38 a
0,44 para o assunto real, 0,11 a 0,22 para o periferico.

**Por que.** Jaccard divide pela uniao, entao pune diferenca de tamanho: uma
frase curta e um episodio longo sobre a mesma coisa parecem distantes. Dice
divide pela soma, que e mais generoso com assimetria.

**O que fazer.** Botao: `purgatory.cluster_similarity` (padrao 0,30).

---

## 7. Agrupamento por palavra-chave perde o mesmo assunto dito com outras palavras

**O que e.** Na mesma cena, o episodio "PDF e melhor porque eu leio no celular
entre reunioes" NAO entrou no cluster dos outros tres sobre formato de
relatorio. Ele compartilha uma palavra so ("pdf") com os demais: dice 0,11-0,22.
E o mesmo assunto para um humano e um assunto diferente para o contador de
palavras.

**Consequencia.** O episodio fica no purgatorio, nao amadurece por cluster, e
so sobrevive se for usado na recuperacao ou se for velho e muito surpreendente.
Uma preferencia expressa uma unica vez, com vocabulario proprio, pode nunca
virar fato.

**O que fazer.** E exatamente o caso que embeddings resolveriam — a Fase 7
hipotetica. Enquanto nao doer de verdade, o custo de errar aqui e um episodio
esquecido, nao um fato falso.

---

## 8. Uma rodada de sono custa ~30x uma decisao do portao

**Medido.** Rodadas reais deste build: US$ 0,26 e US$ 0,21, com 3 chamadas cada
(destilar + auditar + desempatar), ~40s por rodada. Uma decisao do portao com
haiku custa US$ 0,013-0,017.

**Leitura.** E a justificativa numerica do purgatorio: consolidar ansiosamente
toda troca carimbada custaria uma rodada dessas por episodio. O botao
`llm.consolidation_model` aceita `haiku` para cortar isso, ao preco de
destilacao pior — a consolidacao e o unico lugar do motor onde a qualidade do
modelo vira qualidade permanente da memoria.

---

## 9. O `claude -p` tenta usar ferramenta e queima o unico turno

**O que e.** Com o modelo padrao, o `chat.py` falhava com `is_error: true`,
`stop_reason: "tool_use"`, `num_turns: 2`. O CLI carrega o system prompt do
Claude Code inteiro, incluindo as ferramentas; o modelo decide usar uma, e isso
consome o unico turno permitido por `--max-turns 1`.

**O que fazer.** Toda chamada agora leva `--allowed-tools ""` e um
`--system-prompt` proprio (de tarefa, ou de conversa no `chat.py`). Efeito
colateral bom: o input caiu de ~25,5k para ~19k tokens.

**O teto que sobra.** Mesmo com tudo cortado — `--strict-mcp-config`,
`--exclude-dynamic-system-prompt-sections`, system prompt proprio — o piso
ficou em ~19k tokens de entrada por chamada. Medido: 25,5k → 25,4k → 25,2k →
19,0k. Encurtar o nosso prompt nao move esse numero. O custo por chamada e
quase todo overhead de processo, o que reforca o desenho inteiro: o que importa
e **nao chamar**, nao chamar barato.

Custo medido por turno de `chat.py`: US$ 0,075 a 0,10 (resposta + portao).

---

## 10. Mencionar o passado nao e perguntar sobre o passado

**O que e.** O detector de consulta temporal disparava em "Na verdade mudei de
ideia sobre reunioes" — a frase tem "mudei", mas nao e uma pergunta ao
historico, e uma mudanca acontecendo agora. "Marca a reuniao antes das 9h" tem
o mesmo problema com "antes".

**Consequencia.** Fatos com vigencia encerrada voltavam a competir em turnos do
presente — exatamente o que a regra de vigencia existe para impedir.

**O que fazer.** Agora exige marcador de passado **E** contexto interrogativo
(pronome interrogativo ou "?"). Quatro falsos positivos reais viraram teste.

---

## 11. Radicalizar quebrou a busca por prefixo

**O que e.** A busca monta `"palavra"*` para alcancar plural. Mas o radical de
"reunioes" e "reuniao", que **nao e prefixo** de "reunioes" — entao perguntar
"quanto duravam as reunioes?" nao encontrava o fato que dizia "reunioes".
Apareceu na primeira pergunta temporal de verdade: o motor respondeu "nao tenho
nada registrado sobre duracao de reunioes" tendo dois fatos sobre isso.

**O que fazer.** A consulta leva as duas formas (crua e radicalizada) e, para
palavras em -ao/-oes, tambem o radical comum (`"reuni"*`), que alcanca as duas
grafias nas duas direcoes.

**Licao geral.** Radicalizacao para COMPARAR (dice, cobertura) e radicalizacao
para BUSCAR (prefixo FTS) tem requisitos diferentes. A mesma funcao nao serve
para as duas coisas.

---

## 12. Surpresa baixa nao autoriza fundir em qualquer fato

**O que e.** A fusao mirava o fato de maior sobreposicao, sem piso. Numa troca
sobre reunioes com um banco cheio de fatos sobre relatorios, o "menos distante"
ganhava e recebia confianca. Envenenamento silencioso por proximidade.

**O que fazer.** Fusao agora exige cobertura >= `gate.merge_coverage_min`; sem
isso, a decisao e "passe". Ninguem ganha confianca por ser o menos ruim.

---

## 13. O portao acusa contradicao no fato errado (mas acerta a decisao)

**O que e.** Na troca "mudei de ideia sobre reunioes", o portao carimbou com o
rotulo "contradiz fato #2" — e o #2 e sobre relatorios. Os fatos realmente
contrariados eram #5 e #6. A atribuicao vem da sobreposicao de palavras, e
"manha" aparecia nos dois assuntos.

**Impacto.** Nenhum no comportamento: o carimbo estava certo, e quem decide de
verdade qual fato morre e o passo de destino do sono (que usa o modelo caro e
acertou os dois). O rotulo do portao e uma pista, nao um veredito.

---

## 14. O portao pega falha de memoria do proprio assistente

**Observado, nao projetado.** Quando a recuperacao falhou (item 11) e o
assistente respondeu "nao tenho nada registrado sobre duracao de reunioes", o
portao carimbou a troca com surpresa 8,0 e o motivo: *"o Assistente nega ter
informacao sobre duracao de reunioes, mas o fato #7 ja registra explicitamente
que Rafael prefere blocos de 1 hora"*.

O portao compara a troca INTEIRA com os fatos — e a troca inclui a resposta do
assistente. Uma contradicao entre o que o motor sabe e o que o assistente disse
e, tecnicamente, surpresa. Sai de graca um detector de falha de recuperacao.

---

## 15. Cold start: as primeiras mensagens sao as MENOS capturadas

**Observado no eval.** Turno 1 de um banco limpo — "Meu nome e Rafael e eu
trabalho com gestao de trafego pago" — resultou em **passe**. A informacao mais
fundamental da conversa nao foi guardada.

**Por que.** O sinal `assunto_inedito` compara a troca com tudo que ja foi
armazenado, e tinha uma guarda `if known:` para nao carimbar tudo num banco
vazio (aresta 3). So que a guarda faz o contrario do necessario: com memoria
vazia, NADA e inedito, e nao ha fato para contradizer, entao o juiz ve
"(nenhum fato armazenado ainda)" e pontua baixo. O motor e mais cego
justamente quando esta mais vazio.

**O que fazer.** A guarda passou a exigir apenas conteudo minimo
(`gate.min_content_tokens`), nao memoria previa. Num banco vazio, toda troca
com pelo menos 3 palavras de conteudo carimba. "Bom dia, tudo certo?" continua
passando batido porque vira zero token depois das stopwords. O custo de errar
para o lado do carimbo aqui e um episodio que expira sozinho no purgatorio; o
custo de errar para o outro lado e nunca saber o nome do usuario.

---

## 16. Por que a autoinconsistencia tem razao propria

**O que e.** Um carimbo pode nascer de duas coisas muito diferentes:

  - o usuario disse algo novo   -> o motor esta funcionando
  - o assistente contradisse um fato vigente -> o motor FALHOU em recuperar

Somar os dois numa "taxa de carimbo" esconde a segunda. Uma taxa de carimbo
saudavel de 20% pode ser 20% de novidade legitima ou 15% de novidade e 5% de
falha de recuperacao — e o segundo caso exige mexer em `retrieval.top_k` ou
`decay.sink_threshold`, nao no limiar de surpresa.

**Como e detectado.** Camada 1: padroes de negacao de conhecimento no turno do
ASSISTENTE ("nao tenho nada registrado", "nao consta", "voce nao me disse")
**mais** um fato vigente relacionado — negar o que ninguem contou e correto, e
so vira sintoma quando o motor sabia. Camada 2: o juiz declara `origem`
("usuario" ou "assistente").

**Confirmado no eval:** turno 8 ("manda o relatorio em PDF de novo") virou
`carimbo (autoinconsistencia)` numa conversa real.

---

## 17. Apagar a evidencia de uma mudanca reabre o fato antigo

**O caso.** O fato B substituiu o fato A, entao A ficou com `valid_to` fechado.
Depois o unico episodio que sustentava B e apagado. B e aposentado (fato sem
proveniencia nao existe). E o `valid_to` de A?

**A decisao: reabre.** `valid_to` volta a NULL, `superseded_by` volta a NULL, e
o evento `substituicao_revertida` guarda o `valid_to` anterior.

**Por que nao "historia e historia".** Aquele `valid_to` nao era um fato
observado — era uma CONCLUSAO tirada do episodio que acabou de ser apagado.
Mantendo A fechado, o assunto ficaria sem nenhum fato vigente: nem B
(aposentado por falta de origem) nem A (encerrado por uma razao que nao existe
mais). O motor responderia "nao sei" sobre algo que sabe.

**A fronteira.** Se B sobrevive ao reprocessamento — porque sobrou outro
episodio — A continua encerrado, e o A passa a apontar para o B novo. A
reabertura so acontece quando a mudanca fica sem NENHUMA evidencia.

---

## 18. Novidade media pela troca inteira conta as palavras do assistente

**O que e.** O sinal `assunto_inedito` comparava a troca INTEIRA — pergunta do
usuario mais resposta do assistente — com o vocabulario ja conhecido. Como o
assistente escreve prosa longa e variada a cada turno, a novidade ficava alta
artificialmente.

**Medido no eval.** "Obrigado, era isso por hoje" foi CARIMBADO com novidade
**1,0**. As palavras novas eram todas da resposta ("janela de otimizacao",
"criativo em rotacao"). O usuario nao trouxe nada. Isso sozinho respondia por 3
dos 11 carimbos e jogou a taxa para 61%.

**O que fazer.** A novidade agora mede so o turno do usuario. Duas sutilezas no
fallback: se a troca nao tem rotulos identificaveis, usa o texto inteiro (nao
da para separar); mas se o usuario falou e o que ele falou vira zero token de
conteudo, a resposta certa e "nao ha novidade" — nao "entao olha o que o
assistente escreveu".

---

## 19. A fusao de graca pode engolir uma contradicao do assistente

**O que e.** Quando a troca ecoa bem um fato e traz poucas palavras novas, a
camada 1 funde sem consultar o juiz. Se a contradicao estiver na RESPOSTA do
assistente e for sutil ("acho que voce prefere reunioes longas", quando o fato
diz 30 minutos), ela e contada como confirmacao e o fato ganha confianca.

**Cobertura parcial.** A heuristica de autoinconsistencia pega negacao
explicita ("nao tenho registrado", "nao consta"). Afirmacao errada dita com
confianca escapa dela.

**Por que nao esta consertado.** Detectar isso exige comparar semanticamente a
resposta com o fato — ou seja, o juiz, ou seja, pagar em toda troca rotineira.
E o mesmo trade-off da aresta 1, e a mesma resposta: quem quiser fechar esse
buraco baixa `gate.merge_coverage_min` para forcar mais trocas ao juiz, e paga
por isso.

---

## 20. O benchmark é adversarial à tese da fusão; conversas reais não são

**Medido, custo zero, sobre 723 trocas reais do LongMemEval:** a camada 1
resolveria **1%** das trocas de graça. **99%** cairiam no juiz.

**Não é idioma.** As heurísticas são regex em português e o benchmark é em
inglês, mas escrevendo os equivalentes em inglês os sinais duros aparecem em
apenas **~6%** das trocas. A tradução levaria 99% para ~93%.

**A causa é estrutural.** A economia do portão vem da FUSÃO, e fusão exige um
fato consolidado que a troca ecoe. O haystack do LongMemEval é construído com
sessões de assuntos não relacionados e distratores deliberados — por desenho,
quase nada confirma nada. Não há o que fundir.

**O rótulo honesto:** *o benchmark é adversarial à tese da fusão, e conversas
reais não são.* Numa conversa de verdade a pessoa repete, confirma e volta aos
mesmos assuntos — foi o que medimos no `eval.py`, onde a camada 1 resolveu de
graça uma parcela relevante. O número do LongMemEval é o pior caso da nossa
economia, não o caso típico. Reportar os dois é o que torna a leitura honesta.

---

## 21. Destilação com perda descarta justamente o detalhe incidental

**O caso, do piloto oracle.** Troca ingerida:

> "I think the Lavender Dreams Skincare Set sounds like a great idea. **I was
> just at my cousin's wedding at the Grand Ballroom last weekend**, and my mom
> looked absolutely stunning, I want to get her something..."

A pergunta do benchmark: *"Where did I attend my cousin's wedding?"*

O portão CARIMBOU a troca. O sono consolidou. E os três fatos gerados foram:
aniversário da mãe, gostos da mãe, estilo da mãe. **O casamento sumiu** — é uma
oração incidental dentro de uma conversa sobre presente, e qualquer sumarização
razoável a descarta.

**A tensão de fundo.** O LongMemEval pergunta, por construção, sobre detalhes
incidentais ao propósito da conversa. Toda destilação com perda os descarta — e
destilação com perda é exatamente a proposta de valor deste motor. Os sistemas
que pontuam alto no benchmark (Zep, Letta) não destilam: indexam o texto bruto e
recuperam trechos.

**Agravante nosso.** Episódio consolidado sai do alcance da busca (a recuperação
cobre fatos + episódios `buffered`). Então perdemos duas vezes: o detalhe não
virou fato E o texto cru que o continha ficou inalcançável.

### A leitura final, e o que foi feito

**Destilação perde o incidental por desenho.** Não é bug, é o que ela é: trocar
volume por densidade. Um resumo que guardasse a oração incidental sobre o
casamento guardaria tudo, e aí não seria resumo.

**O híbrido não desiste do esquecimento — hierarquiza ele.** O episódio
consolidado continua buscável, com peso baixo, e só responde quando os fatos não
cobrem a pergunta. Ou seja: **sai do índice quente antes de sair do mundo.** São
três degraus, não dois:

  1. **quente** — fatos destilados, competem sempre
  2. **frio**   — episódios consolidados, competem só quando o quente falha
  3. **fora**   — afundado por decaimento (sai do frio), purgado ou deletado
                  (sai do disco, com a cascata de reprocessamento junto)

O esquecimento continua valendo em todos os degraus: deleção dirigida tira do
frio na hora, e o decaimento afunda o cru que ninguém resgata — mais devagar que
os fatos (meia-vida de 90 dias contra 30), porque o arquivo bruto é a rede de
segurança da destilação e vale segurar mais tempo.

**E cada queda para o degrau frio é medida.** Todo resgate emite
`resgate_episodico` com o episódio e a pergunta, e o `calibrate` reporta a taxa.
O número não mede o fallback funcionando — mede **a destilação falhando**. Se
subir, o botão a girar é o escopo do prompt de consolidação, não o do fallback.

### O que o híbrido NÃO resolve: o gargalo está antes dele

Medido no piloto oracle com o híbrido ligado: **75% das trocas (88 de 117) nunca
viraram episódio.** O portão as deixou passar. O fallback alcança só as 29 que
foram carimbadas — o resto não existe em lugar nenhum para ser resgatado.

O caso do casamento, rodado de novo, mostrou isso limpo: numa execução o portão
carimbou a troca e a consolidação perdeu o detalhe (item 21 original); na
seguinte o portão **nem carimbou** — a mesma frase, julgada não-surpreendente
numa conversa sobre presente de aniversário. A perda é estocástica e acontece em
dois lugares diferentes.

**A leitura completa, então, tem três degraus de perda, não um:**

  1. o portão descarta ~75% das trocas na porta        (por desenho: custo)
  2. a destilação descarta o incidental do que passou   (por desenho: densidade)
  3. o decaimento afunda o que ninguém usa              (por desenho: ruído)

O híbrido conserta o degrau 2 e é governado pelo 3. O degrau 1 é o teto, e ele é
a tese inteira do motor: **a acurácia num benchmark que pergunta sobre detalhes
uniformemente distribuídos é limitada pela taxa de carimbo.** Não há ajuste de
recuperação que contorne isso — só desligar o portão, que é desligar o produto.
