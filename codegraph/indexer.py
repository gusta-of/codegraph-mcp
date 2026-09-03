"""Parte 1: varre um projeto, um arquivo por nó (`file`), cada arquivo
quebrado em pedaços -- nós `file_context` filhos (funções/classes/etc
pra qualquer linguagem com gramática tree-sitter, seções pra Markdown,
blocos de linhas pro resto).
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_language_pack as tslp

from codegraph import db

IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".idea", ".mypy_cache", ".pytest_cache", ".ruff_cache", "models",
}
MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB -- acima disso, só cria o nó `file`, sem quebrar em contexto
LINE_CHUNK_SIZE = 150


@dataclass
class Chunk:
    name: str
    content: str
    start_line: int
    end_line: int


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _find_name_node(node, max_depth: int = 2):
    """Acha o identificador "nome" de uma declaração top-level, direto ou
    aninhado (ex: `const foo = () => {}` em JS -- o nome fica dentro de
    `variable_declarator`, não no nó de fora). Convenção `name` como campo
    é comum a praticamente toda gramática tree-sitter -- é isso que torna
    esse chunker genérico por linguagem, sem código específico por
    linguagem: funciona pra qualquer gramática que siga essa convenção,
    sem eu precisar listar tipos de nó um por um."""
    if max_depth < 0:
        return None
    found = node.child_by_field_name("name")
    if found is not None:
        return found
    for child in node.children:
        found = _find_name_node(child, max_depth - 1)
        if found is not None:
            return found
    return None


def _chunk_treesitter(source: str, lang: str, line_offset: int = 0) -> list[Chunk]:
    """Chunk genérico via tree-sitter: pega os nós de nível superior que
    têm um identificador "name" (função, classe, struct, const com nome,
    etc, dependendo da linguagem) -- pula import/statements soltos, que
    não têm. `line_offset` desloca os números de linha (usado pra script
    embutido dentro de HTML, onde o parser só vê o conteúdo do <script>,
    não o arquivo inteiro)."""
    try:
        parser = tslp.get_parser(lang)
    except Exception:
        return []
    src_bytes = source.encode("utf-8", errors="ignore")
    try:
        tree = parser.parse(src_bytes)
    except Exception:
        return []

    chunks: list[Chunk] = []
    for node in tree.root_node.children:
        # Filtro estrutural, sem nada específico de linguagem: só vira
        # chunk quem tem corpo de verdade (mais de 1 linha). Sem isso,
        # coisas como `import json` (Python) ou `echo "..."` (Bash)
        # também têm campo "name" na gramática e viram ruído de 1 linha.
        if node.end_point[0] <= node.start_point[0]:
            continue
        name_node = _find_name_node(node)
        if name_node is None:
            continue
        name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="ignore")
        content = src_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")
        chunks.append(Chunk(
            name=name, content=content,
            start_line=node.start_point[0] + 1 + line_offset,
            end_line=node.end_point[0] + 1 + line_offset,
        ))
    return chunks


def _chunk_html(source: str) -> list[Chunk]:
    """HTML puro não entende o JS dentro de <script> como funções (fica
    como texto bruto na árvore) -- extrai cada bloco <script> e re-parseia
    o conteúdo como javascript, deslocando as linhas de volta pro arquivo
    original."""
    try:
        parser = tslp.get_parser("html")
    except Exception:
        return []
    src_bytes = source.encode("utf-8", errors="ignore")
    tree = parser.parse(src_bytes)

    chunks: list[Chunk] = []

    def walk(node):
        if node.type == "script_element":
            for child in node.children:
                if child.type == "raw_text":
                    script_src = src_bytes[child.start_byte:child.end_byte].decode(
                        "utf-8", errors="ignore"
                    )
                    chunks.extend(_chunk_treesitter(
                        script_src, "javascript", line_offset=child.start_point[0]
                    ))
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return chunks


def _chunk_markdown(source: str) -> list[Chunk]:
    lines = source.splitlines()
    header_re = re.compile(r"^#{1,6}\s+(.*)")
    starts = [i for i, l in enumerate(lines) if header_re.match(l)]
    if not starts:
        return _chunk_lines(source)

    chunks: list[Chunk] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        title = header_re.match(lines[start]).group(1).strip()
        chunks.append(Chunk(
            name=title,
            content="\n".join(lines[start:end]),
            start_line=start + 1,
            end_line=end,
        ))
    return chunks


def _chunk_lines(source: str) -> list[Chunk]:
    lines = source.splitlines()
    chunks: list[Chunk] = []
    for i in range(0, len(lines), LINE_CHUNK_SIZE):
        block = lines[i:i + LINE_CHUNK_SIZE]
        chunks.append(Chunk(
            name=f"linhas {i + 1}-{i + len(block)}",
            content="\n".join(block),
            start_line=i + 1,
            end_line=i + len(block),
        ))
    return chunks


def _chunk_file(path: Path, source: str) -> list[Chunk]:
    if path.suffix in (".md", ".markdown"):
        return _chunk_markdown(source)

    if path.suffix in (".html", ".htm"):
        chunks = _chunk_html(source)
        return chunks or _chunk_lines(source)

    lang = tslp.detect_language_from_path(str(path))
    if lang is not None:
        chunks = _chunk_treesitter(source, lang)
        if chunks:
            return chunks
    return _chunk_lines(source)


def _should_skip_dir(name: str) -> bool:
    return (
        name in IGNORE_DIRS
        or (name.startswith(".") and name != ".")
        or name.endswith((".egg-info", ".dist-info"))
    )


def iter_project_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if any(_should_skip_dir(part) for part in path.relative_to(root).parts[:-1]):
            continue
        yield path


def index_project(conn, root: Path, verbose: bool = True) -> dict:
    root = root.resolve()
    stats = {"files_scanned": 0, "files_indexed": 0, "files_unchanged": 0,
              "files_skipped": 0, "contexts_created": 0}

    for path in iter_project_files(root):
        stats["files_scanned"] += 1
        rel_path = str(path.relative_to(root))
        try:
            raw = path.read_bytes()
        except OSError:
            stats["files_skipped"] += 1
            continue

        content_hash = _hash(raw)
        existing = db.find_node(conn, type="file", path=rel_path)
        if existing is not None and existing["content_hash"] == content_hash:
            stats["files_unchanged"] += 1
            continue

        if len(raw) > MAX_FILE_BYTES:
            stats["files_skipped"] += 1
            if verbose:
                print(f"  [skip: grande demais] {rel_path}")
            continue

        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            stats["files_skipped"] += 1
            continue

        file_id = db.upsert_node(
            conn, type="file", name=path.name, path=rel_path,
            content_hash=content_hash,
            metadata={"size_bytes": len(raw), "suffix": path.suffix},
            existing_id=existing["id"] if existing else None,
        )
        if existing is not None:
            db.delete_children(conn, file_id)

        chunks = _chunk_file(path, source)
        for chunk in chunks:
            db.upsert_node(
                conn, type="file_context", name=chunk.name, path=rel_path,
                parent_id=file_id, content=chunk.content,
                start_line=chunk.start_line, end_line=chunk.end_line,
            )
        stats["contexts_created"] += len(chunks)
        stats["files_indexed"] += 1
        if verbose:
            print(f"  [ok] {rel_path} -> {len(chunks)} contexto(s)")

    conn.commit()
    return stats
