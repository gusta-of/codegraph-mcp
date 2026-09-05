# Comece aqui -- do zero até usando

Este é o guia único, passo a passo, pra sair de "nunca vi isso" até
"usando de verdade no meu projeto". Não pula nada, não assume que você
já sabe nada. Se travar em algum passo, a seção 8 (no fim) lista os
erros mais comuns e o que fazer.

Os outros documentos servem pra depois: [MAP_CODEGRAPH.md](MAP_CODEGRAPH.md)
explica os conceitos em linguagem simples, [README.md](README.md) é a
referência rápida do dia a dia, [ARQUITETURA.md](ARQUITETURA.md) é o
detalhe técnico de como cada peça funciona por dentro, e
[ECONOMIA_DE_TOKENS.md](ECONOMIA_DE_TOKENS.md) explica como deixar tudo
mais eficiente depois que já estiver rodando.

## O que é isso, em uma frase

O `codegraph-mcp` organiza seu código num mapa (arquivo por arquivo,
função por função) e guarda o histórico das suas conversas com o agente
-- tudo isso fica disponível como uma "ferramenta" a mais que o agente
(Kimi Code) pode consultar, em vez de precisar ler o projeto inteiro
toda vez que você pergunta algo.

## O que você vai precisar antes de começar

| Item | Pra que serve |
|---|---|
| **Python 3.10 ou mais novo** | Roda o `codegraph-mcp` inteiro |
| **Um modelo de linguagem rodando local** (via [llama.cpp](https://github.com/ggml-org/llama.cpp)) + um arquivo `.gguf` do modelo | É o "cérebro" que o agente usa pra conversar |
| **Kimi Code CLI** ([github.com/MoonshotAI/kimi-code](https://github.com/MoonshotAI/kimi-code)) | É o agente em si -- a interface que você usa no terminal |

Se você ainda não tem o llama.cpp compilado nem um modelo baixado, faça
isso primeiro (as instruções de build variam por sistema/placa de
vídeo, estão no repositório do llama.cpp) -- confirme que funciona
rodando:

```bash
llama-server --version
```

Se aparecer a versão, pode seguir. Se der "comando não encontrado",
resolva isso antes de continuar (ou anote o caminho completo do
executável, você vai precisar dele no passo 3).

## Passo 1 -- baixar e instalar o codegraph-mcp

Abra um terminal e rode:

```bash
git clone https://github.com/SEU_USUARIO/codegraph-mcp.git
cd codegraph-mcp
```

Agora crie um ambiente Python isolado só pra esse projeto (evita
conflito com outras coisas Python na sua máquina) e instale:

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

O `pip install -e .` é o que faz os comandos `codegraph` e
`codegraph-up` existirem de verdade no seu terminal -- sem isso, nada
mais neste guia funciona. Confirme que deu certo:

```bash
codegraph --help
```

Se aparecer uma lista de comandos, funcionou. Se der "comando não
encontrado", o ambiente virtual (`.venv`) provavelmente não está ativado
-- rode o `source .venv/bin/activate` (ou `.venv\Scripts\Activate.ps1`
no Windows) de novo.

## Passo 2 -- instalar o Kimi Code

Siga as instruções em
[github.com/MoonshotAI/kimi-code](https://github.com/MoonshotAI/kimi-code).
Resumo (Linux/macOS):

```bash
curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash
```

## Passo 3 -- dizer pro Kimi Code conversar através do codegraph-mcp

Essa é a parte mais fácil de errar, então preste atenção: o Kimi Code
não vai falar direto com o `llama-server` -- ele vai falar com o
**proxy** do codegraph-mcp, que fica no meio do caminho, escuta a
conversa, e repassa pro modelo de verdade. É assim que o histórico de
conversas fica registrado automaticamente.

Edite (ou crie, se não existir) o arquivo `~/.kimi-code/config.toml`:

```toml
default_model = "local/meu-modelo"

[providers."local-llama"]
type = "openai"
base_url = "http://localhost:8081/v1"
api_key = "not-needed"

[models."local/meu-modelo"]
provider = "local-llama"
model = "meu-modelo-local"
max_context_size = 8192
capabilities = ["thinking", "tool_use"]
display_name = "Meu modelo (local)"
```

Repare: `base_url` termina em `:8081` -- **não** `:8080`. A porta 8080 é
o `llama-server` puro; 8081 é o proxy. Ajuste `max_context_size` pro
tamanho de contexto real do seu modelo, e `model`/`display_name` como
preferir (só cosmético).

## Passo 4 -- apontar pro seu modelo

O `codegraph-up` (próximo passo) precisa saber onde está o arquivo
`.gguf` do seu modelo. Defina isso como variável de ambiente:

**Linux / macOS** -- adicione no `~/.bashrc` (ou `~/.zshrc`):
```bash
export CODEGRAPH_MODEL_PATH="/caminho/completo/pro/seu-modelo.gguf"
```
Depois: `source ~/.bashrc` (ou abra um terminal novo pra valer).

**Windows (PowerShell)** -- adicione no seu perfil
(`notepad $PROFILE` pra editar):
```powershell
$env:CODEGRAPH_MODEL_PATH = "C:\caminho\completo\pro\seu-modelo.gguf"
```

Isso é o único obrigatório. Existem outras variáveis pra ajustar
detalhes (porta, flags do llama-server) -- só mexa nelas se precisar;
lista completa no [INSTALL.md](INSTALL.md), seção 3.

## Passo 5 -- subir tudo

Com o ambiente virtual ainda ativado (passo 1), rode:

```bash
codegraph-up
```

Isso sobe o `llama-server` (usando o modelo do passo 4) **e** o proxy
do codegraph-mcp, checando antes se algum dos dois já está rodando (não
duplica se você rodar de novo). Deixe esse terminal aberto -- é ele que
mantém tudo no ar. Se algo estiver faltando (esqueceu de configurar o
modelo, por exemplo), a mensagem de erro diz exatamente o que falta.

## Passo 6 -- indexar o seu primeiro projeto

Agora, o projeto que você quer que o agente consiga consultar. Abra
**outro** terminal (deixe o do passo 5 rodando) e rode:

```bash
codegraph setup /caminho/completo/do/seu/projeto
```

Isso faz tudo de uma vez:
- Mapeia o código (arquivo por arquivo, função por função).
- Registra o codegraph-mcp como ferramenta disponível pro Kimi Code
  usar **só dentro desse projeto** (grava em
  `.kimi-code/mcp.json` dentro dele).
- Escreve uma instrução (`.kimi-code/AGENTS.md`) orientando o agente a
  preferir essa ferramenta antes de ler arquivo inteiro.
- Cria um atalho (`.codegraph/reindex.sh` ou `.bat`) pra reindexar na
  mão depois, se precisar (raramente vai precisar -- ver seção 7).

Pode rodar `codegraph setup` de novo a qualquer momento nesse mesmo
projeto -- é seguro, não duplica nada, e não apaga o histórico de
conversas já guardado.

## Passo 7 -- confirmar que funcionou

1. Abra uma sessão **nova** do Kimi Code de dentro da pasta do seu
   projeto (uma sessão que já estava aberta antes do `codegraph setup`
   não pega a configuração sozinha):

   ```bash
   cd /caminho/completo/do/seu/projeto
   kimi
   ```

2. Dentro do Kimi Code, rode `/mcp` -- deve aparecer `codegraph`
   conectado.

3. Abra `http://localhost:8081/dashboard` no navegador. Clique na aba
   **Saúde** -- os cards devem aparecer verdes: modelo respondendo,
   projeto ativo, banco de dados existindo. Se algo aparecer vermelho,
   essa aba já te diz qual peça está faltando.

Se os três passos acima deram certo, está tudo funcionando.

## Agora que está rodando -- como é o dia a dia

Você não precisa fazer mais nada de especial. Use o Kimi Code
normalmente -- pergunte, peça pra editar código, o que for. Por baixo
dos panos:

- O agente **decide sozinho** quando vale a pena consultar o mapa do
  código em vez de ler o arquivo inteiro (a instrução do passo 6 ajuda
  nessa decisão, mas quem escolhe é ele).
- Toda troca completa de mensagem (pergunta real + resposta final) fica
  guardada automaticamente como memória, e o grafo se **reindexação
  sozinho** depois de cada edição -- você não precisa lembrar de rodar
  nada.
- De vez em quando, abra `http://localhost:8081/dashboard` pra ver:
  aba **Visão geral** (números reais de uso -- quantos arquivos
  mapeados, % de trocas que usaram a ferramenta, etc), aba **Árvore**
  (navegar visualmente pelo grafo, clicar num nó pra ver o conteúdo), e
  aba **Saúde** (checagem rápida de "tá tudo bem?").

Quando quiser ir além do básico -- escrever "fluxos" (resumos de como
uma parte do seu sistema funciona, que economizam MUITAS idas-e-voltas
do agente) -- veja a seção "Escrevendo fluxos" do [README.md](README.md)
e o [ECONOMIA_DE_TOKENS.md](ECONOMIA_DE_TOKENS.md) pra entender o
porquê disso valer a pena.

## 8. Erros mais comuns (o que quebra e por quê)

| O que você fez (ou esqueceu) | O que acontece | Como resolver |
|---|---|---|
| Pulou o `pip install -e .` | Comando `codegraph`/`codegraph-up` não existe | Ative o `.venv` e rode `pip install -e .` de novo |
| Não definiu `CODEGRAPH_MODEL_PATH` | `codegraph-up` recusa subir, com mensagem clara | Defina a variável (passo 4) e abra um terminal novo |
| `base_url` do Kimi Code ainda em `:8080` | Conversa funciona, mas histórico nunca é gravado | Troque pra `:8081` no `config.toml` (passo 3) |
| `base_url` em `:8081` mas esqueceu de rodar `codegraph-up` | Kimi Code não consegue falar com o modelo, nenhuma mensagem funciona | Rode `codegraph-up` de novo |
| Rodou `codegraph setup` com uma sessão do Kimi Code **já aberta** | `/mcp` não mostra a conexão, as tools não aparecem | Feche e abra uma sessão nova dentro do projeto |
| Trocou de modelo `.gguf` sem reiniciar | `llama-server` continua com o modelo antigo | Pare o processo e rode `codegraph-up` de novo |

Se o problema não estiver na lista acima, a aba **Saúde** do dashboard
(`http://localhost:8081/dashboard`) costuma apontar exatamente qual
peça está fora do ar.
