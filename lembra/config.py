"""Config loading.

Deliberately dependency-free: this project ships with stdlib only, so we parse
the small YAML subset our own config.yaml uses (nested mappings, scalars,
comments) instead of pulling PyYAML in.
"""

from __future__ import annotations

import os
from pathlib import Path

PACOTE = Path(__file__).resolve().parent
ROOT = PACOTE.parent
EMBARCADO = PACOTE / "config.yaml"      # o padrao que viaja no pacote


def descobrir() -> Path:
    """Onde esta o config, em ordem de precedencia.

    1. LEMBRA_CONFIG, se apontar para arquivo existente
    2. ./config.yaml no diretorio atual — o jeito de um projeto ter o seu
    3. ../config.yaml a partir do pacote — o repositorio em modo de desenvolvimento
    4. o config embarcado no pacote instalado

    O caminho do banco e resolvido RELATIVO AO CONFIG que venceu, entao um
    projeto com config proprio grava no proprio diretorio, e a instalacao
    global grava em ~/.lembra/.
    """
    env = os.environ.get("LEMBRA_CONFIG")
    if env and Path(env).exists():
        return Path(env)
    local = Path.cwd() / "config.yaml"
    if local.exists():
        return local
    do_repo = ROOT / "config.yaml"
    if do_repo.exists():
        return do_repo
    return EMBARCADO


CONFIG_PATH = descobrir()


def _scalar(raw: str):
    raw = raw.strip()
    if not raw:
        return None
    if raw[0] in "\"'" and raw[-1] == raw[0] and len(raw) >= 2:
        return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_scalar(p) for p in inner.split(",")] if inner else []
    low = raw.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse_yaml(text: str) -> dict:
    """Parse the flat/nested mapping subset of YAML we use. 2-space indent."""
    root: dict = {}
    # stack of (indent, mapping); the mapping owning keys at that indent level
    stack = [(-1, root)]
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.split("#", 1)[0].rstrip() if "#" in line else line.rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        body = stripped.strip()
        if ":" not in body:
            raise ValueError(f"config.yaml linha {lineno}: esperava 'chave: valor'")
        key, _, rest = body.partition(":")
        key = key.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"config.yaml linha {lineno}: indentacao invalida")
        parent = stack[-1][1]
        if rest.strip() == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _scalar(rest)
    return root


class Config:
    def __init__(self, data: dict, path: Path | None = None):
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        p = Path(path) if path else descobrir()
        return cls(parse_yaml(p.read_text(encoding="utf-8")), p)

    def get(self, dotted: str, default=None):
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def __getitem__(self, dotted: str):
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise KeyError(f"config.yaml nao define '{dotted}'")
        return value

    @property
    def db_path(self) -> Path:
        """Absoluto vale como esta; `~` expande; relativo resolve contra o CONFIG.

        Resolver contra o config (e nao contra o pacote) e o que faz um projeto
        com config proprio gravar no proprio diretorio, e a instalacao global
        gravar em ~/.lembra/ em vez de dentro do site-packages.
        """
        raw = Path(str(self.get("paths.db", "lembra.db"))).expanduser()
        if raw.is_absolute():
            return raw
        base = self.path.parent if self.path else ROOT
        return (base / raw).resolve()

    def as_dict(self) -> dict:
        return self._data


_cached: Config | None = None


def load(path: Path | None = None) -> Config:
    global _cached
    if path is not None:
        return Config.load(path)
    if _cached is None:
        _cached = Config.load()
    return _cached
