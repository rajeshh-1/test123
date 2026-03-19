import requests, json
POLY_BASE = 'https://gamma-api.polymarket.com'

print('Fetching active Polymarket markets...')
params = {
    'limit': 1000,
    'active': 'true',
    'closed': 'false'
}
r = requests.get(f'{POLY_BASE}/markets', params=params)
markets = r.json()

tags_found = {}
categories_found = set()

for m in markets:
    cat = m.get('category')
    if cat: categories_found.add(cat)
    for t in m.get('tags', []):
        t_id = t.get('id')
        t_label = t.get('label')
        if t_id not in tags_found:
            tags_found[t_id] = t_label

print(f'Categories found: {categories_found}')
print(f'Sample Tags: {list(tags_found.items())[:20]}')

nba_matches = []
esports_matches = []

for m in markets:
    q = m.get('question', '').lower()
    
    if 'nba' in q or 'basketball' in q or 'lakers' in q or 'celtics' in q:
        nba_matches.append({'id': m.get('id'), 'q': m.get('question'), 'tags': m.get('tags')})
        
    if 'lol' in q or 'league of legends' in q or 'cs2' in q or 'valorant' in q or 'esports' in q:
        esports_matches.append({'id': m.get('id'), 'q': m.get('question'), 'tags': m.get('tags')})

print(f'\\nNBA/Basketball string matches: {len(nba_matches)}')
if nba_matches:
    print(f'Sample: {nba_matches[0]["q"]}')
    print(f'Tags: {nba_matches[0].get("tags")}')

print(f'\\neSports string matches: {len(esports_matches)}')
if esports_matches:
    print(f'Sample: {esports_matches[0]["q"]}')
    print(f'Tags: {esports_matches[0].get("tags")}')
