# Instalação (Windows / Linux / macOS)

Guia completo pra rodar o `codegraph-mcp` do zero em qualquer sistema.
Nada aqui depende de caminho de máquina específica -- tudo é configurado
por variável de ambiente (seção 3). Se você só quer entender como o
projeto funciona por dentro, ver [ARQUITETURA.md](ARQUITETURA.md); se já
tem tudo instalado, ver [README.md](README.md) pro dia a dia.

## 0. Pré-requisitos

| O quê | Por quê | Onde conseguir |
|---|---|---|
| **Python 3.10+** | Roda o `codegraph-mcp` inteiro (indexação, servidor MCP, proxy) | [python.org](https://www.python.org/downloads/) |
| **Kimi Code CLI** | O agente que vai usar o `codegraph-mcp` como ferramenta | [github.com/MoonshotAI/kimi-code](https://github.com/MoonshotAI/kimi-code) |
| **llama.cpp** (`llama-server`) | Serve o modelo local que o Kimi Code conversa | [github.com/ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) -- instruções de build por SO e por GPU (Vulkan/CUDA/ROCm/Metal/CPU) estão lá, não duplicadas aqui |
| **Um modelo `.gguf`** | O modelo em si | Ex: [Hugging Face](https://huggingface.co/models?library=gguf) |

Depois de compilar o llama.cpp, confirme que `llama-server` funciona
antes de continuar:

```bash
llama-server --version
```

Se der "comando não encontrado", ou o binário não está no `PATH`, ou
você vai precisar apontar o caminho completo dele mais adiante (seção 3,
`CODEGRAPH_LLAMA_SERVER_BIN`).

## 1. Baixar e instalar o codegraph-mcp

```bash
git clone https://github.com/SEU_USUARIO/codegraph-mcp.git
cd codegraph-mcp
```

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

`pip install -e .` instala o pacote de verdade (não só arquivos soltos)
-- é isso que faz os comandos `codegraph` e `codegraph-up` funcionarem
de qualquer pasta, em qualquer terminal, sem precisar estar dentro da
pasta do projeto.

Validar:

```bash
codegraph --help
```

## 2. Instalar e configurar o Kimi Code

Instruções completas em
[github.com/MoonshotAI/kimi-code](https://github.com/MoonshotAI/kimi-code).
Resumo (Linux/macOS):

```bash
curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash
```

Depois de instalado, edite (ou crie) `~/.kimi-code/config.toml`:

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

⚠️ **`base_url` aponta pra porta `8081` (o proxy do `codegraph-mcp`), não
pra `8080` (o `llama-server` direto)**. Isso é proposital -- é o proxy
que captura o histórico de prompts (ver `ARQUITETURA.md` seção 9). Sem o
proxy rodando, o Kimi Code não fala com o modelo nenhum -- ver seção 5
("o que quebra se você pular um passo") no fim deste documento.

`max_context_size` e `model`/`display_name` ajuste pro seu modelo real
-- os valores acima são só exemplo.

## 3. Variáveis de ambiente

Essa é a estratégia inteira de configuração do projeto: **nenhum caminho
de máquina fica hardcoded em lugar nenhum** -- tudo vem de variável de
ambiente, com defaults sensatos onde dá.

| Variável | Obrigatória? | Default | Pra que serve |
|---|---|---|---|
| `CODEGRAPH_MODEL_PATH` | **sim** | -- | Caminho do seu arquivo `.gguf`. Sem isso, `codegraph-up` recusa subir o servidor. |
| `CODEGRAPH_LLAMA_SERVER_BIN` | não | `llama-server` | Nome/caminho do executável do llama.cpp. Só precisa mudar se ele não estiver no `PATH`. |
| `CODEGRAPH_LLAMA_PORT` | não | `8080` | Porta onde o `llama-server` escuta. |
| `CODEGRAPH_LLAMA_ARGS` | não | `-ngl 999 -fa on -c 8192 --no-prefill-assistant --reasoning-budget 16000` | Flags extras do `llama-server` -- ajuste `-c` (contexto) e `-ngl` pro seu hardware. |
| `CODEGRAPH_PROXY_PORT` | não | `8081` | Porta do proxy de histórico (é a que o `config.toml` do Kimi Code deve apontar). |
| `CODEGRAPH_DB` | não* | `graph.db` | Só usada quando você roda `codegraph.server`/`codegraph.proxy` manualmente fora do fluxo normal -- o `mcp.json` gerado por `codegraph setup` já define isso sozinho por projeto. |

### Como definir (permanente)

**Linux / macOS** -- adicione no `~/.bashrc` (ou `~/.zshrc`):

```bash
export CODEGRAPH_MODEL_PATH="/caminho/completo/pro/seu-modelo.gguf"
# as outras só se precisar mudar do default:
# export CODEGRAPH_LLAMA_ARGS="-ngl 999 -fa on -c 16000"
```

Depois: `source ~/.bashrc` (ou abra um terminal novo).

**Windows (PowerShell)** -- adicione no seu perfil do PowerShell
(`notepad $PROFILE` pra editar):

```powershell
$env:CODEGRAPH_MODEL_PATH = "C:\caminho\completo\pro\seu-modelo.gguf"
```

Ou, pra ficar valendo em qualquer terminal (não só PowerShell), defina
permanente via **Configurações do Windows -> Variáveis de Ambiente** (ou
`setx CODEGRAPH_MODEL_PATH "C:\caminho\..."` -- exige abrir um terminal
novo depois pra pegar o valor).

## 4. Subir tudo e indexar um projeto

```bash
codegraph-up
```

Isso sobe o `llama-server` (usando `CODEGRAPH_MODEL_PATH` e as outras
variáveis) e o proxy de histórico, checando antes se algum dos dois já
está rodando (não duplica). Se faltou configurar `CODEGRAPH_MODEL_PATH`,
ele avisa exatamente isso e para -- não tenta adivinhar.

Depois, pra cada projeto que você quiser que o Kimi Code consiga
consultar:

```bash
codegraph setup /caminho/do/seu/projeto
```

Isso indexa o código, registra o servidor MCP naquele projeto
(`.kimi-code/mcp.json`), cria a config de limite de histórico
(`.kimi-code/codegraph-history.json`, default 15 GB) e marca esse
projeto como o que recebe o histórico de prompts agora.

Abra uma sessão **nova** do Kimi Code dentro desse projeto (sessão já
aberta antes não pega a configuração sozinha) e rode `/mcp` pra
confirmar que conectou.

## 5. O que quebra se você pular um passo

- **Pulou `pip install -e .`** (só clonou e tentou rodar direto): os
  comandos `codegraph`/`codegraph-up` não existem, e `python -m
  codegraph...` só funciona de dentro da pasta do projeto.
- **Não configurou `CODEGRAPH_MODEL_PATH`**: `codegraph-up` recusa subir
  o `llama-server` (mensagem de erro clara, não trava).
- **`base_url` do Kimi Code ainda aponta pra `:8080` em vez de `:8081`**:
  o histórico de prompts nunca é gravado (o Kimi Code fala direto com o
  `llama-server`, passando por cima do proxy).
- **`base_url` aponta pra `:8081` mas o proxy não está rodando**
  (esqueceu `codegraph-up`, ou ele caiu): **o Kimi Code não fala com o
  modelo nenhum** -- toda mensagem falha. É o ponto único de falha desse
  design; se acontecer, rode `codegraph-up` de novo.
- **Rodou `codegraph setup` mas não abriu uma sessão nova do Kimi
  Code**: a sessão antiga não vê o `mcp.json`/config novos -- as tools
  do `codegraph-mcp` não aparecem, `/mcp` não mostra conexão.
- **Editou `.gguf`/mudou de modelo sem reiniciar**: `llama-server`
  precisa subir de novo (matar o processo, `codegraph-up` de novo) --
  ele não recarrega modelo sozinho.

