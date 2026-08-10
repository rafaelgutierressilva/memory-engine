"""Deleção dirigida e a cascata de reprocessamento.

Apagar um episodio nao e apagar uma linha: e retirar uma evidencia que outras
coisas usaram para existir. A cascata percorre tudo que se apoiava nele.

Os quatro casos, do mais simples ao mais sutil:

  1. O episodio nao sustenta nada        -> apaga e acabou.
  2. O fato tem outros episodios         -> REPROCESSA: destila de novo so com
                                            os sobreviventes. O fato antigo e
                                            aposentado e o novo nasce com a
                                            proveniencia que restou.
  3. O fato fica sem nenhum episodio     -> APOSENTA o fato. Fato sem
                                            proveniencia nao pode existir.
  4. O fato aposentado tinha SUBSTITUIDO -> REABRE o fato antigo. Este e o caso
     um outro                              sutil: a vigencia do antigo foi
                                            encerrada por causa de uma evidencia
                                            que acabou de ser apagada. Sem ela, o
                                            encerramento perdeu a justificativa.

A regra 4 merece defesa, porque parece contrariar "nunca sobrescreva a
historia". O `valid_to` do fato antigo nao era historia observada — era uma
CONCLUSAO tirada de um episodio. Apagado o episodio, a conclusao cai junto. Se
mantivessemos o antigo fechado, o motor ficaria sem nenhum fato vigente sobre
aquele assunto: nem o novo (aposentado por falta de origem) nem o velho
(encerrado por uma razao que nao existe mais). O log guarda o `valid_to`
anterior, entao a auditoria nao perde nada.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config, db, llm, sleep

TOMBSTONE = "[conteudo apagado a pedido do usuario]"


@dataclass
class ForgetReport:
    episode_id: int | None = None
    facts_reprocessed: list = field(default_factory=list)   # (antigo, novo, afirmacao)
    facts_retired: list = field(default_factory=list)       # (fact_id, motivo)
    reopened: list = field(default_factory=list)            # (fact_id, valid_to_anterior)
    untouched: list = field(default_factory=list)
    cost_usd: float = 0.0
    llm_calls: int = 0
    error: str | None = None

    def linhas(self) -> list[str]:
        """A cascata inteira, em texto — nada morre em silencio."""
        saida = [f"episodio #{self.episode_id}: marcado como deletado, "
                 f"raw_text sobrescrito"]
        for antigo, novo, afirmacao in self.facts_reprocessed:
            saida.append(f"  fato #{antigo} REPROCESSADO -> #{novo}: {afirmacao}")
        for fid, motivo in self.facts_retired:
            saida.append(f"  fato #{fid} APOSENTADO: {motivo}")
        for fid, anterior in self.reopened:
            saida.append(f"  fato #{fid} REABERTO: vigencia voltou a ficar aberta "
                         f"(estava encerrada em {(anterior or '')[:10]}) porque a "
                         f"evidencia que a encerrou foi apagada")
        for fid in self.untouched:
            saida.append(f"  fato #{fid} mantido: ainda tem proveniencia suficiente")
        return saida


# ------------------------------------------------------------ a cascata

def _reabrir_substituido(conn, fato_aposentado, report) -> None:
    """Caso 4: o fato que este substituiu volta a vigorar."""
    antigo_id = fato_aposentado["supersedes"]
    if not antigo_id:
        return
    antigo = db.get_fact(conn, antigo_id)
    if antigo is None or antigo["valid_to"] is None:
        return
    anterior = antigo["valid_to"]
    conn.execute("UPDATE facts SET valid_to = NULL, superseded_by = NULL, "
                 "status = CASE WHEN status = 'retired' THEN 'active' ELSE status END "
                 "WHERE id = ?", (antigo_id,))
    conn.commit()
    report.reopened.append((antigo_id, anterior))
    db.log_event(conn, "substituicao_revertida", fact_id=antigo_id,
                 valid_to_anterior=anterior,
                 substituto_aposentado=fato_aposentado["id"],
                 motivo="a evidencia que encerrou a vigencia foi apagada")


def _aposentar(conn, fato, motivo, report) -> None:
    db.set_fact_status(conn, fato["id"], "retired")
    if fato["valid_to"] is None:
        db.close_fact(conn, fato["id"])
    report.facts_retired.append((fato["id"], motivo))
    db.log_event(conn, "fact_retired", fact_id=fato["id"], motivo=motivo,
                 afirmacao=fato["statement"][:160])
    _reabrir_substituido(conn, fato, report)


def _reprocessar(conn, fato, sobreviventes, cfg, report,
                 distill_fn=None, audit_fn=None) -> None:
    """Re-destila o fato usando apenas os episodios que restaram."""
    resultado = (distill_fn or sleep.distill)(sobreviventes, cfg, [])
    report.llm_calls += 1
    report.cost_usd += resultado.cost_usd
    dados = llm.as_object(resultado.data)
    propostas = [p for p in ((dados or {}).get("fatos") or [])
                 if isinstance(p, dict) and str(p.get("afirmacao") or "").strip()]

    if not resultado.ok or not propostas:
        _aposentar(conn, fato,
                   "reprocessamento nao extraiu nada dos episodios sobreviventes",
                   report)
        return

    # o auditor tambem vale aqui: o fato renasce, entao renasce verificado
    if cfg.get("consolidation.audit_enabled", True):
        auditoria = (audit_fn or sleep.audit)(sobreviventes, propostas, cfg)
        report.llm_calls += 1
        report.cost_usd += auditoria.cost_usd
        dados_audit = llm.as_object(auditoria.data)
        if not auditoria.ok or dados_audit is None:
            _aposentar(conn, fato, "auditoria indisponivel no reprocessamento",
                       report)
            return
        reprovados = set()
        for item in (dados_audit.get("nao_sustentadas") or []):
            try:
                reprovados.add(int(item.get("indice")))
            except (TypeError, ValueError, AttributeError):
                continue
        propostas = [p for i, p in enumerate(propostas) if i not in reprovados]
        if not propostas:
            _aposentar(conn, fato,
                       "tudo que restou foi reprovado pelo auditor", report)
            return

    escolhida = propostas[0]
    afirmacao = str(escolhida["afirmacao"]).strip()
    ids_validos = {e["id"] for e in sobreviventes}
    citados = [int(x) for x in (escolhida.get("episode_ids") or [])
               if str(x).isdigit() and int(x) in ids_validos] or sorted(ids_validos)

    # o fato antigo sai de cena, o novo herda vigencia e cadeia temporal
    db.set_fact_status(conn, fato["id"], "retired")
    novo = db.add_fact(conn, afirmacao, valid_from=fato["valid_from"],
                       confidence=fato["confidence"], is_core=bool(fato["is_core"]),
                       episode_ids=citados, supersedes=fato["supersedes"])
    if fato["valid_to"]:
        db.close_fact(conn, novo, valid_to=fato["valid_to"])
    if fato["superseded_by"]:
        conn.execute("UPDATE facts SET supersedes = ? WHERE id = ?",
                     (novo, fato["superseded_by"]))
        conn.execute("UPDATE facts SET superseded_by = ? WHERE id = ?",
                     (fato["superseded_by"], novo))
        conn.commit()

    report.facts_reprocessed.append((fato["id"], novo, afirmacao))
    db.log_event(conn, "fact_reprocessed", fact_id=novo, substituiu=fato["id"],
                 afirmacao=afirmacao, episode_ids=citados,
                 motivo="proveniencia mudou apos delecao")


def forget_episode(conn, episode_id: int, cfg=None, *, distill_fn=None,
                   audit_fn=None) -> ForgetReport:
    cfg = cfg or config.load()
    report = ForgetReport(episode_id=episode_id)

    episodio = db.get_episode(conn, episode_id)
    if episodio is None:
        report.error = f"episodio #{episode_id} nao existe"
        return report
    if episodio["status"] == "deleted":
        report.error = f"episodio #{episode_id} ja estava deletado"
        return report

    derivados = db.facts_for_episode(conn, episode_id)

    # 1. o conteudo some fisicamente; a linha fica para a proveniencia nao mentir
    conn.execute("UPDATE episodes SET raw_text = ?, status = 'deleted', "
                 "status_changed_at = ? WHERE id = ?",
                 (TOMBSTONE, db.now_iso(), episode_id))
    conn.commit()
    db.log_event(conn, "episode_deleted", episode_id=episode_id,
                 fatos_afetados=[f["id"] for f in derivados])

    # 2. a cascata sobre cada fato que se apoiava nele
    for fato in derivados:
        sobreviventes = [e for e in db.episodes_for_fact(conn, fato["id"])
                         if e["status"] != "deleted"]
        if not sobreviventes:
            _aposentar(conn, fato, "perdeu toda a proveniencia", report)
        elif cfg.get("llm.enabled", True) or distill_fn is not None:
            _reprocessar(conn, fato, sobreviventes, cfg, report, distill_fn, audit_fn)
        else:
            # sem LLM nao da para re-destilar; manter o fato seria manter uma
            # afirmacao que talvez so o episodio apagado sustentava
            _aposentar(conn, fato,
                       "sem LLM para reprocessar — aposentado por precaucao", report)

    db.log_event(conn, "forget_done", episode_id=episode_id,
                 reprocessados=len(report.facts_reprocessed),
                 aposentados=len(report.facts_retired),
                 reabertos=len(report.reopened),
                 custo_usd=round(report.cost_usd, 6))
    return report
