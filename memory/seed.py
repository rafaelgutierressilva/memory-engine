"""Dados de teste: ~10 episodios falsos e 3 fatos escritos a mao.

Nao ha inteligencia aqui — os scores de surpresa e os fatos foram escritos por
uma pessoa, de proposito, para que a Fase 1 possa ser inspecionada antes de o
portao (Fase 2) e o sono (Fase 3) existirem.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db

# (dias_atras, surpresa, motivos, status, texto)
EPISODES = [
    (14, 0.0, [], "buffered",
     "Rafael: bom dia, tudo certo?\nAssistente: bom dia! Tudo certo por aqui."),
    (13, 7.5, ["pedido explicito de memoria"], "consolidated",
     "Rafael: anota ai — eu trabalho com gestao de trafego pago, Meta Ads "
     "principalmente.\nAssistente: anotado."),
    (12, 0.0, [], "expired",
     "Rafael: qual o status daquele relatorio?\nAssistente: gerado ontem, sem "
     "pendencias."),
    (11, 8.0, ["correcao do usuario", "contradiz fato ativo"], "consolidated",
     "Rafael: nao, nao e Google Ads. Eu falei Meta Ads. O Google eu so uso de "
     "apoio.\nAssistente: corrigido — Meta Ads como principal."),
    (9, 0.0, [], "buffered",
     "Rafael: sobe o orcamento daquela campanha em 20%?\nAssistente: feito."),
    (8, 6.5, ["assunto inedito"], "consolidated",
     "Rafael: a partir de agora quero os relatorios sempre em PDF, nunca em "
     "planilha.\nAssistente: entendido, PDF."),
    (6, 0.0, [], "buffered",
     "Rafael: manda o relatorio da semana.\nAssistente: enviado em PDF, como "
     "combinado."),
    (5, 0.0, [], "buffered",
     "Rafael: obrigado.\nAssistente: de nada!"),
    (3, 9.0, ["falha de ferramenta"], "buffered",
     "Rafael: tenta de novo.\nAssistente: a chamada a API do Meta falhou com "
     "erro de permissao no ad account 120xxxx."),
    (1, 0.0, [], "buffered",
     "Rafael: e o relatorio de hoje?\nAssistente: sai em PDF ate as 18h."),
]

# (statement, confianca, nucleo?, indices dos episodios (0-based) que sustentam)
FACTS = [
    ("Rafael trabalha com gestao de trafego pago, com foco principal em Meta Ads.",
     0.90, True, [1, 3]),
    ("Google Ads e ferramenta de apoio para o Rafael, nao a principal.",
     0.70, False, [3]),
    ("Rafael quer os relatorios sempre em PDF, nunca em planilha.",
     0.85, True, [5, 6]),
]


def run_seed(conn, reset: bool = False) -> int:
    if reset:
        for table in ("provenance", "facts", "episodes", "events"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
        print("banco zerado.")

    if conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]:
        print("banco ja tem episodios — use --reset para zerar antes.")
        return 1

    now = datetime.now(timezone.utc).replace(microsecond=0)
    episode_ids = []
    for days, surprise, reasons, status, text in EPISODES:
        ts = (now - timedelta(days=days)).isoformat()
        eid = db.add_episode(conn, text, surprise_score=surprise, reasons=reasons,
                             status=status, timestamp=ts)
        episode_ids.append(eid)
        db.log_event(conn, "episode_added", episode_id=eid, source="seed",
                     surprise=surprise, reasons=reasons)

    fact_ids = []
    for statement, confidence, core, idxs in FACTS:
        origem = [episode_ids[i] for i in idxs]
        valid_from = db.get_episode(conn, origem[0])["timestamp"]
        fid = db.add_fact(conn, statement, valid_from=valid_from,
                          confidence=confidence, is_core=core, episode_ids=origem)
        fact_ids.append(fid)
        db.log_event(conn, "fact_created", fact_id=fid, source="seed",
                     episode_ids=origem, confidence=confidence)

    print(f"semeados {len(episode_ids)} episodios e {len(fact_ids)} fatos "
          f"(ids {fact_ids[0]}–{fact_ids[-1]}).")
    return 0


def main() -> int:
    conn = db.open_db()
    try:
        return run_seed(conn, reset="--reset" in __import__("sys").argv)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
