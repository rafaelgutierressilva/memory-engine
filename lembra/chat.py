#!/usr/bin/env python3
"""chat.py — conversar com o Claude ATRAVES do motor de memoria.

O ciclo de um turno:

    mensagem -> retrieve()  -> [MEMORIA] + [CONVERSA] -> claude -p -> resposta
                                                                        |
                             portao de surpresa <----------------------- +
                                     |
                             CARIMBADO / fundido / passe, dito em voz alta

A recuperacao acontece ANTES da resposta e ja conta o uso; o portao roda DEPOIS
da resposta aparecer na tela, para a latencia dele nao ficar entre a pergunta e
a resposta.
"""

from __future__ import annotations

import argparse
import sys
import time

from lembra import config, db, forget, gate, llm, retrieval

DIM, RESET = "\033[2m", "\033[0m"
GREEN, YELLOW, CYAN, RED, BOLD = ("\033[32m", "\033[33m", "\033[36m",
                                  "\033[31m", "\033[1m")

PROMPT_TEMPLATE = """[MEMORIA]
O que se sabe sobre a pessoa com quem voce esta falando, recuperado de conversas
anteriores. Use se for util; ignore o que nao vier ao caso.
{memory}

[CONVERSA]
{message}"""

PROMPT_SEM_MEMORIA = """[CONVERSA]
{message}"""


def _c(texto, cor):
    return f"{cor}{texto}{RESET}" if sys.stdout.isatty() else str(texto)


def montar_prompt(recuperacao: retrieval.Retrieval, mensagem: str) -> str:
    bloco = recuperacao.render()
    if not bloco:
        return PROMPT_SEM_MEMORIA.format(message=mensagem)
    return PROMPT_TEMPLATE.format(memory=bloco, message=mensagem)


SYSTEM_CHAT = ("Voce e um assistente pessoal conversando com o usuario. Responda "
               "de forma curta, direta e natural, em portugues do Brasil. O bloco "
               "[MEMORIA], quando existir, traz o que se sabe do usuario de "
               "conversas anteriores: use o que for util e ignore o resto.")


def responder(prompt: str, cfg) -> llm.LLMResult:
    return llm.call(prompt, model=cfg.get("llm.chat_model", "default"), cfg=cfg,
                    expect_json=False, system=SYSTEM_CHAT,
                    timeout=cfg.get("llm.timeout_seconds", 120))


ALVO_PROMPT = """Tarefa de identificacao de alvo para um motor de memoria.

A pessoa pediu para esquecer algo. Descubra O QUE ela quer apagar.

PEDIDO: {pedido}

TROCA ANTERIOR (a mais recente antes do pedido):
{anterior}

EPISODIOS CANDIDATOS ja guardados:
{candidatos}

Formato da resposta: um objeto JSON com "episode_id" (o numero do episodio a
apagar, ou null se nada servir) e "motivo" (texto curto explicando a escolha).
Se o pedido for generico ("esquece isso", "apaga o que eu falei"), o alvo e a
troca anterior. Exemplo: {{"episode_id": 42, "motivo": "o pedido se refere a ultima troca"}}"""


def identificar_alvo(conn, pedido: str, cfg, ultimo_episodio=None) -> tuple:
    """Descobre qual episodio o 'esquece isso' quer apagar."""
    candidatos = conn.execute(
        "SELECT * FROM episodes WHERE status IN ('buffered','consolidated') "
        "ORDER BY id DESC LIMIT 8").fetchall()
    if not candidatos:
        return None, "nao ha episodio nenhum guardado", 0.0

    anterior = db.get_episode(conn, ultimo_episodio) if ultimo_episodio else None
    prompt = ALVO_PROMPT.format(
        pedido=pedido,
        anterior=(" ".join(anterior["raw_text"].split()) if anterior
                  else "(nenhuma troca anterior nesta sessao)"),
        candidatos="\n".join(
            f"[#{c['id']}] {' '.join(c['raw_text'].split())[:160]}" for c in candidatos))

    resultado = llm.call(prompt, model=cfg.get("llm.gate_model", "haiku"), cfg=cfg,
                         timeout=cfg.get("gate.judge_timeout_s", 25))
    dados = llm.as_object(resultado.data)
    if not resultado.ok or dados is None:
        # sem juiz, o alvo padrao e a troca anterior — o significado mais comum
        return (ultimo_episodio, "identificacao por LLM falhou; usando a troca "
                "anterior", resultado.cost_usd)
    alvo = dados.get("episode_id")
    try:
        alvo = int(alvo)
    except (TypeError, ValueError):
        alvo = None
    return alvo, str(dados.get("motivo") or ""), resultado.cost_usd


def turno_esquecimento(conn, mensagem: str, cfg, ultimo_episodio, mostrar) -> dict:
    """Camada 1 detectou pedido de esquecimento: roda a cascata e confirma."""
    mostrar(_c("  ⟢ pedido de esquecimento detectado (heuristica, de graca)", YELLOW))
    alvo, motivo, custo = identificar_alvo(conn, mensagem, cfg, ultimo_episodio)

    if alvo is None or db.get_episode(conn, alvo) is None:
        mostrar("")
        mostrar("Nao consegui identificar o que voce quer que eu apague. "
                "Me diz qual assunto especificamente?")
        mostrar(_c(f"  ⟢ nada apagado ({motivo})", DIM))
        return {"custo_usd": custo, "decisao": None, "resposta": "", "apagou": False}

    episodio = db.get_episode(conn, alvo)
    trecho = " ".join(episodio["raw_text"].split())[:90]
    relatorio = forget.forget_episode(conn, alvo, cfg)
    custo += relatorio.cost_usd

    mostrar("")
    mostrar(f"Apaguei. O que sai da memoria: \"{trecho}\"")
    if relatorio.facts_retired:
        mostrar(f"Com isso, {len(relatorio.facts_retired)} fato(s) que dependiam "
                f"disso foram aposentados.")
    if relatorio.reopened:
        mostrar(f"E {len(relatorio.reopened)} preferencia anterior voltou a valer, "
                f"porque a evidencia da mudanca era justamente essa.")
    mostrar("")
    for linha in relatorio.linhas():
        mostrar(_c(f"  ⟢ {linha}", DIM))
    mostrar(_c(f"  ⟢ alvo escolhido: {motivo}", DIM))
    return {"custo_usd": custo, "decisao": None, "resposta": "", "apagou": True,
            "relatorio": relatorio}


def turno(conn, mensagem: str, cfg, *, verbose: bool = False,
          mostrar=print, ultimo_episodio=None) -> dict:
    """Um turno completo. Devolve o que aconteceu, para o eval.py inspecionar."""
    inicio = time.monotonic()

    # "esquece isso" nao e uma pergunta: e um comando, e a camada 1 pega de graca
    if gate.pede_esquecimento(mensagem):
        saida = turno_esquecimento(conn, mensagem, cfg, ultimo_episodio, mostrar)
        mostrar(_c(f"  ⟢ turno: {time.monotonic() - inicio:.1f}s, "
                   f"US$ {saida['custo_usd']:.4f}", DIM))
        return saida

    # 1. recuperacao — ja conta o uso de cada item injetado, na hora
    recuperacao = retrieval.retrieve(conn, mensagem, cfg)
    prompt = montar_prompt(recuperacao, mensagem)

    if verbose:
        mostrar(_c("┌─ memoria injetada " + "─" * 55, DIM))
        if recuperacao.all_items():
            for item in recuperacao.core:
                mostrar(_c(f"│ ★ nucleo  fato #{item.id}: {item.text}", CYAN))
            for item in recuperacao.winners:
                venceu = (f"score {item.score:.3f} = rel {item.relevance:.2f} × "
                          f"rec {item.recency:.2f} × conf {item.confidence:.2f}")
                mostrar(_c(f"│   {item.label():<10} {item.text[:60]}", DIM))
                mostrar(_c(f"│   {'':<10} {venceu}", DIM))
        else:
            mostrar(_c("│ (nada recuperado — memoria vazia ou sem relacao)", DIM))
        if recuperacao.historical:
            mostrar(_c("│ consulta TEMPORAL: fatos com vigencia encerrada entraram "
                       "na competicao", YELLOW))
        mostrar(_c(f"└─ {recuperacao.considered} candidatos, "
                   f"{len(recuperacao.winners)} vencedores", DIM))

    # 2. a resposta
    resultado = responder(prompt, cfg)
    resposta = resultado.text.strip() if resultado.ok else ""
    if not resultado.ok:
        mostrar(_c(f"[falha ao responder: {resultado.error}]", RED))
    else:
        mostrar("")
        mostrar(resposta)
        mostrar("")

    # 3. o portao, depois da resposta ja estar na tela
    troca = f"Rafael: {mensagem}\nAssistente: {resposta}"
    decisao = gate.process(conn, troca, cfg)
    cor = {"carimbo": YELLOW, "fusao": GREEN, "passe": DIM}[decisao.action]
    mostrar(_c(f"  ⟢ {decisao.headline()}", cor))

    custo = resultado.cost_usd + decisao.cost_usd
    mostrar(_c(f"  ⟢ turno: {time.monotonic() - inicio:.1f}s, US$ {custo:.4f}", DIM))
    return {"recuperacao": recuperacao, "resposta": resposta,
            "decisao": decisao, "custo_usd": custo, "prompt": prompt}


AJUDA = """comandos:
  /sair        encerra
  /verbose     liga/desliga o bloco de memoria injetada
  /memoria     mostra os fatos vigentes
  /sono        roda uma rodada de consolidacao agora
  /ajuda       esta lista"""


def repl(conn, cfg, verbose: bool) -> int:
    print(_c("motor de memoria — conversando com o Claude atraves dele.", BOLD))
    print(_c("digite /ajuda para os comandos, /sair para encerrar.\n", DIM))
    total, ultimo_episodio = 0.0, None
    while True:
        try:
            mensagem = input(_c("você › ", BOLD)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not mensagem:
            continue
        if mensagem in ("/sair", "/quit", "/exit"):
            break
        if mensagem == "/ajuda":
            print(AJUDA)
            continue
        if mensagem == "/verbose":
            verbose = not verbose
            print(_c(f"verbose {'ligado' if verbose else 'desligado'}", DIM))
            continue
        if mensagem == "/memoria":
            from lembra.cli import main as memctl
            memctl(["list-facts", "--current"])
            continue
        if mensagem == "/sono":
            from lembra.cli import main as memctl
            memctl(["sleep"])
            continue

        saida = turno(conn, mensagem, cfg, verbose=verbose,
                      ultimo_episodio=ultimo_episodio)
        total += saida["custo_usd"]
        if saida.get("decisao") is not None and saida["decisao"].episode_id:
            ultimo_episodio = saida["decisao"].episode_id
        print()

    print(_c(f"total da sessao: US$ {total:.4f}", DIM))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="conversa com o Claude atraves do motor")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="mostra o bloco de memoria injetado")
    p.add_argument("--db", help="banco alternativo")
    p.add_argument("-m", "--message", action="append",
                   help="modo nao-interativo: uma ou mais mensagens em sequencia")
    args = p.parse_args(argv)

    cfg = config.load()
    conn = db.open_db(args.db)
    try:
        if args.message:
            ultimo_episodio = None
            for mensagem in args.message:
                print(_c(f"você › {mensagem}", BOLD))
                saida = turno(conn, mensagem, cfg, verbose=args.verbose,
                              ultimo_episodio=ultimo_episodio)
                if saida.get("decisao") is not None and saida["decisao"].episode_id:
                    ultimo_episodio = saida["decisao"].episode_id
                print()
            return 0
        return repl(conn, cfg, args.verbose)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
