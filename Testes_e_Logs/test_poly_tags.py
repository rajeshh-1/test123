import requests
import json

base = "https://gamma-api.polymarket.com/events"
r = requests.get(base, params={"tag_slug": "league-of-legends", "active": "true", "closed": "false"})
print("Tag Slug 'league-of-legends':", len(r.json() if isinstance(r.json(), list) else r.json().get('events', [])))

r = requests.get(base, params={"slug": "esports", "active": "true", "closed": "false"})
print("Tag Slug 'esports':", len(r.json() if isinstance(r.json(), list) else r.json().get('events', [])))

r = requests.get(base, params={"tag_id": "64", "active": "true", "closed": "false"})
print("Tag ID 64:", len(r.json() if isinstance(r.json(), list) else r.json().get('events', [])))

r = requests.get(base, params={"tag_id": "65", "active": "true", "closed": "false"})
print("Tag ID 65:", len(r.json() if isinstance(r.json(), list) else r.json().get('events', [])))
