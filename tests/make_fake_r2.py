"""Builds a small fake R2 folder (manifest.json, p/<fp>/bNNN.pack, recent.json, r/<token>.pack) with the REAL pack encoder
(convert.py), for browser-side tests of amibroker-web-engine.   python tests/make_fake_r2.py <out_dir> [stocks] [bars]
Also writes <out_dir>/expected.json: bars and sum of closes per stock (what iterMany must give back)."""
import hashlib, json, os, sys
import numpy as np
import pandas                                    # (before the stand-ins are put on the path)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend")); sys.path.insert(0, os.path.join(ROOT, "tests", "stubs"))
os.environ.setdefault("DB_DIR", "/tmp/_x"); os.environ.setdefault("CA_DIR", "/tmp/_x")
import convert

out = sys.argv[1]; N = int(sys.argv[2]) if len(sys.argv) > 2 else 240; L = int(sys.argv[3]) if len(sys.argv) > 3 else 3000
RECENT, BUCKET, OVERLAP = 400, 64, 300
rng = np.random.default_rng(11)
base = os.path.join(out, "nse"); os.makedirs(base, exist_ok=True)
days = np.arange(10957, 10957 + L * 7 // 5, dtype="int64"); days = days[(days % 7) < 5][:L]
dates = [str(np.datetime64(int(d), "D")) for d in days]
syms = [f"S{i:04d}" for i in range(N)]
exp, hist_blobs, rec_blobs = {}, {}, {}
for s in syms:
    c = np.maximum(1, 100 * np.exp(np.cumsum(rng.normal(0, 0.02, L)))).round(2)
    o = (c * (1 + rng.normal(0, 0.005, L))).round(2)
    h = np.maximum(np.maximum(o, c), c * 1.01).round(2); l = np.minimum(np.minimum(o, c), c * 0.99).round(2)
    v = rng.lognormal(10, 1.2, L).round(); dq = (v * 0.5).round(); dq[rng.random(L) < 0.2] = np.nan
    enc = lambda a, b: convert.encode_pack_block(dates[a:b], o[a:b], h[a:b], l[a:b], c[a:b], v[a:b], dq[a:b])
    hist_blobs[s] = (enc(0, L - RECENT + OVERLAP), L - RECENT + OVERLAP)       # history ends 100 bars before the end; recent overlaps it
    rec_blobs[s] = (enc(L - RECENT, L), RECENT)
    exp[s] = {"n": L, "sumClose": float(c.sum()), "lastClose": float(c[-1])}

buckets_m, pbuckets, fp = {}, {}, hashlib.sha1(b"fake").hexdigest()
fp10 = fp[:10]; os.makedirs(os.path.join(base, "p", fp10), exist_ok=True); os.makedirs(os.path.join(base, "r"), exist_ok=True)
for bi in range(0, N, BUCKET):
    names = syms[bi:bi + BUCKET]; bn = f"b{bi // BUCKET:03d}"; off, idx, parts = 0, {}, []
    for s in names:
        blob, n = hist_blobs[s]; idx[s] = [off, len(blob), n]; parts.append(blob); off += len(blob)
    open(os.path.join(base, "p", fp10, bn + ".pack"), "wb").write(b"".join(parts))
    pbuckets[bn] = {"size": off, "idx": idx}; buckets_m[bn] = {"symbols": names, "size": 0, "rows": 0}
manifest = {"format": 2, "db": "nse", "firstYear": 2000, "histThrough": 2025, "currentYear": 2026, "fingerprint": fp,
            "histPath": f"h/{fp10}", "bucketSize": BUCKET, "buckets": buckets_m,
            "packs": {"version": convert.PACK_VERSION, "path": f"p/{fp10}", "buckets": pbuckets}, "aliases": {"OLDNAME": syms[0]}, "indices": []}
json.dump(manifest, open(os.path.join(base, "manifest.json"), "w"))
idx, parts, off = {}, [], 0
for s in syms:
    blob, n = rec_blobs[s]; idx[s] = [off, len(blob), n]; parts.append(blob); off += len(blob)
pack = b"".join(parts); token = hashlib.sha1(pack).hexdigest()[:12]
open(os.path.join(base, "r", token + ".pack"), "wb").write(pack)
json.dump({"format": 1, "db": "nse", "token": token, "file": f"r/{token}.pack", "size": len(pack), "bars": RECENT, "currentYear": 2026,
           "stocks": N, "built": "2026-10-29T00:00:00+00:00", "idx": idx}, open(os.path.join(base, "recent.json"), "w"))
json.dump(exp, open(os.path.join(out, "expected.json"), "w"))
print(f"fake R2: {N} stocks x {L} bars; history packs {sum(b['size'] for b in pbuckets.values()) / 1e6:.1f} MB, recent pack {len(pack) / 1e6:.1f} MB")
