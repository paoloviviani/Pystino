"""Quality check: run the real build_detector() over the labelled set.

Env selects the engine exactly as the service would (REDACTION_NLP_ENGINE, ...).
Usage: python quality.py <label>  -> writes quality-<label>.json
"""
import json, sys, collections
import common  # noqa
from quality_set import load
from llmp_redaction.app import build_detector

label = sys.argv[1]
threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
det = build_detector()
GOLD_TYPES = ["PERSON", "LOCATION", "ORGANIZATION", "DATE_TIME", "IT_FISCAL_CODE",
              "HEALTH_INSURANCE_ID", "MEDICAL_CONDITION", "MEDICATION"]

def overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]

stats = {t: collections.Counter() for t in GOLD_TYPES}
misses, fps = [], []
for text, gold in load():
    preds = [(s.start, s.end, s.entity_type, s.score)
             for s in det.analyse_one(text, language="it", score_threshold=threshold, entity_types=None)]
    for g in gold:
        stats[g[2]]["gold"] += 1
        hit = [p for p in preds if p[2] == g[2] and overlap(g, p)]
        exact = [p for p in hit if (p[0], p[1]) == (g[0], g[1])]
        if hit:
            stats[g[2]]["tp"] += 1
        else:
            other = [p[2] for p in preds if overlap(g, p)]
            misses.append((g[2], text[g[0]:g[1]], other))
        if exact:
            stats[g[2]]["exact"] += 1
    for p in preds:
        if p[2] not in GOLD_TYPES:
            continue
        stats[p[2]]["pred"] += 1
        if not any(g[2] == p[2] and overlap(g, p) for g in gold):
            stats[p[2]]["fp"] += 1
            fps.append((p[2], text[p[0]:p[1]], round(p[3], 2)))
out = {"label": label, "threshold": threshold,
       "stats": {t: dict(c) for t, c in stats.items()}, "misses": misses, "fps": fps}
json.dump(out, open(f"quality-{label}.json", "w"), ensure_ascii=False, indent=1)
tot = collections.Counter()
for t in GOLD_TYPES:
    c = stats[t]; tot.update(c)
    r = c["tp"] / c["gold"] if c["gold"] else 0
    p = (c["pred"] - c["fp"]) / c["pred"] if c["pred"] else float("nan")
    print(f"{t:20s} gold={c['gold']:3d} recall={r:.2f} exact={c['exact']:2d} pred={c['pred']:3d} fp={c['fp']:3d} prec={p:.2f}")
print("TOTAL recall=%.2f precision=%.2f" % (tot["tp"] / tot["gold"], (tot["pred"] - tot["fp"]) / max(tot["pred"], 1)))
