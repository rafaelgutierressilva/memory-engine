#!/usr/bin/env python3
"""comparar.py — a tabela crua dos dois bracos, sem leitura nenhuma.

O braco EAGER nao e controle: e a arquitetura do mercado (Mem0-like)
reproduzida no nosso corpo. Mesmas perguntas, mesma seed, mesmo juiz, mesmo
ritmo de sono. A unica variavel e o portao.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TOKENS_POR_CHAMADA = 19_000     # piso medido do `claude -p`


def carregar(caminho):
    if not Path(caminho).exists():
        return []
    return [json.loads(l) for l in open(caminho, encoding="utf-8") if l.strip()]


def agregar(rs):
    validos = [r for r in rs if not r.get("erro")]
    if not validos:
        return None
    custo = sum(r["custo_ingestao"] + r["custo_resposta"] + r["custo_juiz"]
                for r in validos)
    chamadas = sum(r["chamadas_llm"] for r in validos)
    trocas = sum(r["trocas"] for r in validos)
    acertos = sum(r["correct"] for r in validos)
    return {
        "n": len(validos), "acertos": acertos,
        "acuracia": 100 * acertos / len(validos),
        "trocas": trocas,
        "episodios": sum(r["carimbos"] for r in validos),
        "taxa_captura": 100 * sum(r["carimbos"] for r in validos) / trocas if trocas else 0,
        "fatos": sum(r["fatos"] for r in validos),
        "resgates": sum(r["resgates"] for r in validos),
        "taxa_resgate": 100 * sum(1 for r in validos if r["resgates"]) / len(validos),
        "cobertura_media": sum(r["cobertura_fatos"] for r in validos) / len(validos),
        "autoinc": sum(r["autoinconsistencias"] for r in validos),
        "chamadas": chamadas,
        "tokens": chamadas * TOKENS_POR_CHAMADA,
        "custo": custo,
        "custo_por_pergunta": custo / len(validos),
        "acuracia_por_dolar": acertos / custo if custo else 0,
        "minutos": sum(r["segundos"] for r in validos) / 60,
        "por_tipo": _por_tipo(validos),
    }


def _por_tipo(validos):
    tipos = {}
    for r in validos:
        tipos.setdefault(r["question_type"], []).append(r["correct"])
    return {t: (sum(v), len(v)) for t, v in sorted(tipos.items())}


def tabela(portao, eager) -> str:
    L = []
    p = L.append
    p("# Dois bracos, mesma amostra — a tabela crua\n")
    p("Mesmas perguntas, mesma seed, mesmo juiz, mesmo ritmo de sono.")
    p("A unica variavel e o portao de surpresa.\n")

    if not portao or not eager:
        p("**Corrida incompleta** — um dos bracos ainda nao tem resultado.\n")
        for nome, d in (("portao", portao), ("eager", eager)):
            p(f"- {nome}: {d['n'] if d else 0} pergunta(s)")
        return "\n".join(L)

    # ---------------- acuracia por tipo
    p("## Acuracia por tipo de pergunta\n")
    p("| tipo | portao | eager |")
    p("|---|---|---|")
    tipos = sorted(set(portao["por_tipo"]) | set(eager["por_tipo"]))
    for t in tipos:
        a = portao["por_tipo"].get(t, (0, 0))
        b = eager["por_tipo"].get(t, (0, 0))
        p(f"| {t} | {a[0]}/{a[1]} | {b[0]}/{b[1]} |")
    p(f"| **geral** | **{portao['acertos']}/{portao['n']} "
      f"({portao['acuracia']:.0f}%)** | **{eager['acertos']}/{eager['n']} "
      f"({eager['acuracia']:.0f}%)** |")

    # ---------------- ingestao, custo, eficiencia
    p("\n## Ingestao, custo, eficiencia\n")
    p("| metrica | portao | eager |")
    p("|---|---:|---:|")
    linhas = [
        ("trocas ingeridas", "trocas", "{:,.0f}"),
        ("episodios criados", "episodios", "{:,.0f}"),
        ("taxa de captura", "taxa_captura", "{:.1f}%"),
        ("fatos gerados", "fatos", "{:,.0f}"),
        ("resgates episodicos", "resgates", "{:,.0f}"),
        ("perguntas com resgate", "taxa_resgate", "{:.0f}%"),
        ("cobertura media dos fatos", "cobertura_media", "{:.2f}"),
        ("autoinconsistencias", "autoinc", "{:,.0f}"),
        ("chamadas LLM", "chamadas", "{:,.0f}"),
        ("tokens de entrada", "tokens", "{:,.0f}"),
        ("custo total (US$)", "custo", "{:.2f}"),
        ("custo por pergunta (US$)", "custo_por_pergunta", "{:.3f}"),
        ("tempo (min)", "minutos", "{:.0f}"),
    ]
    for rotulo, chave, fmt in linhas:
        p(f"| {rotulo} | {fmt.format(portao[chave])} | {fmt.format(eager[chave])} |")

    # ---------------- as duas colunas-sintese
    p("\n## Sintese\n")
    p("| | portao | eager | delta do portao |")
    p("|---|---:|---:|---:|")
    d_pontos = portao["acuracia"] - eager["acuracia"]
    d_dolares = portao["custo"] - eager["custo"]
    p(f"| acuracia | {portao['acuracia']:.0f}% | {eager['acuracia']:.0f}% | "
      f"{d_pontos:+.0f} pontos |")
    p(f"| custo total | US$ {portao['custo']:.2f} | US$ {eager['custo']:.2f} | "
      f"US$ {d_dolares:+.2f} |")
    p(f"| **acertos por dolar** | **{portao['acuracia_por_dolar']:.3f}** | "
      f"**{eager['acuracia_por_dolar']:.3f}** | "
      f"{portao['acuracia_por_dolar'] - eager['acuracia_por_dolar']:+.3f} |")
    p(f"| custo por acerto | "
      f"US$ {portao['custo']/portao['acertos'] if portao['acertos'] else float('nan'):.2f} | "
      f"US$ {eager['custo']/eager['acertos'] if eager['acertos'] else float('nan'):.2f} | |")
    return "\n".join(L)


def intersecao(a, b):
    """So as perguntas que os DOIS bracos terminaram.

    O teto de gasto pode truncar um braco antes do outro. Comparar conjuntos
    diferentes de perguntas inventaria uma diferenca que e de amostra, nao de
    arquitetura — entao a tabela so olha o que os dois responderam.
    """
    ids = {r["question_id"] for r in a} & {r["question_id"] for r in b}
    return ([r for r in a if r["question_id"] in ids],
            [r for r in b if r["question_id"] in ids], ids)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="tabela crua dos dois bracos")
    ap.add_argument("--portao", default="bench/b_portao.jsonl")
    ap.add_argument("--eager", default="bench/b_eager.jsonl")
    ap.add_argument("--saida", default="bench/dois_bracos.md")
    a = ap.parse_args(argv)

    brutos_p, brutos_e = carregar(a.portao), carregar(a.eager)
    comum_p, comum_e, ids = intersecao(brutos_p, brutos_e)
    aviso = ""
    if brutos_p and brutos_e and (len(brutos_p) != len(ids) or len(brutos_e) != len(ids)):
        aviso = (f"\n> **Amostra pareada:** {len(ids)} perguntas que os dois bracos "
                 f"terminaram (portao rodou {len(brutos_p)}, eager rodou "
                 f"{len(brutos_e)}; o teto de gasto truncou o mais caro).\n")
    texto = tabela(agregar(comum_p), agregar(comum_e))
    if aviso:
        texto = texto.replace("A unica variavel e o portao de surpresa.\n",
                              "A unica variavel e o portao de surpresa.\n" + aviso)
    Path(a.saida).write_text(texto + "\n", encoding="utf-8")
    print(texto)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
