import requests, base64, time, json, unicodedata
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from thefuzz import fuzz
import sys
import os
import concurrent.futures

# ==========================================
# 1. CONFIGURAÇÕES DE MERCADOS (NOVO)
# ==========================================
import sys

session = requests.Session()

# Ajuste de encoding para o terminal Windows lidar com emojis e acentos
# sys.stdout.reconfigure(encoding='utf-8')

SUPPORTED_MARKETS = [
    {"name": "League of Legends", "kalshi_series": "KXLOLGAME", "poly_tags": ['64', '65'], "poly_keywords": ['lol', 'league of legends', 'lck', 'lec', 'lcs', 'lpl', 'cblol', 'nlc']},
    {"name": "CS2", "kalshi_series": "KXCS2GAME", "poly_tags": ['64'], "poly_keywords": ['cs2', 'counter-strike', 'blast', 'iem', 'esl pro league']},
    {"name": "Valorant", "kalshi_series": "KXVALORANT", "poly_tags": ['64'], "poly_keywords": ['valorant', 'vct', 'masters', 'champions']},
    {"name": "NBA", "kalshi_series": "KXNBAGAME", "poly_tags": ['4'], "poly_keywords": ['nba', 'basketball', 'lakers', 'celtics', 'warriors']},
    {"name": "NCAAM", "kalshi_series": "KXNCAAMBGAME", "poly_tags": ['28', '4'], "poly_keywords": ['ncaa', 'college basketball', 'march madness']},
    {"name": "NCAAW", "kalshi_series": "KXNCAAWBGAME", "poly_tags": ['28', '4'], "poly_keywords": ['ncaa', 'womens basketball', 'women']},
    {"name": "Euroleague", "kalshi_series": "KXEUROLEAGUEGAME", "poly_tags": ['4'], "poly_keywords": ['euroleague', 'basketball']},
    {"name": "FIBA", "kalshi_series": "KXFIBAGAME", "poly_tags": ['4'], "poly_keywords": ['fiba', 'basketball']},
    {"name": "WNBA Draft", "kalshi_series": "KXWNBADRAFT1", "poly_tags": ['4'], "poly_keywords": ['wnba', 'draft', 'basketball']},
    {"name": "LNB", "kalshi_series": "KXLNBGAME", "poly_tags": ['4'], "poly_keywords": ['lnb', 'liga nacional', 'basquetbol', 'basketball']}
]

SIMULATED_BUDGET_USD = 100.0

# ==========================================
# 0. CONFIGURAÇÕES GERAIS E AUTENTICAÇÃO
# ==========================================
def load_env_file(path=".env"):
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()
KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "kalshi-key.pem.txt")).strip()
KALSHI_BASE = 'https://api.elections.kalshi.com/trade-api/v2'
POLY_BASE = 'https://gamma-api.polymarket.com'

try:
    if not KEY_ID:
        raise RuntimeError("missing KALSHI_API_KEY_ID")
    with open(KEY_PATH, 'rb') as f:
        kalshi_private_key = serialization.load_pem_private_key(f.read(), password=None)
except Exception as e:
    print(f"Erro ao carregar a chave privada da Kalshi: {e}")
    exit()

def get_kalshi_headers(method, path):
    ts = str(int(time.time() * 1000))
    msg = ts + method + path
    sig = kalshi_private_key.sign(
        msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    return {
        'KALSHI-API-KEY': KEY_ID,
        'KALSHI-API-SIGNATURE': base64.b64encode(sig).decode(),
        'KALSHI-API-TIMESTAMP': ts
    }

# ==========================================
# 1.5 UTILITÁRIOS DE NORMALIZAÇÃO
# ==========================================
def normalize_team_name(name):
    """Normaliza nomes para comparação fuzzy e indexação, removendo acentos e sujeira."""
    if not name: return ""
    # Remove acentos
    n = unicodedata.normalize('NFKD', name).encode('ASCII', 'ignore').decode('ASCII')
    # Lowercase e remove termos comuns de eSports/Esportes
    n = n.lower()
    for word in [" esports", " gaming", " team", " academy", " kings", " c.", " cl.", " sports"]:
        n = n.replace(word, "")
    return n.strip()

GAME_CLASSIFIER = {
    'lol': ['lol', 'league of legends', 'lck', 'lec', 'lcs', 'lpl', 'cblol'],
    'cs': ['cs2', 'cs:go', 'counter-strike', 'blast', 'iem', 'esl'],
    'valorant': ['valorant', 'vct', 'masters', 'champions'],
    'ncaa': ['ncaa', 'college'],
    'nba': ['nba'],
    'soccer': ['soccer', 'football', 'champions league', 'premier league', 'laliga', 'serie a', 'bundesliga'],
    'tennis': ['tennis', 'atp', 'wta', 'open'],
    'baseball': ['baseball', 'mlb']
}

def get_game_category(text):
    """Identifica a categoria do jogo/esporte baseada em keywords no título."""
    if not text: return None
    t = text.lower()
    for cat, keywords in GAME_CLASSIFIER.items():
        if any(kw in t for kw in keywords):
            return cat
    return None

def match_markets(kalshi_market, poly_market):
    """
    Versão compatível do match_markets para manter a lógica de filtragem (Gênero/Data)
    mas focada apenas na validação de metadados, já que o nome é indexado.
    """
    p_title_raw = poly_market.get("title", "").lower()
    p_slug = poly_market.get("slug", "").lower()
    k_title_raw = kalshi_market.get("title", "").lower()
    
    # Classificação de Jogo (Evita cruzamento Valorant vs CS)
    k_cat = get_game_category(k_title_raw)
    p_cat = get_game_category(p_title_raw)
    
    if k_cat and p_cat and k_cat != p_cat:
        # Categorias conflitantes detectadas (ex: um é Valorant e o outro é CS)
        return None

    # Validação de Escopo (Match vs Map/Game)
    # Alguns mercados na Kalshi escondem o rótulo de "Map" ou "Game" do título legível e deixam apenas no final do Ticker (ex: "-2")
    k_ticker = kalshi_market.get("ticker", "").lower()
    
    import re
    # 1. Detectar menção explícita de "map" ou "game" nos títulos
    p_has_map_title = 'map ' in p_title_raw
    p_has_game_title = 'game ' in p_title_raw
    k_has_map_title = 'map ' in k_title_raw
    k_has_game_title = 'game ' in k_title_raw
    
    # 2. Detectar mapas escondidos no ticker da Kalshi (ex: terminando em -1, -2, -3, -4, -5)
    k_map_hidden_match = re.search(r'-([1-5])$', k_ticker)
    k_hidden_map_num = k_map_hidden_match.group(1) if k_map_hidden_match else None
    
    k_is_map_specific = k_has_map_title or k_has_game_title or bool(k_hidden_map_num)
    p_is_map_specific = p_has_map_title or p_has_game_title
    
    # Regra 1: Se um é um mapa/jogo específico e o outro é a linha da série principal (Moneyline), não cruzar.
    if k_is_map_specific != p_is_map_specific:
        return None
        
    # Regra 2: Se AMBOS são referentes a sub-mapas/jogos, certificar-se de que é o mesmo número.
    if k_is_map_specific and p_is_map_specific:
        for i in range(1, 6):
            map_str = f"map {i}"
            game_str = f"game {i}"
            
            # Kalshi tem esse numero? (Via título ou via ticker escondido)
            k_has_num = (map_str in k_title_raw) or (game_str in k_title_raw) or (k_hidden_map_num == str(i))
            # Poly tem esse numero? (Via título)
            p_has_num = (map_str in p_title_raw) or (game_str in p_title_raw)
            
            # A partir do momento em que um número é detectado em qualquer dos lados, 
            # se ele não existir NO OUTRO lado, cancela o cruzamento.
            if k_has_num != p_has_num:
                return None

    # Validação de Gênero (Evita cruzar Masculino com Feminino)
    k_ticker = kalshi_market.get("ticker", "").lower()
    female_indicators = ["(w)", "cwbb", "womens", "women's", "wnba"]
    
    k_is_female = ('wb' in k_ticker) or any(ind in k_title_raw for ind in female_indicators)
    p_is_female = any(ind in p_title_raw or ind in p_slug for ind in female_indicators)
    
    if k_is_female != p_is_female:
        return None

    # Validação Temporal (Tolerância de 4 dias)
    try:
        k_time_str = kalshi_market.get("expiration_time", "").replace("Z", "+00:00")
        p_time_str = poly_market.get("endDate", "").replace("Z", "+00:00")
        if k_time_str and p_time_str:
            k_date = datetime.fromisoformat(k_time_str).date()
            p_date = datetime.fromisoformat(p_time_str).date()
            if abs((k_date - p_date).days) > 4:
                return None
    except Exception:
        pass

    return {"status": "success"}
def fetch_markets_kalshi():
    """Busca TODOS os mercados ativos da Kalshi usando paginação por Cursor."""
    print("🔄 Buscando mercados principais na Kalshi (Paginado)...")
    all_kalshi_markets = []
    cursor = None
    
    while True:
        try:
            # Aumentado para 1000 por request conforme sugestão do usuário
            path = f'/markets?limit=1000&status=open&mve_filter=exclude'
            if cursor:
                path += f'&cursor={cursor}'
                
            r = session.get(KALSHI_BASE + path, headers=get_kalshi_headers('GET', path), timeout=5)
            
            if r.status_code == 200:
                data = r.json()
                markets = data.get('markets', [])
                
                agora = datetime.now(timezone.utc)
                limite_7_dias = agora + timedelta(days=7)

                for m in markets:
                    # Filtro de Liquidez: Ignora se for zero absoluto para poupar cruzamento atoa
                    if m.get('volume_24h', 0) == 0 and m.get('liquidity', 0) == 0:
                        continue

                    # Filtro Temporal: Apenas mercados que fecham nos próximos 7 dias
                    # Isso reduz drasticamente o volume de dados irrelevantes
                    exp_str = m.get('expiration_time')
                    if exp_str:
                        try:
                            # Formato Kalshi: "2026-02-26T23:00:00Z"
                            # Removendo o Z para compatibilidade se necessário
                            exp_dt = datetime.fromisoformat(exp_str.replace('Z', ''))
                            if exp_dt > limite_7_dias:
                                continue
                        except Exception:
                            pass

                    title = m.get('title', '').lower()
                    # Filtro de Precisão: Focamos em Match Winners. 
                    # Pulamos mercados de "Map X", "Round X" ou "Game X" que são sub-mercados.
                    if (':' in title or m.get('yes_sub_title')) and not any(x in title for x in ['wins by over', 'map ', 'round ', 'score ', 'game ']):
                         all_kalshi_markets.append(m)
                
                cursor = data.get('cursor')
                if not cursor:
                    break
                # Rate limit amigável
                time.sleep(0.5)
            else:
                print(f"Erro Kalshi: {r.status_code} - {r.text}")
                break
        except Exception as e:
            print(f"Exceção Kalshi: {e}")
            break
                
    print(f"✅ Kalshi: {len(all_kalshi_markets)} mercados totais encontrados.")
    return all_kalshi_markets

def fetch_markets_poly():
    """Busca todos os eventos esportivos relevantes no Polymarket com paginação real."""
    print(f"🔄 Buscando mercados no Polymarket por Categorias Desportivas...")
    all_poly_markets = []
    
    # Todas as tags prováveis de esportes no Polymarket
    # Se bater no limite de 10k items (API), dividir por tags resolve o limite.
    sports_tags = [
        '1', '2', '3', '4', '5', '6', '7', '9', '26', '28', '30', '33',  # Sports (NFL, Soccer, MLB, NBA, NHL, Tennis, MMA...)
        '64', '65', '66', '89', '90', '1000000', 'esports', 'sports'     # Esports (CS2, LoL, Dota, Valo, CoD) e slubs absolutos
    ]
    
    for tag in sports_tags:
        offset = 0
        limit = 1000
        
        while True:
            try:
                # Com tag_id
                params = {'limit': limit, 'offset': offset, 'active': 'true', "closed": "false", "tag_id": tag}
                r = session.get(f"{POLY_BASE}/events", params=params, timeout=10)
                
                if r.status_code != 200:
                    break
                
                data = r.json()
                events = data if isinstance(data, list) else data.get('events', [])
                if not events: break
            
                agora = datetime.now(timezone.utc)
                limite_7_dias = agora + timedelta(days=7)

                for ev in events:
                    # Filtro Temporal Polymarket
                    end_str = ev.get('endDate')
                    if end_str:
                        try:
                            # Poly ISO format handling
                            end_dt = datetime.fromisoformat(end_str.replace('Z', ''))
                            if end_dt > limite_7_dias:
                                continue
                        except Exception:
                            pass

                    markets = ev.get('markets', [])
                    for m in markets:
                        q_lower = m.get('question', ev.get('title', '')).lower()
                        # Filtro agnóstico de Match Winner/Moneyline
                        # Adicionado 'map ', 'round ' e 'game ' para evitar conflito com Match Winner
                        if any(x in q_lower for x in ['handicap', 'total', 'spread', 'points', 'kill', 'first blood', 'map ', 'round ', 'score ', 'game ']):
                            continue
                            
                        if not m.get('endDate'): m['endDate'] = ev.get('endDate')
                        if not m.get('gameStartTime'): m['gameStartTime'] = ev.get('startDate')
                        if not m.get('question'): m['question'] = ev.get('title')
                        all_poly_markets.append(m)
                
                if len(events) < limit: break
                offset += limit
            except Exception:
                break

    # Deduplicação por Condition ID
    unique_markets = {}
    for m in all_poly_markets:
        mid = m.get('conditionId') or m.get('id')
        if mid and mid not in unique_markets:
            unique_markets[mid] = m
            
    final_list = list(unique_markets.values())
    print(f"✅ Polymarket: {len(final_list)} mercados únicos e relevantes encontrados.")
    return final_list

# ==========================================
# 2. MOTOR DE VALIDAÇÃO CRUZADA (CROSS-MATCH)
# ==========================================
def match_markets(kalshi_market, poly_market):
    """
    Algoritmo de Cross-Validation Matching 
    Ignora lixo, imune a inversões de lado (Blue/Red), e elimina Falsos Positivos.
    """
    
    # ====== PASSO 1: FILTRO DE GÊNERO / CATEGORIA (FALSOS POSITIVOS) ======
    p_title_raw = poly_market.get("title", "").lower()
    p_slug = poly_market.get("slug", "").lower()
    
    # Se Kalshi é Masculino (Ex: NCAAM, NBA) mas o Poly é Feminino (Womens, WNBA, CWBB, (W))
    # Precisamos bloquear o match para não cruzar o jogo do time masculino com o feminino da mesma faculdade.
    female_indicators = ["(w)", "cwbb", "womens", "women's", "wnba"]
    if any(ind in p_title_raw or ind in p_slug for ind in female_indicators):
        # A menos que no futuro adicionemos suporte a ligas femininas na Kalshi
        # Por enquanto, a nossa config SUPPORTED_MARKETS só tem ligas masculinas cadastradas.
        return None

    # ====== PASSO 2: VALIDAÇÃO TEMPORAL ======
    k_time_str = kalshi_market.get("expected_expiration_time", kalshi_market.get("close_time", "")).replace("Z", "+00:00")
    # Polymarket costuma usar startDate, endDate, ou gameStartTime
    p_time_str = poly_market.get("endDate", poly_market.get("gameStartTime", ""))
    
    # Em eSports, Kalshi costuma botar 'close_time' muito para frente (dia de repasse)
    # portanto, se for a mesma semana ou mês, a gente deixa cruzar na validação por Nomes (Cross-Validation).
    # Tolerância de 4 dias.
    if p_time_str:
        p_time_str = p_time_str.replace("Z", "+00:00")
        try:
            k_date = datetime.fromisoformat(k_time_str).date()
            p_date = datetime.fromisoformat(p_time_str).date()
            if abs((k_date - p_date).days) > 4:
                return None
        except ValueError:
            pass
            
    # ====== PASSO 2: A ÂNCORA DO KALSHI ======
    k_title = kalshi_market.get("title", "").lower()
    k_yes_team = kalshi_market.get("yes_sub_title", "").lower()
    
    if not k_yes_team:
        return None

    try:
        p_q_lower = poly_market.get("question", "").lower()
        p_t_lower = poly_market.get("title", "").lower()
        
        # A forma mais segura de pegar os times no Poly é assumir que o Title sempre tem "Time A vs Time B"
        # Não parseamos nada, apenas jogamos a thefuzz nas duas metades usando split " vs "
        # E mesmo que não tenha "vs", separamos os outcomes por fallback do json original
        raw_out = poly_market.get("outcomes", [])
        if isinstance(raw_out, str): p_outcomes_raw = json.loads(raw_out)
        else: p_outcomes_raw = raw_out
        
        if ' vs. ' in p_t_lower:
            parts = p_t_lower.split(' vs. ')
            t1 = parts[0].split(':')[-1].strip()
            t2 = parts[1].split('(')[0].split('match')[0].strip()
            p_outcomes_clean = [t1, t2]
        elif ' vs ' in p_t_lower:
            parts = p_t_lower.split(' vs ')
            t1 = parts[0].split(':')[-1].strip() # Tira "LoL:" ou "LCK:"
            t2 = parts[1].split('(')[0].split('match')[0].strip()  # Tira "(BO3)" e "match"
            p_outcomes_clean = [t1, t2]
        elif '@' in p_t_lower: # Padrão tradicional de NBA (Away @ Home)
            parts = p_t_lower.split('@')
            t1 = parts[0].strip()
            t2 = parts[1].strip()
            p_outcomes_clean = [t1, t2]
        else:
            # Fallback pros outcomes reais se não houver "VS" no titulo (raro em poly lol)
            p_outcomes_clean = [team.lower() for team in p_outcomes_raw]
            
        p_outcomes_clean = [team.replace(" esports", "").replace(" gaming", "").replace(" team", "").replace(" academy", "a").replace(" kings", "") for team in p_outcomes_clean]
    except Exception as e:
        return None

    if len(p_outcomes_clean) != 2:
        return None # Só fazemos arbitragem de mercados binários/Winner

    # ====== PASSO 3: TRIANGULAÇÃO ======
    # token_set_ratio lida bem com abreviações (KTC -> KT Rolster Challengers)
    score_team_0 = fuzz.token_set_ratio(k_yes_team, p_outcomes_clean[0])
    score_team_1 = fuzz.token_set_ratio(k_yes_team, p_outcomes_clean[1])
    
    match_index = -1
    
    # Dicionário manual extensivo
    man_map = {
        'ktc': 'kt rolster challengers',
        'drxc': 'drx challengers',
        't1a': 't1 academy',
        'bme': 'besiktas',
        'tos': 'cdat',
        'fcn': 'fcnv',
        'dmg': 'dmgap',
        'leo': 'leo',
        'rud': 'ruddy',
        'kcg': 'karmine corp',
        'use': 'unicorns of love',
        'blg': 'bilibili',
        'ong': 'ong',
    }
    
    k_team_mapped = man_map.get(k_yes_team, k_yes_team)
    if k_team_mapped != k_yes_team:
        score_team_0 = max(score_team_0, fuzz.token_set_ratio(k_team_mapped, p_outcomes_clean[0]))
        score_team_1 = max(score_team_1, fuzz.token_set_ratio(k_team_mapped, p_outcomes_clean[1]))
    else:
        # Tenta mapear o lado do Polymarket para a sigla da Kalshi se a Kalshi vier crua
        for short, full in man_map.items():
            if full in p_outcomes_clean[0]: score_team_0 = max(score_team_0, fuzz.token_set_ratio(short, k_yes_team))
            if full in p_outcomes_clean[1]: score_team_1 = max(score_team_1, fuzz.token_set_ratio(short, k_yes_team))
            
    # Reduzido limite para 60 para pegar matches parciais pesados
    if score_team_0 > 60 and score_team_0 > score_team_1:
        match_index = 0
    elif score_team_1 > 60 and score_team_1 > score_team_0:
        match_index = 1
        
    if match_index == -1:
        return None

    # ====== PASSO 4: A PROVA REAL ======
    opponent_index = 1 if match_index == 0 else 0
    p_opponent = p_outcomes_clean[opponent_index]
    
    # Extrair os dois times do Kalshi a partir do título para uma comparação justa
    k_opponent = ""
    try:
        k_teams_extracted = []
        title_clean = k_title
        
        # Se for formato Kalshi Esports (KXLOLGAME)
        if " win the " in title_clean and " match" in title_clean:
            match_str = title_clean.split(" win the ")[1].split(" match")[0]
            match_str = match_str.replace(" league of legends", "").replace(" counter-strike", "").replace(" valorant", "").strip()
            if " vs. " in match_str: k_teams_extracted = match_str.split(" vs. ")
            elif " vs " in match_str: k_teams_extracted = match_str.split(" vs ")
        # Se for formato tradicional NBA (KXNBAGAME)
        elif " at " in title_clean and " winner" in title_clean:
            match_str = title_clean.split(" winner")[0].strip()
            k_teams_extracted = match_str.split(" at ")
            
        if len(k_teams_extracted) == 2:
            kt0 = k_teams_extracted[0].strip()
            kt1 = k_teams_extracted[1].strip()
            
            # Identifica qual deles é o k_yes_team e o outro é o oponente
            if fuzz.token_set_ratio(k_yes_team, kt0) > fuzz.token_set_ratio(k_yes_team, kt1):
                k_opponent = kt1
            else:
                k_opponent = kt0
    except Exception:
        pass
            
    if k_opponent:
        score_opp = fuzz.token_set_ratio(p_opponent, k_opponent)
    else:
        # Fallback conservador se falhar a extração: exige token_set_ratio mais restrito
        score_opp = fuzz.token_set_ratio(p_opponent, k_title)

    # Verifica de novo com o dicionário manual se falhou
    if score_opp < 60:
        for short, full in man_map.items():
            if short in p_opponent or full in p_opponent:
                if k_opponent:
                    score_opp = max(score_opp, fuzz.token_set_ratio(full, k_opponent), fuzz.token_set_ratio(short, k_opponent))
                else:
                    score_opp = max(score_opp, fuzz.token_set_ratio(full, k_title), fuzz.token_set_ratio(short, k_title))
                
    # Verifica se o oponente do Poly existe no Kalshi real
    if score_opp < 60: 
        return None 

    return {
        "status": "success",
        "kalshi_yes_team": kalshi_market.get("yes_sub_title"),
        "poly_yes_index": match_index,
        "poly_no_index": opponent_index,
        "poly_opponent_name": p_outcomes_raw[opponent_index] if len(p_outcomes_raw) == 2 else p_outcomes_clean[opponent_index]
    }

# ==========================================
# 3. O SCANNER PRINCIPAL E SUPORTE
# ==========================================

def get_kalshi_orderbook(ticker):
    """Busca o orderbook em tempo real de um mercado específico."""
    path = f'/markets/{ticker}/orderbook'
    try:
        r = session.get(KALSHI_BASE + path, headers=get_kalshi_headers('GET', path), timeout=5)
        return r.json().get('orderbook', {})
    except Exception as e:
        return {}

def get_kalshi_vwap(target_side, ob, quantity_needed):
    """
    Calcula o Preço Médio Ponderado por Volume (VWAP) para Kalshi varrendo o livro.
    - target_side = 'yes' ou 'no' (O lado que queremos COMPRAR)
    Em Kalshi, como os pares são binários (0 a 100), para comprarmos 'YES', 
    nós combinamos/cruzamos as ordens de limite de quem está querendo vender YES/comprar NO.
    A fila 'no' do orderbook contém bids de NO (cujo ASK para YES equivalente é 100 - preço).
    """
    try:
        raw_orders = ob.get('no', []) if target_side == 'yes' else ob.get('yes', [])
        if not raw_orders: return None
        
        # Converte em Asks do lado que queremos comprar e ordena (menor para maior)
        asks = []
        for price_cents, size in raw_orders:
            ask_price_cents = 100 - price_cents
            asks.append((ask_price_cents, float(size)))
            
        asks = sorted(asks, key=lambda x: x[0])
        
        shares_collected = 0.0
        total_cost = 0.0
        for ask_price_cents, size in asks:
            price = ask_price_cents / 100.0
            if shares_collected + size >= quantity_needed:
                needed = quantity_needed - shares_collected
                total_cost += needed * price
                shares_collected += needed
                break
            else:
                total_cost += size * price
                shares_collected += size
                
        if shares_collected < quantity_needed:
            return None # Não há liquidez suficiente
            
        return total_cost / quantity_needed
    except Exception:
        return None

def get_poly_vwap(clob_token_id, quantity_needed):
    """Calcula o Preço Médio Ponderado por Volume (VWAP) para Polymarket varrendo o CLOB."""
    if not clob_token_id:
        return None
    url = f"https://clob.polymarket.com/book?token_id={clob_token_id}"
    try:
        res = session.get(url, timeout=5).json()
        asks = res.get("asks", [])
        if not asks: return None
        
        asks_sorted = sorted(asks, key=lambda x: float(x["price"]))
        
        shares_collected = 0.0
        total_cost = 0.0
        for ask in asks_sorted:
            price, size = float(ask["price"]), float(ask["size"])
            if shares_collected + size >= quantity_needed:
                needed = quantity_needed - shares_collected
                total_cost += needed * price
                shares_collected += needed
                break
            else:
                total_cost += size * price
                shares_collected += size
                
        if shares_collected < quantity_needed:
            return None # Não há liquidez suficiente
            
        return total_cost / quantity_needed
    except Exception:
        return None

def run_scanner():
    print("=" * 50)
    print("🚀 INICIANDO SCANNER UNIVERSAL KALSHI <> POLY")
    print("=" * 50 + "\n")
    
    kalshi_markets = fetch_markets_kalshi()
    poly_markets = fetch_markets_poly()
    
    if not kalshi_markets or not poly_markets:
        print("❌ Falha ao obter lista de mercados. Encerrando.")
        return

    print("\n🔍 Analisando e cruzando mercados com INDEXAÇÃO O(1)...")
    matches_found = []

    # OTIMIZAÇÃO CRÍTICA: Criar um dicionário de busca rápida (Index) para o Polymarket
    # Em vez de O(N*M), teremos O(N)
    poly_index = {}
    
    for p_mkt in poly_markets:
        try:
            outcomes = json.loads(p_mkt.get("outcomes", "[]"))
            for idx, outcome in enumerate(outcomes):
                norm_name = normalize_team_name(outcome)
                if norm_name:
                    if norm_name not in poly_index:
                        poly_index[norm_name] = []
                    poly_index[norm_name].append({
                        'market': p_mkt,
                        'outcome_index': idx,
                        'outcomes': outcomes
                    })
        except Exception:
            continue

    poly_names = list(poly_index.keys())
    fuzzy_cache = {}
    
    # OTIMIZAÇÃO O(1) COM FUZZY MATCH CACHEADO (Magia Híbrida)
    for k_mkt in kalshi_markets:
        # SUPER BUGFIX: A API da Kalshi V2 muitas vezes retorna o time da CASA (Underdog) no yes_sub_title,
        # mas o orderbook de `yes` é SEMPRE amarrado ao primeiro time listado no Título do evento!
        # Sem esse fix, o Scanner compra NO e Vende YES do mesmo time, fingindo que é arbitragem.
        title = k_mkt.get('title', '')
        if " at " in title:
            k_yes_raw = title.split(" at ")[0].strip()
        elif " vs. " in title:
            k_yes_raw = title.split(" vs. ")[0].strip()
        elif " vs " in title:
            k_yes_raw = title.split(" vs ")[0].strip()
        else:
            k_yes_raw = k_mkt.get('yes_sub_title') or ""
            
        if not k_yes_raw: continue
        
        # Limpa o nome ("Winner?" residual)
        k_yes_raw = k_yes_raw.replace(" Winner?", "").strip()
        
        k_yes_norm = normalize_team_name(k_yes_raw)
        
        # Sincroniza abreviações Kalshi ("dallas baptist") com os mascots Polymarket ("dallas baptist patriots")
        if k_yes_norm not in fuzzy_cache:
            fuzzy_cache[k_yes_norm] = []
            for p_name in poly_names:
                # token_set_ratio é perfeito para substring matches
                # 85% impede cruzamentos incorretos entre nomes muito curtos
                if fuzz.token_set_ratio(k_yes_norm, p_name) >= 85:
                    fuzzy_cache[k_yes_norm].append(p_name)
                    
        # Busca direta nas correspondências fuzzadas (Super Rápido!)
        for matched_p_name in fuzzy_cache[k_yes_norm]:
            for match in poly_index[matched_p_name]:
                p_mkt = match['market']
                
                # Validação Contextual (Gênero, Data, Esporte/Game)
                if not match_markets(k_mkt, p_mkt):
                    continue
                    
                match_index = match['outcome_index']
                opp_index = 0 if match_index == 1 else 1 # Simples binário
                
                matches_found.append({
                    "kalshi": k_mkt,
                    "poly": p_mkt,
                    "mapping": {
                        "status": "success",
                        "poly_yes_index": match_index,
                        "poly_no_index": opp_index,
                        "kalshi_yes_team": k_yes_raw, # Corrigido para bater com o esperado no loops seguintes
                        "poly_opponent": match['outcomes'][opp_index]
                    }
                })
                break # Já encontrou este jogo

    print(f"🎯 Cruzamento Finalizado! {len(matches_found)} correspondências encontradas em segundos.\n")
    print("=" * 50)
    print("💲 OPORTUNIDADES DE ARBITRAGEM (Simulação VWAP)")
    print("=" * 50 + "\n")

    oportunidades_reais = 0
    resultados_finais = []
    
    total_matches = len(matches_found)
    print(f"⏳ Consultando Orderbooks na Kalshi para os {total_matches} jogos correspondentes simultaneamente...")

    def process_opportunity(args):
        idx, item = args
        k = item["kalshi"]
        p = item["poly"]
        m = item["mapping"]
        ticker = k.get("ticker", "")
        
        # CHECAGEM REAL DO LIVRO DE OFERTAS DA KALSHI
        ob = get_kalshi_orderbook(ticker)
        
        yes_orders = ob.get('yes', []) or []
        no_orders = ob.get('no', []) or []
        yes_liq_contracts = sum([lvl[1] for lvl in yes_orders])
        no_liq_contracts = sum([lvl[1] for lvl in no_orders])
        k_liq = (yes_liq_contracts * 0.50) + (no_liq_contracts * 0.50)

        # PREÇOS TOPO DE LIVRO PARA ESTIMAR A QUANTIDADE DE SHARES ($100 BUDGET)
        k_yes_ask = k.get('yes_ask')
        k_yes_price_top = float(k_yes_ask) / 100.0 if k_yes_ask is not None and len(yes_orders) > 0 else 1.0
        
        k_no_ask = k.get('no_ask')
        k_no_price_top  = float(k_no_ask) / 100.0 if k_no_ask is not None and len(no_orders) > 0 else 1.0
        
        try:
            p_prices = json.loads(p.get("outcomePrices", "[]"))
            p_yes_price_top = float(p_prices[m["poly_yes_index"]])
            p_no_price_top  = float(p_prices[m["poly_no_index"]])
        except Exception:
            p_yes_price_top = 1.0
            p_no_price_top = 1.0

        # =========================================================================
        # SPREAD DYNAMIC ALIGNMENT (THE SUPER FIX)
        # Polymarket tem nomes fixos e previsíveis nas posições dos Tokens. 
        # API da Kalshi V2 é instável e mapeia 'YES' no orderbook às vezes pro Time da Casa, às vezes pro de Fora,  
        # ignorando a própria string de título. 
        # SOLUÇÃO REAL: Usar a matemática! Alocamos as bolsas olhando p/ a probabilidade!
        # =========================================================================
        if k_yes_price_top < 1.0 and p_yes_price_top < 1.0:
            prob_diff_a = abs(k_yes_price_top - p_yes_price_top)
            prob_diff_b = abs(k_yes_price_top - p_no_price_top)
            # Se a distância matemática for menor para o Oponente (Team B), 
            # significa que a Kalshi inverteu o nome no backend!
            if prob_diff_b < prob_diff_a:
                # INVERSÃO TOTAL DA LÓGICA DE ASSOCIAÇÃO:
                m["poly_yes_index"], m["poly_no_index"] = m["poly_no_index"], m["poly_yes_index"]
                
                # Re-extraímos os nomes corretos do Polymarket para não corromper o output
                p_outcomes_list = p.get('outcomes', [])
                if len(p_outcomes_list) == 2:
                    m["kalshi_yes_team"] = p_outcomes_list[m["poly_yes_index"]]
                    m["poly_opponent"] = p_outcomes_list[m["poly_no_index"]]

        custo_a_top = k_yes_price_top + p_no_price_top
        custo_b_top = k_no_price_top + p_yes_price_top
        melhor_custo_top = min(custo_a_top, custo_b_top)
        
        # Quantas shares precisamos comprar para gastar ~$100 (SIMULATED_BUDGET_USD)?
        target_shares = SIMULATED_BUDGET_USD / melhor_custo_top if melhor_custo_top < 2.0 else 100.0

        # CÁLCULO DE VWAP - "WALKING THE BOOK" PARA A KALSHI PARA X SHARES
        k_vwap_yes = get_kalshi_vwap('yes', ob, target_shares)
        k_vwap_no  = get_kalshi_vwap('no', ob, target_shares)

        # Se não há liquidez suficiente no orderbook, inviabiliza com preço artificial
        k_yes_price = k_vwap_yes if k_vwap_yes is not None else 1.0
        k_no_price = k_vwap_no if k_vwap_no is not None else 1.0

        # CÁLCULO DE VWAP - "WALKING THE BOOK" PARA POLYMARKET
        try:
            clobs_str = p.get("clobTokenIds", "[]") # Polymarket guarda os tokens do CLOB
            clobs = json.loads(clobs_str) if isinstance(clobs_str, str) else clobs_str
            # O array de clobs bate com o array de outcomes
            p_yes_clob = clobs[m["poly_yes_index"]]
            p_no_clob = clobs[m["poly_no_index"]]
        except Exception:
            p_yes_clob = None
            p_no_clob = None

        p_yes_price = get_poly_vwap(p_yes_clob, target_shares)
        p_no_price  = get_poly_vwap(p_no_clob, target_shares)
        
        if p_yes_price is None: p_yes_price = 1.0
        if p_no_price is None: p_no_price = 1.0

        p_liq = float(p.get("liquidityNum", p.get("liquidity", 0)))

        # CÁLCULO GERAL DA ARBITRAGEM (H2H com VWAP em ambas pernas)
        custo_a = k_yes_price + p_no_price
        custo_b = k_no_price + p_yes_price

        # A estratégia só é válida se tiver liquidez suficiente para cobrir o Target (nenhum preço = 1.0)
        valid_strat_a = k_vwap_yes is not None and p_no_price < 0.99
        valid_strat_b = k_vwap_no is not None and p_yes_price < 0.99
        
        if not valid_strat_a and not valid_strat_b:
            melhor_custo = 2.0
            estrategia_escolhida = 'nenhuma'
        elif valid_strat_a and valid_strat_b:
            melhor_custo = min(custo_a, custo_b)
            estrategia_escolhida = 'A' if custo_a < custo_b else 'B'
        elif valid_strat_a:
            melhor_custo = custo_a
            estrategia_escolhida = 'A'
        else:
            melhor_custo = custo_b
            estrategia_escolhida = 'B'
            
        tem_arbitragem = melhor_custo < 0.99
        lucro_pct = ((1.0 - melhor_custo) / melhor_custo) * 100

        return {
            "idx": idx,
            "kalshi_title": k.get('title'),
            "poly_question": p.get('question'),
            "p_liq": p_liq,
            "k_liq": k_liq,
            "custo_a": custo_a,
            "custo_b": custo_b,
            "melhor_custo": melhor_custo,
            "estrategia_escolhida": estrategia_escolhida,
            "k_yes_price": k_yes_price,
            "p_no_price": p_no_price,
            "k_no_price": k_no_price,
            "p_yes_price": p_yes_price,
            "tem_arbitragem": tem_arbitragem,
            "lucro_pct": lucro_pct,
            "kalshi_team": m['kalshi_yes_team'],
            "poly_opp": m['poly_opponent'],
            "target_shares": target_shares
        }

    # Execução Paralela usando ThreadPoolExecutor
    completed_count = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        # Prepara a fila de tarefas
        tasks = [(idx, item) for idx, item in enumerate(matches_found, 1)]
        futures = {executor.submit(process_opportunity, task): task for task in tasks}
        
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                resultados_finais.append(result)
                if result["tem_arbitragem"]:
                    oportunidades_reais += 1
                
                completed_count += 1
                sys.stdout.write(f"\r   🔄 Processados {completed_count}/{total_matches} jogos...")
                sys.stdout.flush()
            except Exception as e:
                print(f"Erro ao processar jogo: {e}")
        
    print("\n✅ Consulta de Orderbooks concluída!\n")

    # Ordena os resultados: Primeiro os com arbitragem (maior lucro primeiro), depois os sem arbitragem (menor custo primeiro)
    resultados_finais.sort(key=lambda x: (x['lucro_pct'], -x['melhor_custo']), reverse=True)

    # ---------------------------------------------------------
    # GERAÇÃO DO ARQUIVO DE RELATÓRIO COMPLETO
    # ---------------------------------------------------------
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    report_filename = f"arbitrage_report_{timestamp}.txt"
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write("==================================================\n")
        f.write(f"📊 RELATÓRIO COMPLETO DE ARBITRAGEM - {timestamp}\n")
        f.write("==================================================\n\n")
        
        for rank, res in enumerate(resultados_finais, 1):
            if res["tem_arbitragem"]:
                f.write(f"🚨 [ OPORTUNIDADE ENCONTRADA: RANK #{rank} | MATCH #{res['idx']} ] 🚨\n")
            else:
                f.write(f"🔸 [ MATCH #{res['idx']} - COMPATÍVEL, MAS SEM SPREAD ]\n")

            f.write(f"   Kalshi: {res['kalshi_title']}\n")
            f.write(f"   Poly  : {res['poly_question']}\n")
            f.write(f"   Liquidez Poly: ${res['p_liq']:,.2f} | Liquidez Kalshi: ${res['k_liq']:,.2f}\n")
            
            if res['custo_a'] < res['custo_b']:
                k_odd = 1.0 / res['k_yes_price'] if res['k_yes_price'] > 0 else 0
                p_odd = 1.0 / res['p_no_price'] if res['p_no_price'] > 0 else 0
                f.write(f"   ► ESTRATÉGIA DE ENTRADA (YES Kalshi + Oponente Poly): $ {res['custo_a']:.3f}\n")
                f.write(f"     Kalshi YES {res['kalshi_team']}: {res['k_yes_price']:.3f} (Odd {k_odd:.2f}x)\n")
                f.write(f"     Polymarket {res['poly_opp']}: {res['p_no_price']:.3f} (Odd {p_odd:.2f}x)\n")
                f.write(f"     [Simulação VWAP com {int(res['target_shares'])} shares]\n")
            else:
                k_odd = 1.0 / res['k_no_price'] if res['k_no_price'] > 0 else 0
                p_odd = 1.0 / res['p_yes_price'] if res['p_yes_price'] > 0 else 0
                f.write(f"   ► ESTRATÉGIA DE ENTRADA (NO Kalshi + Principal Poly): $ {res['custo_b']:.3f}\n")
                f.write(f"     Kalshi NO {res['kalshi_team']}: {res['k_no_price']:.3f} (Odd {k_odd:.2f}x)\n")
                f.write(f"     Polymarket {res['kalshi_team']}: {res['p_yes_price']:.3f} (Odd {p_odd:.2f}x)\n")
                f.write(f"     [Simulação VWAP com {int(res['target_shares'])} shares]\n")
                
            sign = "+" if res['lucro_pct'] > 0 else ""
            if res["tem_arbitragem"]:
                f.write(f"   💰 SPREAD DE LUCRO EST.: {sign}{res['lucro_pct']:.2f}% (Custo: ${res['melhor_custo']:.3f}) 💰\n")
            else:
                f.write(f"   ❌ SPREAD DE LUCRO EST.: {sign}{res['lucro_pct']:.2f}% (Custo: ${res['melhor_custo']:.3f}) ❌\n")
                
            f.write("-" * 60 + "\n")
            
        f.write(f"\n✅ Resumo: Scan concluído. {oportunidades_reais} oportunidade(s) viável(is) de {len(matches_found)} jogos analisados.\n")
        
    print(f"📄 Relatório completo salvo no arquivo: {report_filename}\n")
    print("==================================================")
    print("🏆 TOP 10 MELHORES OPORTUNIDADES (VISUALIZAÇÃO RÁPIDA)")
    print("==================================================\n")

    # Mostrar apenas o TOP 10 no terminal para não poluir
    top_10_resultados = resultados_finais[:10]

    for rank, res in enumerate(top_10_resultados, 1):
        if res["tem_arbitragem"]:
            print(f"🚨 [ OPORTUNIDADE ENCONTRADA: RANK #{rank} | MATCH #{res['idx']} ] 🚨")
        else:
            print(f"🔸 [ MATCH #{res['idx']} - COMPATÍVEL, MAS SEM SPREAD ]")

        print(f"   Kalshi: {res['kalshi_title']}")
        print(f"   Poly  : {res['poly_question']}")
        print(f"   Liquidez Poly: ${res['p_liq']:,.2f} | Liquidez Kalshi: ${res['k_liq']:,.2f}")
        
        if res['custo_a'] < res['custo_b']:
            k_odd = 1.0 / res['k_yes_price'] if res['k_yes_price'] > 0 else 0
            p_odd = 1.0 / res['p_no_price'] if res['p_no_price'] > 0 else 0
            print(f"   ► ESTRATÉGIA DE ENTRADA (YES Kalshi + Oponente Poly): $ {res['custo_a']:.3f}")
            print(f"     Kalshi YES {res['kalshi_team']}: {res['k_yes_price']:.3f} (Odd {k_odd:.2f}x)")
            print(f"     Polymarket {res['poly_opp']}: {res['p_no_price']:.3f} (Odd {p_odd:.2f}x)")
            print(f"     [Simulação VWAP com {int(res['target_shares'])} shares]")
        else:
            k_odd = 1.0 / res['k_no_price'] if res['k_no_price'] > 0 else 0
            p_odd = 1.0 / res['p_yes_price'] if res['p_yes_price'] > 0 else 0
            print(f"   ► ESTRATÉGIA DE ENTRADA (NO Kalshi + Principal Poly): $ {res['custo_b']:.3f}")
            print(f"     Kalshi NO {res['kalshi_team']}: {res['k_no_price']:.3f} (Odd {k_odd:.2f}x)")
            print(f"     Polymarket {res['kalshi_team']}: {res['p_yes_price']:.3f} (Odd {p_odd:.2f}x)")
            print(f"     [Simulação VWAP com {int(res['target_shares'])} shares]")
            
        sign = "+" if res['lucro_pct'] > 0 else ""
        if res["tem_arbitragem"]:
            print(f"   💰 SPREAD DE LUCRO EST.: {sign}{res['lucro_pct']:.2f}% (Custo: ${res['melhor_custo']:.3f}) 💰")
            
            # SIMULAÇÃO PARA $100
            # Recalcula as shares exatas para bater com o orçamento baseado no VWAP final
            k_price_sim = res['k_yes_price'] if res['custo_a'] < res['custo_b'] else res['k_no_price']
            p_price_sim = res['p_no_price'] if res['custo_a'] < res['custo_b'] else res['p_yes_price']
            
            custo_total_unitario = k_price_sim + p_price_sim
            qtd_shares = 100.0 / custo_total_unitario if custo_total_unitario > 0 else 0
            
            custo_k = qtd_shares * k_price_sim
            custo_p = qtd_shares * p_price_sim
            
            if res['custo_a'] < res['custo_b']:
                k_side_str = f"YES {res['kalshi_team']}"
                p_side_str = res['poly_opp']
            else:
                k_side_str = f"NO {res['kalshi_team']}"
                p_side_str = res['kalshi_team']
            
            print(f"\n   🎯 SIMULAÇÃO P/ CAIXA DE $100 (ARBITRAGEM BALANCEADA):")
            print(f"     1) Compre exatamente {int(qtd_shares)} SHARES (Contratos) nas duas corretoras!")
            print(f"     2) Kalshi: Compre a opção [{k_side_str}] (Gasto: ~${custo_k:.2f} a ${k_price_sim:.3f} cada)")
            print(f"     3) Polymarket: Compre a opção [{p_side_str}] (Gasto: ~${custo_p:.2f} a ${p_price_sim:.3f} cada)")
            print(f"     ✅ Retorno Bruto Garantido: ${qtd_shares:.2f} | Lucro Líquido: +${(qtd_shares - 100):.2f}")
        else:
            print(f"   ❌ SPREAD DE LUCRO EST.: {sign}{res['lucro_pct']:.2f}% (Custo: ${res['melhor_custo']:.3f}) ❌")
            
        print("-" * 60)

    print(f"\n✅ Scan concluído! {oportunidades_reais} oportunidade(s) viável(is) de {len(matches_found)} cruzamentos.")
    print(f"👉 Se você quiser ver todos os cruzamentos listados, abra o arquivo {report_filename}")

if __name__ == "__main__":
    run_scanner()
