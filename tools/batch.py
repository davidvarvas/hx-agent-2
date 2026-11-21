"""Run N sim matches in parallel: python3 tools/batch.py N -- cmd..."""
import json, subprocess, sys, os
from concurrent.futures import ThreadPoolExecutor
n = int(sys.argv[1]); cmd = sys.argv[3:]
start = int(os.environ.get("SEED0", "100"))
def one(s):
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "sim.py"), "--seed", str(s), "--"] + cmd, capture_output=True, text=True)
    try: return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception: return {"error": r.stderr[-500:]}
with ThreadPoolExecutor(int(os.environ.get("J", "8"))) as ex:
    res = list(ex.map(one, range(start, start + n)))
for r in res: print(r)
ok = [r for r in res if "points" in r]
print("MEAN", round(sum(r["points"] for r in ok) / max(1, len(ok)), 2), "kills", round(sum(r["kills"] for r in ok)/max(1,len(ok)),2), "t", round(sum(r["t"] for r in ok)/max(1,len(ok)),1), "causes", {c: sum(r["cause"]==c for r in ok) for c in ("alive","zone","killed")}, "maxms", max((r["maxms"] for r in ok), default=0), "late", sum(r["late"] for r in ok))
