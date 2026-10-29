"""Writes a fixture of pack blocks (v1 and v2) with their expected values, for the browser-side decoder test
(amibroker-web-engine/tests/pack-v2-test.mjs). Run:  python tests/make_pack_fixture.py <output.json>
Also prints the size of v2 against v1 on synthetic data (real NSE data was measured ~25% smaller)."""
import base64, json, os, sys, zlib
import numpy as np
import pandas                                   # (before the stand-ins are put on the path)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend")); sys.path.insert(0, os.path.join(ROOT, "tests", "stubs"))   # stand-ins for pyarrow/boto3 (not needed here)
os.environ.setdefault("DB_DIR", "/tmp/_x"); os.environ.setdefault("CA_DIR", "/tmp/_x")
import convert

rng = np.random.default_rng(7)
def series(n, dec=2, delivery="some", big=False):
    days = np.arange(10957, 10957 + n * 2, 2)[:n]                     # 2000-01-01 onwards, gaps like weekends
    c = np.maximum(1, 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))).round(dec)
    o = (c * (1 + rng.normal(0, 0.005, n))).round(dec)
    h = np.maximum(np.maximum(o, c), (c * (1 + abs(rng.normal(0, 0.01, n)))).round(dec))
    l = np.minimum(np.minimum(o, c), (c * (1 - abs(rng.normal(0, 0.01, n)))).round(dec))
    v = (rng.lognormal(10, 1.2, n) * (50000 if big else 1)).round().astype("float64")
    dq = (v * rng.uniform(0.2, 0.8, n)).round()
    if delivery == "some": dq[rng.random(n) < 0.3] = np.nan
    if delivery == "none": dq[:] = np.nan
    return [str(np.datetime64(int(d), "D")) for d in days], o, h, l, c, v, dq

cases = {
    "typical_1500": series(1500),
    "short_1": series(1),
    "no_delivery": series(300, delivery="none"),
    "four_decimals": series(400, dec=4),
    "huge_volume_falls_back_to_v1": series(300, big=True),
}
out, sizes = {}, {}
for name, (d, o, h, l, c, v, dq) in cases.items():
    b2 = convert.encode_pack_block(d, o, h, l, c, v, dq)
    b1 = convert.encode_pack_block_v1(d, o, h, l, c, v, dq)
    ver = zlib.decompress(b2)[2]
    sizes[name] = (len(b1), len(b2), ver)
    out[name] = {"block": base64.b64encode(b2).decode(), "version": ver, "v1block": base64.b64encode(b1).decode(),
                 "dates": d, "o": o.tolist(), "h": h.tolist(), "l": l.tolist(), "c": c.tolist(), "v": v.tolist(),
                 "dq": [None if x != x else x for x in dq.tolist()]}
json.dump(out, open(sys.argv[1], "w"))
for k, (a, b, ver) in sizes.items():
    print(f"{k:32s} v1 {a:8d} B   written v{ver} {b:8d} B   {100 * (1 - b / a):5.1f}% smaller")
assert sizes["huge_volume_falls_back_to_v1"][2] == 1 and sizes["typical_1500"][2] == 2
