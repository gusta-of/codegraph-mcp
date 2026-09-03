"""Parte 2: carrega fluxos de lógica (YAML) como nós `flow`/`flow_step`,
e liga cada passo aos nós `file_context` que o implementam (via `edges`).

Formato do YAML (ver flows/example.yaml):

    name: "login_flow"
    description: "Fluxo de autenticação do usuário"
    steps:
      - name: "validar_credenciais"
        description: "Confere usuário/senha no banco"
        refs:
          - path: "src/auth.py"
            symbol: "validate_credentials"   # opcional; sem symbol, liga no arquivo inteiro
"""

from pathlib import Path

import yaml

from codegraph import db


def _resolve_ref(conn, ref: dict):
    path = ref["path"]
    symbol = ref.get("symbol")
    if symbol:
        row = conn.execute(
            "SELECT * FROM nodes WHERE type='file_context' AND path=? AND name=?",
            (path, symbol),
        ).fetchone()
        if row is not None:
            return row
    return db.find_node(conn, type="file", path=path)


def load_flow_file(conn, yaml_path: Path) -> dict:
    data = yaml.safe_load(yaml_path.read_text())
    name = data["name"]

    existing = db.find_node(conn, type="flow", path=str(yaml_path))
    flow_id = db.upsert_node(
        conn, type="flow", name=name, path=str(yaml_path),
        content=data.get("description", ""),
        existing_id=existing["id"] if existing else None,
    )
    if existing is not None:
        db.delete_children(conn, flow_id)

    unresolved = []
    for order, step in enumerate(data.get("steps", [])):
        step_id = db.upsert_node(
            conn, type="flow_step", name=step["name"], parent_id=flow_id,
            content=step.get("description", ""), metadata={"order": order},
        )
        for ref in step.get("refs", []):
            target = _resolve_ref(conn, ref)
            if target is None:
                unresolved.append((step["name"], ref))
                continue
            db.add_edge(conn, step_id, target["id"], type="implements_in")

    conn.commit()
    return {"flow": name, "steps": len(data.get("steps", [])), "unresolved_refs": unresolved}


def load_flows_dir(conn, flows_dir: Path, verbose: bool = True) -> list[dict]:
    results = []
    for yaml_path in sorted(flows_dir.glob("*.yaml")) + sorted(flows_dir.glob("*.yml")):
        result = load_flow_file(conn, yaml_path)
        results.append(result)
        if verbose:
            msg = f"  [ok] {yaml_path.name} -> {result['steps']} passo(s)"
            if result["unresolved_refs"]:
                msg += f", {len(result['unresolved_refs'])} ref(s) não resolvida(s)"
            print(msg)
            for step_name, ref in result["unresolved_refs"]:
                print(f"        aviso: passo '{step_name}' referencia {ref} -- não encontrado (indexou o arquivo antes?)")
    return results
