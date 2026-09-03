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
usada; é sinal real de adoção, não estimativa nem bug de instrumentação.
