import requests, json, unicodedata
import sys
sys.stdout.reconfigure(encoding='utf-8')

POLY_BASE = 'https://gamma-api.polymarket.com'

def normalize(name):
    name = name.lower()
    name = "".join(c for c in unicodedata.normalize('NFD', name)
                   if unicodedata.category(c) != 'Mn')
    return name.strip()

# ─── Tenta slugs diretos no formato do Polymarket ────────────────────────────
# Padrao: lol-<time1>-<time2>-YYYY-MM-DD  ou  lol-<time1>-vs-<time2>-YYYY-MM-DD
candidate_slugs = [
    "lol-drx-challengers-kt-rolster-challengers-2026-02-26",
    "lol-drx-challengers-vs-kt-rolster-challengers-2026-02-26",
    "lol-drx-kt-rolster-2026-02-26",
    "lol-drx-challengers-ktc-2026-02-26",
    "lol-drxc-kt-rolster-challengers-2026-02-26",
    "lol-drxc-ktc-2026-02-26",
    "lol-drx-kt-2026-02-26",
]

print("=== Tentando slugs diretos no Polymarket ===\n")
found = None
for slug in candidate_slugs:
    r = requests.get(f'{POLY_BASE}/markets/slug/{slug}')
    if r.status_code == 200:
        data = r.json()
        if isinstance(data, dict) and data.get('id'):
            found = data
            print(f"ENCONTRADO! Slug: {slug}\n")
            break
    print(f"  [{r.status_code}] {slug}")

if found:
    q = found.get('question', '')
    outcomes = found.get('outcomes', '[]')
    prices = found.get('outcomePrices', '[]')
    liq = float(found.get('liquidityNum', found.get('liquidity', 0)))
    print(f"\nPergunta : {q}")
    print(f"Times    : {outcomes}")
    print(f"Precos   : {prices}")
    print(f"Liquidez : ${liq:,.2f}")
else:
    # ─── Fallback: busca na API de eventos Poly por serie LoL ───────────────
    print("\n\nSlug direto nao encontrado. Buscando via Events API...\n")

    # Polymarket tem endpoint /events que agrupa mercados por evento
    r = requests.get(f'{POLY_BASE}/events', params={
        'active': 'true',
        'closed': 'false',
        'limit': 500,
        'tag': 'esports'
    })
    events = r.json() if isinstance(r.json(), list) else r.json().get('events', [])
    print(f"Events API retornou {len(events)} eventos\n")

    lol_events = []
    for ev in events:
        title = normalize(ev.get('title','') + ev.get('slug','') + ev.get('description',''))
        if ('lol' in title or 'league' in title or 'legends' in title) and ('drx' in title or 'kt' in title):
            lol_events.append(ev)

    if lol_events:
        print(f"Eventos LoL com DRX/KT: {len(lol_events)}\n")
        for ev in lol_events:
            print(f"  Titulo: {ev.get('title','')}")
            print(f"  Slug  : {ev.get('slug','')}")
            for m in ev.get('markets', []):
                slug_m = m.get('slug','')
                q_m    = m.get('question','')
                pr_m   = m.get('outcomePrices','[]')
                liq_m  = float(m.get('liquidityNum', m.get('liquidity',0)))
                print(f"    -> {slug_m} | {q_m} | Precos: {pr_m} | Liq: ${liq_m:,.2f}")
            print()
    else:
        # Ultimo recurso: lista mercados LoL (series_tag=league-of-legends)
        print("Buscando por tag 'league-of-legends'...")
        r2 = requests.get(f'{POLY_BASE}/events', params={
            'tag': 'league-of-legends', 'active': 'true', 'limit': 200
        })
        ev2 = r2.json() if isinstance(r2.json(), list) else []
        print(f"Retornou {len(ev2)} eventos com tag league-of-legends\n")
        for ev in ev2[:10]:
            t = ev.get('title','')
            sl = ev.get('slug','')
            print(f"  [{sl}] {t}")
        
        # Tenta tambem busca por texto
        print("\nBusca por texto 'DRX' nos eventos:")
        r3 = requests.get(f'{POLY_BASE}/events', params={
            'active': 'true', 'limit': 200, 'search': 'DRX Challengers'
        })
        ev3 = r3.json() if isinstance(r3.json(), list) else []
        print(f"Retornou {len(ev3)} resultados")
        for ev in ev3[:10]:
            print(f"  {ev.get('title','')[:80]} | {ev.get('slug','')}")
