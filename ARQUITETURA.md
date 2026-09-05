# Como o codegraph-mcp funciona

Documentação técnica de ponta a ponta: configuração, como os nós são
gerados, como o grafo é armazenado, e como o servidor MCP expõe tudo
isso pro Kimi Code. Pra instruções rápidas de uso, ver [README.md](README.md).
Pra entender pra que cada parte serve sem termo técnico, ver
[MAP_CODEGRAPH.md](MAP_CODEGRAPH.md).

## 1. Visão geral

O projeto resolve um problema específico: em vez do agente (Kimi Code)
precisar carregar o projeto inteiro (ou re-ler/re-buscar arquivos toda
hora) pra entender código ou um fluxo de lógica, ele consulta um grafo
já indexado, sob demanda, via tools MCP. O grafo guarda:

- **Estrutura do código**: um nó por arquivo, cada arquivo quebrado em
  pedaços menores (funções, seções, blocos).
- **Fluxos de lógica**: definidos à mão em YAML, cada passo do fluxo
  ligado ao trecho de código real que o implementa.
- **Histórico de prompts**: cada troca prompt<->resposta real, capturada
  automaticamente por um proxy entre o Kimi Code e o `llama-server` (ver
  seção 9) -- o modelo consegue "lembrar" de conversas anteriores do
  mesmo projeto sem precisar que o usuário cole isso de novo.

O ganho de token/velocidade vem de três coisas: (1) só carrega o pedaço
relevante, não o projeto inteiro; (2) fluxos já resolvidos evitam o
modelo ter que re-derivar/re-descobrir uma lógica que já foi mapeada
antes; (3) histórico consultável evita re-explicar contexto de conversas
passadas a cada nova sessão.

Guia de instalação completo, multiplataforma (Windows/macOS/Linux), com
todas as variáveis de ambiente: [INSTALL.md](INSTALL.md). O que segue
aqui é o "por dentro" de como cada peça funciona.

## 2. Configuração

### 2.1 Ambiente Python

```bash
cd codegraph-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

**`pip install -e .` (não `pip install -r requirements.txt`)** -- o
projeto virou um pacote instalável de verdade (`pyproject.toml`), não só
uma pasta de scripts soltos. Isso não é só estilo: sem isso, `python -m
codegraph.algumacoisa` só funciona rodando de **dentro** da pasta do
`codegraph-mcp` (o `-m` resolve pacote via `cwd`, e `codegraph` não
estava instalado em lugar nenhum) -- foi um bug real, batido 3 vezes
seguidas (no `mcp.json`, no `setup-project.sh`, no comando `setup`) antes
de resolver na raiz instalando o pacote de verdade. Com `pip install -e
.`, os comandos funcionam de **qualquer pasta**, sem exceção -- testado.

`pyproject.toml` define dois comandos (`[project.scripts]`), instalados
no `.venv` junto com o pacote:
- `codegraph` -- CLI (`index`/`load-flows`/`stats`/`setup`), equivalente
  a `python -m codegraph.cli`.
- `codegraph-up` -- sobe `llama-server` + proxy num comando (ver 2.3).

`requirements.txt`:
```
mcp>=2.0.0       # SDK oficial do Model Context Protocol (classe MCPServer)
pyyaml>=6.0      # parse dos arquivos flows/*.yaml
httpx>=0.27      # cliente HTTP async -- o proxy usa pra falar com o llama-server
starlette>=0.37  # framework ASGI minimo -- roteamento do proxy
uvicorn>=0.30    # servidor ASGI que roda o proxy
tree-sitter>=0.23              # binding Python do tree-sitter
tree-sitter-language-pack>=0.2 # gramaticas prontas (dezenas de linguagens) + deteccao de linguagem por path
```

Nota de versão: o SDK `mcp` fez uma mudança de API entre a v1 e a v2 --
`FastMCP` (v1) virou `MCPServer` (v2), em `mcp.server.mcpserver`. É a
classe usada em `codegraph/server.py`. Se o `pip install` trazer uma v1
por engano (`mcp<2` fixado em algum lugar), o `import` quebra.

### 2.2 Variável de ambiente do servidor

`codegraph/server.py` lê o caminho do banco de `$CODEGRAPH_DB` (default:
`./graph.db`, relativo ao `cwd` do processo). Não tem arquivo de config
separado pro servidor -- é só essa env var.

### 2.3 `codegraph-up`: subir o modelo + o proxy num comando (`codegraph/up.py`)

Multiplataforma de verdade (usa `subprocess.Popen`, não Bash) --
`start_new_session=True` no Linux/macOS ou `CREATE_NEW_PROCESS_GROUP |
DETACHED_PROCESS` no Windows (`os.name == "nt"`) pra desacoplar o
processo filho do terminal que o lançou, funcionando nos dois mundos sem
`if`/`else` de lógica, só a chamada de baixo nível diferente.

Fluxo: checa se `llama-server` (porta `$CODEGRAPH_LLAMA_PORT`, default
8080) já responde `/health` -- se sim, não sobe de novo; se não, exige
`$CODEGRAPH_MODEL_PATH` (sem isso, erro claro e para, não tenta
adivinhar caminho de ninguém) e sobe com `$CODEGRAPH_LLAMA_SERVER_BIN`
(default: `llama-server`, precisa estar no `PATH`) + `$CODEGRAPH_LLAMA_ARGS`
(default conservador: contexto pequeno, funciona em qualquer hardware --
ver tabela completa no `INSTALL.md`). Mesma lógica pro proxy (porta
`$CODEGRAPH_PROXY_PORT`, default 8081), usando `sys.executable -m
codegraph.proxy` -- `sys.executable` já resolve o python certo (do
`.venv` ativo), sem precisar adivinhar `.venv/bin/python` (Unix) vs
`.venv\Scripts\python.exe` (Windows).

Testado de verdade: matei o proxy, rodei `codegraph-up`, confirmou
`llama-server` já rodando (skip) e subiu o proxy do zero, respondendo em
segundos.

### 2.4 Registro no Kimi Code

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

### 2.5 O botão de "atualizar" (`.codegraph/reindex.sh`/`.bat`)

`codegraph setup` (via `cmd_setup`, `cli.py`) termina gerando um arquivo
executável **dentro do próprio projeto** (`.codegraph/reindex.sh` no
Linux/macOS, `.codegraph/reindex.bat` no Windows -- só o formato do SO
onde `setup` rodou, não os dois) com o comando de reindexação **já
preenchido** (caminho do projeto, caminho do Python do `codegraph-mcp`
-- via `sys.executable`, mesma lógica do `mcp.json`). Motivação: você não
deveria precisar lembrar o comando completo (`codegraph setup
/caminho/enorme/do/projeto`) toda vez que só quer atualizar o grafo
depois de criar/mudar arquivos -- só roda esse arquivo.

`.sh` sai com permissão de execução já aplicada
(`path.chmod(... | S_IXUSR | S_IXGRP | S_IXOTH)`) -- funciona direto no
terminal (`./reindex.sh`) ou clicando (dependendo do gerenciador de
arquivos/config do SO). Testado de verdade: gerado, rodado direto por
caminho absoluto de outra pasta, reindexou igual ao comando original.

**Garantia importante, confirmada no código (não só observada) -- o
`reindex.sh`/`codegraph setup` nunca apaga histórico de conversas.** Só
mexe em nós `file`/`file_context` (via `indexer.index_project`) e
`flow`/`flow_step` (via `flows.load_flows_dir`) -- os únicos `DELETE` no
caminho de reindexação são `db.delete_children` chamado com o id de um
nó `file` ou `flow`, que recria só os filhos daquele nó específico. Nós
`history` **não têm pai** (`parent_id` sempre `NULL`, ver seção 3) --
estruturalmente impossível esse `DELETE` alcançar eles, não é regra
condicional que possa falhar. A única função que apaga `history` é
`history.enforce_limit` (seção 9.4), chamada só de dentro de
`history.record_exchange` -- ou seja, só quando uma conversa **nova**
é gravada pelo proxy, nunca durante indexação. `reindex.sh`/`codegraph
setup` sempre abrem o `.codegraph/graph.db` **já existente** e
atualizam em cima dele -- histórico dentro continua intacto. Isso não
vale se você apagar o arquivo `.db` manualmente antes (como fizemos de
propósito em alguns testes desta sessão, pra ver o efeito de uma
mudança de chunking em tudo, seção 4.1) -- aí o banco inteiro some,
histórico incluso; a garantia é sobre o fluxo normal de reindexar, não
sobre recriar o banco do zero.

Rodar de novo (`codegraph setup` de novo, ou o próprio `reindex.sh`)
sobrescreve o launcher -- inofensivo, o conteúdo é sempre recalculado
igual a partir do mesmo projeto.

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
   - **`.md`/`.markdown`**: `_chunk_markdown` acha todas as linhas que
     batem com `^#{1,6}\s+`, e cada seção vai do header até o próximo
     header (ou fim do arquivo). Nome do chunk = texto do header.
   - **`.html`/`.htm`**: `_chunk_html` (ver 4.1) -- extrai `<script>` e
     chunka o JS de dentro.
   - **qualquer outra extensão que `tree_sitter_language_pack.detect_language_from_path`
     reconheça** (dezenas de linguagens, ver 4.1): `_chunk_treesitter`,
     genérico, sem código por linguagem.
   - **fallback** (extensão não reconhecida, ou o passo acima não achou
     nenhum chunk): `_chunk_lines`, blocos fixos de `LINE_CHUNK_SIZE`
     (150) linhas, nome tipo `"linhas 1-150"`.
6. **Nós `file_context`**: um `db.upsert_node(type="file_context", ...)`
   por chunk, com `parent_id` = id do nó `file`, `content` = texto do
   chunk, `start_line`/`end_line` do trecho original.

Resultado real (indexação do `ambiente_pessoal_llm` em 2026-09-03): 412
arquivos, 953 contextos, 2 pulados (binário/grande), rodando em poucos
segundos.

### 4.1 Chunking genérico por linguagem (tree-sitter) -- 2026-09-03

Motivação real: um chat travou tentando diagnosticar 4 bugs interligados
num `poker.html` de 917 linhas, e o `codegraph-mcp` não conseguia ajudar
porque o chunking daquele arquivo era só blocos de linha fixos (`.html`
não tinha chunking inteligente nenhum) -- não dava pra pedir "só a função
de distribuir cartas". A pergunta que motivou essa seção: como generalizar
isso sem escrever um parser por linguagem, já que o projeto pode trocar
de stack a qualquer momento?

**Mecanismo** (`_chunk_treesitter`, substituiu o `_chunk_python` baseado
em `ast`): usa [tree-sitter](https://tree-sitter.github.io/) via o pacote
`tree-sitter-language-pack`, que empacota gramáticas prontas pra dezenas
de linguagens e resolve `extensão -> linguagem` sozinho
(`detect_language_from_path`) -- zero mapa de extensão mantido à mão.

O chunker em si é **uma função só, sem `if lang == "python": ... elif lang == "go": ...`**:

1. Parseia o arquivo com a gramática certa.
2. Pra cada nó de nível superior da árvore, procura um campo `name`
   (`_find_name_node`) -- direto no nó, ou até 2 níveis abaixo (cobre o
   caso comum de `const foo = () => {}`, onde o nome fica dentro de um
   `variable_declarator` aninhado, não no nó de fora). A convenção de
   campo `name` é comum a praticamente toda gramática tree-sitter --
   é isso que generaliza sem código por linguagem.
3. **Filtro estrutural** (não específico de linguagem): só vira chunk
   quem tem corpo de mais de 1 linha. Sem isso, `import json` (Python)
   ou `echo "..."` (Bash) também têm campo `name` na gramática e viram
   ruído de chunk de 1 linha -- descoberto testando de verdade em
   `db.py`/`setup-project.sh`/`schema.sql` (essas linguagens rodaram
   através do mesmo `_chunk_treesitter`, sem tratamento especial).
4. Nó sem `name` (import, statement solto) é ignorado -- não vira chunk,
   igual o `ast` fazia antes só pra Python.

**HTML** (`_chunk_html`) é o único caso com lógica própria, e por um
motivo estrutural, não por ser "JS": a gramática HTML trata o conteúdo
de `<script>` como texto bruto (`raw_text`), não como árvore JS -- não
tem "language injection" automática nesse pacote. `_chunk_html` acha
cada nó `script_element` > `raw_text`, roda `_chunk_treesitter` nesse
texto com `lang="javascript"`, e desloca as linhas resultantes de volta
pro arquivo original (`line_offset`). O mesmo `_chunk_treesitter`
genérico é reaproveitado -- só a extração do texto é HTML-específica.

**Testado de verdade**: JS/Go/Rust (sintéticos, confirmando que
`function`, `const x = () => {}`, `type X struct{}`, `fn`/`struct` do
Rust são todos capturados pela mesma função); HTML com `<script>`
embutido (funções + `const` arrow function + classe, linhas corretas
depois do deslocamento); regressão em Python (`db.py`, 14 chunks, uma
função por chunk, igual ou melhor que o `ast` antigo); SQL (`schema.sql`,
pega `CREATE TABLE`/`CREATE TRIGGER` -- imperfeito, alguns triggers
subsequentes no mesmo arquivo não foram capturados, não investigado a
fundo); Bash (`setup-project.sh`, pega os blocos de heredoc Python
embutidos como chunks, ruído de comando solto filtrado).

**Limitação conhecida**: `content_hash` só rastreia mudança de
*conteúdo* do arquivo, não mudança no *algoritmo* de chunking -- trocar
a lógica de chunking (como aconteceu aqui) não re-processa arquivos que
não mudaram desde a última indexação. Pra ver o efeito de uma mudança de
chunker em tudo que já tá indexado, precisa apagar o `.codegraph/graph.db`
e reindexar do zero (testado, funciona, só não é automático).

### 4.2 `.codegraphignore` -- excluir arquivo do grafo sem tocar no disco (2026-09-05)

Achado mapeando `royal_poker_online` de verdade: `src/poker.html` (versão
pré-migração pra React, não importada em lugar nenhum -- confirmado via
`grep` + `git log` mostrando 3h sem tocar contra 9min do `Poker.tsx`)
tinha **48 contextos indexados** -- mais que qualquer arquivo em uso de
verdade no projeto. Toda busca por lógica de jogo (`bestHand`, `score5`,
etc) vinha duplicada: uma vez do código real, outra do código morto sob
nomes ligeiramente diferentes. Isso é puro ruído -- não existia
mecanismo pra excluir um arquivo específico do índice, só pastas
inteiras (`IGNORE_DIRS`, hardcoded: `.git`, `node_modules`, etc).

Fix: `.codegraphignore` opcional na raiz do projeto -- um padrão glob
por linha, mesma convenção do `.gitignore` (`#` e linha vazia
ignorados; padrão sem `/` casa só pelo nome em qualquer pasta, com `/`
casa o caminho relativo inteiro). `indexer.load_ignore_patterns()` lê o
arquivo; `indexer.is_ignored()` testa um caminho contra os padrões
(`fnmatch`).

Dois pontos onde isso entra:
- `iter_project_files()` não visita mais arquivo que bate num padrão --
  nunca mais vira nó novo.
- **Arquivo que já estava indexado ANTES do padrão existir** não fica
  órfão: no início de `index_project()`, antes do loop principal, varre
  todo nó `type='file'` já no grafo e remove (`db.delete_node()` --
  filhos + arestas + o nó em si) qualquer um cujo `path` bata num
  padrão atual. Sem esse passo, adicionar uma linha no
  `.codegraphignore` não teria efeito nenhum em arquivo já indexado
  antes -- só passaria a valer pra indexação nova, deixando o lixo
  antigo pra sempre no grafo.

Nunca apaga o arquivo de verdade no disco -- só tira ele do grafo/busca.

Testado de ponta a ponta: criado `.codegraphignore` com `src/poker.html`,
rodado `codegraph setup` de novo -- log mostrou
`[removido do índice, .codegraphignore] src/poker.html` e
`1 removido(s) por .codegraphignore` no resumo. Busca por `bestHand`
antes: 2 `file_context` (engine.ts real + poker.html duplicado). Depois:
1 só.

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
| `list_history` | `limit` | `db.get_recent(conn, "history", limit)` -- entradas de prompt+resposta mais recentes primeiro (ver seção 9) |

`get_flow` é a tool que carrega o mecanismo central do projeto: um
fluxo pedido de uma vez só já vem com o código-fonte relevante embutido,
sem o cliente precisar de uma segunda chamada por passo.

## 7. Fluxo ponta a ponta (exemplo real)

Via `./setup-project.sh proj/` (recomendado -- faz os passos 1-3 de uma
vez, incluindo merge cuidadoso no `mcp.json` sem apagar outras entradas
que já estejam lá), ou manualmente:

1. `codegraph.cli --db proj/.codegraph/graph.db index proj/` -- varre
   `proj/`, cria nós `file`/`file_context`.
2. `codegraph.cli --db proj/.codegraph/graph.db load-flows proj/.codegraph/flows/` --
   lê os YAML, cria nós `flow`/`flow_step`, liga via `edges`.
3. `.kimi-code/mcp.json` dentro de `proj/` diz pro Kimi Code subir
   `codegraph/server.py` com `CODEGRAPH_DB=proj/.codegraph/graph.db`.
4. Abrir uma sessão **nova** do Kimi Code dentro de `proj/` -- ele só lê
   o `mcp.json` na hora que inicia; uma sessão que já estava aberta antes
   do arquivo existir/mudar não recarrega sozinha.
5. Dentro da sessão, o modelo decide chamar `get_flow("nome_do_fluxo")`
   em vez de ler os arquivos um por um -- uma tool call, uma resposta,
   com o código já dentro. `/mcp` dentro do Kimi Code confirma a conexão.

Esse ciclo (passos 1-2, ou `setup-project.sh` de novo) precisa ser
**re-rodado manualmente** depois de mudanças relevantes no projeto --
não tem watch/auto-reindex hoje (ver "Próximos passos" no README). É
idempotente: rodar de novo sem mudança nenhuma só confirma que está tudo
sincronizado (`0 indexado(s), N sem mudança`).

## 8. Mapa de arquivos

| Arquivo | Responsabilidade |
|---|---|
| `codegraph/schema.sql` | Definição das tabelas `nodes`/`edges`/`nodes_fts` + triggers |
| `codegraph/db.py` | Camada fina sobre `sqlite3` -- todo SQL do projeto vive aqui, + migração de schema |
| `codegraph/indexer.py` | Parte 1: varredura + chunking + geração de nós `file`/`file_context` |
| `codegraph/flows.py` | Parte 2: parse de YAML + geração de nós `flow`/`flow_step` + arestas |
| `codegraph/history.py` | Parte 3: grava nós `history` (prompt+resposta) + expurgo por tamanho |
| `codegraph/state.py` | Guarda/lê qual projeto está "ativo" (pra onde o proxy grava histórico) |
| `codegraph/proxy.py` | Proxy HTTP entre Kimi Code e llama-server -- passthrough + captura de histórico |
| `codegraph/server.py` | Servidor MCP -- as 7 tools |
| `codegraph/cli.py` | Comandos `index`/`load-flows`/`stats`, sem precisar do servidor no ar |
| `setup-project.sh` | Indexa + registra `mcp.json` + config de histórico + marca projeto ativo, num comando |
| `flows/*.yaml` | Definições de fluxo (não é código -- é dado, editado à mão) |

## 9. Histórico de prompts (memória)

Cada prompt que o usuário manda e a resposta que o modelo devolve viram
um nó `history` no grafo -- capturados automaticamente, sem o modelo
precisar chamar tool nenhuma pra "salvar". Só entradas desse tipo são
sujeitas a expurgo por tamanho; indexação de projeto é permanente.

### 9.1 Por que precisa de um proxy

O Kimi Code é um binário fechado -- não dá pra interceptar o que ele
manda pro modelo de dentro dele. O jeito é colocar um **proxy HTTP**
(`codegraph/proxy.py`, porta 8081) no meio: o Kimi Code fala com o
proxy, o proxy repassa pro `llama-server` de verdade (porta 8080),
deixa a resposta passar de volta (streaming incluso, byte a byte), e
**enquanto isso** grava prompt+resposta.

Isso exige mudar o `base_url` **global** do Kimi Code
(`~/.kimi-code/config.toml`, de `http://localhost:8080/v1` pra
`http://localhost:8081/v1`) -- afeta todo projeto, não só um. Consequência
direta: **o proxy precisa estar no ar antes de abrir o Kimi Code**, se
não nada funciona (nem passthrough, nem chat).

**Auto-start (2026-09-03)**: pra não virar mais uma ação manual pra
lembrar, o próprio alias `llama-qwen` (`~/.bashrc`, uso pessoal desta
máquina -- não faz parte do `codegraph-mcp` em si) checa `curl .../health`
na porta 8081 antes de subir o `llama-server`, e se não responder, sobe
o `codegraph.proxy` sozinho em segundo plano (`nohup`, dentro de um
subshell com `cd` pro diretório certo). Isso foi escrito **antes** do
`pip install -e .` existir (seção 2.1) -- hoje o `cd` nem seria mais
necessário (o pacote resolve de qualquer `cwd`), mas não vale a pena
mexer numa coisa que já funciona só por estética. A versão
multiplataforma equivalente, sem `cd` nenhum porque não precisa mais, é
o `codegraph-up` (seção 2.3) -- é o que deve ser documentado/usado por
quem não é esta máquina específica. Efeito prático aqui: **o usuário só
roda `llama-qwen`, igual antes** -- o número de
ações manuais pra usar o sistema não aumentou por causa do histórico.

### 9.2 Qual projeto recebe o histórico

O `llama-server` não é escopado por projeto -- só existe um rodando,
compartilhado por qualquer projeto que o Kimi Code tenha aberto. O proxy
também não sabe de qual projeto é uma requisição. Solução: um arquivo de
estado simples, `~/.codegraph/active-project` (`codegraph/state.py`),
guardando o path do projeto ativo agora. O proxy lê esse arquivo em cada
requisição de chat. `setup-project.sh` marca o projeto que acabou de
configurar como ativo automaticamente; pra trocar de projeto sem
re-indexar, dá pra chamar `state.set_active_project()` direto.

Se não tem projeto ativo (arquivo não existe), o proxy só faz
passthrough, sem gravar nada -- não quebra, só não loga.

### 9.3 Como o proxy captura prompt+resposta (proxy.py)

Rota `/v1/chat/completions` (a única com lógica especial -- todo o resto
é passthrough genérico, `passthrough()`):

1. Lê o corpo da requisição, extrai a **última mensagem `role=user`**
   (`_last_user_message`) -- não a conversa inteira, só o que é novo
   nesse turno (a conversa toda já vem re-enviada pelo cliente a cada
   turno; logar tudo de novo toda vez duplicaria histórico
   quadraticamente).
2. Repassa a requisição pro `llama-server`, idêntica.
3. **Sem streaming**: espera a resposta completa, extrai `content` e
   `reasoning_content` do JSON.
4. **Com streaming** (o caso normal -- Kimi Code manda `stream: true`):
   itera `aiter_bytes()` do jeito que chega, **repassa cada chunk pro
   cliente imediatamente sem reformatar** (só transparência garante que
   o parser SSE do Kimi Code não quebra), e em paralelo acumula os
   mesmos bytes num buffer, separando por `\n\n` (fronteira de evento
   SSE) só pra extrair `delta.content`/`delta.reasoning_content` de cada
   chunk `data: {...}` -- é leitura, não reescrita do que o cliente
   recebe.
5. Quando o stream fecha, grava o nó `history` (`history.record_exchange`)
   numa task assíncrona separada (`asyncio.create_task`) -- não atrasa a
   resposta ao cliente esperando a escrita no SQLite.

`content` guardado no nó é só o texto final (`PROMPT:\n...\n\nRESPONSE:\n...`)
-- o `reasoning_content` **não** entra no `content` (só a contagem de
caracteres, em `metadata`), pra não inflar o histórico com raciocínio
interno que não serve pra nada numa consulta futura.

### 9.4 Limite de tamanho e expurgo (history.py)

Config por projeto, `.kimi-code/codegraph-history.json` (ao lado do
`mcp.json`, criado pelo `setup-project.sh` na primeira vez, nunca
sobrescrito depois):

```json
{ "max_history_mb": 15360 }
```

15360 MB = 15 GiB, o default (`DEFAULT_MAX_HISTORY_MB`). **Esse limite
conta só o espaço ocupado pelos nós `history`** -- indexação
(file/file_context/flow/flow_step) nunca é contada nem apagada por esse
mecanismo, mesmo que sozinha já exceda o limite (nesse caso o expurgo
simplesmente para quando não sobra mais `history` pra remover, ver
`enforce_limit`).

Mecânica: depois de cada `record_exchange`, `enforce_limit` mede o
tamanho real do arquivo `.db` (`os.path.getsize`) e, enquanto estiver
acima do limite, apaga o nó `history` mais antigo (`created_at ASC`) um
de cada vez, re-medindo depois de cada delete.

Isso só funciona porque o banco roda com **`auto_vacuum = FULL`** --
sem isso, `DELETE` no SQLite não libera espaço em disco de verdade (as
páginas ficam reservadas até um `VACUUM` manual), e `enforce_limit`
ficaria apagando pra sempre sem o tamanho do arquivo nunca cair.
`db._migrate()` liga isso automaticamente (via `PRAGMA auto_vacuum=FULL`
+ `VACUUM` uma vez) em bancos criados antes dessa migração existir --
testado de verdade: inserido ~5 MB de histórico, forçado um teto
apertado, e o arquivo `.db` realmente encolheu de volta depois do
expurgo.

### 9.5 Migração de schema (db.py)

Bancos criados antes do tipo `history` existir têm o `CHECK` antigo
(só file/file_context/flow/flow_step) fisicamente gravado no arquivo --
`CREATE TABLE IF NOT EXISTS` não atualiza um `CHECK` de tabela já
existente. `db._migrate()`, rodado a cada `connect()`, detecta isso
(lendo `sqlite_master`) e reconstrói só a tabela `nodes` (via
`ALTER TABLE ... RENAME` + recriar + copiar dados), preservando tudo
que já tava lá. Roda uma vez só -- depois da primeira migração, o
`CHECK` já bate e a função não faz nada nas próximas conexões.

## 10. Medindo efetividade (`codegraph/metrics.py` + rota `/dashboard`)

Motivação: "o projeto ajuda mesmo?" não pode ser resposta de opinião --
tem que vir de dado real. Duas categorias de métrica, com honestidade
sobre o que é medido de verdade e o que é estimado.

### 10.1 O que é medido de verdade

O `llama-server` devolve `usage` (tokens) e `timings` (velocidade) no
final da resposta -- inclusive em streaming, mas só se o request pedir
`stream_options: {"include_usage": true}` (testado: sem isso, o campo
`usage` não vem no streaming). O proxy (`chat_completions` em
`proxy.py`) injeta esse parâmetro automaticamente em toda requisição de
chat antes de repassar pro `llama-server` -- o Kimi Code não precisa
pedir isso sozinho.

Capturado por troca (gravado em `metadata` do nó `history`, junto do
que já existia -- `reasoning_chars`):
- `prompt_tokens`, `completion_tokens`, `cached_tokens`
- `predicted_per_second`, `prompt_per_second` (velocidade real de geração)
- `used_codegraph_tools`: lista de nomes de tool do `codegraph-mcp` que
  apareceram em `tool_calls` daquela resposta (comparado contra
  `CODEGRAPH_TOOL_NAMES` -- filtra tool calls de outros MCP servers que
  o usuário tenha configurado, se houver).

Tool calls em streaming vêm parciais por índice (`delta.tool_calls`) --
só o nome da função é usado aqui (não precisa reconstruir os argumentos
completos pra saber *se* uma tool foi chamada).

### 10.2 O que é estimado, e por quê é rotulado assim

"Tokens poupados" (`metrics.effectiveness_summary`) **não é rastreado
byte a byte** -- isso exigiria acompanhar o conteúdo exato de cada
resultado de tool call através de múltiplas requisições (o histórico da
conversa cresce a cada turno, reconstruir "o que exatamente entrou nesse
prompt por causa de uma tool" é bem mais caro de implementar certo).

Em vez disso: `(tamanho médio de um arquivo inteiro indexado -
tamanho médio de um chunk `file_context` devolvido) × número de trocas
que usaram alguma tool`, convertido de caracteres pra tokens numa razão
grosseira (~4 chars/token). É uma estimativa de ordem de grandeza, não
uma medição -- o dashboard deixa isso explícito no rodapé, não esconde.

### 10.3 Cobertura de indexação (`coverage_stats`)

Sinal indireto de efetividade: quanto do código indexado tem chunking
"de verdade" (tree-sitter/markdown) vs. caiu no fallback de linha.
Detectado sem estado extra -- um chunk de fallback sempre tem nome no
formato `"linhas N-M"` (`_chunk_lines`, `indexer.py`); qualquer outro
nome veio de chunking real. Medido de verdade no `ambiente_pessoal_llm`:
44.2% (441/998) -- mostra que ainda tem bastante coisa (provavelmente
JSON de dataset, `.txt`, etc.) sem gramática tree-sitter útil.

### 10.4 A rota `/dashboard`

`GET http://localhost:8081/dashboard` (aceita `?project=/caminho` pra
ver outro projeto; default é o projeto ativo, `state.get_active_project()`).
HTML gerado no servidor (nada de build step/framework front-end),
gráficos via Chart.js (CDN -- página é só local/localhost, sem problema
de CSP). Sem autenticação -- mesma superfície de exposição do resto do
proxy (`127.0.0.1` only, ver seção 9.1).

Testado de ponta a ponta contra dado real do `ambiente_pessoal_llm`:
412 arquivos, 18 trocas de histórico (2 delas já com métricas completas
-- as anteriores a essa feature aparecem como `null` nos gráficos, sem
quebrar nada).

### 10.5 Como ler os 7 cards do dashboard

| Card | O que significa |
|---|---|
| `arquivos indexados` | Quantos arquivos do projeto estão no grafo (`coverage_stats`, `type='file'`). |
| `contextos com chunking inteligente` | Dos nós `file_context`, quantos vieram de análise real (tree-sitter/markdown) vs. caíram no fallback burro de linha (`_is_smart_chunk` -- ver seção 10.3). |
| `fluxos mapeados` | Quantos `flows/*.yaml` foram carregados (`type='flow'`). Zero aqui significa `get_flow` não tem nada pra devolver ainda -- é manual, não é gerado sozinho (seção 5). |
| `trocas de prompt registradas` | `COUNT` de nós `type='history'` -- cada ida-e-volta real capturada pelo proxy. |
| `trocas que usaram tool do codegraph-mcp` | % das trocas acima em que `used_codegraph_tools` (seção 10.1) não veio vazio. |
| `tokens/s médio de geração` | Média de `predicted_per_second`, real, direto do `llama-server`. |
| `tokens poupados (estimativa)` | Ver 10.2 -- e a próxima seção, porque esse número **depende diretamente** do card anterior. |

### 10.6 Por que "tokens poupados" fica em zero (achado real, 2026-09-03)

A fórmula (seção 10.2) multiplica a diferença de tamanho por
`len(with_tools)` -- **quantas trocas usaram alguma tool**. Testando
contra uso real do `ambiente_pessoal_llm` (20 trocas registradas), esse
número ficou em **0.0%** -- ou seja, o modelo nunca chamou
`search`/`get_flow`/`get_file_tree`/etc nessas 20 trocas, então a conta
zera inteira, não importa quão boa seja a cobertura de indexação.

Duas causas identificadas, não é bug:

1. **O Kimi Code tem tool de leitura de arquivo própria** (nativa dele) --
   nada hoje diz pro modelo "prefira as tools do `codegraph-mcp`" em vez
   da tool nativa. Sem motivo claro pra preferir, ele usa o que já
   conhece.
2. **Zero fluxos mapeados** nesse projeto -- o cenário onde a ferramenta
   mais se destaca (`get_flow` devolvendo trecho já resolvido) não existe
   ainda pra ele escolher.

Duas formas de forçar um resultado real (não é preciso mudar código
nenhum, é uso):
- Pedir explícito no prompt: *"usa a tool `search` do codegraph pra
  achar X"* -- força a chamada, o card muda na próxima carregada da
  página.
- Mapear um fluxo de verdade (`.codegraph/flows/*.yaml`) pra algo que o
  usuário vá perguntar -- aumenta a chance do modelo escolher `get_flow`
  sozinho, sem precisar pedir explícito.

Sem um desses dois empurrões, `with_tools_pct` tende a ficar em zero
indefinidamente -- a ferramenta existir não é suficiente pra ela ser
usada; é sinal real de adoção. **Ressalva** (ver 10.7): isso só é
confiável depois do fix do bug de detecção -- antes dele, dava zero
mesmo quando a tool *era* usada de verdade.

### 10.7 Bug real: detecção de tool nunca batia (achado e corrigido, 2026-09-03)

Testando os dois prompts sugeridos (10.6) contra uma sessão real do Kimi
Code: o segundo prompt (forçando `search` explícito) **realmente chamou
a tool** -- visível na UI do Kimi Code ("Used search · MCP/codegraph",
"Approve mcp__codegraph__search"). Mesmo assim, o dashboard continuou
em 0.0%.

Causa: o Kimi Code prefixa o nome de cada tool MCP com o nome do
servidor, pra evitar colisão entre servidores diferentes que
exponham uma tool de mesmo nome -- `search` chega no modelo (e volta
no `tool_calls`) como `mcp__codegraph__search`, não como `search`. A
checagem original (`name in CODEGRAPH_TOOL_NAMES`, comparação exata
contra os nomes puros das tools) nunca batia com esse formato --
`used_codegraph_tools` ficava sempre vazio, silenciosamente, mesmo com
a tool sendo chamada de verdade.

Confirmado direto no banco: a entrada de histórico do prompt "usa a
tool search do codegraph..." não tinha `used_codegraph_tools` na
`metadata`, apesar da tool ter rodado (visível na UI).

**Fix** (`_is_codegraph_tool`, `proxy.py`): em vez de comparar o nome
inteiro, checa se `"codegraph"` aparece em qualquer lugar do nome
(case-insensitive) -- como o servidor MCP se chama exatamente
`"codegraph"` (`MCPServer("codegraph")` em `server.py`), esse
substring aparece não importa qual prefixo o cliente use, presente ou
futuro. Mantém também o match exato contra `CODEGRAPH_TOOL_NAMES` como
fallback, pra clientes que não prefixam nada.

Efeito prático: **dados de "trocas que usaram tool" registrados antes
desse fix (2026-09-03) estão subestimados** -- podem ter usado a tool
de verdade e não terem sido contados. O dashboard não reprocessa
histórico antigo automaticamente; só as trocas novas, depois do fix,
contam certo.

### 10.8 Uso real zero mesmo com detecção correta e MCP conectado (2026-09-05)

Mesmo depois do fix da seção 10.7, o dashboard continuava mostrando
"0.0% trocas que usaram tool do codegraph-mcp" e "~0 tokens poupados"
depois de dezenas de trocas reais no projeto `royal_poker_online`. Isso
não é mais bug de contagem -- é a tool nunca sendo chamada, de verdade.

Investigação, eliminando causas uma a uma:

1. **O servidor MCP está registrado certo?** Sim -- `.kimi-code/mcp.json`
   do projeto aponta pro `codegraph.server` certo, no formato que a
   própria documentação embutida no binário do Kimi Code espera
   (`<cwd>/.kimi-code/mcp.json`, confirmado com `strings` no binário).
2. **O servidor MCP sobe e expõe as tools direito?** Sim -- testado
   direto com um `ClientSession` real do SDK `mcp` (sem depender do Kimi
   Code): handshake completo, `list_tools()` devolve as 7 tools
   (`list_files`, `get_file_tree`, `get_node`, `search`, `list_flows`,
   `get_flow`, `list_history`) certinho.
3. **O Kimi Code enxerga essas tools numa sessão de verdade?** Sim --
   rodando `kimi -p "liste suas tools"` dentro do projeto, o modelo lista
   as 7 tools `mcp__codegraph__*` junto com as nativas. Confirmado: a
   conexão MCP nunca foi o problema.

Causa raiz real: **nada nunca instruiu o modelo a preferir essas tools**.
Elas aparecem disponíveis, mas um MCP tool exposto não faz o modelo
escolher usá-lo -- ele decide sozinho, e por padrão prefere as tools
nativas que já conhece bem (`Read`, `Grep`, `Glob`). Não existia nenhum
`AGENTS.md` no projeto (nem em `.kimi-code/`, nem na raiz) com qualquer
menção ao codegraph. Sem essa peça, o servidor MCP existir é necessário
mas não suficiente.

**Fix**: `codegraph setup` (via `_write_agents_md()` em `cli.py`) agora
gera, uma vez só (idempotente -- checa um marcador HTML antes de
escrever, nunca duplica nem apaga o que o usuário já tenha escrito no
arquivo), um bloco em `.kimi-code/AGENTS.md` instruindo o agente a tentar
`mcp__codegraph__search`/`list_flows`/`list_history` antes de recorrer a
`Read`/`Grep` no projeto inteiro. `setup-project.sh` ganha isso de graça
(só chama `codegraph.cli setup`).

**Verificado de verdade, antes/depois**: rodando `kimi -p "Como funciona
o sistema de áudio deste projeto?"` depois do `AGENTS.md` existir, o
modelo narrou explicitamente "Let me start by searching for
audio-related content in the codegraph" -- primeira vez em toda a sessão
que ele tentou a tool por conta própria, sem ninguém pedir
explicitamente "usa o codegraph". Ver seção 10.9 pro que aconteceu
*depois* dessa tentativa (a busca quebrava).

### 10.9 Bug real: `search` quebrava com ponto no termo (2026-09-05)

Continuação direta do teste da seção 10.8: o modelo tentou `search` com
algo envolvendo `audio.ts` (nome de arquivo), a tool "devolveu vazio", e
ele caiu pra `Grep` -- mas na verdade a tool não devolvia vazio, ela
**quebrava**. Reproduzido isolado com um `ClientSession` de teste:

```
search(query="audio.ts")
  -> sqlite3.OperationalError: fts5: syntax error near "."
```

Causa: `db.search()` passava a string de busca direto pro `MATCH` do
FTS5 sem sanitizar. O parser de query do FTS5 (não o tokenizer -- o
tokenizer lida bem com pontuação na hora de indexar) trata caracteres
como `.`, `-`, `:`, `"` como possível início de operador especial fora
de uma frase entre aspas; um termo bareword com esses caracteres no meio
derruba a query inteira com `syntax error`. Como o agente frequentemente
busca por nome de arquivo (`audio.ts`, `Poker.tsx`) ou identificador com
hífen, isso quebrava a tool exatamente nos casos de uso mais comuns.

**Fix**: `db._sanitize_fts_query()` -- separa a query em palavras e
envolve cada uma em aspas duplas (escapando aspas internas no jeito do
FTS5, `"` -> `""`), virando uma sequência de frases literais em AND
implícito. Isso torna qualquer entrada segura (testado com
`audio.ts`, `Poker.tsx`, `get-node`, `a:b`, e até `x"y` -- nenhum quebra
mais), ao custo de deixar de aceitar a sintaxe booleana avançada do FTS5
(`"auth AND token"` como operador -- agora `AND` vira palavra literal
buscada, não operador). Aceitável: quem chama essa tool é o agente
(que não sabe/precisa saber sintaxe FTS5), não uma pessoa digitando
query avançada de propósito -- a docstring da tool `search` (`server.py`)
foi atualizada pra não prometer mais essa sintaxe.

## 11. Árvore interativa (aba "Árvore" do `/dashboard`)

Segunda aba da mesma página do dashboard (seção 10.4) -- navegação
visual do grafo: zoom, arrastar, clicar num nó pra ver o conteúdo e
expandir os filhos. Usa [vis-network](https://visjs.github.io/vis-network/)
via CDN (só JS/CSS, sem passo de build).

### 11.1 Por que carregamento sob demanda, não tudo de uma vez

Primeira tentativa: jogar todo mundo (~1.400 nós de um projeto real)
como filho direto de um nó raiz. Não escala -- renderiza, mas o grafo
fica largo demais pra caber na tela sem dar `fit()` numa visão tão zerada
que fica ilegível. Fix: dois níveis de carregamento preguiçoso:

1. **`GET /api/tree/roots`** devolve só os nós de topo (`file`/`flow`,
   sem `content`, payload pequeno mesmo em projeto grande).
2. **Agrupamento por pasta é 100% client-side** (`buildFolderTree` no
   JS) -- a partir do `path` de cada arquivo, monta uma árvore de pastas
   sintética no navegador, sem endpoint novo nenhum. Pastas viram nós
   com id `"dir:caminho/da/pasta"` (só existem no browser). Isso reduz
   a visão inicial de "412 arquivos soltos" pra "~15-20 pastas de topo"
   -- navegável de verdade.
3. **`GET /api/tree/node/<id>`** só é chamado quando um nó **real** (não
   pasta sintética) é clicado -- devolve o nó completo (com `content`),
   filhos (`db.get_children`) e arestas relacionadas nos dois sentidos
   (`db.get_edges_from`/`get_edges_to`) -- exatamente a mesma composição
   que a tool MCP `get_node` já fazia (`server.py`), só reaproveitada
   aqui pra JSON em vez de resposta de tool.

Clique expande e não colapsa (`Set` de ids já expandidos, `expanded`) --
simples o suficiente pra v1; nós repetidos (mesmo nó alcançável por dois
caminhos, ex: um `flow_step` que aponta pro mesmo `file_context` que já
apareceu como filho de um arquivo) são deduplicados via
`visNodes.get(id)` antes de adicionar de novo.

### 11.2 Testado por dentro, não só por fora (achado de processo, 2026-09-03)

Rodando dentro do ambiente de automação de browser desta sessão: os
endpoints respondiam certo (200, dados reais), o `<canvas>` existia com
tamanho correto, sem erro nenhum no console -- mas nada aparecia no
print de tela. Investigando: o canvas estava **fisicamente vazio**
(`getImageData` -- zero pixels não-transparentes). A própria ferramenta
de automação avisou "the Browser pane is currently hidden" -- rAF
(`requestAnimationFrame`, que o vis-network usa pro loop de desenho) não
roda em aba em segundo plano, em nenhum navegador -- comportamento padrão
do browser, não bug do código.

Verificação alternativa, sem depender de pintura de tela:
`network.getPositions()` (estado interno da biblioteca, calculado pelo
layout, independente de canvas/pixels) devolveu 18 nós com posições reais
calculadas (root + pastas de topo + arquivos-raiz + fluxos) -- confirma
que os dados, o agrupamento por pasta, e o layout hierárquico funcionam
certos.

**Isso não foi suficiente -- ficou faltando um pedaço real.** Testando
no navegador de verdade do usuário: a aba do Chrome **travou e fechou**
("A aplicação Google Chrome fechou inesperadamente"), com a tela
crescendo sem parar antes de morrer. `getPositions()` confirma dados e
layout, mas não teria pego isso -- é um bug de *dimensionamento do
contêiner na tela*, categoria totalmente diferente de "os dados estão
certos". Declarar sucesso só com a verificação por dentro foi cedo
demais.

### 11.3 O bug real: loop de redimensionamento infinito (corrigido, 2026-09-03)

Causa: `.tree-layout`/`#network` tinham altura via `72vh`
(viewport) + CSS Grid esticando o item pra preencher a linha. O
`vis-network` tem seu próprio redimensionamento automático
(`autoResize: true` por padrão, usa `ResizeObserver`) -- com um
container cuja altura depende de unidade relativa/grid em vez de pixel
fixo, entra num ciclo: o canvas mede o espaço disponível, pede pra
ocupar aquilo, o container (grid/vh) recalcula e cresce um pouco em
resposta, o canvas mede de novo um espaço maior, pede mais ainda -- sem
convergir, até estourar memória/travar a aba. É um problema documentado
da comunidade do vis-network, não exclusivo desse projeto.

**Fix**: `.tree-layout`/`#network`/`.side-panel` passaram a ter altura
**fixa em pixel** (`600px`, não `72vh`/grid-stretch), e a opção
`autoResize: false` foi passada explícita pro `new vis.Network(...)`
(junto com `width`/`height` fixos nas options) -- o `vis-network` mede o
espaço **uma vez só** e nunca mais reage a mudança de tamanho do
container, quebrando o ciclo de propósito.

**Validado de um jeito que não depende de pintura de tela nem de aba em
primeiro plano** (contorna a limitação da seção 11.2): medi
`document.body.scrollHeight` e a altura real do `.tree-layout`/`#network`
via `getBoundingClientRect()` em 3 momentos ao longo de ~5 segundos --
ficou **exatamente igual** nos três (`600`/`602px`, sem crescer 1px),
diferente da pintura em canvas, layout/reflow acontece independente da
aba estar em primeiro ou segundo plano, então esse teste específico é
confiável mesmo nesse ambiente de automação. Ainda assim, a confirmação
visual final (o desenho aparecendo mesmo) depende do usuário abrir num
navegador de verdade -- ver seção 11.2.

### 11.4 Layout empilhado + nós menores (2026-09-03)

Pedido do usuário depois de ver funcionando de verdade: lado-a-lado
(grafo + painel) virou **empilhado** -- grafo em cima (100% de largura,
altura em px calculada como fração do viewport -- 60% normal, 85% em
tela cheia, seção 11.6) e painel de conteúdo embaixo (100% de largura,
`max-height: 340px` com scroll próprio). `sizeTreeLayout()` continua
setando altura em **px direto no `#network`**, não mais no wrapper
`.tree-layout` -- mesmo princípio anti-loop da seção 11.3, só mudou qual
elemento recebe o valor calculado.

Nós ficaram menores (`nodes: { size: 10 }`, default da lib é `25`) e o
espaçamento hierárquico caiu de `130`→`90` (nós menores cabem mais
apertados sem colidir).

### 11.5 Bug real: botão quebrava a página inteira (achado e corrigido, 2026-09-03)

O botão "Atualizar árvore" ganhou a mesma classe `tab-btn` das abas, só
por conveniência de estilo. A lógica de troca de aba escuta **todo**
elemento `.tab-btn` e assume que ele tem `data-tab` -- o botão de
atualizar não tem. Clicar nele disparava as duas coisas: a troca de aba
(que remove `.active` de **todos** os `.tab-content` antes de tentar
adicionar de volta em `document.getElementById('tab-' + undefined)`,
que é `null`) E o `refreshTree()`. A chamada em cima do `null` lança
exceção **depois** de já ter escondido as duas abas -- página inteira
fica em branco, exatamente o que o usuário viu de verdade ("quando eu
clico em atualizar árvore ela some").

Comprovado sem depender de tela: `read_console_messages` mostrou o erro
exato (`Cannot read properties of null (reading 'classList')`), apontando
pra linha certa. **Fix**: classe própria (`.refresh-btn`), sem
sobreposição com o seletor `.tab-btn` -- o botão nunca mais entra no
loop de troca de aba. Testado de verdade: clicar em "Atualizar" depois
do fix mantém `tab-tree` com `active` na classe, sem erro nenhum.

Lição: um seletor de classe genérico demais (`.tab-btn` pra "qualquer
botão parecido") pode acoplar comportamento que não devia -- vale
conferir se um elemento novo realmente deve entrar num `querySelectorAll`
já existente, não só copiar a classe pelo visual.

### 11.6 Tela cheia + rótulo de histórico sobrepondo (2026-09-03)

**Botão "Tela cheia"**: `window.open()` pra mesma URL do dashboard +
`?fullscreen=1` (preserva `?project=` se houver), numa janela nova do
tamanho da tela (`screen.availWidth/Height`). A página, ao carregar com
esse parâmetro (`IS_FULLSCREEN`), já abre direto na aba Árvore
(`.click()` programático no botão da aba) e usa `85%` da altura da tela
pro grafo em vez de `60%` -- mesma função `sizeTreeLayout()`, só o
percentual muda.

**Rótulo de histórico sobrepondo**: o nome de um nó `history` é o
preview do prompt, até 80 caracteres (`history.py`) -- com ~50 nós
irmãos e `nodeSpacing: 90`, o texto ficava ilegível, um em cima do outro
(achado real, visto na tela do usuário). Primeira tentativa: cortar o
rótulo em 18 caracteres -- **não foi suficiente** (seção 11.7): muitos
nós de histórico têm conteúdo parecido/repetido (o Kimi Code manda o
mesmo `<system-reminder>` várias vezes), então mesmo curtos os rótulos
formavam uma parede repetida ilegível, visto de novo na tela do usuário.

### 11.7 Troca de estratégia pro rótulo de histórico: número, não texto (2026-09-03)

Cortar o texto (seção 11.6) não resolveu de vez -- largura ainda variava
e textos repetidos/parecidos entre nós vizinhos continuavam formando uma
faixa confusa. Pedido do usuário: usar **numeração** (ordem de entrada)
em vez de qualquer preview de texto, com a descrição completa só no
hover.

Implementado sem precisar de contador à parte: o `id` de um nó no banco
**já é** a ordem real de entrada (`AUTOINCREMENT` do SQLite = ordem
cronológica de criação). `nodeToVis` troca o rótulo pra `'#' + n.id`
só pra `type === 'history'` -- largura fixa e pequena (nunca colide,
não importa quantos irmãos ou quão parecido o conteúdo seja), e o texto
completo continua no `title` (hover) e no painel lateral ao clicar.
Testado: rótulo real virou `"#1526"`, tooltip com o texto original
completo (`"history\n<system-reminder> ..."`).

### 11.8 Estatística de tamanho + teto editável com trava de segurança (2026-09-03)

Pedido do usuário: mostrar tamanho do grafo (MB/GB) e % preenchido do
teto de histórico na aba Árvore, atualizando junto do botão "Atualizar
árvore" -- e, se o usuário tentar baixar o teto (`max_history_mb`) pra
menos do que o banco já ocupa hoje, **bloquear** a ação com um aviso
claro (em vez de deixar `enforce_limit` entrar num estado impossível de
satisfazer sem apagar indexação, que ele nunca faz -- seção 9.4).

Isso exigiu dar um jeito de *ação* no editar o teto pra ter o que
bloquear -- até aqui o valor só era editado abrindo o `.json` na mão. Não
tem mais essa limitação: input + botão "Salvar teto" na própria aba
Árvore.

**Backend** (`history.py`):
- `history.LimitTooSmallError` -- exceção dedicada, guarda `requested_mb`
  e `current_mb`, mensagem já pronta explicando o que fazer (apagar
  `.codegraph/graph.db` manualmente + rodar `codegraph setup` de novo).
- `history.set_max_mb(project_root, mb, db_path)` -- só escreve o novo
  valor se `mb >= tamanho atual do arquivo .db em MB`; caso contrário
  levanta `LimitTooSmallError` **sem tocar no arquivo de config**.

**Rotas novas** (`proxy.py`):
- `GET /api/history-config` -- `{{db_size_bytes, max_history_mb,
  percent_used}}`, calculado na hora (`Path.stat().st_size` + `history.load_max_bytes`).
- `POST /api/history-config` (`{{max_history_mb: N}}`) -- chama
  `history.set_max_mb`; devolve `409` com a mensagem de erro se recusado
  (não `500` -- é uma rejeição esperada/validada, não uma falha).

Ambas na mesma rota `/api/history-config`, diferenciadas só pelo método
HTTP (`GET`/`POST`) -- Starlette resolve isso certo com duas entradas
`Route(...)` de mesmo path, testado (as duas responderam nos métodos
certos).

**Frontend**: `loadHistoryStats()` busca e formata (`formatSize` --
MB abaixo de 1024, GB acima) e roda junto de `buildTree()` tanto no
carregamento inicial (`initTree`) quanto no botão "Atualizar árvore"
(`refreshTree`, via `Promise.all`). `saveMaxHistory()` manda o `POST`;
se a resposta não for `ok`, mostra a mensagem de erro do backend num
`alert()` -- é isso que materializa o "bloqueio" pedido, sem inventar
lógica de validação duplicada no front (a regra mora só no backend).

Testado de ponta a ponta contra dado real: `GET` retornou `4.6 MB de
15360 MB, 0%`; `POST` com `2` (menor que o banco) devolveu `409` com a
mensagem certa e **não alterou o arquivo** de config (confirmado lendo
o `.json` depois); `POST` com um valor válido salvou normal.

### 11.9 Dois bugs reais achados pelo usuário: histórico com "lixo" e scroll na tela cheia (2026-09-05)

Dois problemas achados olhando a árvore de verdade, nenhum dos dois era
do modelo nem do Kimi Code -- os dois eram do jeito que o `proxy.py`
gravava/exibia dado.

**Bug 1 -- nó de histórico com `PROMPT` = lembrete e `RESPONSE` vazia.**
O usuário reparou que muitos nós `history` tinham como conteúdo só um
`<system-reminder>...</system-reminder>` (lembrete de data, de todo
list etc) como "prompt" e nada como resposta. Causa raiz: o Kimi Code
não faz **uma** chamada por pergunta do usuário -- faz uma chamada nova
pra `/v1/chat/completions` a cada rodada do loop de tool-calling
(pergunta -> modelo pede tool -> tool roda -> manda resultado de volta
pro modelo -> repete até ter resposta final). Em cada rodada intermediária
ele reenvia a conversa inteira, e reinjeta os `<system-reminder>` como
mensagens **`role: "user"`** novas, sem nenhum texto humano junto.

`_last_user_message()` pegava sempre a *última* mensagem `role=user` do
array -- que nas rodadas intermediárias é exatamente esse
system-reminder sintético, nunca a pergunta real (que fica mais pra
trás no array). E `_log_exchange()` só recusava gravar quando *as duas*
metades (prompt e resposta) vinham vazias -- uma rodada onde o modelo só
pediu uma tool (sem texto de resposta) ainda passava, porque o "prompt"
(o lembrete) não estava vazio.

Resultado: cada pergunta real do usuário virava VÁRIOS nós de
histórico, a maioria lixo (lembrete como prompt, resposta vazia),
poluindo a árvore e desperdiçando espaço do teto configurável (seção
11.8) com memória que não serve pra nada -- o objetivo dela é dar
contexto de conversas passadas pro modelo, e um par prompt-vazio ou
resposta-vazia não ajuda em nada nisso.

Fix, os dois lados (`proxy.py`):
- `_last_user_message()` agora limpa `<system-reminder>...</system-reminder>`
  de cada mensagem `user` (`_strip_system_reminders`, regex com
  `re.DOTALL`) e só aceita a mensagem se sobrar algo depois de limpar --
  senão continua procurando pra trás no array até achar a pergunta
  humana real.
- `_log_exchange()` trocou o `if not prompt and not content` (só recusa
  se os DOIS estiverem vazios) por `if not prompt.strip() or not
  content.strip()` (recusa se QUALQUER um dos dois estiver vazio) --
  nunca mais grava metade de um par.

Testado isoladamente (sem depender do Kimi Code de verdade): array
simulando uma pergunta real seguida de rodada de tool-call e reminder
sintético -- `_last_user_message` devolveu a pergunta real, ignorando o
reminder; array só com reminder devolveu `""` (e portanto
`_log_exchange` vai recusar gravar).

**Bug 2 -- popup de "Tela cheia" com scroll.** Print do usuário mostrou
barra de rolagem vertical E horizontal dentro do popup aberto por
"🖥️ Tela cheia". Causa: `sizeTreeLayout()` calculava a altura do grafo
como uma fração fixa "no chute" da altura da tela (85% em tela cheia),
sem descontar o que ainda aparecia acima/abaixo dele -- `h1`, subtítulo,
abas, a linha de estatística/botões, o painel de conteúdo e o rodapé de
instruções. Somando tudo isso ao 85% do grafo, o total passava da altura
real da janela -- e a barra de rolagem vertical que aparece por causa
disso rouba espaço horizontal, o que gerava a barra horizontal também
(efeito em cascata de um único erro de conta).

Fix: no popup de tela cheia (`?fullscreen=1`), esconde `h1`/subtítulo/abas
de vez (`body.fullscreen-mode h1, .sub, .tabs { display: none }` -- não
faz sentido navegar de aba lá, o popup já abre direto na árvore) e
`sizeTreeLayout()` deixou de chutar fração e passou a **medir** o espaço
que sobra de verdade: `window.innerHeight` menos a posição real de onde
o `.tree-layout` começa (`getBoundingClientRect().top` -- já reflete
automaticamente se o cabeçalho está escondido ou não) menos a altura
máxima que o painel de conteúdo pode ter (`340px`, o mesmo valor do
`max-height` no CSS -- usa o teto, não o tamanho atual, porque o painel
cresce quando o usuário clica num nó, e a altura do grafo não pode
depender de quando isso acontece) menos a altura real do rodapé de
instruções (`getBoundingClientRect().height`, também dinâmica -- reflete
se o texto quebrou em mais linhas numa janela estreita) menos uma
margem de segurança de 12px. `overflow: hidden` em `html`/`body` no modo
tela cheia fica como rede de segurança contra um resto de arredondamento,
não como a correção em si (a correção é medir certo, não é esconder o
sintoma).

### 11.10 Reforço do fix de histórico incompleto + limpeza retroativa (2026-09-05)

Continuação da seção 11.9: mesmo depois daquele fix, sobravam dois
padrões de "lixo" que o usuário reparou de novo, olhando a árvore:

1. **Narração + tool-call na mesma resposta.** `_log_exchange()` só
   recusava gravar quando `content` vinha vazio -- mas o modelo às vezes
   narra algo ("deixa eu checar o arquivo...") **junto** com uma
   tool-call no meio do loop, o que dá `content` não-vazio mesmo não
   sendo a resposta final. Fix: `has_tool_call` -- `chat_completions()`
   agora repara se a resposta (não-streaming: `message.tool_calls`;
   streaming: acumulado a cada chunk via `_extract_delta`) veio com
   qualquer tool-call, e `_log_exchange()` recusa gravar se
   `has_tool_call` for verdadeiro, não importa o que tinha em `content`.
   Só a resposta final (sem tool-call pendente) conta como troca completa.
2. **Mensagens sintéticas de compactação de contexto.** Além do
   `<system-reminder>...</system-reminder>` (seção 11.9), o Kimi Code
   também injeta como `role: user` texto plano de housekeeping (handoff
   de "vai ficar sem contexto" / "conversa foi compactada") que não é
   `<system-reminder>` (por isso o regex daquela seção não pegava) --
   apareceu de verdade nos ids 201-203 do projeto `royal_poker_online`.
   Fix: `_is_synthetic_prompt()` reconhece esses prefixos fixos
   (`"You are about to run out of context."`,
   `"The conversation so far has been compacted"`) e `_last_user_message()`
   pula essas mensagens do mesmo jeito que pula reminder, continuando a
   busca pra trás no array.

**Limpeza retroativa**: os dois fixes acima (e o da seção 11.9) só
previnem lixo **novo** -- não apagam o que já tinha sido gravado antes
deles existirem. Rodada uma limpeza manual, uma vez só, nos bancos que
já tinham histórico (`royal_poker_online`: 53 de 61 nós `history` eram
lixo por esse critério; `ambiente_pessoal_llm`: 64 de 66) -- removidos
com a mesma regra que `_log_exchange` já aplica (prompt vazio/só-lembrete
OU resposta vazia). Não é uma rotina automática permanente -- rodar de
novo se algum projeto antigo ainda tiver entradas assim (comparar contra
os critérios de `_is_synthetic_prompt`/`_strip_system_reminders`/checar
se a metade depois de `RESPONSE:` no `content` do nó veio vazia).

Efeito medido, projeto `royal_poker_online`: busca por `"audio"` (ver
seção 10.9) foi de 20 resultados (a maioria `<system-reminder>`
repetido) pra 4 (todos genuinamente relevantes).

### 11.11 Paginação do histórico na árvore -- mais recentes primeiro (2026-09-05)

Antes: clicar no balde de histórico (`hist:recent`) buscava as até 50
entradas mais recentes de uma vez e desenhava todas como filhas diretas
do balde -- com dezenas de entradas isso virava uma fileira só de
bolinhas, larga demais pra caber na tela e sem jeito de ver entradas mais
antigas que a 50ª.

Fix, paginação por **cursor** (`before_id`), não por offset -- `id` é
autoincrement e já reflete ordem cronológica real (mesmo raciocínio da
seção 11.7), então `WHERE id < :before_id ORDER BY id DESC LIMIT :n` é
estável mesmo com registros novos chegando entre uma página e outra (uma
paginação por offset teria esse problema: inserções no meio deslocam
o que cada página devolve).

- `db.get_recent()` ganhou `before_id: int | None` opcional.
- `/api/tree/history` aceita `before_id` na query string, busca
  `limit+1` linhas pra saber se sobra mais uma página sem precisar de um
  `COUNT` separado, e devolve `{"nodes", "has_more", "next_before_id"}`.
- No front, `loadHistoryPage(parentId, beforeId)` busca uma página e, se
  `has_more`, pendura um nó sentinela synthetic (`hist:more`, id fixo,
  guarda o cursor da próxima página no próprio objeto do nó) com o rótulo
  "… carregar mais antigas". Clicar nele remove o sentinela antigo e
  busca a próxima página a partir do cursor guardado; se ainda sobrar
  mais depois dessa, um sentinela novo entra no lugar.

Testado ponta a ponta forçando `limit=2` (via um `fetch` monkey-patchado
temporariamente no console) num histórico de 8 entradas: primeira
página trouxe as 2 mais recentes + sentinela; clicar no sentinela trouxe
as próximas 2 (cursor avançou certo) + sentinela de novo, até esgotar.

### 11.12 Espaçamento maior no layout; câmera focando só no que abriu (2026-09-05)

Dois ajustes na aba Árvore depois de feedback visual real:

- **Espaçamento**: `nodeSpacing`/`levelSeparation` do layout hierárquico
  foram de `90`/`100` pra `160`/`150` (mais `blockShifting`/
  `edgeMinimization` explícitos). Com pouco espaço, caixa com rótulo
  comprido (nome de arquivo longo, por exemplo) desenhava por cima da
  vizinha mesmo a árvore crescendo na direção certa (`direction: 'UD'`,
  de cima pra baixo, já era assim antes -- o problema nunca foi a
  direção, era espaço de menos por nível).
- **Câmera**: passou por duas rodadas de ajuste no mesmo dia.
  1. Uma tentativa trocou todo `network.fit(...)` (chamado depois de
     expandir um nó) pra reajustar mostrando o grafo **inteiro**, não só
     o pedaço novo -- revertida na hora por feedback direto do usuário
     ("o autoajuste diminuindo o zoom não deve acontecer, o usuário se
     perde").
  2. Voltar pro `network.fit({{ nodes: [...] }})` escopado só aos nós novos
     (o que já existia antes da tentativa 1) **ainda não resolvia** --
     `fit()` sempre recalcula o zoom pro nível que encaixa os nós dados
     na tela, não importa o escopo. Zoom manual que a pessoa já tivesse
     ajustado (deu zoom, foi abrir outro nó) voltava sozinho pro nível
     de "encaixe" a cada clique -- o usuário apontou isso especificamente
     ("dou zoom e vou abrir um nó com filhos, ele autoajusta... mas o
     zoom deve ficar o que já estava").

  Fix final: `focusNewNodes(nodeIds)` -- em vez de `fit()`, usa
  `network.moveTo({{ position: <centro dos nós novos>, scale:
  network.getScale() }})`. `moveTo` desloca a câmera (pan) pro centro do
  que apareceu, mas a escala passada é a **atual** (lida antes de mexer
  em qualquer coisa), nunca uma recalculada -- zoom nunca muda sozinho,
  só a posição. Testado de verdade: `network.moveTo({{scale: 2.5}})`
  simulando zoom manual, depois `onNodeClick` numa pasta com filhos --
  `network.getScale()` antes e depois deram exatamente `2.5`. O
  `network.fit()` sem argumento no `afterDrawing` (primeiro desenho da
  árvore, antes de qualquer zoom manual existir) continua sendo o único
  lugar que ainda usa `fit()` -- ali faz sentido, não tem zoom anterior
  pra preservar.

## 12. Reindexação automática a cada troca completa (2026-09-05)

Antes, a árvore (e o mapa do código de um jeito geral) só refletia
arquivo editado depois de rodar `.codegraph/reindex.sh` na mão. Pedido
do usuário: reindexar sozinho sempre que o agente termina uma troca,
sem precisar lembrar de rodar nada.

Gancho escolhido: dentro de `_log_exchange()` (`proxy.py`), logo depois
de `history.record_exchange()` gravar com sucesso -- ou seja, o mesmo
momento que já define "troca completa" pro histórico (seção 11.9/11.10:
prompt real + resposta final, sem tool-call pendente). Isso não é
coincidência -- se a resposta não tem mais tool-call pendente, qualquer
edição que o agente tenha feito nessa rodada (via `Write`/`Edit`/`Bash`,
ou qualquer tool de qualquer cliente MCP) já aconteceu; é exatamente o
momento certo pra reindexar, não antes.

`_auto_reindex(project_root, db_path)`:
- Roda `indexer.index_project(conn, project_root, verbose=False)` (o
  mesmo indexador do `codegraph setup`/`reindex.sh`) via
  `asyncio.to_thread` -- não bloqueia a resposta que já foi mandada de
  volta pro Kimi Code (a chamada em si é fire-and-forget,
  `asyncio.create_task`, igual o próprio `_log_exchange`).
- Uma `asyncio.Lock` por projeto (`_reindex_locks`, dict módulo-level)
  evita duas reindexações do mesmo `.db` rodando em paralelo -- se uma
  troca terminar (e tentar disparar reindex) antes da reindexação
  anterior acabar, essa nova chamada só verifica `lock.locked()` e
  desiste na hora, sem enfileirar nem esperar.
- Custo quando nada mudou: o indexador já pula arquivo por
  `content_hash` (seção 4), então rodar de novo sem edição nenhuma é só
  o custo de ler+hashear cada arquivo de novo -- não refaz chunking. Pra
  projeto pequeno/médio (o público-alvo aqui) isso é rápido; não foi
  pensado pra monorepo gigante rodando isso a cada mensagem.

Não é configurável (sem flag pra desligar) -- decisão deliberada de não
adicionar opção que ninguém pediu ainda; se algum caso de uso real
precisar desligar, isso vira um campo a mais no
`.kimi-code/codegraph-history.json` (mesmo arquivo do teto de tamanho,
seção 9.4) quando pedido.

Continua existindo `.codegraph/reindex.sh` pra reindexar na mão -- útil
pra quando o arquivo muda **fora** de uma troca de mensagem (criei um
arquivo novo direto no editor, por exemplo, sem passar pelo agente) ou
quando nenhum projeto está marcado como ativo ainda.

### 12.1 Rota `/health` de verdade (2026-09-05)

Achado revisando o alias `llama-qwen` do usuário (script de shell que
sobe proxy + `llama-server` com um comando só, ver `KIMI_CONFIG.md`): ele
checa `curl http://127.0.0.1:8081/health` pra saber se o proxy já subiu
antes de tentar subir de novo. Só que `/health` não existia como rota --
caía no catch-all (`passthrough`), que tenta repassar pro `llama-server`
upstream; antes do `llama-server` ter subido, isso vira `ConnectError` ->
resposta 500. O script "funcionava" mesmo assim só porque `curl` sem
`-f` considera qualquer resposta HTTP (mesmo erro) como sucesso -- não
testava saúde nenhuma de verdade, só "alguma coisa respondeu na porta".

Fix: rota `GET /health` dedicada, devolve `{"ok": true}` sem depender do
upstream estar de pé -- registrada antes do catch-all (Starlette casa
rota em ordem de declaração). Scripts externos que já checavam
`/health` continuam funcionando, só que agora testando saúde de verdade.

### 12.2 Terceira aba do dashboard: "Saúde" (2026-09-05)

Pedido do usuário depois do fix da seção 12.1: mostrar isso numa aba do
`/dashboard`, não só num endpoint pra script de terminal ler. Diferente
do `/health` (liveness simples, pra script externo), essa é uma visão
agregada pensada pra pessoa olhando a tela decidir "tá tudo bem ou
preciso mexer em algo":

Endpoint novo, `GET /api/health` (`api_health()`, `proxy.py`) --
agregando, sem inventar nada que não seja checável de verdade:
- **modelo (llama-server)**: faz `GET {UPSTREAM}/health` de verdade
  (timeout de 2s) -- `ok` se HTTP 200, `carregando` se responde com
  outro código (modelo ainda subindo), `fora do ar` se a conexão falhar.
- **projeto ativo**: `state.get_active_project()` -- mesmo estado que já
  decide onde o histórico é gravado (seção 9).
- **banco de dados**: existe? tamanho em disco; contagem de nós por
  tipo (`file`/`file_context`/`flow`/`flow_step`/`history`) -- mesma
  query simples usada em `metrics.py`, só que direto aqui pra não
  acoplar num helper que não serve pros outros campos.
- **reindexação automática**: olha o mesmo dict `_reindex_locks` da
  seção 12 -- `locked()` verdadeiro quer dizer que uma reindexação
  disparada por uma troca completa está rodando agora nesse instante
  (informativo, não é erro nem sucesso -- por isso sem cor verde/
  vermelha fixa nesse card).

Front-end: aba "Saúde" carrega sob demanda (só busca `/api/health` no
primeiro clique, mesmo padrão lazy-load da aba Árvore) e o botão
"🔄 Atualizar saúde" força de novo a qualquer momento. Cada card usa
verde/vermelho/cinza (`healthCard()`) conforme o campo é claramente bom,
claramente ruim, ou só informativo -- testado ao vivo com
`llama-server` + proxy no ar: os 6 cards vieram certos (modelo "ok",
projeto ativo, banco existente com contagem, reindexação "ociosa").

### 12.3 Bug real: uso de tool nunca era detectável, mesmo depois do AGENTS.md (2026-09-05)

Usuário perguntou direto: "o codegraph está realmente sendo efetivo?".
Investigando pra responder com dado, achei outro bug estrutural (terceiro
nessa mesma frente, depois das seções 10.7 e 10.8) que fazia
`used_codegraph_tools` nunca preencher, **mesmo com o `AGENTS.md` já
funcionando e o modelo genuinamente chamando a tool**.

Causa: `used_tools` é uma variável local de cada chamada a
`chat_completions()` -- e cada rodada do loop de tool-calling é uma
requisição HTTP separada (mesmo raciocínio da seção 11.9). A troca que
efetivamente vira nó de histórico é a resposta **final** (`has_tool_call
== False`, ver `_log_exchange`) -- e por definição, se ela não tem
tool-call nela mesma, o `used_tools` *daquela requisição específica*
está sempre vazio. A tool foi chamada de verdade numa rodada
**anterior**, cujo `used_tools` nunca chegava a lugar nenhum -- a
requisição que registra e a requisição que usou a tool nunca se falavam.

Fix: `_pending_tool_usage: dict[str, set[str]]`, acumula por **pergunta**
(`_last_user_message`, a mesma chave que já correlaciona rodadas do
mesmo loop, ver seção 11.9) em vez de por requisição -- toda vez que uma
requisição tem `used_tools` não-vazio, funde no dict acumulador; quando
`_log_exchange` grava a troca final, faz `used_tools |
_pending_tool_usage.pop(prompt, set())` antes de montar a metadata.

**Verificado ponta a ponta, com sessão real do Kimi Code** (não só teste
isolado de tool): pergunta "onde fica a lógica de calcular o vencedor de
uma mão de poker?" sem nenhuma dica de usar tool -- o modelo narrou "Let
me use the codegraph-mcp tools first, as the AGENTS.md guidance
suggests", tentou `search` (voltou vazio numa query, ajustou), usou
`get_file_tree` pra achar as funções de `engine.ts` com linhas, depois
`get_node` pra puxar `bestHand`/`cmpScore` -- nunca leu o arquivo
inteiro -- e respondeu certo (arquivo + função + linhas). O nó de
histórico gerado (id 223) veio com `used_codegraph_tools:
["get_file_tree", "get_node", "list_files", "list_flows", "search"]` --
primeira vez, em toda a vida do projeto, que isso preencheu de verdade.

### 12.4 Bug real: painel de conteúdo travava trocando entre nós já vistos (2026-09-05)

Usuário reparou (com print da árvore de verdade em uso): clicando em
vários nós de histórico em sequência, o painel de conteúdo às vezes
parava de atualizar -- ficava mostrando o texto de um nó anterior mesmo
depois de clicar em outro.

Causa: `onNodeClick()` tinha um único guard no topo,
`if (id === 'root' || expanded.has(id)) return;` -- `expanded` é o Set
que marca "já busquei os filhos desse nó" (existe pra não duplicar
filhos na árvore ao reclicar numa pasta/arquivo já aberto). Só que esse
mesmo guard também bloqueava o clique inteiro, painel incluído, em
QUALQUER nó já visitado antes -- reclicar num nó de histórico (ou
arquivo) que já tinha sido aberto antes numa mesma sessão de navegação
simplesmente não fazia nada, deixando o painel com o conteúdo do último
nó que tinha conseguido passar pelo guard.

Fix: `expanded` continua controlando só "não duplica filhos/relações na
árvore", mas o `fetch` + `renderSidePanel()` do nó de conteúdo agora
rodam **sempre**, incondicionalmente -- o `if (expanded.has(id)) return`
que evita reprocessar filhos foi movido pra **depois** de já ter
buscado e renderizado o painel, nunca antes. Mesmo ajuste aplicado nos
outros dois lugares que usavam esse padrão (`dir:` e o balde de
histórico) pra manter a intenção original neles (não há painel dinâmico
de conteúdo ali pra travar, só o carregamento de filhos que não deve
duplicar).

Testado ponta a ponta: clicar no nó #232, depois no #222, depois de
volta no #232 (o cenário exato reportado -- reclicar num nó já visitado)
-- painel mostrou o título de #232 nas duas vezes, #222 no meio,
nenhuma trava.

### 12.5 Raiz da árvore reorganizada em 3 buckets por categoria (2026-09-05)

Pedido do usuário depois de mapear `royal_poker_online` com 5 flows
novos: "categorizar os tipos... criar um nó por categoria
memória|index|flow". Antes, só `history` tinha bucket próprio
(`HISTORY_BUCKET_ID`, seção 11.1) -- pasta/arquivo de raiz e `flow`
ficavam soltos direto embaixo de `root`, misturados. Com vários flows
mapeados, a raiz virava uma fileira cada vez mais cheia e sem
organização por tipo.

Fix: dois buckets novos (`IDX_BUCKET_ID = 'idx:root'`, `FLOW_BUCKET_ID
= 'flow:root'`), mesmo padrão preguiçoso que `HISTORY_BUCKET_ID` já
usava -- `buildTree()` agora só cria **3 nós** embaixo de `root`
(índice, memória, flow), guardando `folderTree`/`pendingFlowNodes` em
variável de módulo pra usar depois. Cada bucket só busca/desenha os
filhos de verdade no primeiro clique (`onNodeClick`), reaproveitando a
mesma lógica que já existia (a árvore de pastas de `IDX_BUCKET_ID` é
literalmente o que `buildTree()` fazia direto na raiz antes -- só
moveu de lugar).

Raiz agora é sempre 3 nós, não importa quantos arquivo ou flow o
projeto tenha. Testado: `royal_poker_online` (27 arquivos, 5 flows) --
clicar em "📁 Índice" revela pastas/arquivos de raiz; clicar em
"🧭 Flows (5)" revela os 5 flows; `visNodes.getIds()` confirmou raiz com
só `['root','idx:root','hist:recent','flow:root']` antes de qualquer
clique.

### 12.6 Clicar de novo num nó aberto agora fecha (toggle) (2026-09-05)

Pedido direto do usuário: "quando clicarmos novamente no nó depois de
aberto ele tem que fechar". Antes, todo nó expansível (`dir:`, os 3
buckets da seção 12.5, e nó de conteúdo com filhos/relações) só ignorava
o segundo clique (`if (expanded.has(id)) return`, seção 12.4) -- abria e
nunca mais fazia nada, só crescendo.

Fix: `collapseNode(id)` -- remove as arestas que saem de `id`; pra cada
filho que ficar **sem nenhuma aresta entrando** depois disso (não é mais
referenciado por ninguém visível na árvore), recolhe ele também
primeiro (recursivo, pra não deixar neto órfão) e só então remove o nó.
Um filho que ainda tem outra aresta apontando pra ele (ex: alvo de uma
referência cruzada tracejada vindo de outro nó) fica no lugar -- só
perde a aresta redundante, não some da tela. Cada um dos 4 lugares que
tinham `if (expanded.has(id)) return` virou
`if (expanded.has(id)) { collapseNode(id); return; }`.

Importante: **collapse não remove o próprio nó `id`**, só o que está
abaixo dele -- clicar de novo numa pasta fechada reabre ela (refaz a
consulta/monta os filhos de novo), igual clicar nela pela primeira vez.
É o comportamento esperado de árvore recolhível (a pasta continua
visível fechada, não desaparece junto com o conteúdo).

Testado ponta a ponta, aninhado em 3 níveis (`idx:root` -> `dir:src` ->
`dir:src/poker`): abrir tudo, fechar só `dir:src` -- os filhos diretos
de `dir:src` **e** os netos que vieram de `dir:src/poker` (que só
existia por causa do primeiro) sumiram juntos; `idx:root` continuou
aberto (não foi tocado); reabrir `dir:src` trouxe tudo de volta
idêntico a antes.
