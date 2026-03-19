import requests
import json

GAMMA_HOST = "https://gamma-api.polymarket.com"

def check():
    try:
        resp = requests.get(
            f"{GAMMA_HOST}/events",
            params={
                "active":    "true",
                "closed":    "false",
                "limit":     50,
                "order":     "startDate",
                "ascending": "false",
            },
            timeout=10,
        )
        data = resp.json()
        events = data if isinstance(data, list) else data.get("events", [])
        print(f"Total events found: {len(events)}")
        for e in events[:5]:
            print(f"Slug: {e.get('slug')} | Markets: {len(e.get('markets', []))}")
            for m in e.get('markets', []):
                print(f"  - Vol: {m.get('volume')} | Question: {m.get('question')}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check()
