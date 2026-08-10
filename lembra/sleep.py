"""O sono — purgatorio, consolidacao, auditoria e atualizacao temporal.

O caro so acontece aqui, e so para o que provou valor. A ordem importa:

  1. maturar   quais episodios do purgatorio merecem consolidacao
  2. expirar   o resto, passado o TTL, em silencio mas com log
  3. destilar  chamada do modelo padrao (caro) sobre os maduros
  4. auditar   segunda chamada, prompt invertido: o que NAO se sustenta cai
  5. datar     fato novo que substitui um antigo encerra a vigencia dele
  6. purgar    expirados velhos saem do disco (nunca em silencio)

Nenhuma etapa depende de embeddings: o agrupamento de assunto e sobreposicao
de palavras-chave, que e grosseiro e de graca.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config, db, llm, retrieval, textutil

# ------------------------------------------------------------- prompts
# Enquadramento de TAREFA, nunca de persona — ver NOTAS-ARESTAS.md item 2.

CONSOLIDATION_PROMPT = """Tarefa de destilacao para um motor de memoria.

Abaixo estao episodios brutos de conversa que amadureceram no buffer, e os
fatos que o motor ja considera vigentes. Extraia dos episodios o que vale a pena
o assistente lembrar em conversas futuras. Duas familias contam:

  SEMANTICO  regras duraveis sobre a pessoa: preferencias, decisoes, restricoes,
             identidade, forma de trabalhar. ("prefere relatorios em PDF")
  EPISODICO  o que aconteceu com ela: eventos, lugares, datas, pessoas, compras,
             planos. ("foi ao casamento do primo no Grand Ballroom em maio")

As duas contam porque a pessoa espera ser lembrada das duas. Perguntar "onde foi
o casamento do meu primo?" e ouvir "nao tenho essa informacao" e uma falha de
memoria, mesmo que a informacao nao seja uma preferencia.

FATOS JA VIGENTES:
{facts}

EPISODIOS:
{episodes}

Regras da destilacao:
- Cada afirmacao precisa estar INTEIRAMENTE sustentada pelos episodios acima.
  Nao preencha lacunas, nao infira motivacao, nao generalize alem do texto.
- Prefira poucos fatos duros a muitos fatos vagos. Devolver zero fato e uma
  resposta valida e frequente.
- Ignore so o que ninguem perguntaria depois: saudacao, agradecimento, "ok",
  status de tarefa em andamento. Um evento concreto NAO e circunstancial: se tem
  lugar, data, pessoa ou objeto, provavelmente vale guardar.
- Cada fato cita os ids dos episodios que o sustentam.

DESTINO — para cada fato proposto, declare a relacao dele com os fatos
vigentes. Esta e a parte mais importante da tarefa, porque decide se um fato
antigo morre, convive ou nem chega a ser incomodado:

  "novo"             nao fala da mesma dimensao de nenhum fato vigente.
  "substitui"        mesma dimensao de um fato vigente e o valor mudou de forma
                     DURAVEL ("mudei de ideia", "a partir de agora", "nao quero
                     mais"). Informe "fato_alvo": <id do fato vigente>.
  "condicional"      descreve um desvio que vale SO em certa condicao e convive
                     com o fato vigente, sem anula-lo ("quando for para o
                     cliente X", "em reuniao com investidor"). Use apenas quando
                     os episodios mostram o desvio REPETIDO com a mesma
                     condicao. Informe "fato_alvo": <id>.
  "excecao_isolada"  o desvio aparece UMA vez e e circunstancial ("dessa vez",
                     "so hoje", "nesse caso especifico"), sem sinal de que vira
                     regra. Informe "fato_alvo": <id>. Fatos assim NAO serao
                     guardados — mas declare-os mesmo assim, para o motor
                     registrar que viu o desvio e decidiu nao mexer no fato.

Na duvida entre "substitui" e "condicional", escolha "condicional": encerrar um
fato vigente por engano apaga historia; guardar uma regra condicional a mais
apenas ocupa espaco.

IDIOMA — escreva cada "afirmacao" NO MESMO IDIOMA dos episodios. Se a conversa
foi em ingles, o fato e em ingles; se foi em portugues, em portugues. A busca do
motor casa as palavras da pergunta com as palavras do fato: um fato guardado num
idioma diferente do que o usuario fala fica invisivel, mesmo estando correto.

Formato da resposta: um objeto JSON com a chave "fatos", uma lista de objetos
com "afirmacao" (uma frase curta, no idioma dos episodios), "episode_ids" (lista
de numeros), "valid_from" (data ISO do episodio mais antigo que sustenta o fato),
"destino" (uma das quatro palavras acima) e "fato_alvo" (o id do fato vigente
envolvido, ou null quando o destino for "novo"). Exemplo:
{{"fatos": [{{"afirmacao": "Rafael quer os relatorios em xlsx quando forem para a Marina.", "episode_ids": [12, 14], "valid_from": "2026-08-01T10:00:00+00:00", "destino": "condicional", "fato_alvo": 3}}]}}"""

AUDIT_PROMPT = """Tarefa de verificacao para um motor de memoria.

Abaixo estao episodios brutos de conversa e, depois, afirmacoes que alguem
destilou deles. Encontre as afirmacoes que os episodios NAO sustentam:
inferencia, exagero, generalizacao indevida, detalhe inventado, ou citacao de um
episodio que nao diz aquilo.

EPISODIOS:
{episodes}

AFIRMACOES PROPOSTAS:
{claims}

Criterio: na duvida, marque como nao sustentada. Descartar um fato bom custa
barato; gravar um fato falso contamina todas as conversas seguintes.

Formato da resposta: um objeto JSON com a chave "nao_sustentadas", uma lista de
objetos com "indice" (o numero da afirmacao) e "motivo" (texto curto). Lista
vazia significa que todas se sustentam. Exemplo:
{{"nao_sustentadas": [{{"indice": 1, "motivo": "os episodios nao mencionam prazo"}}]}}"""

SUPERSEDE_PROMPT = """Tarefa de comparacao temporal para um motor de memoria.

FATO ANTIGO (vigente desde {valid_from}): {old}
FATO NOVO: {new}

O fato novo SUBSTITUI o antigo — isto e, os dois falam da mesma dimensao da
realidade e o valor mudou — ou os dois podem ser verdade ao mesmo tempo?

Formato da resposta: um objeto JSON com "substitui" (true ou false) e "motivo"
(texto curto). Exemplo: {{"substitui": true, "motivo": "mesma preferencia de formato, valor trocou"}}"""


# ------------------------------------------------------------ resultado

@dataclass
class SleepReport:
    matured: list = field(default_factory=list)      # (episode_id, motivo)
    expired: list = field(default_factory=list)
    purged: list = field(default_factory=list)
    facts_created: list = field(default_factory=list)
    facts_rejected: list = field(default_factory=list)   # (afirmacao, motivo)
    superseded: list = field(default_factory=list)       # (antigo, novo, como)
    destinies: list = field(default_factory=list)        # (afirmacao, destino, alvo, como)
    exceptions_dropped: list = field(default_factory=list)  # (afirmacao, fato_alvo)
    unused: list = field(default_factory=list)           # maturou e nao rendeu fato
    sunken: list = field(default_factory=list)           # (fact_id, score, idade)
    sunken_episodes: list = field(default_factory=list)  # (episode_id, score, idade)
    cost_usd: float = 0.0
    llm_calls: int = 0
    skipped: list = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------- 1. agrupamento

def cluster(episodes, cfg=None) -> list[list]:
    """Agrupamento barato por sobreposicao de palavras-chave.

    Guloso e transitivo: entrou num grupo, arrasta o grupo. Grosseiro de
    proposito — o objetivo e so juntar episodios do mesmo assunto para a
    consolidacao ver contexto, nao construir uma taxonomia.
    """
    cfg = cfg or config.load()
    limiar = cfg.get("purgatory.cluster_similarity", 0.30)
    grupos: list[list] = []
    tokens = {e["id"]: textutil.token_set(e["raw_text"]) for e in episodes}
    for ep in episodes:
        alvo = None
        for grupo in grupos:
            if any(textutil.dice(tokens[ep["id"]], tokens[o["id"]]) >= limiar
                   for o in grupo):
                alvo = grupo
                break
        if alvo is None:
            alvo = []
            grupos.append(alvo)
        alvo.append(ep)
    return grupos


# ---------------------------------------------------- 2. maturacao

def select_mature(conn, cfg=None, force: bool = False) -> tuple[list, list, dict]:
    """Decide quem sai do purgatorio. Devolve (grupos maduros, a expirar, motivos)."""
    cfg = cfg or config.load()
    ttl = cfg.get("purgatory.ttl_days", 7)
    min_cluster = cfg.get("purgatory.min_cluster_size", 2)
    alta_surpresa = cfg.get("purgatory.high_surprise_score", 8.0)

    buffered = conn.execute(
        "SELECT * FROM episodes WHERE status = 'buffered' ORDER BY timestamp"
    ).fetchall()
    if not buffered:
        return [], [], {}

    grupos = cluster(buffered, cfg)
    motivos: dict[int, str] = {}

    for grupo in grupos:
        for ep in grupo:
            idade = db.age_days(ep["timestamp"]) or 0.0
            if force:
                motivos[ep["id"]] = "sono forcado"
            elif ep["use_count"] >= 1:
                motivos[ep["id"]] = f"usado {ep['use_count']}x na recuperacao"
            elif len(grupo) >= min_cluster:
                motivos[ep["id"]] = f"cluster de {len(grupo)} episodios do mesmo assunto"
            elif idade >= ttl and ep["surprise_score"] >= alta_surpresa:
                motivos[ep["id"]] = (f"velho ({idade:.0f}d) e muito surpreendente "
                                     f"({ep['surprise_score']:.1f})")

    maduros = [[e for e in g if e["id"] in motivos] for g in grupos]
    maduros = [g for g in maduros if g]
    a_expirar = [e for e in buffered
                 if e["id"] not in motivos and (db.age_days(e["timestamp"]) or 0) >= ttl]
    return maduros, a_expirar, motivos


# ---------------------------------------------------- 3. destilacao

def _format_episodes(episodes) -> str:
    linhas = []
    for e in episodes:
        quando = (e["timestamp"] or "")[:10]
        corpo = " ".join(e["raw_text"].split())
        linhas.append(f"[#{e['id']} | {quando}] {corpo}")
    return "\n".join(linhas)


def _format_facts(rows) -> str:
    if not rows:
        return "  (nenhum fato vigente ainda)"
    return "\n".join(
        f"  #{r['id']} (desde {(r['valid_from'] or '')[:10]}, confianca "
        f"{r['confidence']:.2f}): {r['statement']}" for r in rows)


def current_facts(conn, limit: int = 20) -> list:
    """Os fatos vigentes que a destilacao precisa enxergar para decidir destino."""
    return conn.execute(
        "SELECT * FROM facts WHERE status = 'active' AND valid_to IS NULL "
        "ORDER BY confidence DESC, id DESC LIMIT ?", (limit,)).fetchall()


def distill(episodes, cfg=None, facts=None) -> llm.LLMResult:
    cfg = cfg or config.load()
    prompt = CONSOLIDATION_PROMPT.format(
        facts=_format_facts(facts or []), episodes=_format_episodes(episodes))
    return llm.call(prompt, model=cfg.get("llm.consolidation_model", "default"), cfg=cfg)


# ---------------------------------------------------- 4. auditoria

def audit(episodes, claims, cfg=None) -> llm.LLMResult:
    """A passada invertida. So fatos que sobrevivem a ela sao gravados."""
    cfg = cfg or config.load()
    texto = "\n".join(
        f"[{i}] {c.get('afirmacao', '')} (episodios citados: "
        f"{', '.join(str(x) for x in c.get('episode_ids', []))})"
        for i, c in enumerate(claims))
    prompt = AUDIT_PROMPT.format(episodes=_format_episodes(episodes), claims=texto)
    return llm.call(prompt, model=cfg.get("llm.consolidation_model", "default"), cfg=cfg)


# ---------------------------------------------------- 5. temporal

def supersede_candidates(conn, statement: str, cfg=None) -> list:
    """Fatos vigentes que falam do mesmo assunto que a afirmacao nova."""
    cfg = cfg or config.load()
    minimo = cfg.get("consolidation.supersede_coverage_min", 0.35)
    novos = textutil.token_set(statement)
    saida = []
    for row in conn.execute(
            "SELECT * FROM facts WHERE status = 'active' AND valid_to IS NULL"):
        antigos = textutil.token_set(row["statement"])
        cob = max(textutil.coverage(antigos, novos), textutil.coverage(novos, antigos))
        if cob >= minimo:
            saida.append((row, cob))
    saida.sort(key=lambda p: p[1], reverse=True)
    return saida


def decide_supersede(old_row, statement: str, cfg=None, llm_fn=None) -> tuple[bool, str, llm.LLMResult]:
    """Desempate: haiku decide se o fato novo substitui o antigo."""
    cfg = cfg or config.load()
    fn = llm_fn or (lambda prompt: llm.call(prompt, model=cfg.get("llm.gate_model", "haiku"),
                                            cfg=cfg, timeout=cfg.get("gate.judge_timeout_s", 25)))
    prompt = SUPERSEDE_PROMPT.format(valid_from=(old_row["valid_from"] or "")[:10],
                                     old=old_row["statement"], new=statement)
    resultado = fn(prompt)
    data = llm.as_object(resultado.data)
    if not resultado.ok or data is None:
        return False, "desempate indisponivel — mantem os dois vigentes", resultado
    return bool(data.get("substitui")), str(data.get("motivo") or ""), resultado


# ---------------------------------------------------- 6. a rodada

def _valid_from(proposta, episodes) -> tuple[str, bool]:
    """A data do modelo so vale se for plausivel; senao usa a do episodio mais antigo."""
    mais_antigo = min((e["timestamp"] for e in episodes), default=db.now_iso())
    bruta = str(proposta.get("valid_from") or "")
    try:
        proposta_dt = db.parse_iso(bruta)
    except (ValueError, TypeError):
        return mais_antigo, True
    if proposta_dt is None:
        return mais_antigo, True
    limite_inferior = db.parse_iso(mais_antigo)
    agora = db.parse_iso(db.now_iso())
    if limite_inferior and proposta_dt < limite_inferior:
        return mais_antigo, True          # nao pode valer antes do que a sustenta
    if agora and proposta_dt > agora:
        return mais_antigo, True          # nem no futuro
    return proposta_dt.isoformat(), False


def _fato_alvo(conn, proposta, afirmacao, cfg):
    """O fato vigente que a proposta diz mexer — declarado ou deduzido."""
    bruto = proposta.get("fato_alvo")
    if isinstance(bruto, str):
        bruto = "".join(c for c in bruto if c.isdigit())
    try:
        alvo = int(bruto)
    except (TypeError, ValueError):
        alvo = None
    if alvo is not None:
        row = db.get_fact(conn, alvo)
        if row is not None and row["valid_to"] is None and row["status"] == "active":
            return alvo
    candidatos = supersede_candidates(conn, afirmacao, cfg)
    return candidatos[0][0]["id"] if candidatos else None


def _confirmar_substituicao(conn, candidato, afirmacao, cfg, report, supersede_fn,
                            cobertura=None):
    """Segundo parecer antes de encerrar a vigencia de um fato.

    Encerrar vigencia e a acao mais destrutiva do sono: a partir dai o fato sai
    da competicao da recuperacao. Nunca acontece so porque a destilacao disse.
    """
    if candidato is None:
        return None, "sem candidato"
    usa_llm = cfg.get("llm.enabled", True) or supersede_fn is not None
    if usa_llm:
        substitui, motivo, res = decide_supersede(candidato, afirmacao, cfg,
                                                  llm_fn=supersede_fn)
        report.llm_calls += 1
        report.cost_usd += res.cost_usd
        como = f"desempate haiku: {motivo}"
    else:
        limite = cfg.get("consolidation.supersede_coverage_deterministic", 0.60)
        cob = cobertura if cobertura is not None else 0.0
        substitui = cob >= limite
        como = f"deterministico: cobertura {cob:.2f} vs limite {limite}"
    return (candidato["id"] if substitui else None), como


def _consolidate_group(conn, episodes, cfg, report, distill_fn, audit_fn, supersede_fn):
    """Destila um grupo, audita, grava o que sobrou e data o que substituiu."""
    ids_validos = {e["id"] for e in episodes}

    vigentes = current_facts(conn)
    resultado = (distill_fn or distill)(episodes, cfg, vigentes)
    report.llm_calls += 1
    report.cost_usd += resultado.cost_usd
    data = llm.as_object(resultado.data)
    if not resultado.ok or data is None:
        report.skipped.append(f"destilacao falhou: {resultado.error}")
        db.log_event(conn, "consolidation_failed", erro=resultado.error,
                     episode_ids=sorted(ids_validos))
        return []

    propostas = [p for p in (data.get("fatos") or []) if isinstance(p, dict)
                 and str(p.get("afirmacao") or "").strip()]
    if not propostas:
        db.log_event(conn, "consolidation_empty", episode_ids=sorted(ids_validos),
                     custo_usd=round(resultado.cost_usd, 6))
        return []

    # --- o auditor: prompt invertido sobre as mesmas evidencias
    reprovadas: dict[int, str] = {}
    if cfg.get("consolidation.audit_enabled", True):
        auditoria = (audit_fn or audit)(episodes, propostas, cfg)
        report.llm_calls += 1
        report.cost_usd += auditoria.cost_usd
        dados = llm.as_object(auditoria.data)
        if auditoria.ok and dados is not None:
            for item in (dados.get("nao_sustentadas") or []):
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("indice"))
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(propostas):
                    reprovadas[idx] = str(item.get("motivo") or "sem motivo declarado")
        else:
            # auditor indisponivel: nao grava nada. Fato sem verificacao nao entra.
            report.skipped.append(f"auditoria falhou: {auditoria.error} — nada gravado")
            db.log_event(conn, "audit_failed", erro=auditoria.error,
                         episode_ids=sorted(ids_validos))
            return []

    criados = []
    for idx, proposta in enumerate(propostas):
        afirmacao = str(proposta["afirmacao"]).strip()
        if idx in reprovadas:
            report.facts_rejected.append((afirmacao, reprovadas[idx]))
            db.log_event(conn, "fact_rejected", afirmacao=afirmacao,
                         motivo=reprovadas[idx], episode_ids=sorted(ids_validos))
            continue

        # proveniencia: so ids que realmente estavam neste grupo
        citados = [int(x) for x in (proposta.get("episode_ids") or [])
                   if str(x).isdigit() and int(x) in ids_validos]
        if not citados:
            report.facts_rejected.append(
                (afirmacao, "nenhum episodio valido citado — fato sem proveniencia"))
            db.log_event(conn, "fact_rejected", afirmacao=afirmacao,
                         motivo="sem proveniencia valida")
            continue

        # --- o destino: o fato antigo morre, convive, ou nem e incomodado
        destino = str(proposta.get("destino") or "").strip().lower()
        alvo = _fato_alvo(conn, proposta, afirmacao, cfg)

        if destino == "excecao_isolada":
            # Desvio de uma vez so. Nao vira fato e NAO encosta no fato vigente.
            report.destinies.append((afirmacao, "excecao_isolada", alvo,
                                     "desvio circunstancial — nada gravado"))
            report.exceptions_dropped.append((afirmacao, alvo))
            db.log_event(conn, "fact_destiny", fact_id=alvo, destino="excecao_isolada",
                         afirmacao=afirmacao, gravado=False,
                         motivo="desvio circunstancial, fato vigente intacto")
            continue

        sustentam = [e for e in episodes if e["id"] in citados]
        valid_from, corrigida = _valid_from(proposta, sustentam)

        base = cfg.get("consolidation.new_fact_confidence", 0.70)
        confianca = min(cfg.get("gate.merge_confidence_ceiling", 0.95),
                        base + 0.05 * (len(citados) - 1))

        antigo_id, como = None, "sem fato vigente relacionado"

        if destino == "condicional" and alvo is not None:
            # Regra condicional: coexiste. O valid_to do antigo NAO e fechado.
            como = f"regra condicional — convive com o fato #{alvo}"

        elif destino == "substitui" and alvo is not None:
            # Declarado como substituicao: confirma com um desempate independente
            # antes de encerrar a vigencia de algo.
            antigo_id, como = _confirmar_substituicao(
                conn, db.get_fact(conn, alvo), afirmacao, cfg, report, supersede_fn)
            if antigo_id is None:
                destino = "condicional"
                como += " — rebaixado para coexistencia"

        elif destino in ("", "novo"):
            # O destilador nao viu conflito: o motor confere por conta propria.
            for candidato, cobertura in supersede_candidates(conn, afirmacao, cfg):
                antigo_id, como = _confirmar_substituicao(
                    conn, candidato, afirmacao, cfg, report, supersede_fn,
                    cobertura=cobertura)
                if antigo_id is not None:
                    destino = "substitui"
                    break
            else:
                destino = destino or "novo"

        if antigo_id is not None:
            db.close_fact(conn, antigo_id)

        fid = db.add_fact(conn, afirmacao, valid_from=valid_from, confidence=confianca,
                          episode_ids=citados, supersedes=antigo_id)
        criados.append(fid)
        report.facts_created.append(fid)
        report.destinies.append((afirmacao, destino, alvo or antigo_id, como))
        db.log_event(conn, "fact_created", fact_id=fid, afirmacao=afirmacao,
                     episode_ids=citados, confianca=round(confianca, 3),
                     valid_from=valid_from, data_corrigida=corrigida,
                     origem="consolidacao")
        db.log_event(conn, "fact_destiny", fact_id=fid, destino=destino,
                     afirmacao=afirmacao, gravado=True,
                     fato_alvo=alvo or antigo_id, motivo=como)
        if antigo_id is not None:
            report.superseded.append((antigo_id, fid, como))
            db.log_event(conn, "fact_superseded", fact_id=antigo_id,
                         substituido_por=fid, como=como)

    return criados


def run_sleep(conn, cfg=None, *, dry_run: bool = False, force: bool = False,
              distill_fn=None, audit_fn=None, supersede_fn=None) -> SleepReport:
    cfg = cfg or config.load()
    report = SleepReport(dry_run=dry_run)
    grupos, a_expirar, motivos = select_mature(conn, cfg, force=force)

    for grupo in grupos:
        for ep in grupo:
            report.matured.append((ep["id"], motivos[ep["id"]]))
    report.expired = [(e["id"], f"TTL de {cfg.get('purgatory.ttl_days', 7)}d sem maturar")
                      for e in a_expirar]

    if dry_run:
        return report

    db.log_event(conn, "sleep_start", grupos=len(grupos),
                 maduros=len(report.matured), a_expirar=len(report.expired),
                 forcado=force)

    llm_ligada = cfg.get("llm.enabled", True) or distill_fn is not None
    citados_global: set[int] = set()

    for grupo in grupos:
        if not llm_ligada:
            report.skipped.append(
                f"grupo de {len(grupo)} episodios: consolidacao exige LLM (desligada)")
            db.log_event(conn, "consolidation_skipped", motivo="llm desligada",
                         episode_ids=[e["id"] for e in grupo])
            continue
        maximo = cfg.get("consolidation.max_episodes_per_batch", 12)
        for i in range(0, len(grupo), maximo):
            lote = grupo[i:i + maximo]
            _consolidate_group(conn, lote, cfg, report,
                               distill_fn, audit_fn, supersede_fn)

    # quem foi citado por algum fato vira 'consolidated'
    for episode_id, _ in report.matured:
        if db.facts_for_episode(conn, episode_id):
            db.set_episode_status(conn, episode_id, "consolidated")
            citados_global.add(episode_id)
        elif llm_ligada:
            # maturou, custou consolidacao e nao rendeu fato: nao tenta de novo
            db.set_episode_status(conn, episode_id, "expired")
            report.unused.append(episode_id)
            db.log_event(conn, "episode_expired", episode_id=episode_id,
                         motivo="maturou e nao rendeu fato")

    for episode_id, motivo in report.expired:
        db.set_episode_status(conn, episode_id, "expired")
        db.log_event(conn, "episode_expired", episode_id=episode_id, motivo=motivo)

    report.sunken = retrieval.decay_pass(conn, cfg)
    report.sunken_episodes = retrieval.decay_episodes(conn, cfg)
    report.purged = purge_expired(conn, cfg)
    db.log_event(conn, "sleep_done", afundados=len(report.sunken),
                 fatos_criados=len(report.facts_created),
                 fatos_reprovados=len(report.facts_rejected),
                 substituicoes=len(report.superseded),
                 chamadas_llm=report.llm_calls,
                 custo_usd=round(report.cost_usd, 6))
    return report


def purge_expired(conn, cfg=None) -> list:
    """Expirados velhos saem do disco — nunca em silencio.

    Episodio que ainda sustenta um fato NAO e purgado: o RESTRICT do schema
    obrigaria, mas checar antes deixa o motivo explicito no log.
    """
    cfg = cfg or config.load()
    limite = cfg.get("purgatory.purge_expired_after_days", 30)
    purgados = []
    for row in conn.execute("SELECT * FROM episodes WHERE status = 'expired'"):
        idade = db.age_days(row["status_changed_at"] or row["timestamp"]) or 0.0
        if idade < limite:
            continue
        if db.facts_for_episode(conn, row["id"]):
            db.log_event(conn, "purge_blocked", episode_id=row["id"],
                         motivo="ainda sustenta fato ativo")
            continue
        conn.execute("DELETE FROM episodes WHERE id = ?", (row["id"],))
        conn.commit()
        purgados.append((row["id"], f"expirado ha {idade:.0f}d"))
        db.log_event(conn, "episode_purged", episode_id=row["id"],
                     idade_dias=round(idade, 1))
    return purgados
