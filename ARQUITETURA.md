# Como o codegraph-mcp funciona

Documentação técnica de ponta a ponta: configuração, como os nós são
gerados, como o grafo é armazenado, e como o servidor MCP expõe tudo
isso pro Kimi Code. Pra instruções rápidas de uso, ver [README.md](README.md).

## 1. Visão geral

O projeto resolve um problema específico: em vez do agente (Kimi Code)
precisar carregar o projeto inteiro (ou re-ler/re-buscar arquivos toda
hora) pra entender código ou um fluxo de lógica, ele consulta um grafo
já indexado, sob demanda, via tools MCP. O grafo guarda:

- **Estrutura do código**: um nó por arquivo, cada arquivo quebrado em
  pedaços menores (funções, seções, blocos).
- **Fluxos de lógica**: definidos à mão em YAML, cada passo do fluxo
  ligado ao trecho de código real que o implementa.

O ganho de token/velocidade vem de duas coisas: (1) só carrega o pedaço
relevante, não o projeto inteiro; (2) fluxos já resolvidos evitam o
modelo ter que re-derivar/re-descobrir uma lógica que já foi mapeada
antes.

## 2. Configuração

### 2.1 Ambiente Python

```bash
cd ~/workspace/codegraph-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt`:
```
mcp>=2.0.0       # SDK oficial do Model Context Protocol (classe MCPServer)
pyyaml>=6.0      # parse dos arquivos flows/*.yaml
```

Nota de versão: o SDK `mcp` fez uma mudança de API entre a v1 e a v2 --
`FastMCP` (v1) virou `MCPServer` (v2), em `mcp.server.mcpserver`. É a
classe usada em `codegraph/server.py`. Se o `pip install` trazer uma v1
por engano (`mcp<2` fixado em algum lugar), o `import` quebra.

### 2.2 Variável de ambiente do servidor

`codegraph/server.py` lê o caminho do banco de `$CODEGRAPH_DB` (default:
`./graph.db`, relativo ao `cwd` do processo). Não tem arquivo de config
separado pro servidor -- é só essa env var.

### 2.3 Registro no Kimi Code

Cada projeto que você quer consultar tem seu próprio `.kimi-code/mcp.json`
(config a nível de projeto -- só ativa quando o Kimi Code abre ali):

```json
{
  "mcpServers": {
    "codegraph": {
      "command": "/home/gustavo/workspace/codegraph-mcp/.venv/bin/python",
      "args": ["-m", "codegraph.server"],
      "cwd": "/home/gustavo/workspace/codegraph-mcp",
      "env": {
        "CODEGRAPH_DB": "/caminho/do/projeto/.codegraph/graph.db"
      }
    }
  }
}
```

Por que `cwd` é sempre a pasta do `codegraph-mcp` (não a do projeto
indexado): `python -m codegraph.server` precisa resolver o pacote
`codegraph` via `sys.path`, que inclui o `cwd` do processo. Se `cwd`
fosse outro lugar, o `import codegraph...` falharia. É por isso que quem
diferencia "qual projeto" é a env var `CODEGRAPH_DB`, não o `cwd`.

Exemplo real já configurado: `~/workspace/ambiente_pessoal_llm/.kimi-code/mcp.json`
aponta pro `.codegraph/graph.db` daquele projeto.

## 3. O modelo de dados (schema.sql)

Tudo mora em duas tabelas + um índice de busca, num único arquivo
SQLite por projeto:

```sql
nodes (
    id, type, parent_id, name, path, content,
    start_line, end_line, content_hash, metadata,
    created_at, updated_at
)
```

`type` é um de quatro valores: `file`, `file_context`, `flow`,
`flow_step`. `parent_id` modela a hierarquia (árvore/DAG):
`file -> file_context` e `flow -> flow_step`. Não tem aresta hierárquica
entre `file` e `flow` -- são duas árvores paralelas, separadas.

```sql
edges (
    id, src_id, dst_id, type, metadata
)
```

Referências que **não** são hierarquia -- hoje só um tipo existe:
`implements_in` (de um `flow_step` pra um `file_context`/`file`). É essa
tabela que conecta as duas árvores.

```sql
nodes_fts  -- virtual table FTS5, espelha name+content de `nodes`
```

Mantida em sincronia por três triggers (`nodes_ai`/`nodes_ad`/`nodes_au`)
que disparam em INSERT/DELETE/UPDATE de `nodes` -- a busca (`search()`)
nunca precisa re-escanear a tabela inteira.

`content_hash` (SHA-256 do conteúdo bruto do arquivo) é o que permite
re-indexar um projeto inteiro rapidamente: se o hash bate com o que já
tá no banco, o arquivo é pulado inteiro (nem chega a re-quebrar em
contexto).

## 4. Como os nós de arquivo são gerados (indexer.py)

Pipeline, arquivo por arquivo, disparado por
`codegraph.cli index <raiz>`:

1. **Varredura** (`iter_project_files`): percorre a árvore com
   `Path.rglob("*")`, despreza diretórios em `IGNORE_DIRS` (`.git`,
   `node_modules`, `.venv`, `__pycache__`, `dist`, `build`, `models`,
   etc.) e qualquer diretório começando com `.`.
2. **Hash + diff** (`index_project`): lê os bytes crus do arquivo,
   calcula `sha256`. Se já existe um nó `file` com esse `path` e o mesmo
   hash, marca como `files_unchanged` e pula -- não gasta tempo
   re-parseando algo que não mudou.
3. **Filtros de tamanho/binário**: arquivo maior que `MAX_FILE_BYTES`
   (2 MB) vira nó `file` mas **sem** filhos `file_context` (grande
   demais pra valer a pena quebrar). Se o `decode("utf-8")` falhar
   (binário), o arquivo inteiro é pulado (nem o nó `file` é criado).
4. **Upsert do nó `file`**: `db.upsert_node(type="file", ...)`, guardando
   `content_hash` e metadata (`size_bytes`, `suffix`). Se já existia
   (mudou de conteúdo), os filhos antigos são apagados
   (`db.delete_children`) antes de recriar -- evita duplicar contexto
   obsoleto.
5. **Chunking** (`_chunk_file`), escolhido pela extensão:
   - **`.py`**: `_chunk_python` usa o módulo `ast` da própria stdlib.
     Percorre `tree.body` (só o nível top-level do módulo) e cria um
     chunk por `FunctionDef`/`AsyncFunctionDef`/`ClassDef` -- nome do
     chunk = nome da função/classe, linhas = `node.lineno` até
     `node.end_lineno`. Se o parse falhar (erro de sintaxe) ou não achar
     nenhum def/class no nível top, cai pro fallback de linhas.
   - **`.md`/`.markdown`**: `_chunk_markdown` acha todas as linhas que
     batem com `^#{1,6}\s+`, e cada seção vai do header até o próximo
     header (ou fim do arquivo). Nome do chunk = texto do header.
   - **qualquer outro tipo**: `_chunk_lines`, blocos fixos de
     `LINE_CHUNK_SIZE` (150) linhas, nome tipo `"linhas 1-150"`.
6. **Nós `file_context`**: um `db.upsert_node(type="file_context", ...)`
   por chunk, com `parent_id` = id do nó `file`, `content` = texto do
   chunk, `start_line`/`end_line` do trecho original.

Resultado real (indexação do `ambiente_pessoal_llm` em 2026-09-03): 412
arquivos, 953 contextos, 2 pulados (binário/grande), rodando em poucos
segundos.

### Limitação conhecida

Só Python e Markdown têm chunking "inteligente" por enquanto. Qualquer
outra linguagem (JS/TS, Go, etc.) cai no fallback de blocos de linha
fixos -- funciona, mas não alinha com fronteiras de função/classe. Listado
como próximo passo no README.

## 5. Como os nós de fluxo são gerados (flows.py)

Diferente dos arquivos, fluxos **não são descobertos automaticamente** --
são escritos à mão em YAML (`flows/*.yaml`), um arquivo por fluxo:

```yaml
name: "nome_do_fluxo"
description: "O que esse fluxo faz"
steps:
  - name: "passo_1"
    description: "O que esse passo faz"
    refs:
      - path: "src/auth.py"
        symbol: "validate_login"   # opcional
```

Carregado por `codegraph.cli load-flows <dir>`, que roda
`load_flow_file` pra cada `.yaml`/`.yml` do diretório:

1. **Nó `flow`**: `upsert_node(type="flow", name=data["name"], content=data["description"])`.
   Se já existia (mesmo `path` = caminho do arquivo YAML), os
   `flow_step` antigos são apagados antes de recriar -- editar um YAML e
   rodar `load-flows` de novo sempre reflete o estado atual do arquivo.
2. **Nós `flow_step`**: um por item de `steps`, na ordem do YAML,
   `parent_id` = id do `flow`, `metadata={"order": N}` guarda a posição.
3. **Resolução de `refs`** (`_resolve_ref`): pra cada `{path, symbol}`
   de um passo, tenta achar um `file_context` com aquele `path` **e**
   `name == symbol` (match exato de nome). Se não achar (ou `symbol` não
   foi dado), cai pro nó `file` daquele `path` inteiro. Se nem isso
   existir, o ref fica **não resolvido** -- listado no output do comando
   (`unresolved_refs`), mas não trava o carregamento do resto do fluxo.
4. **Aresta**: pra cada ref resolvida, `db.add_edge(src=step_id,
   dst=target_id, type="implements_in")`.

**Ordem importa**: `refs.symbol` só casa com algo se o arquivo já foi
indexado (`codegraph.cli index`) *antes* do `load-flows` rodar --
`_resolve_ref` procura nos nós que já existem no banco, não faz nenhum
parsing próprio do arquivo referenciado.

Fragilidade conhecida (documentada no README): se a função referenciada
for **renomeada**, o `symbol` para de bater e a referência quebra
silenciosamente até rodar `load-flows` de novo -- não tem hoje nenhuma
notificação automática de "isso ficou desatualizado".

## 6. O servidor MCP (server.py)

Construído com `mcp.server.mcpserver.MCPServer`, transporte stdio
(`mcp.run()` no `if __name__ == "__main__"`) -- é assim que o Kimi Code
espera (processo filho local, fala JSON-RPC via stdin/stdout).

Cada tool é uma função Python decorada com `@mcp.tool()`; o SDK usa a
assinatura (nomes/tipos dos parâmetros) e a docstring pra gerar o schema
que o modelo enxerga. Todas abrem uma conexão SQLite nova por chamada
(`_conn()` chama `db.connect(DB_PATH)`) -- sem estado entre chamadas,
sem cache em memória.

| Tool | Parâmetros | O que faz internamente |
|---|---|---|
| `list_files` | -- | `db.get_roots(conn, "file")` -- todo nó `file` sem pai |
| `get_file_tree` | `path` | acha o nó `file` por path, lista os `file_context` filhos (`db.get_children`) |
| `get_node` | `node_id` | nó completo + filhos + `edges` de entrada/saída (`get_edges_from`/`get_edges_to`) resolvidas pro nó do outro lado |
| `search` | `query`, `limit` | `SELECT ... FROM nodes_fts WHERE nodes_fts MATCH ? ORDER BY rank` -- sintaxe FTS5 (`"auth AND token"`, etc.) |
| `list_flows` | -- | `db.get_roots(conn, "flow")` |
| `get_flow` | `name` | acha o `flow` pelo nome, resolve cada `flow_step` filho e segue as arestas `implements_in` até os `file_context`/`file` de destino, trazendo o **conteúdo real** de cada um |

`get_flow` é a tool que carrega o mecanismo central do projeto: um
fluxo pedido de uma vez só já vem com o código-fonte relevante embutido,
sem o cliente precisar de uma segunda chamada por passo.

## 7. Fluxo ponta a ponta (exemplo real)

1. `codegraph.cli --db proj/.codegraph/graph.db index proj/` -- varre
   `proj/`, cria nós `file`/`file_context`.
2. `codegraph.cli --db proj/.codegraph/graph.db load-flows proj/.codegraph/flows/` --
   lê os YAML, cria nós `flow`/`flow_step`, liga via `edges`.
3. `.kimi-code/mcp.json` dentro de `proj/` diz pro Kimi Code subir
   `codegraph/server.py` com `CODEGRAPH_DB=proj/.codegraph/graph.db`.
4. Dentro de uma sessão do Kimi Code, o modelo decide chamar
   `get_flow("nome_do_fluxo")` em vez de ler os arquivos um por um --
   uma tool call, uma resposta, com o código já dentro.

Esse ciclo (passos 1-2) precisa ser **re-rodado manualmente** depois de
mudanças relevantes no projeto -- não tem watch/auto-reindex hoje (ver
"Próximos passos" no README).

## 8. Mapa de arquivos

| Arquivo | Responsabilidade |
|---|---|
| `codegraph/schema.sql` | Definição das tabelas `nodes`/`edges`/`nodes_fts` + triggers |
| `codegraph/db.py` | Camada fina sobre `sqlite3` -- todo SQL do projeto vive aqui |
| `codegraph/indexer.py` | Parte 1: varredura + chunking + geração de nós `file`/`file_context` |
| `codegraph/flows.py` | Parte 2: parse de YAML + geração de nós `flow`/`flow_step` + arestas |
| `codegraph/server.py` | Servidor MCP -- as 6 tools |
| `codegraph/cli.py` | Comandos `index`/`load-flows`/`stats`, sem precisar do servidor no ar |
| `flows/*.yaml` | Definições de fluxo (não é código -- é dado, editado à mão) |
