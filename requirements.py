import re

def extract_requirements(jd_text, max_reqs=15):
    reqs, weight = [], 2.0                      # 2.0 = must-have, 1.0 = nice-to-have
    for raw in re.split(r"[\r\n]+|(?<=[.;])\s+", jd_text):
        line = re.sub(r"^[\s\-\*•·\d\.\)]+", "", raw).strip()
        if not line:
            continue
        if line.endswith(":") and len(line) < 50:          # section header
            weight = 1.0 if re.search(r"preferred|nice to have|bonus|plus", line, re.I) else 2.0
            continue
        if 3 <= len(line) <= 300:
            reqs.append({"text": line.rstrip(".;:"), "weight": weight})
    if len(reqs) < 3:                                       # one-paragraph JD: fall back to comma phrases
        reqs = [{"text": p.strip(), "weight": 2.0} for p in re.split(r"[,;]", jd_text) if len(p.split()) >= 2]
    return reqs[:max_reqs]