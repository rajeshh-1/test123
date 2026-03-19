import requests
import json

print("DEBUG POLYMARKET TITLE vs OUTCOMES")

try:
    p_res = requests.get('https://gamma-api.polymarket.com/events?limit=100&active=true&closed=false&tag=esports').json()
    for ev in p_res:
        t = ev.get('title', '').lower()
        if 'lol' in t or 'lck' in t or 'lec' in t or 'drx' in t or 'kt' in t or 'geng' in t or 't1' in t:
            print(f"\n[EVENTO] {ev.get('title')}")
            for m in ev.get('markets', []):
                print(f"  - QUE: {m.get('question')} | OUT: {m.get('outcomes')} | TYPE: {type(m.get('outcomes'))}")
except Exception as e:
    print(f"Erro: {e}")
