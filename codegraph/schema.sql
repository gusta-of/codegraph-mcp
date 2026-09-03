-- Um nó representa: arquivo, um pedaço (contexto) de um arquivo, um fluxo
-- de lógica, ou um passo de um fluxo. Hierarquia (parent_id) modela a
-- árvore/DAG: file -> file_context, flow -> flow_step.
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN ('file', 'file_context', 'flow', 'flow_step')),
    parent_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    path TEXT,
    content TEXT,
    start_line INTEGER,
    end_line INTEGER,
    content_hash TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_type   ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_path   ON nodes(path);

-- Referências cruzadas entre as duas árvores (ex: flow_step -> file_context
-- que implementa aquele passo). Não é hierarquia, por isso é tabela separada.
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(src_id, dst_id, type)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id);

-- Busca full-text sobre nome+conteúdo dos nós.
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name, content, content='nodes', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, name, content) VALUES (new.id, new.name, new.content);
END;
CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, content) VALUES('delete', old.id, old.name, old.content);
END;
CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, content) VALUES('delete', old.id, old.name, old.content);
    INSERT INTO nodes_fts(rowid, name, content) VALUES (new.id, new.name, new.content);
END;
