import time
from thefuzz import fuzz

try:
    with open('diagnostics_output.txt', 'r') as f:
        t = f.read()
except:
    pass

import random, string
ks = [''.join(random.choices(string.ascii_lowercase, k=10)) for _ in range(500)]
ps = [''.join(random.choices(string.ascii_lowercase, k=10)) for _ in range(665)]

t0 = time.time()
matches = 0
for k in ks:
    for p in ps:
        if fuzz.token_set_ratio(k, p) > 85:
            matches += 1

print(f"Time for 500x665 token_set_ratio comparisons: {time.time() - t0:.3f}s")
