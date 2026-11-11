import os
import re
import threading

import numpy as np
import spacy
from sentence_transformers import SentenceTransformer

CHUNK_TOKENS = int(os.getenv("CHUNK_TOKENS", "200"))    # model limit is 256 incl. special tokens
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "50"))
TOP_K_CHUNKS = int(os.getenv("TOP_K_CHUNKS", "3"))
assert 0 <= CHUNK_OVERLAP < CHUNK_TOKENS <= 250, "invalid chunk settings"

nlp = spacy.load("en_core_web_sm", disable=["parser", "ner"])

print("Loading Semantic Model (this takes a few seconds)...")
model = SentenceTransformer("all-MiniLM-L6-v2")
tokenizer = model.tokenizer
print("Model loaded successfully!")

_encode_lock = threading.Lock()  # one inference at a time; torch already uses all cores


def _embed(texts):
    with _encode_lock:
        return model.encode(texts, normalize_embeddings=True, batch_size=16)


def chunk_text(text):
    """Split into overlapping windows of CHUNK_TOKENS tokens (sliced by char offsets)."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    offsets = tokenizer(
        text, add_special_tokens=False, return_offsets_mapping=True, verbose=False
    )["offset_mapping"]
    n, step = len(offsets), CHUNK_TOKENS - CHUNK_OVERLAP
    chunks = []
    for start in range(0, n, step):
        end = min(start + CHUNK_TOKENS, n)
        chunks.append(text[offsets[start][0]:offsets[end - 1][1]])
        if end == n:
            break
    return chunks


def build_jd_vector(job_description):
    """Mean of normalized JD chunk embeddings (JD may also exceed 256 tokens)."""
    chunks = chunk_text(job_description)
    if not chunks:
        raise ValueError("Empty job description")
    mean = _embed(chunks).mean(axis=0)
    return mean / np.linalg.norm(mean)


def extract_contact_info(text):
    emails = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', text)
    phones = re.findall(r'\(?\b[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}\b', text)
    return (emails[0] if emails else "N/A"), (phones[0] if phones else "N/A")


def _keywords(text):
    doc = nlp(text.lower()[:200_000])
    return list(dict.fromkeys(
        t.text for t in doc if t.pos_ in ("NOUN", "PROPN") and not t.is_stop and t.is_alpha
    ))


def extract_jd_keywords(job_description):
    return _keywords(job_description)


def analyze_resume(jd_vector, requirements, raw_text):
    chunks = chunk_text(raw_text)
    if not chunks:
        raise ValueError("Resume contains no text")
    chunk_vecs = _embed(chunks)
    score = float(np.sort(chunk_vecs @ jd_vector)[-min(TOP_K_CHUNKS, len(chunks)):].mean())

    units = evidence_units(raw_text)
    matches = match_requirements(requirements, units, _embed(units)) if units else []
    missing = [m["text"][:40] for m in matches if m["status"] == "missing"][:3]
    email, phone = extract_contact_info(raw_text)
    return {
        "Email": email,
        "Phone": phone,
        "Missing_Skills": ", ".join(missing) if missing else "None",
        "Match_Score": round(max(0.0, score) * 100, 1),
        "Requirement_Coverage": coverage(matches),
        "Requirements": matches,
        "Chunks_Scored": len(chunks),
        "_chunks": chunks,
        "_chunk_vecs": chunk_vecs,
    }

T_STRONG, T_PARTIAL = 0.50, 0.35   # initial guesses; calibrate in Step 5

def evidence_units(raw_text):
    units = [u.strip() for u in re.split(r"\n|(?<=[.;•])\s", raw_text)]
    return [u for u in units if len(u.split()) >= 4][:300]

def match_requirements(reqs, units, unit_vecs):
    if not reqs or len(units) == 0:
        return []
    sims = _embed([r["text"] for r in reqs]) @ unit_vecs.T       # (n_req, n_units)
    out = []
    for r, row in zip(reqs, sims):
        j = int(row.argmax()); s = float(row[j])
        status = "strong" if s >= T_STRONG else "partial" if s >= T_PARTIAL else "missing"
        out.append({**r, "score": round(s, 3), "status": status,
                    "evidence": units[j][:300] if status != "missing" else None})
    return out

def coverage(matches):
    pts = {"strong": 1.0, "partial": 0.5, "missing": 0.0}
    total = sum(m["weight"] for m in matches) or 1
    return round(100 * sum(m["weight"] * pts[m["status"]] for m in matches) / total, 1)