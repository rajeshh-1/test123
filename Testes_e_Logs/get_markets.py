import requests, base64, time, json, unicodedata
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from thefuzz import fuzz

# ==========================================
# CONFIGURAÇÕES E AUTENTICAÇÃO
# ==========================================
KEY_ID = '15dc9b9b-5a3b-4c58-8979-8a6e1d675b94'
KEY_PATH = r'C:\Users\Gstangari\Downloads\Arbitrage sports\kalshi-key.pem.txt'
KALSHI_BASE_URL = 'https://api.elections.kalshi.com/trade-api/v2'
POLY_BASE_URL = 'https://gamma-api.polymarket.com'

try:
    with open(KEY_PATH, 'rb') as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
except Exception as e:
    print(f"Erro ao carregar a chave privada: {e}")
    exit()

def get_headers(method, path):
    ts = str(int(time.time() * 1000))
    msg = ts + method + path
    sig = private_key.sign(
        msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    return {
        'KALSHI-API-KEY': KEY_ID,
        'KALSHI-API-SIGNATURE': base64.b64encode(sig).decode(),
        'KALSHI-API-TIMESTAMP': ts
    }

def normalize_team_name(name):
    """Limpa o nome do time para garantir um match perfeito."""
    if not name: return ""
    name = name.lower()
    name = "".join(c for c in unicodedata.normalize('NFD', name) if unicodedata.category(c) != 'Mn')
    for word in [' esports', ' gaming', ' team', ' club', ' e-sports']:
        name = name.replace(word, '')
    return name.strip()

# ==========================================
# 1. BUSCA DE DADOS DIRETOS (POR ID/SLUG)
# ==========================================
# Insira aqui os identificadores dos mercados que você quer cruzar
kalshi_ticker = 'KXLOLGAME-26FEB26DRXCKTC-DRXC'
poly_slug = 'lol-ktc-drxc-2026-02-26'

print(f"🔄 Buscando mercado Kalshi: {kalshi_ticker}")
kalshi_path = f'/markets/{kalshi_ticker}'
r_kalshi = requests.get(KALSHI_BASE_URL + kalshi_path, headers=get_headers('GET', kalshi_path))
kalshi_data = r_kalshi.json()

print(f"🔄 Buscando mercado Polymarket: {poly_slug}")
r_poly = requests.get(f"{POLY_BASE_URL}/markets/slug/{poly_slug}")
poly_data = r_poly.json()

# ==========================================
# 2. ANÁLISE E CRUZAMENTO
# ==========================================
if 'market' not in kalshi_data:
    print("Erro: Mercado Kalshi não encontrado ou erro de acesso.")
    exit()

if 'id' not in poly_data:
    print("Erro: Mercado Polymarket não encontrado.")
    exit()

k_market = kalshi_data['market']
p_market = poly_data

k_title = k_market.get('title', '')
k_yes_raw = k_market.get('yes_sub_title', '')
k_yes_team = normalize_team_name(k_yes_raw)

try:
    p_outcomes_raw = json.loads(p_market.get("outcomes", "[]"))
    p_prices = json.loads(p_market.get("outcomePrices", "[]"))
except Exception as e:
    print(f"Erro ao analisar JSON do Polymarket: {e}")
    exit()

p_outcomes = [normalize_team_name(t) for t in p_outcomes_raw]

# Encontra a posição do time do Kalshi no array do Polymarket
match_index = -1
for i, p_team in enumerate(p_outcomes):
    if fuzz.ratio(k_yes_team, p_team) > 85:
        match_index = i
        break

if match_index == -1:
    print(f"❌ Não foi possível relacionar o time '{k_yes_raw}' com as opções do Polymarket: {p_outcomes_raw}")
else:
    opponent_index = 1 if match_index == 0 else 0
    p_opponent_raw = p_outcomes_raw[opponent_index]
    
    # Extração de Preços
    k_yes_price = k_market.get('yes_ask', 100) / 100.0
    k_no_price = k_market.get('no_ask', 100) / 100.0
    p_target_price = float(p_prices[match_index])
    p_opponent_price = float(p_prices[opponent_index])
    p_liquidity = float(p_market.get('liquidityNum', 0))

    custo_a = k_yes_price + p_opponent_price
    custo_b = k_no_price + p_target_price

    # ==========================================
    # 3. LOG ESTRUTURADO
    # ==========================================
    print(f"\n[ MATCH CONFIRMADO ] 🎮")
    print(f"Kalshi: {k_title}")
    print(f"Poly  : {p_market.get('question')}")
    print(f"Liquidez no Poly: ${p_liquidity:,.2f}")
    
    print(f"\n   [ MAPEAMENTO ]")
    print(f"   Kalshi YES = '{k_yes_raw}'")
    print(f"   Poly [{match_index}] = '{p_outcomes_raw[match_index]}' (${p_target_price:.3f})")
    print(f"   Poly [{opponent_index}] = '{p_opponent_raw}' (${p_opponent_price:.3f})")
    
    print(f"\n   [ MATEMÁTICA DA ARBITRAGEM ]")
    print(f"   A) Ganha {k_yes_raw} no Kalshi / Ganha {p_opponent_raw} no Poly:")
    print(f"      Custo: ${k_yes_price:.3f} + ${p_opponent_price:.3f} = ${custo_a:.3f}")
    
    print(f"   B) Perde {k_yes_raw} no Kalshi / Ganha {k_yes_raw} no Poly:")
    print(f"      Custo: ${k_no_price:.3f} + ${p_target_price:.3f} = ${custo_b:.3f}")
    
    if custo_a < 0.98 or custo_b < 0.98:
        melhor = min(custo_a, custo_b)
        print(f"   🚨 OPORTUNIDADE: Lucro est. de {((1-melhor)/melhor)*100:.2f}% 🚨")
    else:
        print(f"   ❌ Sem oportunidade de lucro no momento.")
    print("-" * 70)