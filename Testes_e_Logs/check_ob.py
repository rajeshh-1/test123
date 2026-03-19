import requests, base64, time, json
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KEY_ID = '15dc9b9b-5a3b-4c58-8979-8a6e1d675b94'
KEY_PATH = r'C:\Users\Gstangari\Downloads\Arbitrage sports\kalshi-key.pem.txt'
KALSHI_BASE = 'https://api.elections.kalshi.com/trade-api/v2'

with open(KEY_PATH, 'rb') as f:
    kalshi_private_key = serialization.load_pem_private_key(f.read(), password=None)

def get_headers(method, path):
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

path = '/markets?limit=200&status=open&series_ticker=KXLOLGAME'
r = requests.get(KALSHI_BASE + path, headers=get_headers('GET', path))
markets = r.json().get('markets', [])

lundqvist = next((m for m in markets if 'Lundqvist' in m.get('title', '')), None)
ruddy = next((m for m in markets if 'Ruddy' in m.get('title', '')), None)

def check_ob(ticker):
    p = f'/markets/{ticker}/orderbook'
    r2 = requests.get(KALSHI_BASE + p, headers=get_headers('GET', p))
    ob = r2.json().get('orderbook', {})
    yes_orders = ob.get('yes', [])
    no_orders = ob.get('no', [])
    yes_orders = yes_orders if yes_orders else []
    no_orders = no_orders if no_orders else []
    
    yes_liq = sum([level[1] for level in yes_orders])
    no_liq = sum([level[1] for level in no_orders])
    
    return f'Yes Liquidity: {yes_liq} contracts | No Liquidity: {no_liq} contracts'

print('LUNDQVIST OB ->', check_ob(lundqvist.get('ticker')) if lundqvist else 'Not found')
print('RUDDY OB    ->', check_ob(ruddy.get('ticker')) if ruddy else 'Not found')
