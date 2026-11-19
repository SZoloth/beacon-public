"""Nearest neighbours by EmbeddingGemma 2, on this Mac.

    embed_gemma.py in.json out.json

in.json: {"texts": [...], "queries": [index, ...], "k": 30, "among": N}. out.json: for each query, its k
most similar other texts as [[index, cosine], ...]. With "among", only the first N texts can be
neighbours. Run through uv by scripts/routing_eval.py, so Beacon itself stays stdlib-only.
"""
import json
import sys

import numpy as np
from sentence_transformers import SentenceTransformer

job = json.load(open(sys.argv[1]))
model = SentenceTransformer("google/embeddinggemma-2")
vectors = model.encode(job["texts"], batch_size=32, normalize_embeddings=True, show_progress_bar=True)
k = job.get("k", 30)
among = job.get("among", len(vectors))
out = []
for query in job["queries"]:
    scores = vectors[:among] @ vectors[query]
    if query < among:
        scores[query] = -1  # never its own neighbour
    top = np.argsort(-scores)[:k]
    out.append([[int(i), round(float(scores[i]), 4)] for i in top])
json.dump(out, open(sys.argv[2], "w"))
