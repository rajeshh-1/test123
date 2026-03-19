import requests, base64, time, json
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KEY_ID = '15dc9b9b-5a3b-4c58-8979-8a6e1d675b94'
KEY_PATH = r'C:\Users\Gstangari\Downloads\Arbitrage sports\kalshi-key.pem.txt'
BASE_URL = 'https://api.elections.kalshi.com/trade-api/v2'

with open(KEY_PATH, 'rb') as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None)

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

ticker = 'KXLOLGAME-26FEB25LEOFF15-LEO'
path = f'/markets/{ticker}'
r = requests.get(BASE_URL + path, headers=get_headers('GET', path))
market = r.json().get('market', {})

print("=== PREÇOS KALSHI (em centavos, 0-100) ===")
campos = ['yes_bid', 'yes_ask', 'no_bid', 'no_ask', 'last_price', 'volume', 'open_interest', 'status']
for campo in campos:
    val = market.get(campo)
    if isinstance(val, (int, float)) and campo not in ['volume', 'open_interest']:
        print(f"  {campo:20s}: {val} centavos = ${val/100:.2f}")
    else:
        print(f"  {campo:20s}: {val}")
