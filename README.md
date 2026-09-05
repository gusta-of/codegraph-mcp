# codegraph-mcp

Grafo (árvore/DAG) dos meus projetos, exposto como servidor MCP pro Kimi
Code (ou qualquer cliente MCP) consultar sob demanda, em vez de carregar
o projeto inteiro na janela de contexto.

**Nunca usou isso antes?** [COMECE_AQUI.md](COMECE_AQUI.md) -- guia
único, passo a passo, do zero até usando de verdade. Isso aqui embaixo
é a referência rápida de uso pra quem já tem tudo instalado.

Outros documentos: instalação com todo detalhe de variável de ambiente
em [INSTALL.md](INSTALL.md); não quer saber de termo técnico, só
entender pra que cada parte serve? [MAP_CODEGRAPH.md](MAP_CODEGRAPH.md),
em linguagem simples; por que a conversa fica cara conforme cresce e o
que ajuda de verdade a segurar isso: [ECONOMIA_DE_TOKENS.md](ECONOMIA_DE_TOKENS.md);
como o projeto funciona por dentro (schema, pipeline de indexação, como
cada tool resolve suas queries): [ARQUITETURA.md](ARQUITETURA.md).

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

Ele também acrescenta um bloco em `.kimi-code/AGENTS.md` instruindo o
agente a preferir as tools do codegraph antes de ler arquivo inteiro ou
usar `Grep` no projeto todo (também idempotente -- não escreve de novo
se já existir, não apaga nada que eu tenha escrito nesse arquivo). Sem
isso, ter o servidor MCP conectado não é suficiente -- confirmado de
verdade: mesmo com as tools disponíveis, o agente simplesmente nunca
escolhia usá-las sozinho (0% de uso em dezenas de trocas reais) até esse
bloco existir (detalhe completo no `ARQUITETURA.md`, seção 10.8).

Ele também deixa um atalho pronto dentro do próprio projeto --
`.codegraph/reindex.sh` (ou `.bat` no Windows) -- já com o caminho certo
preenchido. Depois da primeira vez, pra atualizar o grafo (fui criando
arquivo novo, por exemplo), não preciso mais lembrar o comando completo:
só rodo esse arquivo.

**Também reindexa sozinho** a cada troca completa (o agente termina de
responder, edições já aplicadas) -- o proxy dispara isso em background,
sem travar a resposta, e pula reindexar de novo se ainda tiver uma
reindexação rodando. Continuo podendo rodar `reindex.sh` na mão pra
mudança feita fora de uma conversa (editei um arquivo direto, sem o
agente). Detalhe técnico: `ARQUITETURA.md`, seção 12.

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
| `search(query, limit)` | Busca full-text por palavras-chave (separadas por espaço, todas precisam aparecer) em qualquer nó -- inclui histórico. Nome de arquivo com ponto/hífen funciona normal (`audio.ts`) |
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

### Excluir arquivo do índice sem tocar nele (`.codegraphignore`)

Código legado/duplicado (uma versão antiga que não é mais importada em
lugar nenhum, por exemplo) só suja busca -- criei um `.codegraphignore`
opcional na raiz do projeto, mesma sintaxe do `.gitignore` (um padrão
por linha, `#` comentário, sem `/` casa só pelo nome em qualquer pasta):

```
# .codegraphignore
src/poker.html
*.min.js
```

Roda `codegraph setup` de novo depois de criar/editar esse arquivo --
ele remove do grafo (não do disco) qualquer arquivo já indexado que
passou a bater num padrão, e mostra quantos removeu. Detalhe técnico:
`ARQUITETURA.md`, seção 4.2.

## Dashboard de efetividade + árvore interativa

```
http://localhost:8081/dashboard
```

(o proxy já expõe isso sozinho, não precisa subir nada a mais). Tem duas
abas:

- **Visão geral**: com dado real do projeto ativo -- cobertura de
  indexação (% de contextos com chunking de verdade vs. fallback de
  linha), volume de conversas, % de trocas que usaram alguma tool do
  `codegraph-mcp`, tokens/s médio de geração, e uma estimativa de tokens
  poupados -- **estimativa mesmo, rotulada como tal no rodapé da
  página**, não finjo que é medição exata (explicado em detalhe no
  `ARQUITETURA.md`, seção 10). Os números de token/velocidade são reais,
  direto do `llama-server` (`usage`/`timings` da API) -- isso sim é
  medição, não estimativa.
- **Árvore**: navegação visual do grafo -- zoom, arrasto, clique num nó
  pra ver o conteúdo e expandir os filhos. Arquivos vêm agrupados por
  pasta (senão um projeto com centenas de arquivos vira uma parede
  ilegível de nós). Tem um nó "🕒 Histórico (memória)" na raiz também --
  as conversas reais (prompt+resposta) ficam navegáveis igual ao código,
  não só nos gráficos da Visão geral; cada uma aparece com um número
  (`#123`, a ordem real de entrada -- passar o mouse mostra a pergunta e
  resposta completas, não dava pra usar texto no próprio rótulo porque
  muitas conversas parecidas ficavam uma parede ilegível de texto
  cortado). Histórico vem **paginado** (mais recentes primeiro, 20 por
  vez) -- clicar em "… carregar mais antigas" busca a próxima leva, não
  trava a tela desenhando tudo de uma vez em projetos com muita conversa
  acumulada. Só entram na árvore/no dashboard trocas **completas**
  (prompt real do usuário + resposta final de verdade, sem tool-call
  pendente) -- lembretes internos do Kimi Code e passos intermediários
  de um loop de tool-calling não viram memória (detalhe no
  `ARQUITETURA.md`, seções 11.9-11.10). Linha sólida = hierarquia; linha
  tracejada laranja = referência cruzada (passo de fluxo → trecho que
  implementa). Cada clique só desloca a câmera até o que acabou de abrir
  -- nunca muda o zoom que eu já tinha ajustado na mão, mesmo escondido
  num canto da tela. Do lado
  esquerdo dos botões tem o tamanho atual do grafo (MB/GB) e quanto já
  foi preenchido do teto de histórico configurável -- dá pra editar esse
  teto ali mesmo, mas só pra cima: tentar um valor menor que o banco já
  ocupa hoje é recusado com uma explicação (apagar o `.codegraph/graph.db`
  manualmente e reindexar é o único jeito de encolher de verdade). Botão
  **🔄 Atualizar árvore** (reindexei/rodei prompts novos, quero ver sem
  recarregar a página) e **🖥️ Tela cheia** (abre numa janela nova,
  maior). Detalhe técnico completo -- inclusive os bugs reais achados
  testando de verdade (um deles derrubou o Chrome; outro gravava
  conversa incompleta -- lembrete de sistema como "pergunta" e resposta
  vazia -- na memória) e como validei sem conseguir print de tela
  (achado curioso sobre `requestAnimationFrame` em aba de automação em
  segundo plano) -- na `ARQUITETURA.md`, seção 11.
- **Saúde**: visão rápida de "tá tudo bem?" -- modelo (`llama-server`)
  respondendo ou não, projeto ativo, banco de dados existe/tamanho/
  contagem de nós, e se uma reindexação automática (próxima seção) está
  rodando agora. Carrega sob demanda (só busca ao clicar na aba), botão
  **🔄 Atualizar saúde** força de novo. Detalhe técnico:
  `ARQUITETURA.md`, seção 12.2.

⚠️ **Testei contra uso real e "tokens poupados" ficou em 0"** num
primeiro momento -- três causas empilhadas, todas corrigidas: (1) bug de
contagem que não reconhecia o prefixo `mcp__codegraph__` (seção 10.7);
(2) o modelo simplesmente nunca escolhia usar as tools sozinho, mesmo
com o MCP conectado certo -- ter a tool disponível não basta, precisa de
instrução explícita, por isso `codegraph setup` agora gera um
`.kimi-code/AGENTS.md` com essa orientação (seção "Registrar no Kimi
Code" acima, `ARQUITETURA.md` seção 10.8); (3) mesmo com o modelo já
chamando a tool de verdade, a métrica ainda não detectava -- cada rodada
do loop de tool-calling é uma requisição separada, e a que vira memória
nunca tem tool-call nela mesma (seção 12.3). Com as três corrigidas,
testei ponta a ponta com uma pergunta real sem nenhuma dica ("onde fica
a lógica de calcular o vencedor de uma mão de poker?") -- o modelo achou
sozinho via `search`/`get_file_tree`/`get_node`, nunca leu o arquivo
inteiro, e o dashboard registrou o uso de verdade pela primeira vez.

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
