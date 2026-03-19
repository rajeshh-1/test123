import requests

def get_poly():
    url = "https://gamma-api.polymarket.com/events/cbb-manh-stpete-2026-02-27"
    r = requests.get(url)
    if r.status_code == 200:
        data = r.json()
        print("======== POLYMARKET ========")
        for m in data.get('markets', []):
            if m.get('active'):
                print(f"Question: {m['question']}")
                for k, v in zip(m.get('outcomes', []), m.get('outcomePrices', [])):
                    print(f"  {k}: {round(float(v)*100, 1)}%")
    else:
        print("Poly Error:", r.status_code)

def get_kalshi():
    url = "https://api.elections.kalshi.com/trade-api/v2/markets/KXNCAAMBGAME-26FEB27MANSPC"
    r = requests.get(url)
    if r.status_code == 200:
        data = r.json()
        print("\n======== KALSHI ========")
        m = data.get('market', {})
        y_team = m.get('yes_sub_title')
        n_team = m.get('no_sub_title')
        yes_price = m.get('yes_ask', 0)
        no_price = m.get('no_ask', 0)
        print(f"Title: {m.get('title')}")
        print(f"  YES ({y_team}): {yes_price}c")
        print(f"  NO ({n_team}): {no_price}c")
    else:
        print("Kalshi Error:", r.status_code, r.text)

get_poly()
get_kalshi()
