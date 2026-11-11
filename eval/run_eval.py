import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import numpy as np, pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import ndcg_score
from sentence_transformers import CrossEncoder
from ranker import chunk_text, _embed, build_jd_vector

resumes = pd.read_csv("eval/resumes.csv")   # id, text, category
jds = pd.read_csv("eval/jds.csv")           # id, text, category
rel = np.array([[2 if jc == rc else 0 for rc in resumes.category] for jc in jds.category], dtype=float)
# overlay hand labels: rel[jd_idx, resume_idx] = grade

# precompute once
chunks = [chunk_text(t) for t in resumes.text]
chunk_vecs = [_embed(c) for c in chunks]
trunc_vecs = _embed(list(resumes.text))              # default 256-token truncation = old behavior
jd_vecs = np.stack([build_jd_vector(t) for t in jds.text])
tfidf = TfidfVectorizer(stop_words="english", sublinear_tf=True).fit(resumes.text)
R = tfidf.transform(resumes.text)

def s_tfidf(j):      return (R @ tfidf.transform([jds.text[j]]).T).toarray().ravel()
def s_trunc(j):      return trunc_vecs @ jd_vecs[j]
def s_chunk(j, agg="top3"):
    out = []
    for v in chunk_vecs:
        sims = np.sort(v @ jd_vecs[j])[::-1]
        out.append(sims[0] if agg == "max" else sims.mean() if agg == "mean" else sims[:3].mean())
    return np.array(out)

ce = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", max_length=512)
def s_rerank(j, top=30, alpha=0.5):
    base = s_chunk(j); idx = np.argsort(-base)[:top]
    pairs = [(jds.text[j], chunks[i][int(np.argmax(chunk_vecs[i] @ jd_vecs[j]))]) for i in idx]
    z = lambda x: (x - x.mean()) / (x.std() + 1e-9)
    out = np.full(len(base), -1e9)
    out[idx] = alpha * z(base[idx]) + (1 - alpha) * z(ce.predict(pairs))
    return out

def mrr(r, s):
    for rank, i in enumerate(np.argsort(-s), 1):
        if r[i] > 0: return 1 / rank
    return 0.0

def evaluate(fn, js, k=10):
    nd = [ndcg_score([rel[j]], [fn(j)], k=k) for j in js]
    rr = [mrr(rel[j], fn(j)) for j in js]
    return round(np.mean(nd), 3), round(np.mean(rr), 3)

test = list(range(len(jds) // 2, len(jds)))   # dev = first half, used only for tuning
for name, fn in [("TF-IDF", s_tfidf), ("MiniLM truncated", s_trunc), ("Chunked top-3", s_chunk),
                 ("Chunked max", lambda j: s_chunk(j, "max")), ("Chunked + cross-encoder", s_rerank)]:
    print(f"{name:26s} NDCG@10, MRR = {evaluate(fn, test)}")