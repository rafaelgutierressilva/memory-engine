"""lembra — a CLI do motor de memoria.

Um binario so, subcomandos para tudo:

    lembra chat          conversar com o Claude atraves do motor
    lembra gate          rodar o portao sobre uma troca avulsa
    lembra sleep         consolidacao + decaimento
    lembra forget        delecao dirigida + cascata
    lembra inspect       fato + proveniencia + cadeia temporal
    lembra calibrate     que botao girar, e por que
    lembra eval          suite de regressao ponta-a-ponta
    lembra bench         LongMemEval
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap

from . import config, db

# ----------------------------------------------------------- formatacao

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, YELLOW, RED, CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[36m"

EP_COLOR = {"buffered": YELLOW, "consolidated": GREEN, "expired": DIM, "deleted": RED}
FA_COLOR = {"active": GREEN, "sunken": DIM, "retired": RED}


def _c(text, color):
    return f"{color}{text}{RESET}" if sys.stdout.isatty() else str(text)


def _short(text: str, width: int = 68) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _ts(value: str | None) -> str:
    return (value or "—")[:16].replace("T", " ")


def _rule(title: str = "") -> None:
    print(_c(f"── {title} " .ljust(78, "─") if title else "─" * 78, DIM))


# -------------------------------------------------------------- comandos

def cmd_init(args, conn):
    print(f"banco pronto em {config.load().db_path}")


def cmd_add_episode(args, conn):
    text = args.text if args.text else sys.stdin.read()
    if not text.strip():
        print("nada para gravar (texto vazio)", file=sys.stderr)
        return 1
    eid = db.add_episode(
        conn, text.strip(),
        surprise_score=args.surprise,
        reasons=args.reason or [],
        status=args.status,
    )
    db.log_event(conn, "episode_added", episode_id=eid,
                 source="lembra", surprise=args.surprise, reasons=args.reason or [])
    print(f"episodio #{eid} gravado como {args.status} (surpresa {args.surprise})")
    return 0


def cmd_list_episodes(args, conn):
    rows = db.list_episodes(conn, status=args.status, limit=args.limit)
    if not rows:
        print("nenhum episodio.")
        return 0
    _rule(f"episodios ({len(rows)})")
    print(f"{'ID':>4}  {'QUANDO':<16} {'STATUS':<12} {'SURP':>4} {'USOS':>4}  TEXTO")
    for r in rows:
        status = _c(r["status"].ljust(12), EP_COLOR.get(r["status"], ""))
        print(f"{r['id']:>4}  {_ts(r['timestamp']):<16} {status} "
              f"{r['surprise_score']:>4.1f} {r['use_count']:>4}  {_short(r['raw_text'], 40)}")
    return 0


def cmd_list_facts(args, conn):
    rows = db.list_facts(conn, status=args.status,
                         current_only=args.current, limit=args.limit)
    if not rows:
        print("nenhum fato.")
        return 0
    _rule(f"fatos ({len(rows)})")
    print(f"{'ID':>4}  {'STATUS':<9} {'CONF':>5} {'DE':<11} {'ATE':<11} {'C':<2} AFIRMACAO")
    for r in rows:
        core = "★" if r["is_core"] else " "
        ate = _ts(r["valid_to"])[:10] if r["valid_to"] else "vigente"
        status = _c(r["status"].ljust(9), FA_COLOR.get(r["status"], ""))
        print(f"{r['id']:>4}  {status} "
              f"{r['confidence']:>5.2f} {_ts(r['valid_from'])[:10]:<11} {ate:<11} {core:<2} "
              f"{_short(r['statement'], 46)}")
    return 0


def cmd_inspect(args, conn):
    from . import retrieval
    fact = db.get_fact(conn, args.fact_id)
    if fact is None:
        print(f"fato #{args.fact_id} nao existe", file=sys.stderr)
        return 1

    if fact["status"] == "sunken" and retrieval.resurrect(conn, fact["id"]):
        print(_c(f"  ↑ fato #{fact['id']} estava AFUNDADO e foi ressuscitado por "
                 f"consulta direta.", YELLOW))
        print(_c("    registrado como sinal de calibragem: o limiar de "
                 "afundamento pode estar agressivo demais.", DIM))
        print()
        fact = db.get_fact(conn, fact["id"])

    _rule(f"fato #{fact['id']}")
    print(textwrap.fill(fact["statement"], 78,
                        initial_indent="  ", subsequent_indent="  "))
    print()
    vig = "vigente" if fact["valid_to"] is None else f"encerrado em {_ts(fact['valid_to'])}"
    print(f"  status .......... {_c(fact['status'], FA_COLOR.get(fact['status'], ''))}"
          f"{'  ★ nucleo' if fact['is_core'] else ''}")
    print(f"  vigencia ........ {_ts(fact['valid_from'])} → {vig}")
    print(f"  confianca ....... {fact['confidence']:.2f}")
    print(f"  criado / confirm. {_ts(fact['created_at'])} / {_ts(fact['last_confirmed_at'])}")
    print(f"  usos ............ {fact['use_count']} (ultimo: {_ts(fact['last_used_at'])})")

    if fact["supersedes"] or fact["superseded_by"]:
        print()
        _rule("cadeia temporal")
        if fact["supersedes"]:
            old = db.get_fact(conn, fact["supersedes"])
            print(f"  ↑ substitui  #{old['id']}: {_short(old['statement'], 58)}")
        if fact["superseded_by"]:
            new = db.get_fact(conn, fact["superseded_by"])
            print(f"  ↓ substituido por #{new['id']}: {_short(new['statement'], 52)}")

    print()
    eps = db.episodes_for_fact(conn, fact["id"])
    _rule(f"proveniencia ({len(eps)} episodio(s))")
    if not eps:
        print(_c("  SEM PROVENIENCIA — nenhum fato deveria existir assim.", RED))
    for e in eps:
        print(f"  episodio #{e['id']}  {_ts(e['timestamp'])}  "
              f"{_c(e['status'], EP_COLOR.get(e['status'], ''))}  "
              f"surpresa {e['surprise_score']:.1f}  usos {e['use_count']}")
        motivos = json.loads(e["surprise_reasons"] or "[]")
        if motivos:
            print(f"    {_c('motivos: ' + ', '.join(motivos), DIM)}")
        body = "(raw_text apagado)" if e["status"] == "deleted" else e["raw_text"]
        for line in textwrap.wrap(" ".join(body.split()), 70) or [""]:
            print(f"    {_c(line, DIM)}")
    return 0


def cmd_inspect_episode(args, conn):
    ep = db.get_episode(conn, args.episode_id)
    if ep is None:
        print(f"episodio #{args.episode_id} nao existe", file=sys.stderr)
        return 1
    _rule(f"episodio #{ep['id']}")
    print(f"  quando .......... {_ts(ep['timestamp'])}")
    print(f"  status .......... {_c(ep['status'], EP_COLOR.get(ep['status'], ''))}")
    print(f"  surpresa ........ {ep['surprise_score']:.1f} "
          f"{json.loads(ep['surprise_reasons'] or '[]')}")
    print(f"  usos ............ {ep['use_count']} (ultimo: {_ts(ep['last_used_at'])})")
    print()
    for line in textwrap.wrap(" ".join(ep["raw_text"].split()), 74):
        print(f"  {line}")
    facts = db.facts_for_episode(conn, ep["id"])
    print()
    _rule(f"fatos destilados deste episodio ({len(facts)})")
    for f in facts:
        print(f"  fato #{f['id']}  [{f['status']}]  {_short(f['statement'], 56)}")
    return 0


def cmd_events(args, conn):
    rows = db.list_events(conn, kind=args.kind, limit=args.limit)
    if not rows:
        print("log vazio.")
        return 0
    _rule(f"eventos ({len(rows)}, mais recentes primeiro)")
    for r in rows:
        alvo = []
        if r["episode_id"]:
            alvo.append(f"ep#{r['episode_id']}")
        if r["fact_id"]:
            alvo.append(f"fato#{r['fact_id']}")
        print(f"{r['id']:>5}  {_ts(r['timestamp']):<16} {r['kind']:<18} "
              f"{','.join(alvo):<14} {_c(_short(r['payload'], 40), DIM)}")
    return 0


def cmd_gate(args, conn):
    """Roda o portao sobre uma troca avulsa (sem chamar o Claude p/ responder)."""
    from . import gate
    text = args.text if args.text else sys.stdin.read()
    if not text.strip():
        print("nada para avaliar (texto vazio)", file=sys.stderr)
        return 1
    use_llm = False if args.no_llm else None
    d = gate.process(conn, text, use_llm=use_llm)
    cor = {"carimbo": YELLOW, "fusao": GREEN, "passe": DIM}[d.action]
    print(_c(d.headline(), cor))
    if d.llm_error:
        print(_c(f"  (juiz LLM falhou: {d.llm_error})", RED))
    if args.verbose:
        print(f"  sinais ......... {d.signals or '—'}")
        print(f"  motivos ........ {d.reasons}")
        print(f"  cobertura fato . {d.fact_coverage:.2f} | novidade {d.novelty:.2f}")
    return 0


GATE_KINDS = ("gate_stamp", "gate_merge", "gate_pass")
DEC_COLOR = {"carimbo": YELLOW, "fusao": GREEN, "passe": DIM}


def cmd_gate_log(args, conn):
    """O KPI do carimbo: cada decisao e como ela foi resolvida."""
    rows = conn.execute(
        "SELECT * FROM events WHERE kind IN (?,?,?) ORDER BY id DESC LIMIT ?",
        (*GATE_KINDS, args.limit),
    ).fetchall()
    if not rows:
        print("o portao ainda nao decidiu nada.")
        return 0

    _rule(f"decisoes do portao ({len(rows)}, mais recentes primeiro)")
    print(f"{'ID':>5}  {'QUANDO':<16} {'DECISAO':<9} {'RESOLUCAO':<13} "
          f"{'PAGOU':<6} {'US$':>8} {'SURP':>5}  MOTIVO")
    total_cost, pagas = 0.0, 0
    por_resolucao, por_decisao = {}, {}
    for r in reversed(rows):
        p = json.loads(r["payload"])
        total_cost += p.get("custo_usd") or 0.0
        pagas += 1 if p.get("pagou_llm") else 0
        por_resolucao[p.get("resolucao")] = por_resolucao.get(p.get("resolucao"), 0) + 1
        por_decisao[p.get("decisao")] = por_decisao.get(p.get("decisao"), 0) + 1
    for r in rows:
        p = json.loads(r["payload"])
        dec = p.get("decisao", "?")
        pagou = _c("haiku", YELLOW) if p.get("pagou_llm") else _c("graca", GREEN)
        motivo = (p.get("motivos") or ["—"])[0]
        if p.get("autoinconsistencia"):
            motivo = _c("[AUTOINCONSISTENCIA] ", RED) + motivo
        elif p.get("expectativa_violada"):
            motivo += f" (viola {p['expectativa_violada']})"
        print(f"{r['id']:>5}  {_ts(r['timestamp']):<16} "
              f"{_c(dec.ljust(9), DEC_COLOR.get(dec, ''))} {p.get('resolucao', '?'):<13} "
              f"{pagou:<6} {p.get('custo_usd', 0):>8.4f} {p.get('surpresa', 0):>5.1f}  "
              f"{_short(motivo, 30)}")

    n = len(rows)
    print()
    _rule("KPI do portao")
    carimbos = por_decisao.get("carimbo", 0)
    print(f"  decisoes ........ {n}   " +
          "  ".join(f"{k}: {v}" for k, v in por_decisao.items()))
    taxa = 100 * carimbos / n
    faixa = _c("saudavel", GREEN) if 10 <= taxa <= 30 else _c("fora da faixa 10-30%", YELLOW)
    print(f"  taxa de carimbo . {taxa:.0f}%  ({faixa})")
    por_razao = {}
    for r in rows:
        p = json.loads(r["payload"])
        if p.get("decisao") == "carimbo":
            razao = p.get("razao_carimbo") or "desconhecida"
            por_razao[razao] = por_razao.get(razao, 0) + 1
    if por_razao:
        detalhe = "  ".join(f"{k}: {v}" for k, v in sorted(por_razao.items()))
        print(f"    por razao ..... {detalhe}")
        auto = por_razao.get("autoinconsistencia", 0)
        if auto:
            print(_c(f"    ⚠ {auto} carimbo(s) por AUTOINCONSISTENCIA — o "
                     f"assistente contradisse a memoria (falha de recuperacao, "
                     f"nao novidade)", RED))
    print(f"  resolvidas gratis {n - pagas}/{n} ({100 * (n - pagas) / n:.0f}%)   "
          + "  ".join(f"{k}: {v}" for k, v in por_resolucao.items()))
    print(f"  custo total ..... US$ {total_cost:.4f}"
          f"   (media US$ {total_cost / n:.4f}/decisao)")
    economia = pagas and total_cost / pagas or 0.0
    print(f"  economizado ..... US$ {(n - pagas) * economia:.4f} "
          f"{_c('(o que custaria se toda decisao pagasse haiku)', DIM)}")
    return 0


def cmd_sleep(args, conn):
    from . import sleep as sleep_mod
    cfg = config.load()
    if args.no_llm:
        cfg._data.setdefault("llm", {})["enabled"] = False

    rep = sleep_mod.run_sleep(conn, cfg, dry_run=args.dry_run, force=args.force)

    if rep.dry_run:
        _rule("ensaio do sono (nada foi gravado)")
    else:
        _rule("rodada de sono")

    print(f"  maduros ......... {len(rep.matured)}")
    for eid, motivo in rep.matured:
        print(f"    ep#{eid:<4} {_c(motivo, DIM)}")
    print(f"  a expirar ....... {len(rep.expired)}")
    for eid, motivo in rep.expired:
        print(f"    ep#{eid:<4} {_c(motivo, DIM)}")
    if rep.dry_run:
        return 0

    if rep.facts_created:
        print()
        _rule(f"fatos gravados ({len(rep.facts_created)})")
        for fid in rep.facts_created:
            f = db.get_fact(conn, fid)
            origem = [str(e["id"]) for e in db.episodes_for_fact(conn, fid)]
            print(f"  {_c('fato #' + str(fid), GREEN)}  {_short(f['statement'], 52)}")
            print(f"    {_c('proveniencia: ep#' + ', ep#'.join(origem), DIM)}")

    if rep.destinies:
        print()
        _rule(f"destinos decididos ({len(rep.destinies)})")
        cor_destino = {"substitui": YELLOW, "condicional": CYAN,
                       "excecao_isolada": DIM, "novo": GREEN}
        for afirmacao, destino, alvo, como in rep.destinies:
            marca = _c(destino.upper().ljust(16), cor_destino.get(destino, ""))
            ref = f" (fato #{alvo})" if alvo else ""
            print(f"  {marca}{_short(afirmacao, 44)}{ref}")
            print(f"    {_c(como, DIM)}")

    if rep.facts_rejected:
        print()
        _rule(f"reprovados pelo auditor ({len(rep.facts_rejected)})")
        for afirmacao, motivo in rep.facts_rejected:
            print(f"  {_c('DESCARTADO', RED)}  {_short(afirmacao, 56)}")
            print(f"    {_c('motivo: ' + motivo, DIM)}")

    if rep.superseded:
        print()
        _rule(f"substituicoes temporais ({len(rep.superseded)})")
        for antigo, novo, como in rep.superseded:
            velho = db.get_fact(conn, antigo)
            print(f"  fato #{antigo} encerrado em {_ts(velho['valid_to'])} "
                  f"-> substituido por #{novo}")
            print(f"    {_c(como, DIM)}")

    if rep.unused:
        print()
        print(_c(f"  {len(rep.unused)} episodio(s) maturaram e nao renderam fato "
                 f"(expirados p/ nao pagar consolidacao de novo): "
                 f"{', '.join('ep#' + str(i) for i in rep.unused)}", DIM))
    if rep.sunken:
        print()
        _rule(f"afundados pelo decaimento ({len(rep.sunken)})")
        for fid, score, idade in rep.sunken:
            f = db.get_fact(conn, fid)
            print(f"  fato #{fid}  score {score:.3f} apos {idade:.0f}d sem uso  "
                  f"{_c('(sai da busca, fica no disco)', DIM)}")
            print(f"    {_c(_short(f['statement'], 66), DIM)}")

    if rep.purged:
        print()
        _rule(f"purgados do disco ({len(rep.purged)})")
        for eid, motivo in rep.purged:
            print(f"  ep#{eid} — {motivo}")
    for aviso in rep.skipped:
        print(_c(f"  ! {aviso}", YELLOW))

    print()
    print(f"  chamadas LLM .... {rep.llm_calls}   custo US$ {rep.cost_usd:.4f}")
    return 0


def cmd_forget(args, conn):
    """Delecao dirigida: apaga e mostra a cascata inteira."""
    from . import forget as forget_mod
    alvo = db.get_episode(conn, args.episode_id)
    if alvo is None:
        print(f"episodio #{args.episode_id} nao existe", file=sys.stderr)
        return 1

    derivados = db.facts_for_episode(conn, args.episode_id)
    _rule(f"deletar episodio #{args.episode_id}")
    print(f"  {_short(alvo['raw_text'], 70)}")
    print(f"  sustenta {len(derivados)} fato(s): "
          f"{', '.join('#' + str(f['id']) for f in derivados) or '—'}")
    if not args.yes:
        try:
            if input("\n  confirmar delecao? [s/N] ").strip().lower() not in ("s", "sim"):
                print("  cancelado.")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\n  cancelado.")
            return 1

    rep = forget_mod.forget_episode(conn, args.episode_id)
    print()
    _rule("cascata")
    if rep.error:
        print(_c(f"  {rep.error}", RED))
        return 1
    for linha in rep.linhas():
        cor = (RED if "APOSENTADO" in linha else
               YELLOW if "REABERTO" in linha else
               CYAN if "REPROCESSADO" in linha else DIM)
        print(_c(f"  {linha}", cor))
    print()
    print(f"  chamadas LLM .... {rep.llm_calls}   custo US$ {rep.cost_usd:.4f}")
    return 0


def cmd_calibrate(args, conn):
    from . import calibrate as calib
    rel = calib.build(conn)
    marca = {"ok": _c("ok", GREEN), "atencao": _c("!!", YELLOW),
             "sem_dados": _c("--", DIM)}

    _rule("relatorio de calibragem")
    for m in rel.metricas:
        print(f"\n  {marca[m.saude]} {BOLD if sys.stdout.isatty() else ''}{m.nome}"
              f"{RESET if sys.stdout.isatty() else ''}: {m.valor}")
        for linha in textwrap.wrap(m.leitura, 70):
            print(f"      {_c(linha, DIM)}")
        if m.botao:
            print(f"      {_c('botao: ' + m.botao, CYAN)}")

    print()
    _rule("totais")
    t = rel.totais
    print(f"  decisoes do portao {t.get('decisoes', 0)}  "
          f"(carimbos {t.get('carimbos', 0)}, fusoes {t.get('fusoes', 0)})")
    print(f"  custo do portao ... US$ {t.get('custo_portao', 0.0):.4f}")
    return 0


def cmd_reindex(args, conn):
    db.rebuild_fts(conn)
    print("indice FTS reconstruido.")
    return 0


def cmd_stats(args, conn):
    s = db.stats(conn)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    _rule("episodios")
    print(f"  total ........... {s['episodes_total']}")
    for st in ("buffered", "consolidated", "expired", "deleted"):
        n = s["episodes_by_status"].get(st, 0)
        rotulo = {"buffered": "no purgatorio", "consolidated": "consolidados",
                  "expired": "expirados", "deleted": "deletados"}[st]
        print(f"    {rotulo:<16} {_c(n, EP_COLOR.get(st, ''))}")
    media = s["avg_surprise_stamped"]
    print(f"  surpresa media .. {media:.1f}" if media else "  surpresa media .. —")

    print()
    _rule("fatos")
    print(f"  total ........... {s['facts_total']}")
    for st in ("active", "sunken", "retired"):
        n = s["facts_by_status"].get(st, 0)
        rotulo = {"active": "ativos", "sunken": "afundados", "retired": "aposentados"}[st]
        print(f"    {rotulo:<16} {_c(n, FA_COLOR.get(st, ''))}")
    print(f"  vigentes ........ {s['facts_current']}  (encerrados: {s['facts_closed']})")
    print(f"  nucleo (★) ...... {s['facts_core']}")

    print()
    _rule("proveniencia")
    print(f"  ligacoes ........ {s['provenance_links']}")
    orfaos = s["facts_without_provenance"]
    marca = _c(orfaos, RED) if orfaos else _c(0, GREEN)
    print(f"  fatos sem origem  {marca}   {_c('(invariante: deve ser 0)', DIM)}")

    print()
    _rule("log de eventos")
    print(f"  total ........... {s['events_total']}")
    for kind, n in list(s["events_by_kind"].items())[:10]:
        print(f"    {kind:<20} {n}")
    return 0


def cmd_seed(args, conn):
    from .seed import run_seed
    return run_seed(conn, reset=args.reset)


def cmd_chat(args, conn):
    from . import chat
    argv = []
    if args.verbose:
        argv.append("-v")
    if args.no_llm:
        argv.append("--no-llm")
    for m in (args.message or []):
        argv += ["-m", m]
    if args.db:
        argv += ["--db", args.db]
    return chat.main(argv)


def cmd_eval(args, conn):
    from . import evaluation
    argv = []
    if args.estimar:
        argv.append("--estimar")
    if args.chat_model:
        argv += ["--chat-model", args.chat_model]
    if args.verbose:
        argv.append("-v")
    if args.no_llm:
        argv.append("--no-llm")
    return evaluation.main(argv)


def cmd_bench(args, conn):
    from . import bench
    return bench.main(args.resto)


def cmd_config(args, conn):
    cfg = config.load()
    print(f"# {cfg.path}")
    print(json.dumps(cfg.as_dict(), ensure_ascii=False, indent=2))
    return 0


# ------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lembra", description="lembra — memoria de longo prazo para agentes")
    p.add_argument("--db", help="caminho alternativo do banco")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="cria/atualiza o schema").set_defaults(func=cmd_init)

    a = sub.add_parser("add-episode", help="grava um episodio cru")
    a.add_argument("text", nargs="?", help="texto da troca (ou stdin)")
    a.add_argument("--surprise", type=float, default=0.0)
    a.add_argument("--reason", action="append", help="pode repetir")
    a.add_argument("--status", default="buffered", choices=db.EPISODE_STATUSES)
    a.set_defaults(func=cmd_add_episode)

    le = sub.add_parser("list-episodes", help="lista episodios")
    le.add_argument("--status", choices=db.EPISODE_STATUSES)
    le.add_argument("--limit", type=int, default=50)
    le.set_defaults(func=cmd_list_episodes)

    lf = sub.add_parser("list-facts", help="lista fatos")
    lf.add_argument("--status", choices=db.FACT_STATUSES)
    lf.add_argument("--current", action="store_true", help="so os vigentes")
    lf.add_argument("--limit", type=int, default=100)
    lf.set_defaults(func=cmd_list_facts)

    i = sub.add_parser("inspect", help="fato + cadeia de proveniencia")
    i.add_argument("fact_id", type=int)
    i.set_defaults(func=cmd_inspect)

    ie = sub.add_parser("inspect-episode", help="episodio + fatos derivados")
    ie.add_argument("episode_id", type=int)
    ie.set_defaults(func=cmd_inspect_episode)

    ev = sub.add_parser("events", help="log append-only de decisoes")
    ev.add_argument("--kind")
    ev.add_argument("--limit", type=int, default=30)
    ev.set_defaults(func=cmd_events)

    g = sub.add_parser("gate", help="roda o portao sobre uma troca avulsa")
    g.add_argument("text", nargs="?", help="a troca (ou stdin)")
    g.add_argument("--no-llm", action="store_true", help="modo deterministico")
    g.add_argument("-v", "--verbose", action="store_true")
    g.set_defaults(func=cmd_gate)

    gl = sub.add_parser("gate-log", help="KPI: como cada decisao foi resolvida")
    gl.add_argument("--limit", type=int, default=30)
    gl.set_defaults(func=cmd_gate_log)

    sl = sub.add_parser("sleep", help="rodada de consolidacao (o sono)")
    sl.add_argument("--dry-run", action="store_true", help="so mostra quem maturaria")
    sl.add_argument("--force", action="store_true", help="matura todo o purgatorio")
    sl.add_argument("--no-llm", action="store_true", help="sem destilacao")
    sl.set_defaults(func=cmd_sleep)

    fg = sub.add_parser("forget", help="deleta um episodio e reprocessa a cascata")
    fg.add_argument("episode_id", type=int)
    fg.add_argument("-y", "--yes", action="store_true", help="sem confirmacao")
    fg.set_defaults(func=cmd_forget)

    sub.add_parser("calibrate", help="relatorio de calibragem").set_defaults(
        func=cmd_calibrate)
    sub.add_parser("reindex", help="reconstroi o indice FTS").set_defaults(func=cmd_reindex)

    st = sub.add_parser("stats", help="panorama do arquivo")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_stats)

    sd = sub.add_parser("seed", help="popula dados de teste")
    sd.add_argument("--reset", action="store_true", help="zera o banco antes")
    sd.set_defaults(func=cmd_seed)

    ch = sub.add_parser("chat", help="conversar com o Claude atraves do motor")
    ch.add_argument("-v", "--verbose", action="store_true",
                    help="mostra o bloco de memoria injetado")
    ch.add_argument("-m", "--message", action="append",
                    help="modo nao-interativo; pode repetir")
    ch.add_argument("--no-llm", action="store_true", help="modo seco, sem token")
    ch.set_defaults(func=cmd_chat)

    ev = sub.add_parser("eval", help="suite de regressao ponta-a-ponta")
    ev.add_argument("--estimar", action="store_true", help="so mostra a conta")
    ev.add_argument("--chat-model")
    ev.add_argument("-v", "--verbose", action="store_true")
    ev.add_argument("--no-llm", action="store_true", help="modo seco, sem token")
    ev.set_defaults(func=cmd_eval)

    bc = sub.add_parser("bench", help="LongMemEval (repassa os argumentos)")
    bc.add_argument("resto", nargs=argparse.REMAINDER)
    bc.set_defaults(func=cmd_bench)

    sub.add_parser("config", help="mostra a config carregada").set_defaults(func=cmd_config)
    return p


# subcomandos que abrem o proprio banco (ou nenhum)
SEM_CONEXAO = {"cmd_chat", "cmd_eval", "cmd_bench"}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.func.__name__ in SEM_CONEXAO:
        return args.func(args, None) or 0
    conn = db.open_db(args.db)
    try:
        return args.func(args, conn) or 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
