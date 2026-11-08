"""Latency / CPU / RSS benchmark of the detector (single process, unique text per request).
Usage: python bench.py <label>   (engine from env; threads from OMP_NUM_THREADS)
"""
import json, os, random, re, resource, statistics, sys, time
import common  # noqa
import psutil
from quality_set import load
from llmp_redaction.app import build_detector

label = sys.argv[1]
if os.environ.get("ONNX_THREADS"):
    # GLiNER builds its own InferenceSession; pin ORT's pool so "1 core" is honest.
    import onnxruntime as ort
    _orig = ort.InferenceSession.__init__
    def _init(self, path, sess_options=None, *a, **k):
        so = sess_options or ort.SessionOptions()
        so.intra_op_num_threads = int(os.environ["ONNX_THREADS"]); so.inter_op_num_threads = 1
        _orig(self, path, so, *a, **k)
    ort.InferenceSession.__init__ = _init
SIZES = {35: 12, 300: 8, 1100: 5, 4500: 3}
if os.environ.get("BENCH_SIZES"):
    SIZES = {int(k): int(v) for k, v in (p.split(":") for p in os.environ["BENCH_SIZES"].split(","))}
SURN = ["Rinaldi", "Moretti", "Gallo", "Conti", "De Santis", "Marchetti", "Bruno", "Greco", "Fontana", "Caruso", "Di Stefano", "Lombardi"]
NAMES = ["Paolo", "Sara", "Marco", "Elisa", "Davide", "Irene", "Nicola", "Giulia", "Fabio", "Laura"]
sents = [t for t, _ in load()]
tokre = re.compile(r"\w+|[^\w\s]")
rng = random.Random(7)

def make(ntok):
    parts, n = [], 0
    while n < ntok:
        s = rng.choice(sents)
        s = f"{rng.choice(NAMES)} {rng.choice(SURN)} (rif. {rng.randrange(10**7)}): " + s
        parts.append(s); n += len(tokre.findall(s))
    return " ".join(parts), n

t0 = time.monotonic()
det = build_detector()
load_s = time.monotonic() - t0
proc = psutil.Process()
rss_loaded = proc.memory_info().rss / 2**20
det.analyse_one("Mario Rossi abita a Roma.", language="it", score_threshold=0.5, entity_types=None)  # warm-up
res = {"label": label, "load_s": round(load_s, 1), "rss_loaded_mb": round(rss_loaded), "sizes": {}}
for ntok, reps in SIZES.items():
    lat, cpu, toks = [], [], []
    for _ in range(reps):
        text, n = make(ntok)
        c0 = time.process_time(); w0 = time.perf_counter()
        det.analyse_one(text, language="it", score_threshold=0.5, entity_types=None)
        lat.append((time.perf_counter() - w0) * 1000); cpu.append(time.process_time() - c0); toks.append(n)
    p50 = statistics.median(lat); nt = statistics.median(toks)
    res["sizes"][ntok] = {"tokens": nt, "p50_ms": round(p50), "ms_per_tok": round(p50 / nt, 3),
                          "cpu_s": round(statistics.median(cpu), 2), "reps": reps}
    print(label, ntok, res["sizes"][ntok], flush=True)
res["rss_end_mb"] = round(proc.memory_info().rss / 2**20)
res["rss_peak_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
json.dump(res, open(f"bench-{label}.json", "w"), indent=1)
print(label, "load", res["load_s"], "rss_loaded", res["rss_loaded_mb"], "rss_end", res["rss_end_mb"], "peak", res["rss_peak_mb"])
