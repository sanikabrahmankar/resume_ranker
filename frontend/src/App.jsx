import { useState, useEffect, useRef, useCallback, Fragment } from 'react';
const API = import.meta.env.VITE_API_URL || 'http://127.0.0.1:5000';
const POLL_MS = 2000;
const MAX_POLL_FAILURES = 5;

async function getJSON(path) {
  const res = await fetch(`${API}${path}`);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.message || `Request failed (${res.status})`);
  return body;
}

const BADGE = { strong: 'text-bg-success', partial: 'text-bg-warning', missing: 'text-bg-danger' };

function EvidencePanel({ candidateId }) {
  const [data, setData] = useState(null);
  
  useEffect(() => {
    getJSON(`/api/candidates/${candidateId}/evidence`)
      .then((b) => setData(b.data))
      .catch(() => setData({ requirements: [] }));
  }, [candidateId]);
  
  if (!data) return <div className="small text-muted">Loading…</div>;
  
  return (
    <div>
      <div className="small fw-bold mb-2">Requirement coverage: {data.coverage}%</div>
      {data.requirements.map((r, i) => (
        <div key={i} className="mb-2 small">
          <span className={`badge ${BADGE[r.status]} me-2`}>{r.status}</span>
          <span className="fw-semibold">{r.text}</span>
          {r.evidence && <div className="text-muted fst-italic ms-2">“{r.evidence}”</div>}
        </div>
      ))}
    </div>
  );
}

function App() {
  const [jobs, setJobs] = useState([]);
  const [activeJobId, setActiveJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [candidates, setCandidates] = useState([]);
  const [jobDescription, setJobDescription] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [openId, setOpenId] = useState(null);
  const fileRef = useRef(null);

  const loadJobs = useCallback(async () => {
    const body = await getJSON('/api/jobs');
    setJobs(body.data || []);
    return body.data || [];
  }, []);

  // Initial load: list jobs and open the most recent one.
  useEffect(() => {
    loadJobs()
      .then((list) => { if (list.length) setActiveJobId(list[0].job_id); })
      .catch((e) => setError(e.message));
  }, [loadJobs]);

  // Poll the active job until it reaches a terminal state.
  useEffect(() => {
    if (!activeJobId) return;
    let cancelled = false;
    let timer;
    let failures = 0;

    const tick = async () => {
      try {
        const [s, c] = await Promise.all([
          getJSON(`/api/jobs/${activeJobId}`),
          getJSON(`/api/jobs/${activeJobId}/candidates`),
        ]);
        if (cancelled) return;
        failures = 0;
        setJobStatus(s.data);
        setCandidates(c.data || []);
        if (s.data.status === 'completed' || s.data.status === 'failed') {
          loadJobs().catch(() => {});
          return;
        }
      } catch (e) {
        if (cancelled) return;
        failures += 1;
        if (failures >= MAX_POLL_FAILURES) {
          setError(`Lost contact with server: ${e.message}`);
          return;
        }
      }
      timer = setTimeout(tick, POLL_MS);
    };

    setCandidates([]);
    setJobStatus(null);
    setOpenId(null); // Reset open evidence panel when switching jobs
    tick();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [activeJobId, loadJobs]);

  const handleUpload = async (e) => {
    e.preventDefault();
    setError('');
    const files = fileRef.current?.files;
    if (!files || !files.length) return;

    const formData = new FormData();
    formData.append('job_description', jobDescription);
    for (const f of files) formData.append('resumes', f);

    setIsSubmitting(true);
    try {
      const res = await fetch(`${API}/api/upload`, { method: 'POST', body: formData });
      const body = await res.json().catch(() => ({}));
      if (res.status !== 202) throw new Error(body.message || `Upload failed (${res.status})`);
      setJobDescription('');
      fileRef.current.value = '';
      await loadJobs().catch(() => {});
      setActiveJobId(body.job_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setIsSubmitting(false);
    }
  };

  // Fresh short-lived pre-signed URL on every click; resumes are never publicly readable.
  const openResume = async (candidateId) => {
    const win = window.open('', '_blank'); // open synchronously to avoid popup blockers
    try {
      const body = await getJSON(`/api/candidates/${candidateId}/resume-url`);
      if (win) { win.opener = null; win.location.href = body.url; }
      else window.location.href = body.url;
    } catch (err) {
      if (win) win.close();
      setError(err.message);
    }
  };

  const inFlight = jobStatus && (jobStatus.status === 'queued' || jobStatus.status === 'processing');
  const pct = jobStatus && jobStatus.total ? Math.round((jobStatus.processed / jobStatus.total) * 100) : 0;

  return (
    <div className="bg-light min-vh-100 pb-5">
      <div className="bg-primary text-white text-center py-4 mb-4 shadow-sm">
        <h1 className="fw-bold">AI-Powered Resume Ranker</h1>
        <p className="mb-0">Enterprise ATS Dashboard</p>
      </div>

      <div className="container">
        {error && (
          <div className="alert alert-danger alert-dismissible" role="alert">
            {error}
            <button type="button" className="btn-close" onClick={() => setError('')} aria-label="Close"></button>
          </div>
        )}

        <div className="row g-4">
          <div className="col-lg-5">
            <div className="card border-0 shadow-sm p-4 h-100">
              <h4 className="fw-bold mb-3">Screen Candidates</h4>
              <form onSubmit={handleUpload}>
                <div className="mb-3">
                  <label className="form-label fw-semibold text-secondary">Job Description</label>
                  <textarea
                    className="form-control bg-light"
                    rows="5"
                    required
                    value={jobDescription}
                    onChange={(e) => setJobDescription(e.target.value)}
                    placeholder="Enter the required skills..."
                  ></textarea>
                </div>
                <div className="mb-4">
                  <label className="form-label fw-semibold text-secondary">Upload Resumes (PDF)</label>
                  <input ref={fileRef} className="form-control" type="file" accept=".pdf,application/pdf" multiple required />
                </div>
                <button type="submit" className="btn btn-primary w-100 fw-bold" disabled={isSubmitting || inFlight}>
                  {isSubmitting ? 'Submitting...' : inFlight ? 'Analysis in progress...' : 'Analyze & Rank'}
                </button>
              </form>
            </div>
          </div>

          <div className="col-lg-7">
            <div className="card border-0 shadow-sm p-4 h-100">
              <div className="d-flex justify-content-between align-items-center mb-3">
                <h4 className="fw-bold mb-0">Leaderboard</h4>
                <select
                  className="form-select form-select-sm w-auto"
                  style={{ maxWidth: '55%' }}
                  value={activeJobId || ''}
                  onChange={(e) => setActiveJobId(e.target.value || null)}
                >
                  <option value="">Select a job…</option>
                  {jobs.map((j) => (
                    <option key={j.job_id} value={j.job_id}>
                      {j.target_job} ({j.status}) · {new Date(j.created_at).toLocaleDateString()}
                    </option>
                  ))}
                </select>
              </div>

              {inFlight && (
                <div className="mb-3">
                  <div className="small text-secondary mb-1">
                    Processed {jobStatus.processed} of {jobStatus.total}
                  </div>
                  <div className="progress" style={{ height: '8px' }}>
                    <div className="progress-bar progress-bar-striped progress-bar-animated" style={{ width: `${pct}%` }}></div>
                  </div>
                </div>
              )}

              {jobStatus && jobStatus.status === 'failed' && (
                <div className="alert alert-danger py-2 small">
                  {jobStatus.error || 'All files in this job failed to process.'}
                </div>
              )}

              {jobStatus && jobStatus.failed_files && jobStatus.failed_files.length > 0 && (
                <div className="alert alert-warning py-2 small">
                  <strong>{jobStatus.failed_files.length} file(s) could not be processed:</strong>
                  <ul className="mb-0">
                    {jobStatus.failed_files.map((f, i) => (
                      <li key={i}>{f.filename} — {f.reason}</li>
                    ))}
                  </ul>
                </div>
              )}

              {candidates.length > 0 ? (
                <div className="table-responsive">
                  <table className="table table-hover align-middle">
                    <thead className="bg-light">
                      <tr>
                        <th>#</th>
                        <th>Candidate</th>
                        <th>Match Score</th>
                      </tr>
                    </thead>
                    <tbody>
                      {candidates.map((c, index) => (
                        <Fragment key={c.candidate_id}>
                          <tr>
                            <td className="text-muted">{index + 1}</td>
                            <td className="fw-semibold">
                              <button
                                type="button"
                                className="btn btn-link p-0 text-decoration-none fw-semibold"
                                onClick={() => openResume(c.candidate_id)}
                              >
                                {c.Candidate} ↗
                              </button>
                              {c.Extraction === 'ocr' && <span className="badge text-bg-secondary ms-2">OCR</span>}
                              <div className="text-muted small">📧 {c.Email}</div>
                              <div className="text-muted small">Missing: {c.Missing_Skills}</div>
                              <button
                                type="button"
                                className="btn btn-sm btn-outline-secondary mt-1"
                                onClick={() => setOpenId(openId === c.candidate_id ? null : c.candidate_id)}
                              >
                                {openId === c.candidate_id ? 'Hide evidence' : 'Evidence'}
                              </button>
                            </td>
                            <td style={{ minWidth: '120px' }}>
                              <div className="fw-bold small mb-1">{c.Match_Score}%</div>
                              <div className="progress" style={{ height: '6px' }}>
                                <div
                                  className={`progress-bar ${c.Match_Score > 70 ? 'bg-success' : c.Match_Score > 40 ? 'bg-primary' : 'bg-danger'}`}
                                  style={{ width: `${c.Match_Score}%` }}
                                ></div>
                              </div>
                            </td>
                          </tr>
                          {openId === c.candidate_id && (
                            <tr>
                              <td colSpan={3} className="pt-0 pb-3 border-bottom">
                                <EvidencePanel candidateId={c.candidate_id} />
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                !inFlight && (
                  <div className="text-center text-muted mt-5 pt-4">
                    <p>{activeJobId ? 'No candidates ranked for this job yet.' : 'Upload resumes to generate a ranking.'}</p>
                  </div>
                )
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;