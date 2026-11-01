"""Run the CLI pipeline on CPU over a folder and record real model input feeds per session kind.

Usage: python capture_feeds.py <folder> <out.pkl> [max_per_kind]
Feeds recorded per kind: detector, sam_enc, sam_dec, bird_species, siglip_image, quality (+ others if used).
"""
import collections
import os
import pickle
import sys

ANALYZER = os.path.join(os.path.dirname(__file__), "..", "..", "analyzer")
sys.path.insert(0, os.path.abspath(ANALYZER))
folder, out = sys.argv[1], sys.argv[2]
cap = int(sys.argv[3]) if len(sys.argv) > 3 else 12

from kestrel_analyzer.ml import resilient_session as rs

rec = collections.defaultdict(list)
counts = collections.Counter()
orig = rs.ResilientOnnxSession.run


def run(self, output_names, input_feed, run_options=None):
    k = self._kind
    counts[k] += 1
    if len(rec[k]) < cap:
        rec[k].append({n: v.copy() for n, v in input_feed.items()})
    return orig(self, output_names, input_feed, run_options)


rs.ResilientOnnxSession.run = run
import cli

try:
    cli.main([folder, "--no-gpu", "--parallel-prefetch", "2"])
except SystemExit:
    pass
with open(out, "wb") as f:
    pickle.dump({"feeds": dict(rec), "counts": dict(counts)}, f)
print("recorded", {k: len(v) for k, v in rec.items()}, "calls", dict(counts))
