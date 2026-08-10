#!/usr/bin/env python3
"""bench.py — LongMemEval pelo pipeline do motor.

Para cada pergunta:
  1. INGESTAO  cada sessao do historico passa troca a troca pelo portao;
               ao fim de cada sessao, um sono forcado consolida o que maturou
  2. RESPOSTA  retrieve(pergunta) -> [MEMORIA] + [PERGUNTA] -> claude -p
  3. JUIZ      o prompt OFICIAL do benchmark, por tipo de pergunta, com as
               leniencias que ele define (temporal aceita erro de +-1 dia,
               knowledge-update aceita a resposta antiga junto da nova)

Grava tudo incrementalmente em JSONL: matar no meio e retomar nao perde nada.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from memory import config, db, gate, llm, retrieval, sleep as sleep_mod

BENCH_DIR = Path(__file__).resolve().parent / "bench"

# ------------------------------------------------------- juiz oficial
# Copiado verbatim de src/evaluation/evaluate_qa.py do repositorio oficial
# (xiaowu0162/LongMemEval). Nao reescrever: a comparabilidade depende disso.

BASE = ("I will give you a question, a correct answer, and a response from a model. "
        "Please answer yes if the response contains the correct answer. Otherwise, "
        "answer no. If the response is equivalent to the correct answer or contains "
        "all the intermediate steps to get the correct answer, you should also answer "
        "yes. If the response only contains a subset of the information required by "
        "the answer, answer no. ")

TEMPLATES = {
    "default": BASE + "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
                      "Is the model response correct? Answer yes or no only.",
    "temporal-reasoning": BASE + (
        "In addition, do not penalize off-by-one errors for the number of days. If "
        "the question asks for the number of days/weeks/months, etc., and the model "
        "makes off-by-one errors (e.g., predicting 19 days when the answer is 18), "
        "the model's response is still correct. "
        "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
        "Is the model response correct? Answer yes or no only."),
    "knowledge-update": (
        "I will give you a question, a correct answer, and a response from a model. "
        "Please answer yes if the response contains the correct answer. Otherwise, "
        "answer no. If the response contains some previous information along with an "
        "updated answer, the response should be considered as correct as long as the "
        "updated answer is the required answer."
        "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
        "Is the model response correct? Answer yes or no only."),
    "single-session-preference": (
        "I will give you a question, a rubric for desired personalized response, and "
        "a response from a model. Please answer yes if the response satisfies the "
        "desired response. Otherwise, answer no. The model does not need to reflect "
        "all the points in the rubric. The response is correct as long as it recalls "
        "and utilizes the user's personal information correctly."
        "\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\n"
        "Is the model response correct? Answer yes or no only."),
    "abstention": (
        "I will give you an unanswerable question, an explanation, and a response "
        "from a model. Please answer yes if the model correctly identifies the "
        "question as unanswerable. The model could say that the information is "
        "incomplete, or some other information is given but the asked information is "
        "not."
        "\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\n"
        "Does the model correctly identify the question as unanswerable? Answer yes "
        "or no only."),
}

LENIENCIAS = {
    "temporal-reasoning": "aceita erro de +-1 dia na contagem de dias/semanas/meses",
    "knowledge-update": "aceita a resposta antiga junto da nova, se a nova estiver la",
    "single-session-preference": "julga por rubrica; nao exige todos os pontos",
    "abstention": "acerto = reconhecer que a pergunta nao tem resposta",
}


def judge_prompt(question_id: str, task: str, question: str, answer: str,
                 response: str) -> tuple[str, str]:
    """Devolve (prompt, rotulo da leniencia aplicada)."""
    if question_id.endswith("_abs"):
        return (TEMPLATES["abstention"].format(question, answer, response),
                "abstention")
    chave = task if task in TEMPLATES else "default"
    return TEMPLATES[chave].format(question, answer, response), chave


# ------------------------------------------------------- respostas

SYSTEM_BENCH = ("You are a helpful assistant answering a question about a user, "
                "using only the memory provided. Answer concisely and directly.")

ANSWER_PROMPT = """[MEMORY]
What is known about the user, retrieved from earlier conversations:
{memory}

[QUESTION] (asked on {date})
{question}

Answer using the memory above. If the memory does not contain the answer, say so
plainly instead of guessing."""


@dataclass
class QuestionResult:
    question_id: str = ""
    question_type: str = ""
    correct: bool = False
    hypothesis: str = ""
    answer: str = ""
    leniencia: str = ""
    # ingestao
    trocas: int = 0
    carimbos: int = 0
    fusoes: int = 0
    passes: int = 0
    autoinconsistencias: int = 0
    chamadas_juiz_portao: int = 0
    sonos: int = 0
    fatos: int = 0
    episodios_consolidados: int = 0
    episodios_nunca_consolidados: int = 0
    resgates: int = 0                  # trechos crus injetados na resposta
    cobertura_fatos: float = 1.0       # quanto os fatos cobriram a pergunta
    modo: str = "portao"
    # custo
    custo_ingestao: float = 0.0
    custo_resposta: float = 0.0
    custo_juiz: float = 0.0
    chamadas_llm: int = 0
    segundos: float = 0.0
    erro: str | None = None

    @property
    def custo_total(self) -> float:
        return self.custo_ingestao + self.custo_resposta + self.custo_juiz


def ingerir(conn, question, cfg, sono_a_cada: int, resultado: QuestionResult,
            modo: str = "portao") -> None:
    """Sessoes -> (portao) -> buffer -> sono forcado.

    modo="portao"  o desenho deste motor: so o surpreendente vira episodio.
    modo="eager"   a arquitetura do mercado reproduzida no nosso corpo: TODA
                   troca vira episodio, sem juiz, sem portao. O sono roda no
                   mesmo ritmo. Nada alem disso muda — otimizar o eager seria
                   transformar o braco num strawman ao contrario.
    """
    sessoes = question["haystack_sessions"]
    datas = question.get("haystack_dates") or [None] * len(sessoes)

    for idx, sessao in enumerate(sessoes):
        for i in range(len(sessao) - 1):
            u, a = sessao[i], sessao[i + 1]
            if u.get("role") != "user" or a.get("role") != "assistant":
                continue
            troca = (f"User: {u.get('content', '')}\n"
                     f"Assistant: {a.get('content', '')}")
            resultado.trocas += 1

            if modo == "eager":
                # sem portao: tudo entra. surpresa neutra porque nao houve juiz.
                db.add_episode(conn, troca, surprise_score=5.0,
                               reasons=["ingestao eager, sem portao"])
                resultado.carimbos += 1
                continue

            d = gate.process(conn, troca, cfg)
            resultado.custo_ingestao += d.cost_usd
            if d.paid_llm:
                resultado.chamadas_juiz_portao += 1
                resultado.chamadas_llm += 1
            if d.action == "carimbo":
                resultado.carimbos += 1
                if d.self_inconsistent:
                    resultado.autoinconsistencias += 1
            elif d.action == "fusao":
                resultado.fusoes += 1
            else:
                resultado.passes += 1

        if (idx + 1) % sono_a_cada == 0 or idx == len(sessoes) - 1:
            rel = sleep_mod.run_sleep(conn, cfg, force=True)
            resultado.sonos += 1
            resultado.custo_ingestao += rel.cost_usd
            resultado.chamadas_llm += rel.llm_calls


def responder(conn, question, cfg, resultado: QuestionResult) -> str:
    rec = retrieval.retrieve(conn, question["question"], cfg)
    resultado.resgates = len(rec.resgates)
    resultado.cobertura_fatos = round(rec.cobertura_fatos, 3)
    memoria = rec.render() or "(nothing relevant found in memory)"
    prompt = ANSWER_PROMPT.format(memory=memoria, date=question.get("question_date", ""),
                                  question=question["question"])
    r = llm.call(prompt, model=cfg.get("llm.chat_model", "default"), cfg=cfg,
                 expect_json=False, system=SYSTEM_BENCH)
    resultado.custo_resposta += r.cost_usd
    resultado.chamadas_llm += 1
    return r.text.strip() if r.ok else ""


def julgar(question, hypothesis: str, cfg, resultado: QuestionResult) -> tuple[bool, str]:
    prompt, leniencia = judge_prompt(
        question["question_id"], question["question_type"],
        question["question"], question["answer"], hypothesis)
    r = llm.call(prompt, model=cfg.get("llm.gate_model", "haiku"), cfg=cfg,
                 expect_json=False,
                 system="You are a strict grader. Answer only 'yes' or 'no'.")
    resultado.custo_juiz += r.cost_usd
    resultado.chamadas_llm += 1
    # parsing identico ao oficial: label = 'yes' in response.lower()
    return ("yes" in (r.text or "").lower()), leniencia


def rodar_pergunta(question, cfg, sono_a_cada: int, modo: str = "portao") -> QuestionResult:
    res = QuestionResult(question_id=question["question_id"],
                         question_type=question["question_type"],
                         answer=question["answer"], modo=modo)
    inicio = time.monotonic()
    tmp = tempfile.TemporaryDirectory()
    try:
        conn = db.open_db(Path(tmp.name) / "bench.db")
        try:
            ingerir(conn, question, cfg, sono_a_cada, res, modo)
            res.fatos = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE status='active'").fetchone()[0]
            res.episodios_consolidados = conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE status='consolidated'").fetchone()[0]
            res.episodios_nunca_consolidados = conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE status != 'consolidated'").fetchone()[0]
            res.hypothesis = responder(conn, question, cfg, res)
            res.correct, res.leniencia = julgar(question, res.hypothesis, cfg, res)
        finally:
            conn.close()
    except Exception as exc:                       # nunca derruba a corrida inteira
        res.erro = f"{type(exc).__name__}: {exc}"
    finally:
        tmp.cleanup()
    res.segundos = time.monotonic() - inicio
    return res


# ------------------------------------------------------- persistencia

def carregar_feitas(saida: Path) -> dict:
    feitas = {}
    if saida.exists():
        for linha in saida.read_text(encoding="utf-8").splitlines():
            if linha.strip():
                try:
                    d = json.loads(linha)
                    feitas[d["question_id"]] = d
                except json.JSONDecodeError:
                    continue
    return feitas


def gravar(saida: Path, res: QuestionResult) -> None:
    with saida.open("a", encoding="utf-8") as f:
        f.write(json.dumps(res.__dict__, ensure_ascii=False) + "\n")


# ------------------------------------------------------- relatorio

def relatorio(feitas: dict, variante: str, saida_md: Path | None = None) -> str:
    rs = list(feitas.values())
    validos = [r for r in rs if not r.get("erro")]
    linhas = []
    p = linhas.append

    p(f"# LongMemEval ({variante}) — {len(validos)} perguntas\n")

    # ---------------- tabela 1: acuracia por tipo
    p("## Tabela 1 — acurácia por tipo de pergunta\n")
    p("| tipo | n | acertos | acurácia |")
    p("|---|---:|---:|---:|")
    tipos = {}
    for r in validos:
        tipos.setdefault(r["question_type"], []).append(r["correct"])
    for tipo, acertos in sorted(tipos.items()):
        n, ok = len(acertos), sum(acertos)
        p(f"| {tipo} | {n} | {ok} | {100*ok/n:.0f}% |")
    total_ok = sum(r["correct"] for r in validos)
    p(f"| **geral** | **{len(validos)}** | **{total_ok}** | "
      f"**{100*total_ok/len(validos) if validos else 0:.0f}%** |")

    # ---------------- tabela 2: custo e eficiencia (a nossa tese)
    p("\n## Tabela 2 — custo e eficiência\n")
    p("A tabela que ninguém publica: não o quanto acerta, mas o quanto custa acertar.\n")
    trocas = sum(r["trocas"] for r in validos)
    carimbos = sum(r["carimbos"] for r in validos)
    fusoes = sum(r["fusoes"] for r in validos)
    juiz = sum(r["chamadas_juiz_portao"] for r in validos)
    chamadas = sum(r["chamadas_llm"] for r in validos)
    custo = sum(r["custo_total"] if "custo_total" in r else
                r["custo_ingestao"] + r["custo_resposta"] + r["custo_juiz"]
                for r in validos)
    nunca = sum(r["episodios_nunca_consolidados"] for r in validos)
    consolidados = sum(r["episodios_consolidados"] for r in validos)
    auto = sum(r["autoinconsistencias"] for r in validos)
    tokens = chamadas * 19_000            # piso medido do `claude -p`

    def linha(k, v, nota=""):
        p(f"| {k} | {v} | {nota} |")

    p("| métrica | valor | leitura |")
    p("|---|---:|---|")
    linha("trocas ingeridas", f"{trocas:,}", "o histórico inteiro passou pelo portão")
    linha("taxa de carimbo na ingestão", f"{100*carimbos/trocas:.1f}%" if trocas else "—",
          "quanto virou episódio; o resto não custou nada de consolidação")
    linha("taxa de fusão", f"{100*fusoes/trocas:.1f}%" if trocas else "—",
          "confirmações que viraram confiança em vez de linha nova")
    linha("trocas que nunca pagaram consolidação",
          f"{100*(trocas-carimbos)/trocas:.1f}%" if trocas else "—",
          "a economia do purgatório versus um sistema eager")
    linha("episódios consolidados / expirados",
          f"{consolidados} / {nunca}",
          "do que foi carimbado, quanto chegou a virar fato")
    linha("chamadas ao juiz do portão", f"{juiz:,}",
          f"{100*juiz/trocas:.0f}% das trocas" if trocas else "")
    linha("**autoinconsistência**", f"{auto} ({100*auto/trocas:.2f}% das trocas)" if trocas else "—",
          "o assistente contradizendo a memória — sobe antes da acurácia cair")
    linha("chamadas LLM totais", f"{chamadas:,}", "")
    linha("tokens de entrada (estimado)", f"{tokens/1e6:.1f}M",
          "piso medido de ~19k por chamada do `claude -p`")
    linha("custo total", f"US$ {custo:.2f}", "")
    linha("**custo por pergunta**", f"US$ {custo/len(validos):.3f}" if validos else "—", "")
    linha("**custo por acerto**",
          f"US$ {custo/total_ok:.3f}" if total_ok else "—",
          "a métrica que importa: acurácia POR token gasto")
    linha("tempo total", f"{sum(r['segundos'] for r in validos)/60:.0f} min", "")

    erros = [r for r in rs if r.get("erro")]
    if erros:
        p(f"\n**{len(erros)} pergunta(s) falharam:** "
          + ", ".join(f"{r['question_id']} ({r['erro']})" for r in erros[:5]))

    texto = "\n".join(linhas)
    if saida_md:
        saida_md.write_text(texto + "\n", encoding="utf-8")
    return texto


# ------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="LongMemEval pelo motor de memoria")
    p.add_argument("--variante", choices=("oracle", "s"), default="oracle")
    p.add_argument("-n", "--num", type=int, default=10, help="tamanho da amostra")
    p.add_argument("--seed", type=int, default=42, help="semente da amostra aleatoria")
    p.add_argument("--estratificado", action="store_true",
                   help="garante ao menos uma pergunta de cada tipo")
    p.add_argument("--modo", choices=("portao", "eager"), default="portao",
                   help="portao = este motor; eager = arquitetura do mercado")
    p.add_argument("--teto", type=float, default=None,
                   help="teto de gasto em USD; para limpo ao atingir")
    p.add_argument("--sono-a-cada", type=int, default=1,
                   help="sono forcado a cada N sessoes (1 = ao fim de cada sessao)")
    p.add_argument("--chat-model", help="sobrepoe llm.chat_model")
    p.add_argument("--saida", help="arquivo JSONL de resultados (retomavel)")
    p.add_argument("--so-relatorio", action="store_true",
                   help="nao roda nada; so imprime o relatorio do que ja existe")
    args = p.parse_args(argv)

    cfg = config.load()
    if args.chat_model:
        cfg._data.setdefault("llm", {})["chat_model"] = args.chat_model

    arquivo = {"oracle": "longmemeval_oracle.json", "s": "longmemeval_s.json"}[args.variante]
    saida = Path(args.saida or
                 BENCH_DIR / f"resultados_{args.variante}_{args.modo}_{args.num}.jsonl")
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida_md = saida.with_suffix(".md")

    feitas = carregar_feitas(saida)
    if args.so_relatorio:
        print(relatorio(feitas, args.variante, saida_md))
        return 0

    dados = json.loads((BENCH_DIR / "data" / arquivo).read_text(encoding="utf-8"))
    rng = random.Random(args.seed)
    if args.estratificado:
        # garante ao menos uma pergunta de cada tipo antes de completar a
        # amostra: com 10 perguntas sorteadas puras, os tipos raros
        # (preference = 6% do conjunto) somem, e junto com eles a chance de
        # verificar as leniencias do juiz oficial.
        por_tipo = {}
        for q in dados:
            por_tipo.setdefault(q["question_type"], []).append(q)
        amostra, vistos = [], set()
        for tipo in sorted(por_tipo):
            escolhido = rng.choice(por_tipo[tipo])
            amostra.append(escolhido)
            vistos.add(escolhido["question_id"])
        resto = [q for q in dados if q["question_id"] not in vistos]
        amostra += rng.sample(resto, max(0, min(args.num, len(dados)) - len(amostra)))
        amostra = amostra[:args.num]
    else:
        amostra = rng.sample(dados, min(args.num, len(dados)))

    pendentes = [q for q in amostra if q["question_id"] not in feitas]
    gasto = sum(f.get("custo_ingestao", 0) + f.get("custo_resposta", 0) +
                f.get("custo_juiz", 0) for f in feitas.values())
    print(f"LongMemEval {args.variante} — braco {args.modo.upper()} — "
          f"amostra {len(amostra)} (seed {args.seed})")
    print(f"  ja feitas: {len(feitas)}   pendentes: {len(pendentes)}")
    print(f"  sono forcado a cada {args.sono_a_cada} sessao(oes)")
    if args.teto:
        print(f"  teto de gasto: US$ {args.teto:.2f}  (ja gasto US$ {gasto:.2f})")
    print(f"  resultados: {saida}\n", flush=True)

    for i, q in enumerate(pendentes, 1):
        if args.teto and gasto >= args.teto:
            print(f"\n!! TETO DE US$ {args.teto:.2f} ATINGIDO (gasto US$ {gasto:.2f}). "
                  f"Parando limpo com {len(feitas)}/{len(amostra)} perguntas.",
                  flush=True)
            print("   O JSONL fica retomavel: subir o teto e rodar de novo continua "
                  "de onde parou.", flush=True)
            break
        n_sess = len(q["haystack_sessions"])
        print(f"[{i}/{len(pendentes)}] {q['question_id'][:20]:<22} "
              f"{q['question_type']:<26} {n_sess:>3} sessoes", flush=True)
        res = rodar_pergunta(q, cfg, args.sono_a_cada, args.modo)
        feitas[res.question_id] = res.__dict__
        gravar(saida, res)
        gasto += res.custo_total
        marca = "OK " if res.correct else "erro" if res.erro else "X  "
        print(f"     {marca} {res.trocas:>4} trocas, {res.carimbos:>4} episodios "
              f"({100*res.carimbos/res.trocas if res.trocas else 0:>3.0f}%), "
              f"{res.fatos:>3} fatos, {res.resgates} resgate(s), "
              f"US$ {res.custo_total:.3f}, {res.segundos/60:.1f} min "
              f"[acum US$ {gasto:.2f}]", flush=True)
        if res.erro:
            print(f"     ! {res.erro}", flush=True)

    print("\n" + relatorio(feitas, args.variante, saida_md))
    print(f"\nrelatorio salvo em {saida_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
