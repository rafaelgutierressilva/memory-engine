"""Camada de LLM: chamadas ao `claude -p` headless.

Duas regras que valem para tudo aqui:

1. Nenhuma falha de LLM derruba o pipeline. Toda chamada devolve um LLMResult;
   `ok=False` significa "siga sem mim", nunca uma excecao vazando.
2. O texto que volta em `result` NAO e JSON garantido — o CLI hoje embrulha em
   cerca markdown (```json ... ```), e isso pode mudar de versao. Por isso o
   desembrulho e um scanner tolerante (`extract_json`), nao um regex frageis.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field

from . import config

FENCES = ("```", "~~~")

# System prompt das chamadas internas do motor (portao, consolidacao, auditor).
# Substituir o system prompt do Claude Code aqui faz tres coisas: evita que o
# modelo tente usar ferramentas (o que estourava `--max-turns 1` e derrubava a
# chamada), tira a persona de agente de codigo que nao tem nada a ver com a
# tarefa, e corta ~25% dos tokens de entrada.
SYSTEM_TAREFA = ("Voce executa tarefas de analise de texto e responde exatamente "
                 "no formato pedido, sem comentarios em volta.")


# ------------------------------------------------------- parsing tolerante

def strip_fences(text: str) -> str:
    """Remove cercas markdown em volta do bloco, se houver. Idempotente.

    Tolera ```json, ``` JSON, ~~~, cerca sem fechamento e texto sem cerca
    nenhuma (nesse caso devolve o texto intacto).
    """
    out = text.strip()
    for _ in range(3):  # ate 3 camadas de cerca, por paranoia
        if not out.startswith(FENCES):
            break
        fence = out[:3]
        newline = out.find("\n")
        if newline == -1:
            return ""  # so a linha da cerca, sem corpo
        first_line = out[3:newline].strip().lower()
        if first_line and not first_line.isalnum():
            break  # nao era uma cerca com linguagem: nao mexe
        out = out[newline + 1:]
        close = out.rfind(fence)
        if close != -1:
            out = out[:close]
        out = out.strip()
    return out


def _json_candidates(text: str):
    """Gera as substrings que abrem em { ou [ e fecham balanceado.

    Consciente de strings e escapes, entao chaves dentro de aspas nao contam.
    """
    openers = {"{": "}", "[": "]"}
    for start, char in enumerate(text):
        if char not in openers:
            continue
        depth, in_str, escaped = 0, False, False
        for end in range(start, len(text)):
            c = text[end]
            if in_str:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c in openers:
                depth += 1
            elif c in ("}", "]"):
                depth -= 1
                if depth == 0:
                    yield text[start:end + 1]
                    break


def extract_json(text: str | None):
    """Desembrulha JSON de um texto de LLM. Devolve None se nao houver nenhum.

    Ordem: tenta o texto puro, depois sem cerca, depois cada bloco balanceado
    encontrado no meio de prosa.
    """
    if not text:
        return None
    for candidate in (text.strip(), strip_fences(text)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
    for candidate in _json_candidates(strip_fences(text) or text):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def as_object(data):
    """Normaliza o que o modelo devolveu para um dict, quando da.

    As vezes a mesma pergunta volta como {...} e as vezes como [{...}] — nao e
    erro do modelo, e variacao. Desembrulhar aqui evita tratar isso como falha.
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return data[0]
    return None


# ------------------------------------------------------------- a chamada

@dataclass
class LLMResult:
    ok: bool = False
    called: bool = False          # False = nem tentou (config desligada)
    text: str = ""
    data: object = None           # JSON ja desembrulhado, se houver
    cost_usd: float = 0.0
    duration_ms: int = 0
    model: str = ""
    error: str | None = None
    timed_out: bool = False       # estourou o orcamento de tempo do chamador
    timeout_s: float | None = None
    raw: dict = field(default_factory=dict)


def call(prompt: str, *, model: str = "haiku", cfg=None,
         expect_json: bool = True, timeout: float | None = None,
         system: str | None = None) -> LLMResult:
    """`timeout` em segundos; None usa llm.timeout_seconds da config.

    Quem chama no caminho quente (o portao) passa um orcamento apertado —
    estourar e um resultado legitimo, nao um erro fatal.
    """
    cfg = cfg or config.load()
    if not cfg.get("llm.enabled", True):
        return LLMResult(ok=False, called=False, model=model,
                         error="llm desligada em config.yaml")
    budget = timeout if timeout is not None else cfg.get("llm.timeout_seconds", 120)

    cmd = ["claude", "-p", prompt, "--output-format", "json", "--max-turns", "1",
           # sem ferramentas: uma tentativa de tool use consome o unico turno
           # disponivel e a chamada volta com is_error
           "--allowed-tools", "",
           # so os MCPs que passarmos: a sessao do Rafael nao entra aqui
           "--strict-mcp-config",
           "--system-prompt", system or SYSTEM_TAREFA]
    if model and model != "default":
        cmd += ["--model", model]

    started = time.monotonic()
    try:
        # stdin=DEVNULL de proposito: sem isso o `claude -p` espera dados que
        # nunca vem, avisa "no stdin data received in 3s" e sai com exit 1.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=budget,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return LLMResult(called=True, model=model, error="binario `claude` nao encontrado")
    except subprocess.TimeoutExpired:
        return LLMResult(called=True, model=model, timed_out=True, timeout_s=budget,
                         error=f"timeout apos {budget}s",
                         duration_ms=int((time.monotonic() - started) * 1000))
    except Exception as exc:  # nunca derruba o pipeline
        return LLMResult(called=True, model=model, error=f"{type(exc).__name__}: {exc}")

    elapsed = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0:
        return LLMResult(called=True, model=model, duration_ms=elapsed,
                         error=f"exit {proc.returncode}: {proc.stderr.strip()[:200]}")

    envelope = extract_json(proc.stdout)
    if not isinstance(envelope, dict):
        return LLMResult(called=True, model=model, duration_ms=elapsed,
                         error="envelope do CLI nao era JSON", text=proc.stdout[:400])

    if envelope.get("is_error"):
        return LLMResult(called=True, model=model, duration_ms=elapsed, raw=envelope,
                         error=f"erro do CLI: {str(envelope.get('result'))[:200]}")

    text = envelope.get("result") or ""
    data = extract_json(text) if expect_json else None
    cost = float(envelope.get("total_cost_usd") or 0.0)
    if expect_json and data is None:
        return LLMResult(called=True, model=model, duration_ms=elapsed, raw=envelope,
                         text=text, cost_usd=cost,
                         error="resposta sem JSON reconhecivel")
    return LLMResult(ok=True, called=True, model=model, text=text, data=data,
                     cost_usd=cost, duration_ms=elapsed, raw=envelope)
