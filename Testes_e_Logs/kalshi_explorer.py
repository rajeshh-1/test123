import requests, base64, time, json
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import sys

KEY_ID = '15dc9b9b-5a3b-4c58-8979-8a6e1d675b94'
KEY_PATH = r'C:\Users\Gstangari\Downloads\Arbitrage sports\kalshi-key.pem.txt'
KALSHI_BASE = 'https://api.elections.kalshi.com/trade-api/v2'

with open(KEY_PATH, 'rb') as f:
    key_data = f.read()
    key = serialization.load_pem_private_key(key_data, password=None)

def get_headers(method, path):
    ts = str(int(time.time() * 1000))
    msg = ts + method + path
    sig = key.sign(
        msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    return {
        'KALSHI-API-KEY': KEY_ID,
        'KALSHI-API-SIGNATURE': base64.b64encode(sig).decode(),
        'KALSHI-API-TIMESTAMP': ts
    }

print('Searching for simple match-winner markets (mve_filter=exclude)...')
path = '/markets?limit=100&status=open&mve_filter=exclude'
r = requests.get(KALSHI_BASE + path, headers=get_headers('GET', path))
markets = r.json().get('markets', [])

simple_markets = []
for m in markets:
    simple_markets.append({
        'ticker': m.get('ticker'),
        'series_ticker': m.get('series_ticker'),
        'title': m.get('title'),
        'category': m.get('category'),
        'yes_sub_title': m.get('yes_sub_title')
    })

print(f'Found {len(simple_markets)} simple markets.')
# Print unique series tickers found
series_found = list(set(m.get('series_ticker') for m in simple_markets if m.get('series_ticker')))
print(f'Series Tickers: {series_found}')

with open('kalshi_simple_markets.json', 'w', encoding='utf-8') as f:
    json.dump(simple_markets, f, indent=2)
