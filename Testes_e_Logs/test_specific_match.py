import sys

# overwrite sys.stdout to a file to avoid encoding issues
f = open('debug_match.json', 'w', encoding='utf-8')
sys.stdout = f

import arbitrage_scanner
import json

kalshi_markets = arbitrage_scanner.get_kalshi_lol_markets()
poly_markets = arbitrage_scanner.get_poly_lol_markets()

k_match = None
for k in kalshi_markets:
    title = k.get('title', '').lower()
    ticker = k.get('ticker', '').lower()
    if 'ktc' in title or 'drxc' in title or 'drxc' in ticker or 'ktc' in ticker:
        k_match = k
        print("KALSHI CANDIDATE:")
        print(json.dumps(k, indent=2))
        print("--------------")
        break

for p in poly_markets:
    title = p.get('title', '').lower()
    q = p.get('question', '').lower()
    if ('kt rolster' in title and 'drx challengers' in title) or ('kt rolster' in q and 'drx challengers' in q):
        p_match = p
        print("POLYMARKET CANDIDATE:")
        print(json.dumps(p, indent=2))
        print("--------------")
        # Test the match mathematically now
        res = arbitrage_scanner.match_markets(k_match, p_match)
        print("MATCH RESULT FOR THIS ONE:", res)
        print("--------------")

if k_match and p_match:
    res = arbitrage_scanner.match_markets(k_match, p_match)
    print("MATCH RESULT:")
    print(res)
else:
    print("Could not find candidates on both sides.")

f.close()
