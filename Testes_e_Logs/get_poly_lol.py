import requests
import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

url = 'https://gamma-api.polymarket.com/events'
params = {
    'limit': 1000,
    'offset': 100,
    'active': 'true',
    'closed': 'false'
}

print("Buscando todos os eventos ativos do Polymarket...")
try:
    r = requests.get(url, params=params)
    events = r.json()
except Exception as e:
    print(f"Erro na requisição: {e}")
    exit()

lol_events = []
for e in events:
    title = e.get('title', '').lower()
    description = e.get('description', '').lower()
    # Verifica padroes comuns para eventos de LoL
    if ('lol' in title or 'league of legends' in title or 
        'lck' in title or 'lec' in title or 'lcs' in title or 'lpl' in title):
        lol_events.append(e)

print(f"\nTotal de eventos ativos no Poly: {len(events)}")
print(f"Total de eventos relacionados a LoL: {len(lol_events)}\n")

for e in lol_events:
    t = e.get('title', '')
    slug = e.get('slug', '')
    print(f"-> {t}  [{slug}]")

if not lol_events:
    # Se nao achar nada, procurar por 'esports' tag
    r2 = requests.get('https://gamma-api.polymarket.com/events', params={'limit': 500, 'tag': 'esports', 'active': 'true'})
    evs = r2.json()
    print(f"\nTentativa 2 (Tag esports): Encontrados {len(evs)} eventos")
    for e in evs[:20]:
         print(f"-> {e.get('title', '')}  [{e.get('slug', '')}]")
