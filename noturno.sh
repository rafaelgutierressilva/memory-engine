#!/usr/bin/env bash
# noturno.sh — os dois bracos do LongMemEval_S, sem supervisao.
#
#   portao  este motor: so o surpreendente vira episodio
#   eager   a arquitetura do mercado no nosso corpo: toda troca vira episodio
#
# Mesmas 10 perguntas, mesma seed, mesmo juiz, mesmo ritmo de sono (8 sessoes).
# A unica variavel e o portao.
#
# Retomavel: cada pergunta e gravada em JSONL assim que termina, e o bench pula
# as ja feitas. Este laco atravessa corte de janela, queda de rede e erro
# transitorio do CLI sem perder trabalho pago.
set -uo pipefail
# locale pt_BR faz o awk imprimir "50,00" e o argparse recusa. C forca ponto.
export LC_ALL=C LC_NUMERIC=C
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

N="${1:-10}"
TETO_TOTAL="${2:-100}"          # USD, os dois bracos somados
SONO=8
LOG="bench/noturno.log"
MAX_TENTATIVAS=60

gasto_de() {   # soma o custo ja gravado num arquivo de resultados
    [ -f "$1" ] || { echo 0; return; }
    "$DIR/.venv/bin/python" - "$1" <<'PY'
import json, sys
t = 0.0
for l in open(sys.argv[1], encoding="utf-8"):
    if l.strip():
        d = json.loads(l)
        t += d.get("custo_ingestao", 0) + d.get("custo_resposta", 0) + d.get("custo_juiz", 0)
print(f"{t:.4f}")
PY
}

feitas_em() { [ -f "$1" ] && grep -c . "$1" || echo 0; }

roda_braco() {
    local modo="$1" saida="$2" teto="$3"
    for tentativa in $(seq 1 $MAX_TENTATIVAS); do
        local n; n=$(feitas_em "$saida")
        if [ "$n" -ge "$N" ]; then
            echo "[$modo] completo: $n/$N" >> "$LOG"
            return 0
        fi
        local g; g=$(gasto_de "$saida")
        if awk "BEGIN{exit !($g >= $teto)}"; then
            echo "[$modo] teto de US\$ $teto atingido (US\$ $g) com $n/$N" >> "$LOG"
            return 2
        fi
        echo "--- [$modo] tentativa $tentativa: $n/$N feitas, US\$ $g gastos, $(date '+%F %T')" >> "$LOG"
        ./runbench --variante s -n "$N" --estratificado --sono-a-cada $SONO \
                   --modo "$modo" --teto "$teto" --saida "$saida" >> "$LOG" 2>&1
        [ $? -ne 0 ] && { echo "--- [$modo] saiu com erro; 60s e retoma" >> "$LOG"; sleep 60; }
    done
    echo "[$modo] desisti apos $MAX_TENTATIVAS tentativas" >> "$LOG"
    return 1
}

echo "=== inicio $(date '+%F %T') — $N perguntas por braco, teto US\$ $TETO_TOTAL ===" >> "$LOG"

# Metade do orcamento para cada braco. Se o primeiro sobrar, o segundo herda.
META=$(awk "BEGIN{printf \"%.2f\", $TETO_TOTAL/2}")
roda_braco portao bench/b_portao.jsonl "$META"
GASTO_P=$(gasto_de bench/b_portao.jsonl)
SOBRA=$(awk "BEGIN{printf \"%.2f\", $TETO_TOTAL - $GASTO_P}")
echo "=== braco portao fechou em US\$ $GASTO_P; eager recebe US\$ $SOBRA ===" >> "$LOG"
roda_braco eager bench/b_eager.jsonl "$SOBRA"

echo "=== fim $(date '+%F %T') ===" >> "$LOG"
"$DIR/.venv/bin/python" comparar.py >> "$LOG" 2>&1
