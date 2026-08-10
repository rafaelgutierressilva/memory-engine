-- ============================================================
-- Motor de Memoria — o arquivo (Fase 1)
--
--   episodes    o purgatorio: a troca crua, carimbada pelo portao
--   facts       o cartorio: verdades datadas (valid_from/valid_to)
--   provenance  a corrente: fato -> episodios que o sustentam
--   events      o log append-only de toda decisao (dados de calibragem)
-- ============================================================

PRAGMA foreign_keys = ON;

-- Marcadores internos do motor (ex.: se o indice FTS ja foi construido).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         TEXT    NOT NULL,             -- ISO-8601 UTC
    raw_text          TEXT    NOT NULL,             -- a troca completa, sem compressao
    surprise_score    REAL    NOT NULL DEFAULT 0.0, -- 0-10
    surprise_reasons  TEXT    NOT NULL DEFAULT '[]',-- JSON: lista de motivos
    status            TEXT    NOT NULL DEFAULT 'buffered'
                      CHECK (status IN ('buffered','consolidated','expired','deleted')),
    use_count         INTEGER NOT NULL DEFAULT 0,
    last_used_at      TEXT,
    status_changed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
CREATE INDEX IF NOT EXISTS idx_episodes_ts     ON episodes(timestamp);

CREATE TABLE IF NOT EXISTS facts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    statement         TEXT    NOT NULL,
    valid_from        TEXT    NOT NULL,
    valid_to          TEXT,                          -- NULL = vigente
    confidence        REAL    NOT NULL DEFAULT 0.5,
    created_at        TEXT    NOT NULL,
    last_confirmed_at TEXT,
    status            TEXT    NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','sunken','retired')),
    is_core           INTEGER NOT NULL DEFAULT 0,    -- nucleo sempre-presente
    use_count         INTEGER NOT NULL DEFAULT 0,
    last_used_at      TEXT,
    supersedes        INTEGER REFERENCES facts(id),  -- fato que este substituiu
    superseded_by     INTEGER REFERENCES facts(id)   -- fato que substituiu este
);

CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status);
CREATE INDEX IF NOT EXISTS idx_facts_valid  ON facts(valid_to);

-- Muitos-para-muitos. RESTRICT de proposito: um episodio so pode sair do disco
-- depois que a cascata de reprocessamento (Fase 5) soltar seus fatos.
CREATE TABLE IF NOT EXISTS provenance (
    fact_id    INTEGER NOT NULL REFERENCES facts(id)    ON DELETE CASCADE,
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE RESTRICT,
    PRIMARY KEY (fact_id, episode_id)
);

CREATE INDEX IF NOT EXISTS idx_prov_episode ON provenance(episode_id);

-- Append-only. Sem foreign keys: o log precisa sobreviver a deletar de tudo.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT    NOT NULL,
    kind       TEXT    NOT NULL,   -- gate_stamp | gate_merge | gate_pass | inject | decay | delete | ...
    episode_id INTEGER,
    fact_id    INTEGER,
    payload    TEXT    NOT NULL DEFAULT '{}'  -- JSON
);

CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(timestamp);

-- ============================================================
-- Fase 4 — o indice de busca (FTS5, sem embeddings)
--
-- Conteudo externo: o indice espelha as tabelas de origem e e mantido pelos
-- gatilhos abaixo. A elegibilidade (fato afundado, fato com vigencia
-- encerrada, episodio ja consolidado) NAO e filtrada aqui e sim no JOIN da
-- consulta — mesmo efeito pratico, muito menos chance de indice e verdade
-- discordarem.
-- ============================================================

CREATE VIRTUAL TABLE IF NOT EXISTS fts_facts USING fts5(
    statement, content='facts', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2");

CREATE TRIGGER IF NOT EXISTS facts_fts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO fts_facts(rowid, statement) VALUES (new.id, new.statement);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO fts_facts(fts_facts, rowid, statement)
    VALUES ('delete', old.id, old.statement);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO fts_facts(fts_facts, rowid, statement)
    VALUES ('delete', old.id, old.statement);
    INSERT INTO fts_facts(rowid, statement) VALUES (new.id, new.statement);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS fts_episodes USING fts5(
    raw_text, content='episodes', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2");

CREATE TRIGGER IF NOT EXISTS episodes_fts_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO fts_episodes(rowid, raw_text) VALUES (new.id, new.raw_text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_fts_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO fts_episodes(fts_episodes, rowid, raw_text)
    VALUES ('delete', old.id, old.raw_text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_fts_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO fts_episodes(fts_episodes, rowid, raw_text)
    VALUES ('delete', old.id, old.raw_text);
    INSERT INTO fts_episodes(rowid, raw_text) VALUES (new.id, new.raw_text);
END;
