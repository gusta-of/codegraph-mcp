# Mapa do codegraph-mcp (explicado simples)

Este documento explica **o que cada parte do projeto faz e pra que
serve**, sem termos técnicos. Se quiser o "como funciona por dentro" de
verdade, isso está no `ARQUITETURA.md` -- aqui é só o "o que é e pra
quê".

## Em uma frase

O `codegraph-mcp` é uma **memória externa organizada** pro seu Agente
(Kimi Code) consultar. Em vez do Agente precisar ler o projeto inteiro
toda vez que você pergunta algo, ele consulta essa memória e pega só o
pedaço que interessa.

## As 5 peças, uma por uma

### 1. O Mapa do Código

**O que é**: uma varredura que passa por todos os arquivos do seu
projeto e organiza eles -- não guarda o arquivo inteiro de qualquer
jeito, separa em pedacinhos com nome (tipo "essa função aqui é a que
distribui as cartas", "essa é a que reseta o saldo").

**Pra que serve**: o Agente consegue pedir só o pedaço que precisa
("me mostra a função de distribuir cartas") em vez de precisar ler o
arquivo inteiro de 900 linhas pra achar aquele trecho.

**Como se conecta com o Agente**: quando você roda o comando de indexar
um projeto, esse mapa fica pronto e guardado. O Agente, durante a
conversa, pode "perguntar" pra essa memória -- é uma tool a mais que ele
tem disponível, do mesmo jeito que ele já sabe ler arquivo ou rodar
comando no terminal.

### 2. Os Fluxos (o "como as coisas funcionam aqui")

**O que é**: um resumo, escrito à mão por você, de como um pedaço de
lógica do seu sistema funciona -- tipo um passo a passo, ligado direto
nos trechos de código reais que fazem aquilo acontecer.

**Pra que serve**: quando o Agente precisa entender "como funciona o
login" ou "como funciona a distribuição de cartas", em vez de ele ter
que ler vários arquivos e juntar as peças sozinho (o que pode confundir
ele em tarefas grandes), ele pede esse resumo já pronto, com o código
certo já anexado.

**Como se conecta com o Agente**: só existe se você escrever. É opcional,
mas é a parte que mais ajuda em problemas grandes/complicados -- dar pro
Agente um mapa já pronto em vez dele ter que descobrir sozinho.

### 3. A Memória de Conversas

**O que é**: toda vez que você pergunta algo pro Agente e ele responde,
essa troca fica guardada automaticamente -- sem você precisar fazer
nada.

**Pra que serve**: o Agente consegue "lembrar" de conversas anteriores
daquele projeto. Se você já discutiu um assunto semana passada, ele
consegue puxar isso de volta em vez de você ter que explicar tudo de
novo do zero.

**Como se conecta com o Agente**: acontece sozinho, no fundo, sem
interromper a conversa. Tem um limite de espaço configurável -- quando
enche, as conversas mais antigas vão sendo apagadas primeiro (as mais
recentes ficam).

**Importante**: atualizar o Mapa do Código (peça 1) **nunca apaga** essa
memória de conversas -- são duas coisas guardadas separadas. Rodar o
"atalho de atualizar" (`.codegraph/reindex.sh`) mexe só no mapa, o
histórico de conversas continua do jeito que estava.

### 4. A Ponte (o que faz a memória de conversas funcionar)

**O que é**: um "meio de campo" que fica entre o Agente e o modelo de
linguagem -- toda mensagem que vai e volta passa por ele.

**Pra que serve**: é ele quem escuta a conversa passando e grava na
Memória de Conversas (peça 3). Sem ele, o Agente conversa com o modelo
direto, e nada fica guardado.

**Como se conecta com o Agente**: é uma peça que precisa estar ligada
(rodando) **antes** de você abrir o Agente -- se ela não estiver de pé,
o Agente simplesmente não consegue conversar com o modelo nenhum. É tipo
uma central telefônica: se ela cai, ninguém completa a ligação.

### 5. O Painel de Resultados

**O que é**: uma página que você abre no navegador e mostra números
reais de como tudo isso está sendo usado -- quantos arquivos foram
mapeados, quantas conversas já rolaram, se o Agente está de fato usando
essa memória ou não, etc.

**Pra que serve**: responder "isso realmente ajuda?" com dado de
verdade, não com "eu acho que sim".

**Como se conecta com o Agente**: é só um espelho -- não interfere em
nada, só mostra o que já está acontecendo.

## O caminho de uma pergunta, passo a passo

1. Você pergunta algo pro Agente (Kimi Code).
2. A pergunta passa pela **Ponte** (peça 4) a caminho do modelo.
3. O modelo decide: "isso eu já sei responder" ou "preciso olhar o
   código pra responder direito".
4. Se precisar olhar o código, ele tem duas opções: ler o arquivo
   inteiro (do jeito que ele já sabia fazer antes), ou perguntar pro
   **Mapa do Código** / pedir um **Fluxo** já pronto (peças 1 e 2) --
   mais rápido e mais barato.
5. A resposta volta, passa pela **Ponte** de novo, que aproveita e
   guarda a troca na **Memória de Conversas** (peça 3).
6. Você pode conferir se isso tudo tá realmente sendo usado no
   **Painel de Resultados** (peça 5).

## Os comandos que você realmente usa

| Quando você quer... | Roda isto |
|---|---|
| Preparar um projeto pra ser consultado pela primeira vez (ou depois de mudanças grandes) | `codegraph setup /caminho/do/projeto` |
| Ligar tudo (o modelo + a Ponte) antes de usar o Agente | `codegraph-up` |
| Ver se está funcionando de verdade | abrir `http://localhost:8081/dashboard` no navegador |

Tudo o resto (o Mapa do Código sendo consultado, a Memória de Conversas
sendo gravada) acontece sozinho, sem você precisar mexer em nada.

## O que essa memória NÃO faz sozinha

- Ela não obriga o Agente a usá-la -- ele só usa se achar que vale a
  pena, ou se você pedir explicitamente ("usa a busca do codegraph pra
  achar X"). Se ele nunca usar, o Painel de Resultados vai mostrar isso
  com números reais, não vai fingir que está ajudando.
- Ela não entende sozinha "como seu sistema funciona" em nível de
  negócio -- isso é o que os **Fluxos** (peça 2) resolvem, e alguém
  precisa escrever eles.
- Ela não some magicamente de espaço -- tem um teto configurável, e o
  que passa desse teto (só as conversas antigas, nunca o mapa do código)
  vai sendo apagado.
