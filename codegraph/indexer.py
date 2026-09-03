"""Parte 1: varre um projeto, um arquivo por nó (`file`), cada arquivo
quebrado em pedaços -- nós `file_context` filhos (funções/classes pra
Python, seções pra Markdown, blocos de linhas pro resto).
"""

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

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


def _chunk_python(source: str) -> list[Chunk]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _chunk_lines(source)

    lines = source.splitlines()
    chunks: list[Chunk] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            chunks.append(Chunk(
                name=node.name,
                content="\n".join(lines[start - 1:end]),
                start_line=start,
                end_line=end,
            ))
    if not chunks:
        return _chunk_lines(source)
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
    if path.suffix == ".py":
        return _chunk_python(source)
    if path.suffix in (".md", ".markdown"):
        return _chunk_markdown(source)
    return _chunk_lines(source)


def _should_skip_dir(name: str) -> bool:
    return name in IGNORE_DIRS or (name.startswith(".") and name != ".")


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
