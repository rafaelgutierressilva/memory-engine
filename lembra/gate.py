"""O portao de surpresa — o carimbo.

Duas camadas:

  Camada 1 (gratis, sempre ligada): sinais duros. Se um deles bate, a decisao
  sai sem gastar um token.
  Camada 2 (haiku, so no inconclusivo): um juiz recebe a troca + os fatos mais
  relacionados e devolve um score de surpresa.

Toda decisao registra em `events` COMO foi resolvida — `resolucao` e `pagou_llm`
sao o KPI do portao: quanto do trafego o de graca resolveu.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config, db, llm, textutil

# --------------------------------------------------------------- padroes
# Cada padrao vale por si so: se bate, e carimbo, sem consultar LLM.

PATTERNS = {
    "correcao_usuario": [
        r"\bnao\b\s*[,.!]", r"\bnao\b\s+(e|eh|era|foi|sao|seria)\b",
        r"\bna verdade\b", r"\berrado\b", r"\bta errado\b", r"\bcorrig",
        r"\bpelo contrario\b", r"\bme confundi\b", r"\bnao foi isso\b",
        r"\bnao e isso\b", r"\bmudou\b", r"\bmudei\b",
    ],
    "pedido_memoria": [
        r"\blembra\b", r"\blembre\b", r"\banota\b", r"\banote\b",
        r"\bguarda (isso|essa|esse)\b", r"\bmemoriza\b", r"\bgrava (isso|essa)\b",
        r"\btoma nota\b", r"\bregistra (isso|essa)\b", r"\bnao esquece\b",
    ],
    "pedido_esquecimento": [
        r"\besquece (isso|aquilo|o que)\b", r"\bapaga (isso|aquilo|o que)\b",
        r"\bdescarta (isso|aquilo)\b", r"\bremove (isso|aquilo)\b",
        r"\besquece que\b",
    ],
    # Negacao de conhecimento pelo ASSISTENTE. Sozinho nao vale nada; so vira
    # sinal quando existe um fato vigente relacionado — dai e falha de
    # recuperacao, nao novidade do usuario.
    "negacao_de_conhecimento": [
        r"\bnao tenho (nada )?(registrado|informacao|informacoes|dado|dados|anotado)\b",
        r"\bnao consta\b", r"\bnao sei\b", r"\bnao me lembro\b",
        r"\bnao tenho memoria\b", r"\bsem informacao\b",
        r"\bnao encontrei nada\b", r"\bvoce nao (me )?(disse|falou|contou|mencionou)\b",
        r"\bnao foi (me )?informado\b", r"\bnao ha registro\b",
    ],
    "falha_ferramenta": [
        r"\berro\b", r"\berros\b", r"\bfalhou\b", r"\bfalha\b", r"\btimeout\b",
        r"\bexception\b", r"\btraceback\b", r"\bpermission denied\b",
        r"\bnao consegui\b", r"\bnao foi possivel\b", r"\bindisponivel\b",
        r"\brecusou\b", r"\bstatus [45]\d\d\b",
    ],
}

# Marcadores de negacao usados pelo sinal de contradicao (precisam de um fato
# ativo com boa sobreposicao para valerem).
NEGATION = [
    r"\bnao\b", r"\bnunca\b", r"\bjamais\b", r"\bdeixa de\b", r"\bparei de\b",
    r"\bnem\b", r"\btroca\b", r"\btrocar\b", r"\bem vez de\b", r"\bao inves de\b",
    r"\bmudou\b", r"\bmudei\b", r"\bagora (e|eh|prefiro|quero)\b",
]

_COMPILED = {name: [re.compile(p) for p in pats] for name, pats in PATTERNS.items()}
_NEGATION = [re.compile(p) for p in NEGATION]


def _hits(compiled, text: str) -> list[str]:
    return [c.pattern for c in compiled if c.search(text)]


def pede_esquecimento(texto: str) -> bool:
    """Camada 1, sem custo: a pessoa mandou apagar alguma coisa?"""
    return bool(_hits(_COMPILED["pedido_esquecimento"], textutil.normalize(texto)))


# ------------------------------------------------------------- resultado

@dataclass
class Decision:
    action: str                       # "carimbo" | "fusao" | "passe"
    surprise: float = 0.0
    reasons: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    resolution: str = "heuristica"    # heuristica | llm_haiku | determinista | llm_falhou
    paid_llm: bool = False
    cost_usd: float = 0.0
    llm_ms: int = 0
    expectation_violated: str | None = None
    target_fact_id: int | None = None
    fact_coverage: float = 0.0
    novelty: float = 0.0
    episode_id: int | None = None
    confidence_before: float | None = None
    confidence_after: float | None = None
    llm_error: str | None = None
    timed_out: bool = False
    timeout_s: float | None = None
    self_inconsistent: bool = False   # o carimbo nasceu de falha de recuperacao

    @property
    def stamp_reason(self) -> str | None:
        """Por que este carimbo existe. So faz sentido em carimbos.

        A autoinconsistencia vem primeiro de proposito: um carimbo que nasce do
        assistente contradizer a memoria e sintoma de doenca do motor, e essa
        taxa precisa ser lida separada da taxa de novidade do usuario.
        """
        if self.action != "carimbo":
            return None
        if self.self_inconsistent:
            return "autoinconsistencia"
        return {"heuristica": "heuristica", "llm_haiku": "juiz",
                "llm_timeout": "timeout", "llm_falhou": "falha_llm",
                "determinista": "determinista"}.get(self.resolution, self.resolution)

    def headline(self) -> str:
        """A linha que o chat.py grita a cada turno."""
        selo = "de graca" if not self.paid_llm else f"haiku US${self.cost_usd:.4f}"
        if self.action == "carimbo":
            motivo = self.reasons[0] if self.reasons else "sem motivo declarado"
            if self.self_inconsistent:
                return (f"CARIMBADO por AUTOINCONSISTENCIA (surpresa "
                        f"{self.surprise:.1f}: {motivo}) [{self.resolution}, {selo}] "
                        f"-> episodio #{self.episode_id}  ⚠ falha de recuperacao")
            alvo = f" (contradiz fato #{self.target_fact_id})" if self.expectation_violated else ""
            return (f"CARIMBADO (surpresa {self.surprise:.1f}: {motivo}{alvo}) "
                    f"[{self.resolution}, {selo}] -> episodio #{self.episode_id}")
        if self.action == "fusao":
            return (f"fundido -> fato #{self.target_fact_id} confianca "
                    f"{self.confidence_before:.2f}->{self.confidence_after:.2f} "
                    f"[{self.resolution}, {selo}]")
        return f"passe (surpresa {self.surprise:.1f}) [{self.resolution}, {selo}]"


# ----------------------------------------------------------- camada 1

def _known_tokens(conn) -> set[str]:
    """Tudo que o motor ja viu — base para medir 'assunto inedito'."""
    known: set[str] = set()
    for row in conn.execute(
            "SELECT statement FROM facts WHERE status != 'retired'"):
        known |= textutil.token_set(row["statement"])
    for row in conn.execute(
            "SELECT raw_text FROM episodes WHERE status IN ('buffered','consolidated')"):
        known |= textutil.token_set(row["raw_text"])
    return known


def related_facts(conn, exchange: str, limit: int = 5) -> list[tuple]:
    """Fatos ativos e vigentes ordenados por quanto a troca os ecoa."""
    ex_tokens = textutil.token_set(exchange)
    scored = []
    for row in conn.execute(
            "SELECT * FROM facts WHERE status = 'active' AND valid_to IS NULL"):
        cov = textutil.coverage(textutil.token_set(row["statement"]), ex_tokens)
        if cov > 0:
            scored.append((row, cov))
    scored.sort(key=lambda pair: (pair[1], pair[0]["confidence"]), reverse=True)
    return scored[:limit]


def heuristics(conn, exchange: str, cfg=None) -> dict:
    """Camada 1. Sempre roda, custa zero, e decide sozinha na maioria das vezes."""
    cfg = cfg or config.load()
    user_text, assistant_text = textutil.split_turns(exchange)
    norm_user = textutil.normalize(user_text)
    norm_all = textutil.normalize(exchange)

    signals: list[str] = []
    reasons: list[str] = []

    for name in ("correcao_usuario", "pedido_memoria", "pedido_esquecimento"):
        if _hits(_COMPILED[name], norm_user):
            signals.append(name)
            reasons.append({
                "correcao_usuario": "usuario corrigiu algo",
                "pedido_memoria": "pedido explicito de memoria",
                "pedido_esquecimento": "pedido de esquecimento",
            }[name])

    if _hits(_COMPILED["falha_ferramenta"], textutil.normalize(assistant_text) or norm_all):
        signals.append("falha_ferramenta")
        reasons.append("falha de ferramenta/acao")

    matches = related_facts(conn, exchange, limit=cfg.get("gate.judge_facts", 5))
    best_fact, best_cov = (matches[0] if matches else (None, 0.0))

    if (best_fact is not None
            and best_cov >= cfg.get("gate.contradiction_coverage_min", 0.30)
            and any(c.search(norm_user) for c in _NEGATION)):
        signals.append("contradicao_fato")
        reasons.append(f"contradiz o fato #{best_fact['id']}")

    # Autoinconsistencia: o assistente NEGA saber algo que o motor sabe.
    # Sintoma de falha de recuperacao, nao de novidade — por isso ganha razao
    # propria no log e no relatorio de calibragem.
    autoinconsistente = False
    if (best_fact is not None
            and best_cov >= cfg.get("gate.contradiction_coverage_min", 0.30)
            and _hits(_COMPILED["negacao_de_conhecimento"],
                      textutil.normalize(assistant_text))):
        autoinconsistente = True
        signals.append("autoinconsistencia")
        reasons.append(f"o assistente negou saber algo que o fato "
                       f"#{best_fact['id']} registra")

    # A novidade mede o que o USUARIO trouxe, nao o vocabulario que o
    # assistente inventou na resposta. Medindo a troca inteira, "Obrigado, era
    # isso por hoje" aparecia com novidade 1,0 — por causa da prosa da resposta
    # — e virava "assunto inedito". Achado pelo eval da Fase 5.
    # Fallback so quando NAO HA turno de usuario identificavel. Se o usuario
    # falou e o que ele falou vira zero token de conteudo ("obrigado, era isso"),
    # a resposta certa e "nao ha novidade" — nao "entao olha o que o assistente
    # escreveu".
    ex_tokens = textutil.token_set(user_text if user_text.strip() else exchange)
    known = _known_tokens(conn)
    unknown = ex_tokens - known
    novelty = 1.0 if not ex_tokens else len(unknown) / len(ex_tokens)
    # Sem a guarda `if known`: num banco vazio NADA seria inedito, e as
    # primeiras mensagens — as mais fundamentais — passariam batido. O peso de
    # nao carimbar aqui e nunca saber o nome do usuario; o de carimbar demais e
    # um episodio que expira sozinho no purgatorio.
    if (len(ex_tokens) >= cfg.get("gate.min_content_tokens", 3)
            and novelty >= 1.0 - cfg.get("gate.novel_topic_max_overlap", 0.10)):
        signals.append("assunto_inedito")
        reasons.append("assunto inedito")

    return {
        "signals": signals,
        "reasons": reasons,
        "matches": matches,
        "best_fact": best_fact,
        "best_coverage": best_cov,
        "novelty": novelty,
        "unknown_tokens": len(unknown),
        "content_tokens": len(ex_tokens),
        "self_inconsistent": autoinconsistente,
    }


# ----------------------------------------------------------- camada 2

# NOTA DE PROJETO — nao reescreva este prompt em forma de persona.
# A primeira versao comecava com "Voce e o portao de surpresa... responda SOMENTE
# com JSON" e o modelo recusou, lendo aquilo como tentativa de jailbreak (troca de
# persona + formato imposto). Enquadrar como TAREFA de classificacao resolve.
# Vale para todos os prompts internos deste projeto.

JUDGE_PROMPT = """Tarefa de classificacao para um motor de memoria.

Avalie o quanto a TROCA abaixo traz informacao nova em relacao aos FATOS
CONHECIDOS, para decidir se ela merece ser guardada na memoria de longo prazo.

FATOS CONHECIDOS:
{facts}

TROCA:
{exchange}

Escala de surpresa:
  0-3  a troca apenas confirma, repete ou executa o que os fatos ja dizem
  4-6  traz um detalhe novo, porem menor ou circunstancial
  7-10 contradiz, corrige, atualiza ou introduz algo que os fatos nao sustentam

Declare tambem a ORIGEM da surpresa:
  "usuario"    a novidade esta no que a pessoa disse
  "assistente" a surpresa vem da RESPOSTA do assistente contradizer, ignorar ou
               negar um fato conhecido (ex.: dizer que nao sabe algo que os
               fatos acima registram). Isso e falha de recuperacao, nao novidade.
  "nenhuma"    surpresa baixa, nada a assinalar

Formato da resposta: um objeto JSON com as chaves "surpresa" (numero de 0 a 10),
"motivos" (lista de textos curtos), "expectativa_violada" (o #id do fato
contrariado, ou "nenhuma") e "origem". Exemplo:
{{"surpresa": 7, "motivos": ["muda o formato acordado"], "expectativa_violada": "#3", "origem": "usuario"}}"""


def _format_facts(matches) -> str:
    if not matches:
        return "  (nenhum fato armazenado ainda)"
    return "\n".join(
        f"  #{row['id']} (confianca {row['confidence']:.2f}): {row['statement']}"
        for row, _ in matches
    )


def judge(exchange: str, matches, cfg=None) -> llm.LLMResult:
    """O juiz roda com orcamento de tempo proprio, mais apertado que o geral.

    O portao esta no caminho quente da conversa: se o juiz demora, ele perde a
    vez e a camada 1 decide sozinha. Nunca vira gargalo.
    """
    cfg = cfg or config.load()
    prompt = JUDGE_PROMPT.format(facts=_format_facts(matches), exchange=exchange.strip())
    return llm.call(prompt, model=cfg.get("llm.gate_model", "haiku"), cfg=cfg,
                    timeout=cfg.get("gate.judge_timeout_s", 20))


# --------------------------------------------------------------- decisao

def evaluate(conn, exchange: str, cfg=None, llm_fn=None, use_llm: bool | None = None) -> Decision:
    """Decide sem escrever nada. `apply()` e que persiste."""
    cfg = cfg or config.load()
    h = heuristics(conn, exchange, cfg)
    threshold = cfg.get("gate.surprise_threshold", 6.0)

    # --- camada 1 decidiu: carimbo duro, zero token
    if h["signals"]:
        base = cfg.get("gate.heuristic_score", 8.0)
        bonus = cfg.get("gate.signal_bonus", 0.5) * (len(h["signals"]) - 1)
        violado = (f"#{h['best_fact']['id']}"
                   if "contradicao_fato" in h["signals"] else None)
        return Decision(
            action="carimbo", surprise=min(10.0, base + bonus),
            reasons=h["reasons"], signals=h["signals"], resolution="heuristica",
            self_inconsistent=h["self_inconsistent"], expectation_violated=violado,
            target_fact_id=h["best_fact"]["id"] if h["best_fact"] is not None else None,
            fact_coverage=h["best_coverage"], novelty=h["novelty"],
        )

    # --- camada 1 decidiu: confirmacao rotineira, zero token
    #     (a troca ecoa um fato conhecido e nao traz assunto novo)
    #
    #     O veto de novidade so vale quando ha bastante coisa nova em termos
    #     ABSOLUTOS. Em proporcao pura, uma troca curta como "marca a reuniao"
    #     parece 50% nova por causa de dois verbos — e uma memoria pequena faz
    #     todo mundo parecer novidade.
    novo_demais = (h["novelty"] > cfg.get("gate.merge_novelty_max", 0.30)
                   and h["unknown_tokens"] >= cfg.get("gate.merge_novelty_min_tokens", 3))
    if (h["best_fact"] is not None
            and h["best_coverage"] >= cfg.get("gate.merge_coverage_min", 0.30)
            and not novo_demais):
        return Decision(
            action="fusao", surprise=1.0,
            reasons=[f"confirma o fato #{h['best_fact']['id']}"],
            signals=["confirmacao_rotineira"], resolution="heuristica",
            target_fact_id=h["best_fact"]["id"], fact_coverage=h["best_coverage"],
            novelty=h["novelty"],
        )

    # --- inconclusivo: so aqui o dinheiro sai do bolso
    piso_fusao = cfg.get("gate.merge_coverage_min", 0.30)

    def veredito_heuristico(resolucao, motivo, **extra) -> Decision:
        """Sem juiz, a camada 1 decide sozinha: funde se a troca ecoa um fato,
        senao deixa passar. Nunca carimba por engano."""
        alvo = h["best_fact"]["id"] if h["best_fact"] is not None else None
        return Decision(
            action="fusao" if alvo is not None and h["best_coverage"] >= piso_fusao else "passe",
            surprise=0.0, reasons=[motivo], signals=h["signals"],
            resolution=resolucao, target_fact_id=alvo,
            fact_coverage=h["best_coverage"], novelty=h["novelty"], **extra,
        )

    llm_enabled = cfg.get("llm.enabled", True) if use_llm is None else use_llm
    if not llm_enabled and llm_fn is None:
        # modo deterministico: o encanamento inteiro roda sem LLM nenhuma
        return veredito_heuristico("determinista", "nenhum sinal duro; juiz LLM desligado")

    result = (llm_fn or judge)(exchange, h["matches"], cfg)
    data = llm.as_object(result.data)
    if not result.ok or data is None:
        # O juiz perdeu a vez: registra o furo e devolve o veredito da camada 1.
        if result.timed_out:
            resolucao, motivo = "llm_timeout", (
                f"juiz estourou {result.timeout_s}s — veredito heuristico")
        else:
            resolucao, motivo = "llm_falhou", "juiz LLM indisponivel"
        return veredito_heuristico(
            resolucao, motivo, paid_llm=result.called, cost_usd=result.cost_usd,
            llm_ms=result.duration_ms, llm_error=result.error,
            timed_out=result.timed_out, timeout_s=result.timeout_s,
        )

    try:
        score = float(data.get("surpresa", 0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(10.0, score))
    motivos = [str(m) for m in (data.get("motivos") or [])] or ["sem motivo declarado"]
    violado = str(data.get("expectativa_violada") or "nenhuma")
    violado = None if violado.strip().lower() in ("nenhuma", "none", "", "null") else violado

    alvo = h["best_fact"]["id"] if h["best_fact"] is not None else None
    if violado:
        digits = re.sub(r"\D", "", violado)
        if digits:
            alvo = int(digits)

    origem = str(data.get("origem") or "").strip().lower()
    common = dict(
        surprise=score, reasons=motivos, signals=h["signals"],
        self_inconsistent=h["self_inconsistent"] or origem == "assistente",
        resolution="llm_haiku", paid_llm=True, cost_usd=result.cost_usd,
        llm_ms=result.duration_ms, expectation_violated=violado,
        target_fact_id=alvo, fact_coverage=h["best_coverage"], novelty=h["novelty"],
    )
    if score >= threshold:
        return Decision(action="carimbo", **common)
    # Surpresa baixa nao basta para fundir: sem um fato REALMENTE parecido, a
    # confianca subiria no fato errado so por ser o menos distante da lista.
    if h["best_fact"] is not None and h["best_coverage"] >= piso_fusao:
        common["target_fact_id"] = h["best_fact"]["id"]
        return Decision(action="fusao", **common)
    common["reasons"] = motivos + [
        f"nenhum fato proximo o bastante para fundir (cobertura "
        f"{h['best_coverage']:.2f} < {piso_fusao})"] if h["best_fact"] is not None else motivos
    return Decision(action="passe", **common)


# ------------------------------------------------------------- aplicacao

def apply(conn, exchange: str, decision: Decision, cfg=None) -> Decision:
    """Persiste a decisao e registra no log. O `events` e o KPI do portao."""
    cfg = cfg or config.load()

    if decision.action == "carimbo":
        decision.episode_id = db.add_episode(
            conn, exchange.strip(), surprise_score=decision.surprise,
            reasons=decision.reasons, status="buffered")

    elif decision.action == "fusao" and decision.target_fact_id is not None:
        before, after = db.bump_confidence(
            conn, decision.target_fact_id,
            step=cfg.get("gate.merge_confidence_step", 0.02),
            ceiling=cfg.get("gate.merge_confidence_ceiling", 0.95))
        decision.confidence_before, decision.confidence_after = before, after

    elif decision.action == "fusao":
        decision.action = "passe"  # nada para fundir: vira passe

    kind = {"carimbo": "gate_stamp", "fusao": "gate_merge", "passe": "gate_pass"}[decision.action]
    db.log_event(
        conn, kind,
        episode_id=decision.episode_id, fact_id=decision.target_fact_id,
        decisao=decision.action,
        resolucao=decision.resolution,
        pagou_llm=decision.paid_llm,
        custo_usd=round(decision.cost_usd, 6),
        latencia_llm_ms=decision.llm_ms,
        surpresa=round(decision.surprise, 2),
        motivos=decision.reasons,
        sinais=decision.signals,
        expectativa_violada=decision.expectation_violated,
        cobertura_fato=round(decision.fact_coverage, 3),
        novidade=round(decision.novelty, 3),
        confianca=([round(decision.confidence_before, 4), round(decision.confidence_after, 4)]
                   if decision.confidence_before is not None else None),
        erro_llm=decision.llm_error,
        estouro_timeout=decision.timed_out,
        orcamento_s=decision.timeout_s,
        razao_carimbo=decision.stamp_reason,
        autoinconsistencia=decision.self_inconsistent,
    )
    return decision


def process(conn, exchange: str, cfg=None, llm_fn=None, use_llm: bool | None = None) -> Decision:
    """Avalia e persiste — o caminho normal, usado pelo chat.py."""
    cfg = cfg or config.load()
    return apply(conn, exchange, evaluate(conn, exchange, cfg, llm_fn, use_llm), cfg)
