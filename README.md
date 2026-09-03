# codegraph-mcp

Grafo (árvore/DAG) dos meus projetos, exposto como servidor MCP pro Kimi
Code (ou qualquer cliente MCP) consultar sob demanda, em vez de carregar
o projeto inteiro na janela de contexto.

Instalação do zero (Windows/macOS/Linux, todas as variáveis de ambiente):
[INSTALL.md](INSTALL.md). Isso aqui é o guia rápido de uso pra quem já
tem tudo instalado. Não quer saber de termo técnico, só entender pra que
cada parte serve? [MAP_CODEGRAPH.md](MAP_CODEGRAPH.md), em linguagem
simples. Como o projeto funciona por dentro (schema, pipeline de
indexação, como cada tool resolve suas queries) tá documentado em
detalhe no [ARQUITETURA.md](ARQUITETURA.md).

## O modelo de dados

Dois tipos de árvore, mais referências cruzadas entre elas:

- **`file` -> `file_context`**: cada arquivo indexado vira um nó `file`;
  cada arquivo é quebrado em pedaços (funções/classes/structs/etc pra
  qualquer linguagem com gramática tree-sitter -- Python, JS/TS, Go,
  Rust, Java, e dezenas de outras, sem eu precisar escrever código por
  linguagem; seções pra `.md`; blocos de linha pro resto) -- cada
  pedaço é um nó `file_context` filho.
- **`flow` -> `flow_step`**: fluxos de lógica do sistema, que eu defino à
  mão em YAML (ver `flows/example.yaml`). Cada `flow_step` pode referenciar
  um ou mais `file_context`/`file` que o implementam -- essas referências
  viram **arestas** (tabela `edges`, não é hierarquia) do tipo
  `implements_in`.
- **`history`**: cada troca prompt+resposta real, capturada sozinha pelo
  proxy (ver seção "Histórico de prompts" abaixo). Lista plana, sem
  filhos -- e o único tipo que é apagado automaticamente quando o banco
  passa de um teto de tamanho configurável (indexação nunca é apagada).

A ideia: quando o Kimi Code precisa entender um fluxo, ele chama
`get_flow("nome_do_fluxo")` e recebe os passos **já resolvidos com o
código real** de uma vez -- não precisa re-derivar aquilo lendo/buscando
nos arquivos, nem carregar o projeto inteiro pra ter certeza de que
entendeu certo. É isso que economiza tokens e acelera: reaproveito uma
leitura determinística já feita, em vez de reprocessar toda vez.

## Instalação

```bash
git clone <este-repo> && cd codegraph-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

`-e .` instala de verdade (não é `-r requirements.txt`) -- é o que faz
os comandos `codegraph`/`codegraph-up` funcionarem de qualquer pasta,
sem precisar `cd` pro `codegraph-mcp` toda vez. Detalhe completo (e o
porquê) no `ARQUITETURA.md`, seção 2.1. Passo a passo multiplataforma
completo, com todas as variáveis de ambiente: [INSTALL.md](INSTALL.md).

## Uso -- subir tudo + integrar um projeto

```bash
codegraph-up                          # sobe llama-server + proxy (variáveis de ambiente, ver INSTALL.md)
codegraph setup /caminho/do/projeto   # indexa + registra + configura histórico, um comando
```

(`./setup-project.sh /caminho/do/projeto` continua funcionando, é só um
atalho Bash pro `codegraph setup` -- útil se eu preferir sem o `.venv`
ativado.)

`codegraph setup` indexa o projeto, carrega `.codegraph/flows/*.yaml` se
já existirem, e escreve/atualiza `.kimi-code/mcp.json` dentro do projeto
(sem apagar outras entradas MCP que já estejam lá) -- de uma vez. Roda de
novo sempre que o projeto mudar -- é idempotente (arquivo sem mudança de
hash é pulado, `mcp.json` é atualizado no lugar, não duplicado).

Ele também deixa um atalho pronto dentro do próprio projeto --
`.codegraph/reindex.sh` (ou `.bat` no Windows) -- já com o caminho certo
preenchido. Depois da primeira vez, pra atualizar o grafo (fui criando
arquivo novo, por exemplo), não preciso mais lembrar o comando completo:
só rodo esse arquivo.

⚠️ **Reindexar só atualiza o mapa do código -- o histórico de conversas
fica intacto**, sempre (confirmado no código, `ARQUITETURA.md` seção
2.5). Só sai por expurgo de tamanho (seção "Histórico de prompts" acima)
ou se eu apagar o `.db` inteiro na mão.

Depois: **abrir uma sessão nova do Kimi Code** dentro do projeto (sessão
já aberta antes não pega o `mcp.json` sozinha) e rodar `/mcp` pra
confirmar a conexão.

O grafo de cada projeto fica no seu próprio arquivo SQLite
(`<projeto>/.codegraph/graph.db>`), não dentro do `codegraph-mcp`.

### Passo a passo manual (o que o script acima faz por baixo)

```bash
# 1. indexar
.venv/bin/python -m codegraph.cli --db /caminho/do/projeto/.codegraph/graph.db \
  index /caminho/do/projeto

# 2. (opcional) carregar fluxos, depois de indexar -- pra `symbol` casar
#    com os `file_context` já existentes
.venv/bin/python -m codegraph.cli --db /caminho/do/projeto/.codegraph/graph.db \
  load-flows /caminho/do/projeto/.codegraph/flows

# conferir o que entrou
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

O `setup-project.sh` já faz isso sozinho (seção acima). Só documentando
o que ele escreve, caso eu precise editar à mão ou entender o formato:
servidor MCP local (stdio), registrado em `.kimi-code/mcp.json` **dentro
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

## Histórico de prompts (memória)

Cada prompt real que mando pro modelo e a resposta que ele devolve viram
um nó `history` no grafo, automaticamente -- sem eu ter que pedir. Assim
o modelo consegue puxar conversas anteriores do mesmo projeto (via
`search`/`list_history`) em vez de eu ter que reexplicar contexto toda
sessão nova.

### Como funciona (visão rápida -- detalhe técnico completo no `ARQUITETURA.md`)

Um **proxy** (`codegraph/proxy.py`, porta 8081) fica entre o Kimi Code e
o `llama-server`: repassa tudo transparente e grava prompt+resposta por
baixo. Isso exige duas mudanças de configuração, feitas **uma vez**:

1. **`~/.kimi-code/config.toml`** -- troquei o `base_url` de
   `http://localhost:8080/v1` pra `http://localhost:8081/v1` (o proxy,
   não mais o `llama-server` direto). Já feito nesta máquina.
2. **Subir o proxy** -- não precisa fazer isso separado: o próprio
   `llama-qwen` (o alias que já uso pra subir o servidor) checa se o
   proxy tá no ar e sobe ele sozinho, em segundo plano, se não estiver.
   **Um comando só continua fazendo tudo** -- não virou uma ação a mais
   pra lembrar. `codegraph-proxy` continua existindo como comando manual
   (pra debug, ou subir só ele sem o modelo).

   ⚠️ **Sem o proxy rodando, o Kimi Code não fala com o modelo nenhum** --
   o `base_url` aponta só pro proxy, não tem mais fallback direto pro
   `llama-server`. Se algo quebrar e eu quiser voltar rápido: trocar o
   `base_url` de volta pra `:8080` no `config.toml`.

3. `setup-project.sh` já cuida do resto sozinho: cria
   `.kimi-code/codegraph-history.json` (teto de armazenamento, default
   15360 MB = 15 GiB) e marca o projeto como "ativo" -- é pra esse
   projeto que o proxy grava enquanto eu não rodar o script de novo em
   outro.

### Teto de armazenamento

`.kimi-code/codegraph-history.json`, ao lado do `mcp.json`:

```json
{ "max_history_mb": 15360 }
```

Só conta o espaço dos nós `history` -- indexação de projeto nunca é
apagada, mesmo que o banco inteiro passe do teto por causa dela sozinha.
Quando o histórico passa do limite, as entradas mais antigas são
apagadas primeiro (assumo que são as menos valiosas). Testado de
verdade: inseri ~5 MB de histórico, forcei um teto apertado, e o arquivo
`.db` encolheu de volta depois do expurgo.

## Tools expostas

| Tool | O que faz |
|---|---|
| `list_files()` | Lista todos os arquivos indexados |
| `get_file_tree(path)` | Nó `file` + lista dos `file_context` filhos (nome/linhas, sem conteúdo) |
| `get_node(node_id)` | Nó completo (com conteúdo) + filhos + arestas relacionadas |
| `search(query, limit)` | Busca full-text (sintaxe FTS5) por nome/conteúdo em qualquer nó -- inclui histórico |
| `list_flows()` | Lista os fluxos de lógica carregados |
| `get_flow(name)` | Fluxo completo: passos em ordem, cada um já resolvido com o código que o implementa |
| `list_history(limit)` | Entradas de prompt+resposta mais recentes deste projeto, mais nova primeiro |

## Chunking por linguagem (tree-sitter)

Motivo real de existir: um bug real (`poker.html`, 917 linhas, 4
problemas interligados) travou uma sessão do Kimi Code num loop de
raciocínio, e o `codegraph-mcp` não conseguia ajudar porque `.html`
só tinha chunking de blocos de linha fixos -- não dava pra pedir "só a
função de distribuir cartas".

Resolvido com [tree-sitter](https://tree-sitter.github.io/) -- **uma
função de chunking só, sem código por linguagem** (nada de
`if lang == "javascript": ...`). Funciona hoje pra Python, JS/TS, Go,
Rust, Java, Ruby, C/C++, SQL, Bash e dezenas de outras (o pacote
`tree-sitter-language-pack` resolve `extensão -> linguagem` sozinho) --
se eu trocar de stack amanhã, não preciso mexer nesse código, só
funciona (com uma ressalva: reindexar do zero, ver abaixo).

HTML é o único caso com tratamento próprio, e não por ser "JS": a
gramática HTML não entende o conteúdo de `<script>` como código, só
como texto bruto -- extraio esse texto e mando pro mesmo chunker
genérico com `lang="javascript"`. Detalhe técnico completo (o algoritmo
do "acha o nome", o filtro que tira ruído de `import`/`echo`) no
`ARQUITETURA.md` seção 4.1.

⚠️ **`content_hash` só rastreia mudança de conteúdo, não de algoritmo de
chunking** -- trocar a lógica de chunking (como fiz aqui) não
re-processa arquivos que já estavam indexados e não mudaram. Rodei
`rm .codegraph/graph.db` + `setup-project.sh` de novo pra ver o efeito
em tudo -- é o jeito de sempre que eu mudar como o chunking funciona.

## Dashboard de efetividade

```
http://localhost:8081/dashboard
```

(o proxy já expõe isso sozinho, não precisa subir nada a mais). Mostra,
com dado real do projeto ativo: cobertura de indexação (% de contextos
com chunking de verdade vs. fallback de linha), volume de conversas,
% de trocas que usaram alguma tool do `codegraph-mcp`, tokens/s médio de
geração, e uma estimativa de tokens poupados -- **estimativa mesmo,
rotulada como tal no rodapé da página**, não finjo que é medição exata
(explicado em detalhe no `ARQUITETURA.md`, seção 10). Os números de
token/velocidade são reais, direto do `llama-server` (`usage`/`timings`
da API) -- isso sim é medição, não estimativa.

⚠️ **Testei contra uso real e "tokens poupados" ficou em 0** -- não é
bug: significa que o modelo usou 0% das minhas tools nas trocas
registradas (ele tem tool de leitura de arquivo nativa do Kimi Code e
usa ela por padrão; sem fluxo mapeado, também não tem muito motivo pra
preferir `get_flow`). Pra ver um número real: pedir explícito no prompt
pra usar `search`/`get_flow`, ou mapear um fluxo de verdade primeiro.
Causa completa e o porquê na `ARQUITETURA.md`, seção 10.6.

`?project=/caminho/de/outro/projeto` na URL pra ver o dashboard de um
projeto que não é o ativo no momento.

## Status

Testado de ponta a ponta: indexação com chunking genérico (regressão
confirmada em Python, mais SQL/Bash/HTML+JS testados), fluxos
(`flows/example.yaml`, 3 passos resolvidos, sobreviveu à troca de
chunker), histórico de prompts (agora com sessão real do Kimi Code
passando pelo proxy, não só `curl` -- confirmado processo filho
`codegraph.server` rodando de dentro da sessão real), instalação como
pacote de verdade (`pip install -e .`, comandos funcionando de qualquer
pasta), e o dashboard de efetividade (`/dashboard`), com dado real de um
projeto de 412 arquivos.

Próximos passos que pretendo fazer (ainda não implementados):
- Comando pra "re-sincronizar" fluxos quando os arquivos referenciados
  mudam de linha (hoje `refs` casa por `symbol` = nome, então sobrevive a
  mudança de linha; mas se eu renomear a função, a referência quebra
  silenciosamente até eu rodar `load-flows` de novo).
- Extração assistida por LLM dos `flows/*.yaml` a partir do código, em
  vez de escrever à mão -- é o próximo passo real pro `poker.html`: agora
  que o chunking de JS funciona, mapear os 4 bugs como flows separados.
- Comando pra trocar de "projeto ativo" (histórico) sem precisar
  re-rodar `setup-project.sh` inteiro (hoje dá pra fazer chamando
  `state.set_active_project()` direto em Python, mas não tem CLI pra isso
  ainda).
- Re-chunking automático quando o algoritmo de chunking muda (hoje é
  manual: apagar o `.db` e reindexar do zero).
- `save_flow`: tool MCP pra o próprio agente gravar um flow durante a
  sessão (em vez de exigir YAML escrito à mão antes) -- discutido, não
  implementado ainda.
- Medição exata de "tokens poupados" (hoje é estimativa por tamanho
  médio, ver `ARQUITETURA.md` seção 10.2) -- rastrear de verdade exigiria
  acompanhar o conteúdo de cada resultado de tool call através dos
  turnos da conversa.
