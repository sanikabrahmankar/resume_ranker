import io
import json
import logging
import os
import re
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3
import numpy as np
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()  # must run before anything that reads env at import time

from flask import Flask, jsonify, request  # noqa: E402
from flask_cors import CORS  # noqa: E402
from pymongo import ASCENDING, DESCENDING, MongoClient  # noqa: E402
from werkzeug.utils import secure_filename  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ats")


def require_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BUCKET_NAME = require_env("S3_BUCKET_NAME")
QUEUE_URL = require_env("SQS_QUEUE_URL")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
PRESIGN_TTL = int(os.getenv("PRESIGN_TTL_SECONDS", "300"))
MAX_FILES = int(os.getenv("MAX_FILES_PER_UPLOAD", "50"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "10"))
UPLOAD_THREADS = int(os.getenv("UPLOAD_THREADS", "8"))

# Credentials come from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars or an IAM role.
s3 = boto3.client("s3", region_name=AWS_REGION, config=Config(signature_version="s3v4"))
sqs = boto3.client("sqs", region_name=AWS_REGION)

mongo = MongoClient(require_env("MONGO_URI"))
db = mongo[os.getenv("MONGO_DB", "resume_ranker_db")]
jobs = db["jobs"]
candidates = db["candidates"]

jobs.create_index("job_id", unique=True)
jobs.create_index([("created_at", DESCENDING)])
candidates.create_index("candidate_id", unique=True)
candidates.create_index([("job_id", ASCENDING), ("Match_Score", DESCENDING)])

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILES * MAX_FILE_MB * 1024 * 1024
CORS(app, origins=os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","))


def now():
    return datetime.now(timezone.utc).isoformat()


def error(message, code):
    return jsonify({"status": "error", "message": message}), code


@app.errorhandler(413)
def too_large(_):
    return error(f"Upload too large (max {MAX_FILES} files, {MAX_FILE_MB} MB each)", 413)


# ---------------------------------- helpers ----------------------------------

def _put_object(job_id, name, data, uploaded):
    """Stream one PDF from memory to S3 under a UUID key (no local disk)."""
    file_id = uuid.uuid4().hex
    key = f"resumes/{job_id}/{file_id}.pdf"
    s3.upload_fileobj(
        io.BytesIO(data),
        BUCKET_NAME,
        key,
        ExtraArgs={"ContentType": "application/pdf", "ServerSideEncryption": "AES256"},
    )
    uploaded.append(key)
    return file_id, key, name


def _cleanup_objects(keys):
    for key in keys:
        try:
            s3.delete_object(Bucket=BUCKET_NAME, Key=key)
        except Exception:
            log.exception("Could not delete orphaned object %s", key)


# ---------------------------------- routes ----------------------------------

@app.post("/api/upload")
def upload_and_rank():
    jd = (request.form.get("job_description") or "").strip()
    if not jd:
        return error("job_description is required", 400)

    files = [f for f in request.files.getlist("resumes") if f.filename]
    if not files:
        return error("No files uploaded", 400)
    if len(files) > MAX_FILES:
        return error(f"Too many files (max {MAX_FILES})", 400)

    payloads, invalid = [], []
    for f in files:
        name = secure_filename(f.filename) or "resume.pdf"
        data = f.read()  # straight into memory; nothing touches local disk
        if len(data) > MAX_FILE_MB * 1024 * 1024 or b"%PDF" not in data[:1024]:
            invalid.append(name)
        else:
            payloads.append((name, data))
    if invalid:
        return error(f"Not a valid PDF or exceeds {MAX_FILE_MB} MB: {', '.join(invalid)}", 400)

    job_id = uuid.uuid4().hex
    target_job = re.sub(r"\s+", " ", jd)
    target_job = target_job[:30] + "..." if len(target_job) > 30 else target_job

    jobs.insert_one({
        "job_id": job_id, "status": "queued", "target_job": target_job,
        "job_description": jd, "total": len(payloads), "processed": 0,
        "failed_files": [], "created_at": now(),
    })

    uploaded = []
    try:
        with ThreadPoolExecutor(max_workers=UPLOAD_THREADS) as pool:
            stored = list(pool.map(lambda p: _put_object(job_id, p[0], p[1], uploaded), payloads))

        entries = [
            {"Id": file_id, "MessageBody": json.dumps(
                {"job_id": job_id, "file_id": file_id, "s3_key": key, "filename": name})}
            for file_id, key, name in stored
        ]
        for i in range(0, len(entries), 10):  # SQS batch limit is 10
            resp = sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=entries[i:i + 10])
            if resp.get("Failed"):
                raise RuntimeError(f"enqueue failed: {resp['Failed']}")
    except Exception:
        log.exception("Enqueue failed for job %s", job_id)
        _cleanup_objects(uploaded)
        jobs.update_one({"job_id": job_id}, {"$set": {"status": "failed", "error": "Could not enqueue job"}})
        return error("Could not enqueue job", 500)

    resp = jsonify({"status": "accepted", "job_id": job_id})
    resp.status_code = 202
    resp.headers["Location"] = f"/api/jobs/{job_id}"
    return resp


JOB_FIELDS = {"_id": 0, "job_description": 0}


@app.get("/api/jobs")
def list_jobs():
    rows = list(jobs.find({}, JOB_FIELDS).sort("created_at", -1).limit(100))
    return jsonify({"status": "success", "data": rows})


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    job = jobs.find_one({"job_id": job_id}, JOB_FIELDS)
    if not job:
        return error("Job not found", 404)
    return jsonify({"status": "success", "data": job})


@app.get("/api/jobs/<job_id>/candidates")
def get_job_candidates(job_id):
    if not jobs.find_one({"job_id": job_id}, {"_id": 1}):
        return error("Job not found", 404)
    rows = list(
        candidates.find({"job_id": job_id}, {"_id": 0, "S3_Key": 0, "Requirements": 0})
        .sort("Match_Score", -1)
    )
    return jsonify({"status": "success", "data": rows})


@app.get("/api/candidates/<candidate_id>/resume-url")
def get_resume_url(candidate_id):
    doc = candidates.find_one({"candidate_id": candidate_id}, {"S3_Key": 1})
    if not doc:
        return error("Candidate not found", 404)
    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": doc["S3_Key"],
            "ResponseContentType": "application/pdf",
            "ResponseContentDisposition": "inline",
        },
        ExpiresIn=PRESIGN_TTL,
    )
    return jsonify({"status": "success", "url": url, "expires_in": PRESIGN_TTL})


@app.get("/api/candidates/<candidate_id>/evidence")
def get_evidence(candidate_id):
    doc = candidates.find_one({"candidate_id": candidate_id}, {"Requirements": 1, "Requirement_Coverage": 1})
    if not doc:
        return error("Candidate not found", 404)
    return jsonify({"status": "success", "data": {
        "coverage": doc.get("Requirement_Coverage"), "requirements": doc.get("Requirements", [])}})


@app.post("/api/talent-pool/search")
def talent_pool_search():
    from ranker import build_jd_vector  # lazy: keeps torch/spaCy out of API boot

    payload = request.get_json(silent=True) or request.form
    jd_text = (payload.get("job_description") or "").strip()
    if not jd_text:
        return error("job_description is required", 400)

    try:
        top_candidates = max(1, min(int(request.args.get("limit", 20)), 100))
    except ValueError:
        top_candidates = 20

    q = build_jd_vector(jd_text).tolist()

    hits = db["chunks"].aggregate([
        {"$vectorSearch": {
            "index": "chunk_vec",
            "path": "embedding",
            "queryVector": q,
            "numCandidates": 400,
            "limit": 100,
        }},
        {"$project": {"_id": 0, "candidate_id": 1, "text": 1, "s": {"$meta": "vectorSearchScore"}}},
    ])

    by_c = defaultdict(list)
    for h in hits:
        by_c[h["candidate_id"]].append((2 * h["s"] - 1, h["text"]))  # Atlas score -> cosine

    people = {
        c["candidate_id"]: c
        for c in candidates.find(
            {"candidate_id": {"$in": list(by_c)}}, {"_id": 0, "candidate_id": 1, "Candidate": 1, "Email": 1}
        )
    }

    ranked = []
    for cid, lst in by_c.items():
        lst.sort(reverse=True)
        cand = people.get(cid, {})
        ranked.append({
            "candidate_id": cid,
            "Candidate": cand.get("Candidate", "Unknown"),
            "Email": cand.get("Email", "N/A"),
            "score": round(100 * float(np.mean([s for s, _ in lst[:3]])), 1),
            "best_chunk": lst[0][1][:300] + "...",
        })

    ranked = sorted(ranked, key=lambda r: -r["score"])[:top_candidates]
    return jsonify({"status": "success", "data": ranked})


if __name__ == "__main__":
    # API only. Run `python worker.py` separately; jobs stay 'queued' without it.
    # Prod (Windows): waitress-serve --listen=127.0.0.1:5000 --threads=8 app:app
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG") == "1",
        use_reloader=False,
        threaded=True,
    )