"""Recuperacao — a competicao, e o decaimento que ela alimenta.

Duas regras que valem para todo este modulo:

1. **Vigencia manda.** Fato com `valid_to` fechado NAO compete na recuperacao
   padrao. Ele nao foi apagado — continua no disco, no `inspect`, e volta a
   aparecer quando a consulta e explicitamente temporal ("o que eu preferia
   antes?"). Injetar um fato vencido numa pergunta do presente e pior do que
   nao injetar nada: o assistente responde com confianca a coisa errada.

2. **Toda injecao conta como uso, na hora.** `use_count += 1` e
   `last_used_at = agora` sao gravados no mesmo instante em que o item entra no
   prompt — nao no fim do turno, nao no sono seguinte. O decaimento e a
   maturacao do purgatorio leem exatamente esse sinal, entao ele precisa existir
   desde o primeiro turno.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from . import config, db, textutil

# Sinais de que a pergunta e sobre o passado, e nao sobre o estado atual.
TEMPORAL_MARKERS = [
    r"\bantes\b", r"\bantigamente\b", r"\bcostumava\b", r"\bja foi\b",
    r"\bno passado\b", r"\banteriormente\b", r"\bhistorico\b", r"\bhistoria\b",
    r"\bmudou\b", r"\bmudei\b", r"\bmudanca\b", r"\busava\b", r"\bera\b",
    r"\bdeixou de\b", r"\bdeixei de\b", r"\bantiga\b", r"\bantigo\b",
    r"\bo que eu (queria|preferia|usava|pedia)\b", r"\bnaquela epoca\b",
]
_TEMPORAL = [re.compile(p) for p in TEMPORAL_MARKERS]


INTERROGATIVES = [
    r"\?", r"\bqual\b", r"\bquais\b", r"\bo que\b", r"\bque que\b", r"\bcomo\b",
    r"\bquando\b", r"\bpor que\b", r"\bporque\b", r"\bqual era\b", r"\blembra\b",
    r"\bme conta\b", r"\bme diz\b",
]
_INTERROGATIVE = [re.compile(p) for p in INTERROGATIVES]


def is_temporal_query(text: str) -> bool:
    """A frase PERGUNTA sobre o passado? Marcador de passado nao basta.

    Aprendido no primeiro chat de verdade: "mudei de ideia sobre reunioes" tem
    marcador de passado ("mudei") mas nao e uma consulta ao historico — e uma
    mudanca acontecendo agora. Do mesmo jeito, "marca a reuniao antes das 9h"
    tem "antes" sem nada de historico. Exigir contexto interrogativo separa a
    pergunta sobre o passado da fala que apenas menciona o passado.
    """
    norm = textutil.normalize(text)
    tem_passado = any(c.search(norm) for c in _TEMPORAL)
    pergunta = any(c.search(norm) for c in _INTERROGATIVE)
    return tem_passado and pergunta


def build_match(text: str) -> str:
    """Traduz a fala do usuario para uma consulta FTS5.

    Duas formas de cada palavra, as duas como prefixo:

      crua        "relatorio"* alcanca "relatorios"
      radicalizada "reuniao"*  alcanca "reuniao" quando o usuario escreveu
                              "reunioes"

    As duas sao necessarias porque a radicalizacao de -oes para -ao NAO produz
    um prefixo da palavra original: buscar so "reuniao"* nunca encontraria um
    fato que diz "reunioes", e foi exatamente isso que aconteceu na primeira
    pergunta temporal de verdade.
    """
    formas = set(textutil.raw_tokens(text)) | set(textutil.tokens(text))
    # -ao / -oes nao compartilham prefixo entre si ("reuniao" vs "reunioes"),
    # entao para essas palavras a consulta usa o radical comum ("reuni"*), que
    # alcanca as duas grafias em qualquer direcao.
    for forma in list(formas):
        if forma.endswith("ao") and len(forma) >= 5:
            formas.add(forma[:-2])
    return " OR ".join(f'"{t}"*' for t in sorted(formas))


# ------------------------------------------------------------- resultado

@dataclass
class Item:
    kind: str            # "fato" | "episodio"
    id: int
    text: str
    score: float = 0.0
    relevance: float = 0.0
    recency: float = 0.0
    confidence: float = 0.0
    core: bool = False
    valid_to: str | None = None
    fallback: bool = False     # veio do arquivo bruto, nao dos fatos

    def label(self) -> str:
        if self.core:
            return f"★ #{self.id}"
        if self.fallback:
            return f"cru #{self.id}"
        return f"{'fato' if self.kind == 'fato' else 'ep'} #{self.id}"


@dataclass
class Retrieval:
    query: str
    core: list = field(default_factory=list)
    winners: list = field(default_factory=list)
    considered: int = 0
    historical: bool = False
    cobertura_fatos: float = 1.0    # quanto os fatos cobriram a pergunta
    resgates: list = field(default_factory=list)   # itens vindos do arquivo bruto

    def all_items(self) -> list:
        return list(self.core) + list(self.winners)

    def render(self) -> str:
        """O bloco que entra no prompt. Curto de proposito."""
        if not self.all_items():
            return ""
        linhas = []
        for item in self.core:
            linhas.append(f"- {item.text}")
        for item in self.winners:
            prefixo = ""
            if item.valid_to:
                prefixo = f"[vigorou ate {item.valid_to[:10]}] "
            if item.kind == "episodio":
                prefixo = ("[trecho bruto de conversa anterior] " if item.fallback
                           else "[trecho de conversa anterior] ")
            linhas.append(f"- {prefixo}{item.text}")
        return "\n".join(linhas)


# ------------------------------------------------------------ a competicao

def _recency(value: str | None, half_life: float) -> float:
    idade = db.age_days(value)
    if idade is None or half_life <= 0:
        return 1.0
    return math.pow(0.5, max(0.0, idade) / half_life)


def core_facts(conn, cfg=None) -> list:
    """O nucleo sempre-presente: poucas linhas, sempre injetadas."""
    cfg = cfg or config.load()
    limite = cfg.get("retrieval.core_max_lines", 5)
    rows = conn.execute(
        "SELECT * FROM facts WHERE is_core = 1 AND status = 'active' "
        "AND valid_to IS NULL ORDER BY confidence DESC, id DESC LIMIT ?",
        (limite,)).fetchall()
    return [Item(kind="fato", id=r["id"], text=r["statement"], core=True,
                 confidence=r["confidence"], score=float("inf")) for r in rows]


def _search_facts(conn, match: str, cfg, historical: bool) -> list:
    if not match:
        return []
    vigencia = "" if historical else "AND f.valid_to IS NULL"
    sql = (f"SELECT f.*, bm25(fts_facts) AS bm FROM fts_facts "
           f"JOIN facts f ON f.id = fts_facts.rowid "
           f"WHERE fts_facts MATCH ? AND f.status = 'active' "
           f"AND f.is_core = 0 {vigencia}")
    meia_vida = cfg.get("retrieval.recency_half_life_days", 45)
    saida = []
    for r in conn.execute(sql, (match,)):
        rel = 1.0 + max(0.0, -float(r["bm"]))
        rec = _recency(r["last_used_at"] or r["valid_from"], meia_vida)
        conf = r["confidence"]
        saida.append(Item(kind="fato", id=r["id"], text=r["statement"],
                          relevance=rel, recency=rec, confidence=conf,
                          valid_to=r["valid_to"], score=rel * rec * conf))
    return saida


def _search_episodes(conn, match: str, cfg) -> list:
    """So episodios no purgatorio: os consolidados ja viraram fato."""
    if not match:
        return []
    sql = ("SELECT e.*, bm25(fts_episodes) AS bm FROM fts_episodes "
           "JOIN episodes e ON e.id = fts_episodes.rowid "
           "WHERE fts_episodes MATCH ? AND e.status = 'buffered'")
    meia_vida = cfg.get("retrieval.recency_half_life_days", 45)
    saida = []
    for r in conn.execute(sql, (match,)):
        rel = 1.0 + max(0.0, -float(r["bm"]))
        rec = _recency(r["last_used_at"] or r["timestamp"], meia_vida)
        # episodio nao tem confianca; a surpresa e o proxy disponivel
        conf = max(0.3, min(1.0, (r["surprise_score"] or 0.0) / 10.0))
        saida.append(Item(kind="episodio", id=r["id"],
                          text=" ".join(r["raw_text"].split()),
                          relevance=rel, recency=rec, confidence=conf,
                          score=rel * rec * conf))
    return saida


def _search_fallback(conn, match: str, cfg) -> list:
    """O arquivo bruto: episodios JA consolidados, com peso baixo.

    Existe porque a destilacao perde o detalhe incidental por desenho — a frase
    solta no meio de uma conversa sobre outro assunto. O fato nao guarda; o
    episodio cru guarda. Peso baixo de proposito: o cru so aparece quando os
    fatos nao cobriram a pergunta.

    Episodio 'expired' NAO entra: e assim que o decaimento e a purga mandam
    nesta camada tambem — sai do indice quente antes de sair do mundo.
    """
    if not match:
        return []
    peso = cfg.get("retrieval.fallback_weight", 0.25)
    meia_vida = cfg.get("retrieval.recency_half_life_days", 45)
    sql = ("SELECT e.*, bm25(fts_episodes) AS bm FROM fts_episodes "
           "JOIN episodes e ON e.id = fts_episodes.rowid "
           "WHERE fts_episodes MATCH ? AND e.status = 'consolidated'")
    saida = []
    for r in conn.execute(sql, (match,)):
        rel = 1.0 + max(0.0, -float(r["bm"]))
        rec = _recency(r["last_used_at"] or r["timestamp"], meia_vida)
        conf = max(0.3, min(1.0, (r["surprise_score"] or 0.0) / 10.0))
        saida.append(Item(kind="episodio", id=r["id"],
                          text=" ".join(r["raw_text"].split()),
                          relevance=rel, recency=rec, confidence=conf,
                          fallback=True, score=rel * rec * conf * peso))
    return saida


def _cobertura(query: str, itens) -> float:
    """Quanto das palavras da pergunta os itens ja trazem (0-1)."""
    alvo = textutil.token_set(query)
    if not alvo:
        return 1.0
    tidos = set()
    for i in itens:
        tidos |= textutil.token_set(i.text)
    return len(alvo & tidos) / len(alvo)


def retrieve(conn, query: str, cfg=None, *, k: int | None = None,
             historical: bool | None = None, touch: bool = True) -> Retrieval:
    """Nucleo + os top-k vencedores. Cada item devolvido conta como uso."""
    cfg = cfg or config.load()
    k = k if k is not None else cfg.get("retrieval.top_k", 4)
    if historical is None:
        historical = is_temporal_query(query)

    match = build_match(query)
    candidatos = _search_facts(conn, match, cfg, historical) + \
        _search_episodes(conn, match, cfg)
    candidatos.sort(key=lambda i: i.score, reverse=True)

    nucleo = core_facts(conn, cfg)
    vencedores = candidatos[:k]

    # --- camada de fallback: so quando os fatos nao cobriram a pergunta
    cobertura = _cobertura(query, nucleo + vencedores)
    resgates = []
    if cobertura < cfg.get("retrieval.fallback_coverage_min", 0.60):
        ja = {i.id for i in vencedores if i.kind == "episodio"}
        brutos = [i for i in _search_fallback(conn, match, cfg) if i.id not in ja]
        brutos.sort(key=lambda i: i.score, reverse=True)
        resgates = brutos[:cfg.get("retrieval.fallback_k", 2)]
        vencedores = vencedores + resgates

    resultado = Retrieval(query=query, core=nucleo,
                          winners=vencedores, considered=len(candidatos),
                          historical=historical, cobertura_fatos=cobertura,
                          resgates=resgates)

    if touch:
        registrar_uso(conn, resultado)
    return resultado


def registrar_uso(conn, resultado: Retrieval) -> None:
    """Injetou, contou. Na hora — o decaimento depende deste sinal."""
    for item in resultado.all_items():
        if item.kind == "fato":
            db.touch_fact(conn, item.id)
        else:
            db.touch_episode(conn, item.id)
    db.log_event(
        conn, "inject",
        consulta=resultado.query[:200],
        historica=resultado.historical,
        candidatos=resultado.considered,
        nucleo=[i.id for i in resultado.core],
        cobertura_fatos=round(resultado.cobertura_fatos, 3),
        resgates=[i.id for i in resultado.resgates],
        vencedores=[{"tipo": i.kind, "id": i.id, "score": round(i.score, 4),
                     "rel": round(i.relevance, 3), "rec": round(i.recency, 3),
                     "conf": round(i.confidence, 3),
                     "fallback": i.fallback} for i in resultado.winners],
    )

    # O resgate episodico e o detector de perda da destilacao: toda vez que ele
    # dispara, um fato deveria existir e nao existe.
    for item in resultado.resgates:
        db.log_event(
            conn, "resgate_episodico", episode_id=item.id,
            pergunta=resultado.query[:200],
            cobertura_fatos=round(resultado.cobertura_fatos, 3),
            score=round(item.score, 4),
            trecho=item.text[:220],
            motivo="os fatos nao cobriram a pergunta; respondido pelo texto cru")


# ------------------------------------------------------------- decaimento

def decay_pass(conn, cfg=None) -> list:
    """Score cai com o tempo desde o ultimo uso; afundado sai da competicao."""
    cfg = cfg or config.load()
    meia_vida = cfg.get("decay.half_life_days", 30)
    limiar = cfg.get("decay.sink_threshold", 0.15)
    afundados = []
    for r in conn.execute("SELECT * FROM facts WHERE status = 'active'"):
        referencia = r["last_used_at"] or r["created_at"]
        idade = db.age_days(referencia) or 0.0
        score = r["confidence"] * math.pow(0.5, idade / meia_vida) if meia_vida else r["confidence"]
        if score < limiar:
            db.set_fact_status(conn, r["id"], "sunken")
            afundados.append((r["id"], round(score, 4), round(idade, 1)))
            db.log_event(conn, "fact_sunken", fact_id=r["id"], score=round(score, 4),
                         idade_dias=round(idade, 1), limiar=limiar,
                         afirmacao=r["statement"][:120])
    return afundados


def decay_episodes(conn, cfg=None) -> list:
    """O decaimento tambem manda no arquivo bruto.

    Episodio consolidado que ninguem usa vira 'expired': sai do fallback e do
    indice quente, mas continua no disco, no `inspect` e na proveniencia. Sai do
    indice quente antes de sair do mundo — e so sai do mundo pela purga, que o
    RESTRICT do schema bloqueia enquanto ele sustentar um fato.
    """
    cfg = cfg or config.load()
    meia_vida = cfg.get("decay.episode_half_life_days", 90)
    limiar = cfg.get("decay.episode_sink_threshold", 0.05)
    afundados = []
    for r in conn.execute("SELECT * FROM episodes WHERE status = 'consolidated'"):
        referencia = r["last_used_at"] or r["timestamp"]
        idade = db.age_days(referencia) or 0.0
        base = max(0.3, min(1.0, (r["surprise_score"] or 0.0) / 10.0))
        score = base * math.pow(0.5, idade / meia_vida) if meia_vida else base
        if score < limiar:
            db.set_episode_status(conn, r["id"], "expired")
            afundados.append((r["id"], round(score, 4), round(idade, 1)))
            db.log_event(conn, "episode_sunken", episode_id=r["id"],
                         score=round(score, 4), idade_dias=round(idade, 1),
                         limiar=limiar,
                         motivo="sem uso; sai do fallback, fica no disco")
    return afundados


def resurrect(conn, fact_id: int) -> bool:
    """Fato afundado pedido por ID volta — e isso e sinal de calibragem.

    Se ressurreicao vira rotina, o limiar de afundamento esta agressivo demais.
    O relatorio da Fase 5 conta quantas aconteceram.
    """
    row = db.get_fact(conn, fact_id)
    if row is None or row["status"] != "sunken":
        return False
    db.set_fact_status(conn, fact_id, "active")
    db.log_event(conn, "fact_resurrected", fact_id=fact_id,
                 afirmacao=row["statement"][:120],
                 sinal="limiar de afundamento pode estar agressivo demais")
    return True
