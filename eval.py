#!/usr/bin/env python3
"""eval.py — a suite de regressao ponta-a-ponta do motor.

Roda uma conversa roteirizada de 20 trocas pelo mesmo caminho do chat.py
(recuperacao -> resposta -> portao), com duas rodadas de sono no meio, e afirma
as propriedades que nao podem quebrar em nenhuma mudanca futura:

  1. a taxa de carimbo fica na faixa esperada para este roteiro
  2. ao menos uma substituicao temporal aconteceu
  3. a cascata de esquecimento rodou inteira
  4. nenhum fato existe sem proveniencia
  5. toda decisao do portao registrou COMO foi resolvida

Custa dinheiro de verdade: ~20 chamadas de conversa + as do portao + 2 sonos.
Use --estimar para ver a conta antes.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import chat
from memory import calibrate, config, db, sleep as sleep_mod

# ------------------------------------------------------------- o roteiro
# 12 rotineiras, 4 correcoes, 2 mudancas de assunto, 2 pedidos de esquecimento.

# A ordem importa mais do que parece. Uma troca so e "rotineira" se a memoria JA
# absorveu o assunto: reforcar uma mudanca que o motor ainda nao consolidou e
# legitimamente surpreendente, e o juiz acerta ao carimbar. Por isso o sono vem
# logo depois de cada mudanca, e nao no meio das confirmacoes dela.
ROTEIRO = [
    ("mudanca_assunto", "Meu nome e Rafael e eu trabalho com gestao de trafego pago."),
    ("mudanca_assunto", "Quero os relatorios das campanhas sempre em PDF, nunca em planilha."),
    ("__sono__",        None),
    ("rotineira",       "Manda o relatorio das campanhas dessa semana em PDF."),
    ("rotineira",       "Valeu, chegou certinho."),
    ("correcao",        "Nao, nao e campanha de conversao que eu pedi, e de alcance."),
    ("rotineira",       "Beleza, obrigado."),
    ("rotineira",       "Manda o relatorio das campanhas em PDF de novo, por favor."),
    ("correcao",        "Na verdade mudei de ideia: quero os relatorios em xlsx a partir de agora."),
    ("__sono__",        None),
    ("rotineira",       "Isso, xlsx mesmo."),
    ("rotineira",       "O xlsx ficou bem melhor pra mexer nos numeros."),
    ("rotineira",       "Agenda uma call com o time pra amanha."),
    ("rotineira",       "Ok, combinado."),
    ("correcao",        "Errado, nao e com o time, e com o cliente."),
    ("rotineira",       "Perfeito assim."),
    ("rotineira",       "Quanto custou a campanha de alcance ontem?"),
    ("esquecimento",    "Esquece isso que eu falei sobre a call."),
    ("correcao",        "Nao, meu foco principal e Meta Ads, nao Google Ads."),
    ("rotineira",       "Confirmado, pode seguir."),
    ("esquecimento",    "Esquece o que eu falei sobre Meta Ads."),
    ("rotineira",       "Obrigado, era isso por hoje."),
]

TROCAS = [p for p in ROTEIRO if p[0] != "__sono__"]

# Faixa esperada de carimbo PARA ESTE ROTEIRO. Nao e a faixa saudavel de uma
# conversa real (10-30%): o roteiro concentra 6 trocas surpreendentes em 18 que
# passam pelo portao (as 2 de esquecimento nao passam), de proposito, para
# exercitar o caminho caro. Conversa de verdade e mais mansa.
FAIXA_CARIMBO = (15.0, 50.0)


class Resultado:
    def __init__(self):
        self.checks, self.custo, self.turnos = [], 0.0, 0

    def check(self, nome, ok, detalhe):
        self.checks.append((nome, bool(ok), detalhe))
        return ok

    @property
    def passou(self):
        return all(ok for _, ok, _ in self.checks)


def _silencio(*args, **kwargs):
    pass


def rodar(db_path, cfg, verbose=False) -> Resultado:
    res = Resultado()
    conn = db.open_db(db_path)
    mostrar = print if verbose else _silencio
    ultimo_episodio = None

    try:
        for i, (tipo, mensagem) in enumerate(ROTEIRO, 1):
            if tipo == "__sono__":
                print(f"  [{i:>2}] ── sono ──", flush=True)
                rel = sleep_mod.run_sleep(conn, cfg, force=True)
                res.custo += rel.cost_usd
                print(f"       {len(rel.facts_created)} fato(s), "
                      f"{len(rel.superseded)} substituicao(oes), "
                      f"US$ {rel.cost_usd:.4f}", flush=True)
                continue

            print(f"  [{i:>2}] {tipo:<15} {mensagem[:52]}", flush=True)
            saida = chat.turno(conn, mensagem, cfg, mostrar=mostrar,
                               ultimo_episodio=ultimo_episodio)
            res.custo += saida["custo_usd"]
            res.turnos += 1
            decisao = saida.get("decisao")
            if decisao is not None:
                print(f"       -> {decisao.action}"
                      f"{' (' + (decisao.stamp_reason or '') + ')' if decisao.action == 'carimbo' else ''}",
                      flush=True)
                if decisao.episode_id:
                    ultimo_episodio = decisao.episode_id
            elif saida.get("apagou"):
                relatorio = saida["relatorio"]
                print(f"       -> apagou ep#{relatorio.episode_id}: "
                      f"{len(relatorio.facts_retired)} aposentado(s), "
                      f"{len(relatorio.facts_reprocessed)} reprocessado(s), "
                      f"{len(relatorio.reopened)} reaberto(s)", flush=True)
            else:
                print("       -> nada apagado (alvo nao identificado)", flush=True)

        avaliar(conn, res)
    finally:
        conn.close()
    return res


def avaliar(conn, res: Resultado) -> None:
    decisoes = [json.loads(r["payload"]) for r in conn.execute(
        "SELECT payload FROM events WHERE kind IN "
        "('gate_stamp','gate_merge','gate_pass') ORDER BY id")]
    carimbos = [d for d in decisoes if d.get("decisao") == "carimbo"]
    taxa = 100.0 * len(carimbos) / len(decisoes) if decisoes else 0.0

    res.check(
        "taxa de carimbo na faixa do roteiro",
        FAIXA_CARIMBO[0] <= taxa <= FAIXA_CARIMBO[1],
        f"{taxa:.0f}% ({len(carimbos)}/{len(decisoes)}), "
        f"esperado {FAIXA_CARIMBO[0]:.0f}-{FAIXA_CARIMBO[1]:.0f}%")

    substituicoes = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE superseded_by IS NOT NULL").fetchone()[0]
    res.check("ao menos uma substituicao temporal", substituicoes >= 1,
              f"{substituicoes} fato(s) substituido(s), com valid_to fechado")

    apagados = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE status = 'deleted'").fetchone()[0]
    cascatas = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'forget_done'").fetchone()[0]
    res.check("cascata de esquecimento completada",
              apagados >= 1 and cascatas >= 1,
              f"{apagados} episodio(s) apagado(s), {cascatas} cascata(s) concluida(s)")

    vazios = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE status = 'deleted' "
        "AND raw_text != ?", ("[conteudo apagado a pedido do usuario]",)).fetchone()[0]
    res.check("raw_text apagado foi sobrescrito", vazios == 0,
              f"{vazios} episodio(s) deletado(s) ainda com texto original")

    orfaos = len(db.orphan_facts(conn))
    res.check("nenhum fato sem proveniencia", orfaos == 0,
              f"{orfaos} fato(s) orfao(s)")

    sem_razao = [d for d in decisoes
                 if not d.get("resolucao") or "pagou_llm" not in d]
    res.check("toda decisao registrou como foi resolvida", not sem_razao,
              f"{len(sem_razao)} decisao(oes) sem resolucao/pagou_llm")

    stamps_sem_razao = [d for d in carimbos if not d.get("razao_carimbo")]
    res.check("todo carimbo registrou a razao", not stamps_sem_razao,
              f"{len(stamps_sem_razao)} carimbo(s) sem razao_carimbo")


def estimar() -> None:
    print("Estimativa de custo do eval (medido no build):")
    print(f"  {len(TROCAS)} trocas x 1 resposta            ~ US$ 0.04-0.08 cada")
    print(f"  ate {len(TROCAS)} chamadas do juiz (so as inconclusivas) ~ US$ 0.015 cada")
    print("  2 rodadas de sono                    ~ US$ 0.20 cada")
    print("  2 identificacoes de alvo de delecao  ~ US$ 0.015 cada")
    print("  ----------------------------------------------------")
    print("  total esperado: US$ 1.20 a 2.50, ~6-10 minutos")
    print("\n  Dica: --chat-model haiku fica na ponta baixa da faixa.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="suite de regressao ponta-a-ponta")
    p.add_argument("--estimar", action="store_true", help="so mostra a conta")
    p.add_argument("--db", help="banco a usar (padrao: temporario e limpo)")
    p.add_argument("--chat-model", help="sobrepoe llm.chat_model (ex.: haiku)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.estimar:
        estimar()
        return 0

    cfg = config.load()
    if args.chat_model:
        cfg._data.setdefault("llm", {})["chat_model"] = args.chat_model

    tmp = None
    if args.db:
        caminho = Path(args.db)
    else:
        tmp = tempfile.TemporaryDirectory()
        caminho = Path(tmp.name) / "eval.db"

    print(f"eval: banco limpo em {caminho}")
    print(f"      {len(TROCAS)} trocas, 2 rodadas de sono\n")
    try:
        res = rodar(caminho, cfg, verbose=args.verbose)
    finally:
        pass

    print("\n" + "─" * 70)
    for nome, ok, detalhe in res.checks:
        print(f"  {'PASSA' if ok else 'FALHA'}  {nome}")
        print(f"         {detalhe}")
    print("─" * 70)
    print(f"  {res.turnos} turnos, custo total US$ {res.custo:.4f}")
    print(f"  {'VERDE — tudo passou' if res.passou else 'VERMELHO — ver acima'}")

    if tmp and not res.passou:
        destino = Path("eval-falhou.db")
        Path(caminho).replace(destino)
        print(f"  banco preservado em {destino} para inspecao")
    if tmp:
        tmp.cleanup()
    return 0 if res.passou else 1


if __name__ == "__main__":
    raise SystemExit(main())
