import requests, json

print("Buscando Kalshi...")
try:
    k_res = requests.get("https://api.elections.kalshi.com/trade-api/v2/markets?limit=10&status=open&series_ticker=KXLOLGAME").json().get("markets", [])
    print("Kalshi Top 10 YES Teams:")
    for m in k_res[:10]: 
        print(f"  - {m.get('yes_sub_title')}")
except Exception as e: print(e)

print("\nBuscando Poly...")
try:
    p_res = requests.get("https://gamma-api.polymarket.com/events?limit=100&active=true&closed=false&tag=esports").json()
    print("\nPoly Top LOL Events:")
    for e in p_res:
        t = e.get("title", "").lower()
        if "lol" in t or "lck" in t or "lec" in t or "drx" in t or "kt" in t:
            m = e.get("markets", [{}])[0]
            outcomes = m.get("outcomes", "VAZIO")
            print(f"  - TITLE: {e.get('title')}")
            print(f"    OUTCOMES: {outcomes}")
            print(f"    QUESTION: {m.get('question', '')}")
except Exception as e: print(e)
