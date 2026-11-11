import json
import logging
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache

import boto3
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()  # must run before importing ranker/extractor (they read env at import time)

from pymongo import MongoClient, ReplaceOne  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

from extractor import PDFExtractionError, extract_text_from_pdf  # noqa: E402
from ranker import analyze_resume, build_jd_vector  # noqa: E402
from requirements import extract_requirements  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")

# Optional: when running several worker processes, give each a slice of the CPU cores.
_torch_threads = os.getenv("TORCH_THREADS")
if _torch_threads:
    import torch
    torch.set_num_threads(int(_torch_threads))

BUCKET = os.environ["S3_BUCKET_NAME"]
QUEUE_URL = os.environ["SQS_QUEUE_URL"]
REGION = os.getenv("AWS_REGION", "ap-south-1")
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
BATCH_SIZE = min(max(int(os.getenv("SQS_BATCH_SIZE", "5")), 1), 10)
PREFETCH_THREADS = int(os.getenv("PREFETCH_THREADS", "4"))

s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
sqs = boto3.client("sqs", region_name=REGION)
db = MongoClient(os.environ["MONGO_URI"])[os.getenv("MONGO_DB", "resume_ranker_db")]
jobs, candidates, chunks = db["jobs"], db["candidates"], db["chunks"]

now = lambda: datetime.now(timezone.utc).isoformat()

_started_jobs = set()  # jobs already flipped queued -> processing by this process


@lru_cache(maxsize=32)
def job_context(job_id):
    job = jobs.find_one({"job_id": job_id}, {"job_description": 1, "target_job": 1})
    if not job:
        raise RuntimeError(f"Job {job_id} not found")
    jd = job["job_description"]
    return build_jd_vector(jd), extract_requirements(jd), job["target_job"]


def refresh_job(job_id):
    job = jobs.find_one({"job_id": job_id}, {"total": 1, "failed_files": 1})
    if not job:
        return
    n_failed = len(job.get("failed_files", []))
    done = candidates.count_documents({"job_id": job_id}) + n_failed
    update = {"processed": done}
    if done >= job["total"]:
        update["status"] = "failed" if n_failed >= job["total"] else "completed"
        update["finished_at"] = now()
    jobs.update_one({"job_id": job_id}, {"$set": update})


def prepare(body):
    """I/O stage (runs in a thread pool): skip if already done, download, extract text."""
    if candidates.find_one({"candidate_id": body["file_id"]}, {"_id": 1}):
        return None
    t0 = time.perf_counter()
    data = s3.get_object(Bucket=BUCKET, Key=body["s3_key"])["Body"].read()
    text, method = extract_text_from_pdf(data)
    return text, method, time.perf_counter() - t0


def handle(body, prepared):
    """Inference + persistence stage (main thread)."""
    job_id, file_id = body["job_id"], body["file_id"]

    if job_id not in _started_jobs:
        jobs.update_one({"job_id": job_id, "status": "queued"}, {"$set": {"status": "processing"}})
        _started_jobs.add(job_id)

    if prepared is not None:
        text, method, prep_s = prepared
        t0 = time.perf_counter()
        jd_vec, reqs, target = job_context(job_id)
        analysis = analyze_resume(jd_vec, reqs, text)

        # Raw vectors go to the dedicated 'chunks' collection, not the candidate document.
        chunk_list = analysis.pop("_chunks", [])
        chunk_vecs = analysis.pop("_chunk_vecs", [])

        # Chunks first, candidate last: the candidate doc is the commit marker, so a retry after a
        # partial failure redoes the chunks. Deterministic _ids + upsert make this idempotent.
        if len(chunk_list) > 0:
            chunks.bulk_write([
                ReplaceOne(
                    {"_id": f"{file_id}:{i}"},
                    {"candidate_id": file_id, "job_id": job_id, "chunk_idx": i,
                     "text": c, "embedding": v.tolist()},
                    upsert=True,
                )
                for i, (c, v) in enumerate(zip(chunk_list, chunk_vecs))
            ], ordered=False)

        try:
            candidates.insert_one({
                "candidate_id": file_id, "job_id": job_id, "Candidate": body["filename"],
                "Target_Job": target, "S3_Key": body["s3_key"], "Extraction": method,
                "created_at": now(),
                "processing_ms": int((prep_s + time.perf_counter() - t0) * 1000),
                **analysis,
            })
        except DuplicateKeyError:
            pass  # another delivery won the race (idempotent design)

    refresh_job(job_id)


def fail(body, reason):
    try:
        s3.delete_object(Bucket=BUCKET, Key=body["s3_key"])
    except Exception:
        log.exception("Could not delete failed object %s", body["s3_key"])
    jobs.update_one(
        {"job_id": body["job_id"], "failed_files.file_id": {"$ne": body["file_id"]}},
        {"$push": {"failed_files": {"file_id": body["file_id"], "filename": body["filename"], "reason": reason}}},
    )
    refresh_job(body["job_id"])


running = True


def _stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def delete_messages(msgs):
    for i in range(0, len(msgs), 10):
        part = msgs[i:i + 10]
        try:
            resp = sqs.delete_message_batch(
                QueueUrl=QUEUE_URL,
                Entries=[{"Id": str(j), "ReceiptHandle": m["ReceiptHandle"]} for j, m in enumerate(part)],
            )
            if resp.get("Failed"):
                log.warning("Some deletes failed: %s", resp["Failed"])
        except Exception:
            log.exception("delete_message_batch failed; messages will be redelivered")


def process_batch(pool, messages):
    items, done = [], []
    for m in messages:
        try:
            body = json.loads(m["Body"])
            for key in ("job_id", "file_id", "s3_key", "filename"):
                body[key]
        except (ValueError, KeyError, TypeError):
            log.error("Dropping malformed message %s", m.get("MessageId"))
            done.append(m)
            continue
        # Download + extraction for the whole batch overlaps with inference on earlier messages.
        items.append((m, body, pool.submit(prepare, body)))

    try:
        for m, body, fut in items:
            if not running:
                break  # unprocessed messages reappear after the visibility timeout
            attempt = int(m.get("Attributes", {}).get("ApproximateReceiveCount", 1))
            try:
                handle(body, fut.result())
            except PDFExtractionError as e:
                fail(body, str(e))
            except Exception:
                log.exception("Attempt %s failed for %s", attempt, body.get("file_id"))
                if attempt >= MAX_ATTEMPTS:
                    fail(body, "Internal processing error")
                else:
                    continue  # leave on queue for redelivery
            done.append(m)
    finally:
        for _, _, fut in items:
            fut.cancel()
        delete_messages(done)


def main():
    log.info("Worker started (batch=%s, prefetch threads=%s). Polling SQS...", BATCH_SIZE, PREFETCH_THREADS)
    with ThreadPoolExecutor(max_workers=PREFETCH_THREADS, thread_name_prefix="prefetch") as pool:
        while running:
            try:
                resp = sqs.receive_message(
                    QueueUrl=QUEUE_URL,
                    MaxNumberOfMessages=BATCH_SIZE,
                    WaitTimeSeconds=20,
                    MessageSystemAttributeNames=["ApproximateReceiveCount"],
                )
            except Exception:
                log.exception("SQS receive failed; retrying in 5s")
                time.sleep(5)
                continue
            messages = resp.get("Messages", [])
            if messages:
                try:
                    process_batch(pool, messages)
                except Exception:
                    log.exception("Batch processing error")
    log.info("Worker stopped.")


if __name__ == "__main__":
    main()