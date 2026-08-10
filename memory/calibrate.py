"""Relatorio de calibragem — le o log de eventos e diz qual botao girar.

Cada metrica vem com uma faixa saudavel, uma leitura em portugues e o nome do
parametro do config.yaml que a move. Sem isso, os limiares viram superticao.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import db

GATE_KINDS = ("gate_stamp", "gate_merge", "gate_pass")


@dataclass
class Metrica:
    nome: str
    valor: str
    leitura: str
    botao: str | None = None
    saude: str = "ok"          # ok | atencao | sem_dados


@dataclass
class Relatorio:
    metricas: list = field(default_factory=list)
    totais: dict = field(default_factory=dict)

    def add(self, *args, **kw):
        self.metricas.append(Metrica(*args, **kw))


def _payloads(conn, kinds) -> list[dict]:
    marcadores = ",".join("?" * len(kinds))
    return [json.loads(r["payload"]) for r in conn.execute(
        f"SELECT payload FROM events WHERE kind IN ({marcadores}) ORDER BY id", kinds)]


def _pct(parte, total) -> float:
    return 100.0 * parte / total if total else 0.0


def build(conn) -> Relatorio:
    rel = Relatorio()
    decisoes = _payloads(conn, GATE_KINDS)
    total = len(decisoes)
    carimbos = [d for d in decisoes if d.get("decisao") == "carimbo"]
    fusoes = [d for d in decisoes if d.get("decisao") == "fusao"]
    pagas = [d for d in decisoes if d.get("pagou_llm")]
    timeouts = [d for d in decisoes if d.get("estouro_timeout")]
    auto = [d for d in carimbos if d.get("autoinconsistencia")]

    rel.totais = {
        "decisoes": total,
        "carimbos": len(carimbos),
        "fusoes": len(fusoes),
        "custo_portao": sum(d.get("custo_usd") or 0.0 for d in decisoes),
    }

    # ------------------------------------------------------- taxa de carimbo
    if total:
        taxa = _pct(len(carimbos), total)
        saude = "ok" if 10 <= taxa <= 30 else "atencao"
        if taxa > 30:
            leitura = ("Carimbando demais: o purgatorio vai encher de episodio "
                       "que nunca vira fato. Suba o limiar de surpresa ou aperte "
                       "os padroes de 'assunto inedito'.")
        elif taxa < 10:
            leitura = ("Carimbando de menos: mudancas reais estao passando batido. "
                       "Baixe o limiar de surpresa.")
        else:
            leitura = "Dentro da faixa saudavel (10-30%)."
        rel.add("taxa de carimbo", f"{taxa:.0f}%  ({len(carimbos)}/{total})",
                leitura, "gate.surprise_threshold", saude)

        rel.add("taxa de fusao", f"{_pct(len(fusoes), total):.0f}%  ({len(fusoes)}/{total})",
                "Confirmacoes que viraram confianca em vez de episodio. Se estiver "
                "perto de zero com muita conversa rotineira, a cobertura minima "
                "esta alta demais e nada casa com os fatos.",
                "gate.merge_coverage_min")
    else:
        rel.add("taxa de carimbo", "—", "O portao ainda nao decidiu nada.",
                saude="sem_dados")

    # ------------------------------------------- carimbos por razao (adendo 3)
    if carimbos:
        por_razao = {}
        for d in carimbos:
            # eventos gravados antes da Fase 5 nao tem o campo; rotular como
            # desconhecido esconderia que o dado simplesmente nao existia
            razao = d.get("razao_carimbo") or "anterior_ao_campo"
            por_razao[razao] = por_razao.get(razao, 0) + 1
        ordem = ("heuristica", "juiz", "autoinconsistencia", "timeout",
                 "determinista", "falha_llm")
        linhas = [f"{r}: {por_razao.get(r, 0)}" for r in ordem if r in por_razao or
                  r in ("heuristica", "juiz", "autoinconsistencia", "timeout")]
        extras = [f"{r}: {n}" for r, n in por_razao.items() if r not in ordem]
        rel.add("carimbos por razao", "  ".join(linhas + extras),
                "Heuristica de graca e o ideal; juiz e o custo aceitavel; "
                "timeout deveria ser sempre 0 (o fallback de timeout nunca "
                "carimba, por desenho — se aparecer, tem bug).",
                "gate.judge_timeout_s")

    # ------------------------------ autoinconsistencia: saude da recuperacao
    taxa_auto = _pct(len(auto), total)
    rel.add("autoinconsistencia",
            f"{taxa_auto:.0f}%  ({len(auto)}/{total} decisoes)" if total else "—",
            "Carimbos que nasceram do ASSISTENTE contradizer um fato vigente — "
            "isto e, o motor sabia e nao entregou. Nao e novidade do usuario, e "
            "falha de recuperacao. Acima de ~5% investigue a busca: top_k baixo, "
            "consulta que nao alcanca a grafia do fato, ou fato afundado cedo "
            "demais.",
            "retrieval.top_k / decay.sink_threshold",
            "atencao" if taxa_auto > 5 else ("ok" if total else "sem_dados"))

    # --------------------------------------------------- economia do portao
    if total:
        gratis = total - len(pagas)
        medio = (sum(d.get("custo_usd") or 0.0 for d in pagas) / len(pagas)
                 if pagas else 0.0)
        rel.add("resolvido de graca",
                f"{_pct(gratis, total):.0f}%  ({gratis}/{total})",
                f"O que a camada 1 resolveu sem token. Economia estimada de "
                f"US$ {gratis * medio:.4f} nesta amostra "
                f"(custo medio de uma chamada paga: US$ {medio:.4f}).",
                "gate.merge_coverage_min / gate.novel_topic_max_overlap")
        rel.add("estouros de orcamento do juiz",
                f"{len(timeouts)}  ({_pct(len(timeouts), total):.0f}%)",
                "Cada estouro e uma decisao que a camada 1 teve que tomar "
                "sozinha. Acima de ~10% o orcamento esta curto para esta "
                "maquina.",
                "gate.judge_timeout_s",
                "atencao" if _pct(len(timeouts), total) > 10 else "ok")

    # ------------------------------------- resgate episodico (camada hibrida)
    injecoes = _payloads(conn, ("inject",))
    resgates = _payloads(conn, ("resgate_episodico",))
    if injecoes:
        taxa_resgate = _pct(len([i for i in injecoes if i.get("resgates")]),
                            len(injecoes))
        rel.add("resgate episodico",
                f"{taxa_resgate:.0f}%  ({len(resgates)} resgates em "
                f"{len(injecoes)} injecoes)",
                "Quantas vezes os fatos NAO cobriram a pergunta e o motor teve "
                "que responder pelo texto cru. Cada resgate e um fato que "
                "deveria existir e nao existe: a destilacao perdeu aquele "
                "detalhe. Alto e sinal de que a consolidacao esta resumindo "
                "demais — nao de que o fallback esta funcionando mal. Zero por "
                "muito tempo com memoria grande e suspeito: ou a destilacao "
                "esta otima, ou o limiar de cobertura esta baixo demais e o "
                "fallback nunca dispara.",
                "consolidation (escopo do prompt) / retrieval.fallback_coverage_min",
                "atencao" if taxa_resgate > 40 else "ok")
    else:
        rel.add("resgate episodico", "—", "Nenhuma recuperacao aconteceu ainda.",
                saude="sem_dados")

    # ------------------------------------------------------- ressurreicoes
    ressurreicoes = len(_payloads(conn, ("fact_resurrected",)))
    afundados = len(_payloads(conn, ("fact_sunken",)))
    afundados_eps = len(_payloads(conn, ("episode_sunken",)))
    rel.add("ressurreicoes",
            f"{ressurreicoes} (de {afundados} afundamentos)",
            "Fato afundado que precisou ser pedido por ID. Ressurreicao "
            "frequente significa que o decaimento esta agressivo demais: "
            "o motor esqueceu o que ainda importava.",
            "decay.sink_threshold / decay.half_life_days",
            "atencao" if afundados and ressurreicoes / afundados > 0.2 else "ok")

    rel.add("episodios afundados do arquivo bruto", str(afundados_eps),
            "Episodios ja consolidados que sairam do fallback por falta de uso. "
            "Continuam no disco e na proveniencia — saem do indice quente antes "
            "de sair do mundo.",
            "decay.episode_sink_threshold / decay.episode_half_life_days")

    # ---------------------------------------- aposentadoria: por que morrem
    substituidos = len(_payloads(conn, ("fact_superseded",)))
    aposentados = _payloads(conn, ("fact_retired",))
    rel.add("fatos encerrados",
            f"substituicao: {substituidos}   decaimento: {afundados}   "
            f"aposentadoria: {len(aposentados)}",
            "Substituicao e o motor funcionando (a realidade mudou). Decaimento "
            "e o motor limpando. Aposentadoria vem de delecao: se estiver alta "
            "sem voce ter apagado nada, tem cascata disparando sozinha.")

    # -------------------------------------------- taxa de captura do auditor
    rejeitados = _payloads(conn, ("fact_rejected",))
    criados = _payloads(conn, ("fact_created",))
    propostos = len(rejeitados) + len(criados)
    if propostos:
        taxa_auditor = _pct(len(rejeitados), propostos)
        rel.add("captura do auditor",
                f"{taxa_auditor:.0f}%  ({len(rejeitados)}/{propostos} propostas)",
                "Afirmacoes barradas por nao se sustentarem nos episodios. Zero "
                "por muito tempo e suspeito (auditor complacente ou desligado); "
                "acima de ~40% a destilacao esta inventando demais — considere "
                "um modelo melhor em llm.consolidation_model.",
                "consolidation.audit_enabled / llm.consolidation_model",
                "atencao" if taxa_auditor > 40 else "ok")
    else:
        rel.add("captura do auditor", "—", "Nenhuma consolidacao rodou ainda.",
                saude="sem_dados")

    # --------------------------------------------------- purgatorio: destino
    expirados = _payloads(conn, ("episode_expired",))
    total_eps = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    consolidados = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE status = 'consolidated'").fetchone()[0]
    if total_eps:
        rel.add("destino dos episodios",
                f"consolidados: {consolidados}   expirados: {len(expirados)}   "
                f"total: {total_eps}",
                "Expirar e saudavel: e o purgatorio evitando consolidacao cara. "
                "Se quase tudo expira, o portao esta carimbando ruido; se quase "
                "nada expira, o TTL esta longo demais.",
                "purgatory.ttl_days / purgatory.min_cluster_size")

    # ---------------------------------------------- invariante de integridade
    orfaos = len(db.orphan_facts(conn))
    rel.add("fatos sem proveniencia", str(orfaos),
            "Invariante do motor: precisa ser 0. Qualquer outro numero e bug.",
            saude="ok" if orfaos == 0 else "atencao")

    return rel
