import requests
import json

GAMMA_HOST = "https://gamma-api.polymarket.com"

def check_all_categories():
    try:
        resp = requests.get(
            f"{GAMMA_HOST}/events",
            params={
                "active":    "true",
                "closed":    "false",
                "limit":     100,
                "order":     "volume24hr", # Ver os mais movimentados
                "ascending": "false",
            },
            timeout=10,
        )
        data = resp.json()
        events = data if isinstance(data, list) else data.get("events", [])
        
        counts = {"cs2": 0, "nba": 0, "soccer": 0, "crypto": 0, "politics": 0, "other": 0}
        
        for e in events:
            slug = e.get('slug', '').lower()
            found = False
            for cat, keywords in {
                "cs2": ["cs2", "counter-strike"],
                "nba": ["nba", "basketball"],
                "soccer": ["soccer", "futebol", "epl", "laliga"],
                "crypto": ["crypto", "btc", "eth", "solana"],
                "politics": ["politics", "trump", "election"]
            }.items():
                if any(kw in slug for kw in keywords):
                    counts[cat] += 1
                    found = True
                    break
            if not found:
                counts["other"] += 1
        
        print(json.dumps(counts, indent=2))

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_all_categories()
