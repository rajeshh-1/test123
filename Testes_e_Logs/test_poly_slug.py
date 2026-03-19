import requests
import json
import codecs

POLY_BASE = 'https://gamma-api.polymarket.com'

slug = "lol-ktc-drxc-2026-02-26"
r = requests.get(f"{POLY_BASE}/events?slug={slug}")
out = json.dumps(r.json(), indent=2)

with codecs.open("poly_slug_clean.json", "w", encoding="utf-8") as f:
    f.write(out)
