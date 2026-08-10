"""Normalizacao e sobreposicao de palavras-chave.

Tudo aqui e deterministico e gratis. E a base da camada 1 do portao e — na
Fase 3 — do agrupamento barato de assuntos, que substitui embeddings.
"""

from __future__ import annotations

import re
import unicodedata

USER_LABELS = ("rafael", "usuario", "user", "eu", "voce")
ASSISTANT_LABELS = ("assistente", "assistant", "claude", "ia", "bot")

STOPWORDS = {
    "que", "com", "para", "pra", "por", "dos", "das", "nos", "nas", "uma", "uns",
    "umas", "aos", "ao", "sem", "mais", "mas", "como", "quando", "onde", "isso",
    "isto", "aquilo", "esse", "essa", "este", "esta", "aquele", "aquela", "seu",
    "sua", "meu", "minha", "nosso", "nossa", "dele", "dela", "eles", "elas",
    "ele", "ela", "voce", "vc", "sim", "ok", "tudo", "todo", "toda", "todos",
    "todas", "ser", "estar", "ter", "tem", "foi", "era", "sao", "esta", "estao",
    "vou", "vai", "pode", "posso", "quero", "queria", "fazer", "feito", "faz",
    "sobre", "entao", "ainda", "ja", "so", "muito", "bem", "bom", "boa", "dia",
    "obrigado", "obrigada", "nada", "certo", "aqui", "ali", "agora", "hoje",
    "ontem", "amanha", "depois", "antes", "aquele", "algum", "alguma", "outro",
    "outra", "mesmo", "mesma", "ate", "des", "dessa", "desse", "nesse", "nessa",
    "rafael", "assistente", "claude", "usuario", "user", "assistant",
}

_WORD = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    """minusculas, sem acentos — para casar 'nao' com 'não'."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def stem(word: str) -> str:
    """Radicalizacao minima: so o plural em -s.

    Sem isso 'relatorios' e 'relatorio' viram assuntos diferentes e a fusao
    nunca acontece. Nao e um stemmer de verdade, e nem deveria ser — o custo
    de um falso positivo aqui e baixo (uma confirmacao a mais).
    """
    if len(word) > 4 and word.endswith(("oes", "aes")):
        return word[:-3] + "ao"   # reunioes -> reuniao
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]          # relatorios -> relatorio
    return word


def raw_tokens(text: str) -> list[str]:
    """Palavras normalizadas SEM radicalizar — o que o indice FTS realmente ve."""
    return [w for w in _WORD.findall(normalize(text))
            if len(w) >= 3 and w not in STOPWORDS]


def tokens(text: str) -> list[str]:
    return [stem(w) for w in raw_tokens(text)]


def token_set(text: str) -> set[str]:
    return set(tokens(text))


def coverage(needle: set[str], haystack: set[str]) -> float:
    """Quanto de `needle` aparece em `haystack` (0-1). Assimetrico de proposito:
    queremos saber quanto de um FATO foi ecoado pela troca."""
    if not needle:
        return 0.0
    return len(needle & haystack) / len(needle)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dice(a: set[str], b: set[str]) -> float:
    """Metrica de similaridade do agrupamento de assunto.

    Preferida ao jaccard porque nao pune diferenca de tamanho: uma frase curta
    e um episodio longo sobre o mesmo assunto continuam parecidos. Medido no
    build, o jaccard espremia tudo entre 0,06 e 0,29 (dificil de calibrar),
    enquanto o dice separou 0,38-0,44 (mesmo assunto) de 0,11-0,22 (periferico).
    """
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def split_turns(exchange: str) -> tuple[str, str]:
    """Separa a troca em (fala do usuario, fala do assistente).

    Sem rotulos reconheciveis, tudo vira fala do usuario — o lado conservador,
    porque as heuristicas duras de correcao olham o turno do usuario.
    """
    user, assistant, current = [], [], None
    for line in exchange.splitlines():
        head, sep, rest = line.partition(":")
        label = normalize(head.strip())
        if sep and label in USER_LABELS:
            current = user
            current.append(rest.strip())
        elif sep and label in ASSISTANT_LABELS:
            current = assistant
            current.append(rest.strip())
        elif current is not None:
            current.append(line.strip())
        else:
            user.append(line.strip())
    return "\n".join(user).strip(), "\n".join(assistant).strip()
