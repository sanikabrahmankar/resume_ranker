import io, sys, time, requests
from reportlab.pdfgen import canvas

API, N = sys.argv[1], int(sys.argv[2])
JD = "Looking for a software engineer with Python, AWS, Docker and machine learning pipeline experience."

def make_pdf(i):
    b = io.BytesIO(); c = canvas.Canvas(b)
    for _ in range(2):
        y = 800
        for line in range(45):
            c.drawString(50, y, f"Candidate {i} built python aws docker ml pipelines, item {line}"); y -= 16
        c.showPage()
    c.save(); return b.getvalue()

t0, job_ids = time.time(), []
for s in range(0, N, 50):  # MAX_FILES_PER_UPLOAD
    files = [("resumes", (f"r{i}.pdf", make_pdf(i), "application/pdf")) for i in range(s, min(s + 50, N))]
    r = requests.post(f"{API}/api/upload", data={"job_description": JD}, files=files)
    r.raise_for_status(); job_ids.append(r.json()["job_id"])

pending = set(job_ids)
while pending:
    for j in list(pending):
        if requests.get(f"{API}/api/jobs/{j}").json()["data"]["status"] in ("completed", "failed"):
            pending.discard(j)
    time.sleep(2)
dt = time.time() - t0
print(f"{N} resumes in {dt:.1f}s -> {N/dt:.2f} resumes/s")