from arbitrage_scanner import fetch_markets_kalshi, KalshiAuth, session, KALSHI_BASE, get_kalshi_headers
import json

auth = KalshiAuth()
token = auth.get_token()

if token:
    session.headers.update({"Authorization": f"Bearer {token}"})

print("buscando...")
kalshi_markets = fetch_markets_kalshi()
print(f"achou {len(kalshi_markets)}")

count = 0
for m in kalshi_markets:
    if 'Winner' in m.get('title', ''):
        print(f"Title: {m['title']}")
        print(f"YES Team (yes_sub_title): {m.get('yes_sub_title')}")
        print(f"NO Team (no_sub_title): {m.get('no_sub_title')}")
        ticker = m['ticker']
        try:
            r = session.get(f"{KALSHI_BASE}/markets/{ticker}/orderbook", headers=get_kalshi_headers('GET', f"/markets/{ticker}/orderbook"))
            ob = r.json().get('orderbook', {})
            yes_bids = ob.get('yes', [])
            no_bids = ob.get('no', [])
            print(f"Top Yes Bid: {yes_bids[0] if yes_bids else 'None'} | Top No Bid: {no_bids[0] if no_bids else 'None'}")
        except Exception as e:
            pass
        print("-" * 40)
        count += 1
        if count > 10: break
