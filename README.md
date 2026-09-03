# codegraph-mcp

Grafo (árvore/DAG) dos meus projetos, exposto como servidor MCP pro Kimi
Code (ou qualquer cliente MCP) consultar sob demanda, em vez de carregar
o projeto inteiro na janela de contexto.

Isso aqui é o guia rápido de uso. Como o projeto funciona por dentro
(schema, pipeline de indexação, como cada tool resolve suas queries) tá
documentado em detalhe no [ARQUITETURA.md](ARQUITETURA.md).

## O modelo de dados

Dois tipos de árvore, mais referências cruzadas entre elas:

- **`file` -> `file_context`**: cada arquivo indexado vira um nó `file`;
  cada arquivo é quebrado em pedaços (funções/classes pra `.py`, seções
  pra `.md`, blocos de linha pro resto) -- cada pedaço é um nó
  `file_context` filho.
- **`flow` -> `flow_step`**: fluxos de lógica do sistema, que eu defino à
  mão em YAML (ver `flows/example.yaml`). Cada `flow_step` pode referenciar
  um ou mais `file_context`/`file` que o implementam -- essas referências
  viram **arestas** (tabela `edges`, não é hierarquia) do tipo
  `implements_in`.

A ideia: quando o Kimi Code precisa entender um fluxo, ele chama
`get_flow("nome_do_fluxo")` e recebe os passos **já resolvidos com o
código real** de uma vez -- não precisa re-derivar aquilo lendo/buscando
nos arquivos, nem carregar o projeto inteiro pra ter certeza de que
entendeu certo. É isso que economiza tokens e acelera: reaproveito uma
leitura determinística já feita, em vez de reprocessar toda vez.

## Instalação

```bash
cd ~/workspace/codegraph-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Uso -- indexar um projeto

O grafo de cada projeto fica no seu próprio arquivo SQLite (`--db`, não
precisa estar dentro do `codegraph-mcp`):

```bash
.venv/bin/python -m codegraph.cli --db /caminho/do/projeto/.codegraph/graph.db \
  index /caminho/do/projeto
```

Rodar de novo é seguro e rápido -- arquivos sem mudança (mesmo hash de
conteúdo) são pulados, só o que mudou é re-indexado.

Carregar os fluxos de lógica (depois de indexar, pra `symbol` conseguir
casar com os `file_context` já existentes):

```bash
.venv/bin/python -m codegraph.cli --db /caminho/do/projeto/.codegraph/graph.db \
  load-flows /caminho/do/projeto/.codegraph/flows
```

Conferir o que entrou:

```bash
.venv/bin/python -m codegraph.cli --db /caminho/do/projeto/.codegraph/graph.db stats
```

## Escrevendo fluxos (`flows/*.yaml`)

Ver `flows/example.yaml` (indexa o próprio código deste repo, de
exemplo). Formato:

```yaml
name: "nome_do_fluxo"
description: "O que esse fluxo faz"
steps:
  - name: "passo_1"
    description: "O que esse passo faz"
    refs:
      - path: "src/auth.py"       # relativo à raiz que indexei
        symbol: "validate_login"  # nome de função/classe (Python) ou título de seção (Markdown)
                                   # sem `symbol`: liga no arquivo inteiro
```

## Registrar no Kimi Code

Servidor MCP local (stdio). Adiciono em `.kimi-code/mcp.json` **dentro
do projeto que eu quero consultar** (config a nível de projeto, só ativa
quando o Kimi Code abrir ali):

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

`cwd` tem que ser a pasta do `codegraph-mcp` (é de onde o `-m
codegraph.server` resolve o pacote); `CODEGRAPH_DB` é o `.db` daquele
projeto específico, gerado no passo de indexação acima -- **cada projeto
que eu quiser consultar precisa do seu próprio `mcp.json` apontando pro
seu próprio `.db`**.

Dentro do Kimi Code, `/mcp` mostra o status da conexão.

## Tools expostas

| Tool | O que faz |
|---|---|
| `list_files()` | Lista todos os arquivos indexados |
| `get_file_tree(path)` | Nó `file` + lista dos `file_context` filhos (nome/linhas, sem conteúdo) |
| `get_node(node_id)` | Nó completo (com conteúdo) + filhos + arestas relacionadas |
| `search(query, limit)` | Busca full-text (sintaxe FTS5) por nome/conteúdo em qualquer nó |
| `list_flows()` | Lista os fluxos de lógica carregados |
| `get_flow(name)` | Fluxo completo: passos em ordem, cada um já resolvido com o código que o implementa |

## Status

Primeira versão funcional -- testei indexando o próprio `codegraph-mcp`
(11 arquivos, 48 contextos) e carregando `flows/example.yaml` (3 passos,
3 referências resolvidas). Ainda não testei plugado de verdade no Kimi
Code contra um projeto real.

Próximos passos que pretendo fazer (ainda não implementados):
- Comando pra "re-sincronizar" fluxos quando os arquivos referenciados
  mudam de linha (hoje `refs` casa por `symbol` = nome, então sobrevive a
  mudança de linha; mas se eu renomear a função, a referência quebra
  silenciosamente até eu rodar `load-flows` de novo).
- Chunking mais esperto pra linguagens além de Python (hoje só Python e
  Markdown têm chunk "inteligente"; o resto cai em blocos de linha fixos).
- Extração assistida por LLM dos `flows/*.yaml` a partir do código, em
  vez de escrever à mão.
