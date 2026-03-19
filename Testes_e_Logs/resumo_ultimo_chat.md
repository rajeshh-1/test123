# Resumo do Chat Anterior: Debugging Arbitrage Scanner Logic
**Data do Log:** 25 de Fevereiro de 2026

## Arquivos Trabalhados / Foco
* `arbitrage_scanner.py`
* `find_poly_match.py`
* `get_markets.py`

## 1. O que foi implementado e discutido
Conforme solicitado na conversa anterior, concluímos o desenvolvimento do `arbitrage_scanner.py`, um sistema inteligente *Headless* projetado para:
1. Conectar-se às APIs privadas e públicas das casas de predição Kalshi e Polymarket simultaneamente.
2. Identificar todos os mercados ativos disponíveis para League of Legends (`series_ticker=KXLOLGAME` e `tag=esports`).
3. Relacionar matematicamente as equipes oponentes superando inconsistências severas de formatação (nomes das equipes não batiam entre os sites).
4. Identificar spread positivo para apostas sem risco garantido por hedge.

## 2. Desafios Técnicos Solucionados (Cross-Matching)
A maior barreira discutida para este bot foi a falta de uniformidade nos nomes e identificadores. Polymarket e Kalshi **não compartilham nenhum ID de evento**.
Para cruzar o campeonato de LoL LCK e LEC de forma confiável, projetamos um filtro em quatro etapas no script:

> **A Natureza do JSON do Polymarket (Descoberta do Chat):**
> Durante as análises da API, descobrimos que o array nativo de times (A tag `outcomes: ["Equipe A", "Equipe B"]`) **não é utilizado nas apostas de partidas do Polymarket (eSports)**. O Polymarket lista apenas `["Yes", "No"]` em resposta à pergunta "A Equipe A vencerá?".

Para resolver esse silêncio de dados, o "Motor de Validação Cruzada" extrai a identidade dos dois times **diretamente do Título do evento** usando um sistema de Token Split Híbrido somado à biblioteca `thefuzz`.
* As strings sofrem trim reverso para anular nomes inúteis como "Esports", "Gaming" ou "Team".
* Os bloqueios de timezone rigorosos baseados na data ISO 8601 foram ajustados para **margem de tolerância de 4 dias**, pois a Kalshi muitas vezes "segura" a data final por até 5 dias pós-partida, enquanto a Polymarket fecha no ato. O Scanner seria cegado sem isso.
* Adicionamos um **Dicionário Hardcoded Expandido (`man_map`)** capaz de transmutar siglas como "KTC", "DRXC", "T1A" e "BME" nativamente durante o cruzamento das pontuações de Fuzzy Ratio (Tolerância de 60%).

## 3. Como Utilizar o Scanner (Resultado Final)
O script está totalmente finalizado e contido em um único arquivo de execução.
Rodando o comando `python arbitrage_scanner.py`, o bot efetuará as leituras e imprimirá na tela o status de matching de acordo com este formato de métrica de lucro:

> ► Lado A (YES Kalshi + Oponente Poly): $ 0.941
>   [Kalshi YES Bilibili Gaming: $0.44] + [Poly Weibo: $0.501]
> 💰 SPREAD DE LUCRO EST.: +6.27% (Custo: $0.941) 💰

*Nota do Teste Final:* As plataformas continham grades de campeonatos diferentes naquele minuto. O filtro cruzado logou sucesso em separar os "Falsos Positivos" das ligas chinesas vs as ligas coreanas, não recomendando entradas arriscadas.
