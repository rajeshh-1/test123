import json
import arbitrage_scanner as asc

k_mkts = asc.fetch_markets_kalshi()
p_mkts = asc.fetch_markets_poly()

k_names = set([asc.normalize_team_name(m.get('yes_sub_title') or '') for m in k_mkts if m.get('yes_sub_title')])

p_names = set()
for m in p_mkts:
    try:
        out = json.loads(m.get('outcomes', '[]'))
        for o in out:
            p_names.add(asc.normalize_team_name(o))
    except Exception:
        pass

intersection = k_names.intersection(p_names)

with open('diagnostics_output.txt', 'w', encoding='utf-8') as f:
    f.write(f"Total Kalshi markets: {len(k_mkts)}\n")
    f.write(f"Total Poly markets: {len(p_mkts)}\n")
    f.write(f"Unique normalized Kalshi names: {len(k_names)}\n")
    f.write(f"Unique normalized Poly names: {len(p_names)}\n")
    f.write(f"Intersection count: {len(intersection)}\n\n")
    
    f.write("Sample valid intersections:\n")
    for name in list(intersection)[:50]:
        f.write(f"- {name}\n")
    
    f.write("\nSample Kalshi missing names:\n")
    for name in list(k_names - p_names)[:50]:
        f.write(f"- {name}\n")
        
    f.write("\nSample Poly missing names:\n")
    for name in list(p_names - k_names)[:50]:
        f.write(f"- {name}\n")

print("Diagnostics written to diagnostics_output.txt")
