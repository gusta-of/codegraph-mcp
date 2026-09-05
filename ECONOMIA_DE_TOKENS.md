# Economia de tokens (explicado simples)

Este documento explica **por que a conversa fica cara conforme cresce, o
que realmente ajuda a segurar isso, e o que já foi testado com número
real** -- pra quem quer entender a estratégia sem ler o `ARQUITETURA.md`
inteiro.

## A confusão mais comum: "cada mensagem fica mais cara" não é bem assim

O `llama-server` guarda em cache o que já processou -- então reenviar a
conversa inteira a cada rodada (é assim que a API funciona, não tem
"memória" do lado do servidor) **não é caro de recomputar**. Conferi de
verdade: em praticamente toda troca registrada, **97% a 100%** do prompt
já veio do cache.

O problema é outro: a conversa tem um **teto de tamanho** (a "janela de
contexto", hoje 118 mil tokens nesta máquina). Cada pergunta que exige
várias rodadas de exploração (o agente tenta uma busca, não acha,
ajusta, tenta de novo, olha um arquivo, olha outro...) empurra o total
mais perto desse teto -- e vimos isso acontecer de verdade nesta sessão:
o teto estourou e a conversa precisou ser **compactada** (resumida à
força, perdendo detalhe).

**A conclusão prática**: o que realmente economiza não é "deixar cada
mensagem mais barata" (isso já está resolvido, pelo cache). É **reduzir
quantas idas-e-voltas o agente precisa até responder** -- menos rodadas
= a conversa cresce mais devagar = demora mais pra precisar compactar =
menos texto de raciocínio gerado do zero (isso sim não tem cache, é
sempre computado na hora).

## A estratégia, do que rende mais pro que rende menos

### 1. Escrever `flows/*.yaml` pros assuntos mais perguntados -- ✅ testado, funciona

Um "fluxo" é um resumo que **você** escreve uma vez, ligando direto nos
pedaços de código reais. Em vez do agente ter que descobrir sozinho
"onde fica isso", ele pede o fluxo pronto com **uma chamada só**.

Testei com um exemplo real (a lógica de decidir o vencedor de uma mão de
poker, no projeto `royal_poker_online`): mesma pergunta ampla, sem
nenhuma dica --

| | Sem fluxo | Com fluxo |
|---|---|---|
| Tokens de prompt usados | 69.926 | 31.787 |
| Qualidade da resposta | mais curta | mais completa (com exemplo numérico e linha exata de cada passo) |
| Como o agente explorou | busca → ajusta → tenta de novo → olha 2 arquivos separados | 1 chamada trouxe o núcleo pronto; só precisou explorar a parte que o fluxo avisou que não tinha resolvido sozinha |

Menos idas-e-voltas **e** resposta melhor ao mesmo tempo -- não é
sacrifício de qualidade por economia, os dois andam juntos aqui, porque
o gasto real que evitamos é o agente tateando às cegas.

**O que fazer**: escrever um `.yaml` por assunto que você já sabe que
vai perguntar de novo (ver `flows/example.yaml` pro formato). Quem
escreve é você -- é conhecimento de domínio, não tem como eu adivinhar
sozinho.

### 2. Busca (`search`) devolver um pedacinho do conteúdo junto -- ainda não feito

Hoje `search` devolve só id/nome/caminho -- quase sempre precisa de uma
segunda chamada (`get_node`) pra ver o conteúdo de verdade. Se a busca já
trouxesse um trechinho junto, cortaria essa segunda rodada na maioria
dos casos.

### 3. Orientar exploração mais decisiva no `AGENTS.md` -- ainda não feito

Hoje o agente às vezes tenta 2-3 variações de busca antes de desistir e
ir pro caminho certo. Uma instrução tipo "se a busca não achar nada de
jeito, vai direto pro arquivo mais provável em vez de tentar reformular
várias vezes" devia cortar essa tentativa-e-erro.

### 4. Depois que a conversa é compactada, apontar pro histórico -- ainda não feito

Quando a conversa estoura o teto e é resumida à força, o agente perde
detalhe do que já foi decidido. Ele já tem uma ferramenta pra recuperar
isso (`list_history`/`search` no histórico de conversas antigas) -- só
falta uma instrução lembrando ele de usar isso em vez de assumir que o
resumo compactado tem tudo.

### 5. Medir de verdade em vez de estimar -- ainda não feito

O número "tokens poupados" do dashboard hoje é uma conta aproximada
(tamanho médio de arquivo vs tamanho médio do trecho devolvido). Dá pra
comparar de verdade: pegar tarefas parecidas que usaram tool com sucesso
vs que não usaram, e olhar o `prompt_tokens` acumulado até a resposta
final de cada uma -- foi assim que a tabela do item 1 foi montada, só
falta automatizar isso no dashboard em vez de fazer na mão.

### 6. Tirar código morto/duplicado do índice -- ✅ testado, funciona

Mapeando o `royal_poker_online` de propósito (pra escrever os flows do
item 1), achei `src/poker.html` -- versão antiga do jogo, de antes da
migração pra React, não importada em lugar nenhum -- com **48 contextos
indexados**, mais que qualquer arquivo em uso de verdade no projeto.
Toda busca por lógica de jogo vinha duplicada: uma vez do código real,
outra do código morto sob nomes parecidos. Isso não é só "espaço
desperdiçado" -- é ruído que sobra pro agente filtrar toda vez que
busca algo, o oposto de "menos rodadas de exploração" que é o objetivo
principal desta estratégia.

Criei um arquivo `.codegraphignore` (mesma sintaxe do `.gitignore`) e um
mecanismo no indexador pra excluir arquivo do grafo sem tocar no código
de verdade -- ver `ARQUITETURA.md` seção 4.2. Testado: busca por
`bestHand` foi de 2 resultados (1 real + 1 duplicado) pra 1.

### 7. Minimizar o `AGENTS.md` gerado -- ✅ feito

O bloco automático do item 3 (`.kimi-code/AGENTS.md`) tinha ~12 linhas
com explicação -- e diferente de uma tool result (que só entra no
contexto quando chamada), esse texto entra no contexto de **toda
mensagem** da sessão. Virou uma frase só, sem perder a instrução:
"prefira `search`/`get_flow`/`list_history` a ler arquivo inteiro ou
Grep, consulte primeiro". `codegraph setup` agora também **atualiza**
esse bloco em runs futuros (antes só pulava se já existisse) -- rodar
de novo já troca a versão antiga pela mínima, sem precisar apagar nada
na mão. Detalhe técnico: `ARQUITETURA.md`, seção 13.

## Achado relacionado, não é bem "economia de token" mas apareceu testando

`Poker.tsx` (o componente principal do jogo) virou **um chunk só de
1015 linhas** -- o tree-sitter não separa função de dentro de um
componente React grande. Então mesmo com um fluxo apontando pra dentro
dele, se o pedaço que interessa está lá dentro (foi o caso de
`showdown()`), a única saída hoje é ler um trecho grande do arquivo
inteiro. Resolver isso é mudar a lógica de chunking (mais arriscado,
fica pro roadmap) -- por enquanto, o jeito de contornar é escrever o
fluxo apontando pro arquivo inteiro nesses casos e deixar a descrição do
passo bem específica (nome da função, o que ela faz), como fiz no
`vencedor_da_mao.yaml`.

## Onde ver os números de verdade

Aba **Saúde** do `/dashboard` mostra se está tudo no ar; aba **Visão
geral** mostra `%` de trocas que usaram alguma tool do codegraph e o
tokens/s médio -- ambos com dado real, atualizando sozinho conforme você
usa. Detalhe técnico completo de cada item acima: `ARQUITETURA.md`,
seção 12.
