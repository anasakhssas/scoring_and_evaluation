import time
import json
import os
from dotenv import load_dotenv
from litellm import completion

# ──────────────────────────────────────────────
# 1. Configuration — change the model here each run
# ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

MODEL = "groq/llama-3.3-70b-versatile"
REQUEST_DELAY = 2  # seconds between requests (Groq free tier: 30 RPM)

# ──────────────────────────────────────────────
# 2. Load candidates & jobs from JSON files
# ──────────────────────────────────────────────
with open(os.path.join(BASE_DIR, "candidates.json"), encoding="utf-8") as f:
    candidates = json.load(f)

with open(os.path.join(BASE_DIR, "jobs.json"), encoding="utf-8") as f:
    jobs = json.load(f)

print(f"Model: {MODEL}")
print(f"Loaded {len(candidates)} candidates and {len(jobs)} jobs.\n")

# ──────────────────────────────────────────────
# 3. System prompt
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert HR scoring assistant. Evaluate a candidate's CV against a job position and return a structured JSON score.

CRITICAL RULE: The integer you write in score_details for each component MUST be the EXACT result of the formula computation — nothing more, nothing less. Never round up. Never use the maximum value when your calculation gives a lower result. A computed value of 5 means you write 5, not 10. A computed value of 9 means you write 9, not 10. You must be PRECISE.

You must score the candidate across exactly 5 components. Total max score is 100 points.

Silently compute each component step by step using the exact formulas below, then write the exact computed integer in score_details.

═══════════════════════════════════════════════════════════
COMPONENT 1: Compétences techniques (0-40 pts)
═══════════════════════════════════════════════════════════
KNOWN LANGUAGES (always exclude from technical skills):
  French, English, Arabic, Spanish, German, Chinese, Japanese, Italian, Portuguese, Dutch, Russian, Korean

Compétences techniques must be isolated from task evidence.

Step 1 — ENUMERATE:
  • Copy every item from the job's required_skills list.
  • Remove any item that is a known language (see list above).
  • The remaining items are your TECH_SKILLS list.
  • N = number of items in TECH_SKILLS.
  • If N = 0, set competences_techniques = 0.
  • Otherwise: pts_per_skill = 40 / N.
  • Write "N={N}, pts_per_skill=40/{N}={pts_per_skill}" in the explanation.

Step 2 — DECLARED SKILLS ONLY (SCORING):
  For each skill in TECH_SKILLS:
    IF the exact skill name exists as a key in candidate's skills dict:
      level = candidate's skills dict value (0..5)
      score_i = pts_per_skill × (level / 5)
    ELSE:
      score_i = 0
  declared_raw = sum of all score_i values.

Step 3 — SUM and ROUND:
  raw_total = declared_raw
  competences_techniques = round(raw_total)
  If competences_techniques > 40, set it to 40.

Step 4 — VERIFY:
  Re-add all score_i values. Confirm the sum equals what you wrote.
  If it doesn't match, fix it before outputting.

WORKED EXAMPLE (5 required tech skills after removing languages):
  required_skills = [Python, Docker, dbt, BigQuery, RAG/LLM applications, French, English]
  Remove languages → TECH_SKILLS = [Python, Docker, dbt, BigQuery, RAG/LLM applications]
  N=5, pts_per_skill = 40/5 = 8

  Skills dict: {Python:5, Docker:5, dbt:4, BigQuery:5, LangChain:4, Airflow:3, FastAPI:3}

  DECLARED SCORING:
  Python key exists level=5 → 8×(5/5)=8.0
  Docker key exists level=5 → 8×(5/5)=8.0
  dbt key exists level=4 → 8×(4/5)=6.4
  BigQuery key exists level=5 → 8×(5/5)=8.0
  RAG/LLM applications key missing → 0
  declared_raw = 30.4

  raw_total = 30.4
  competences_techniques = round(30.4) = 30  ← write 30, NOT 40

  VERIFY: 8.0+8.0+6.4+8.0+0 = 30.4 → round = 30 ✓

═══════════════════════════════════════════════════════════
COMPONENT 2: Expérience (0-25 pts)
═══════════════════════════════════════════════════════════
Step 1: candidate_years = sum of numeric years extracted from each experience duration field.
Step 2: required_years = job's min_experience_years.
Step 3:
  IF candidate_years >= required_years:
    base  = 15
    extra = min((candidate_years - required_years) * 5, 10)
    experience = base + extra   [this will be at most 25]
  ELSE:
    experience = 0  → add "Insufficient experience" to missing_requirements

WORKED EXAMPLE A: candidate 3 years, required 2 years
  base=15, extra=min((3-2)*5,10)=5, score=20 → write 20

WORKED EXAMPLE B: candidate 2 years, required 2 years
  base=15, extra=min((2-2)*5,10)=0, score=15 → write 15

WORKED EXAMPLE C: candidate 1 year, required 2 years
  1 < 2 → score=0 → write 0

═══════════════════════════════════════════════════════════
COMPONENT 3: Éducation (0-15 pts)
═══════════════════════════════════════════════════════════
Use your judgment. Consider degree level (Bac+2 < Bac+3 < Bac+5 < PhD) and field relevance.
IF candidate Bac+ level < required Bac+ level → education = 0, add to missing_requirements.

═══════════════════════════════════════════════════════════
COMPONENT 4: Langues (0-10 pts)
═══════════════════════════════════════════════════════════
Step 1: Extract ONLY languages from required_skills using the known language list above.
Step 2: L = count of required languages.
  IF L = 0 → langues = 10
  ELSE:
    pts_per_lang = 10 / L
    For each required language:
      IF present in skills: contribution = pts_per_lang × (level / 5)
      ELSE: contribution = 0
    langues = round(sum of contributions). Cap at 10 only if result > 10.

WORKED EXAMPLE (2 required: French level 5, English level 4):
  pts_per_lang = 10 / 2 = 5
  French: 5 × (5/5) = 5.0
  English: 5 × (4/5) = 4.0
  langues = round(5.0 + 4.0) = round(9.0) = 9 → write 9 in score_details, NOT 10

═══════════════════════════════════════════════════════════
COMPONENT 5: Bonus / nice-to-have (0-10 pts)
═══════════════════════════════════════════════════════════
Step 1: B = count of skills in job's nice_to_have list.
  IF B = 0 OR nice_to_have not present → bonus = 0
  ELSE:
    pts_per_bonus = 10 / B
    For each nice-to-have skill:
      IF it appears in candidate's experience tasks OR in candidate's skills dict → contribution = pts_per_bonus
      ELSE: contribution = 0
    bonus = round(sum of contributions). Cap at 10 only if result > 10.

WORKED EXAMPLE (4 nice-to-have: FastAPI matched, Airflow matched, Flask missing, ERP missing):
  pts_per_bonus = 10 / 4 = 2.5
  FastAPI: 2.5, Airflow: 2.5, Flask: 0, ERP: 0
  bonus = round(2.5 + 2.5) = round(5.0) = 5 → write 5 in score_details, NOT 10

═══════════════════════════════════════════════════════════
FINAL RULES:
═══════════════════════════════════════════════════════════
- score_total = competences_techniques + experience + education + langues + bonus
- The value in score_details MUST exactly match your formula result. Do NOT round up to max. Do NOT "cap" when under max.
- NEVER exceed component maximums (40 / 25 / 15 / 10 / 10)
- NEVER hallucinate skills or experience not present in the data
- NEVER invent extra points. NEVER add bonus points beyond the formula. NEVER write "added X for..." — only the formula result counts.
- Only score skills that are EXPLICITLY listed in the job's required_skills or nice_to_have. Do NOT give credit for related or similar skills.
- For competences_techniques, use only candidate's skills dict matches with required TECH_SKILLS.
- ALL explanation values MUST be plain text strings — NEVER arrays, lists, or objects
- Return ONLY valid JSON — no markdown, no preamble, no trailing text
- Show the arithmetic in each explanation string (e.g. "5 × (4/5) = 4.0")

OUTPUT FORMAT (all explanation values are plain strings):
{
  "explanation": {
    "competences_techniques": "<STRING: start with N=… pts_per_skill=… then DECLARED SCORING only — e.g. N=5 pts_per_skill=8 | Python level=5 8×(5/5)=8 | Docker level=5 8×(5/5)=8 | dbt level=4 8×(4/5)=6.4 | RAG/LLM applications key-missing=0 | declared_raw=22.4 | raw_total=22.4 round=22>",
    "experience": "<STRING: e.g. candidate 3y >= required 2y → base=15 extra=min((3-2)*5,10)=5 → score=20>",
    "education": "<STRING: degree level and field relevance judgment>",
    "langues": "<STRING: e.g. 2 required langs, pts_per_lang=5 | French 5×(5/5)=5 | English 5×(4/5)=4 | total=round(9.0)=9>",
    "bonus": "<STRING: e.g. 4 nice-to-have, pts_per_bonus=2.5 | FastAPI MATCHED=2.5 | Airflow MATCHED=2.5 | Flask MISSING=0 | ERP MISSING=0 | total=round(5.0)=5>"
  },
  "score_details": {
    "competences_techniques": <integer 0-40>,
    "experience": <integer 0-25>,
    "education": <integer 0-15>,
    "langues": <integer 0-10>,
    "bonus": <integer 0-10>
  },
  "score_total": <integer 0-100>,
  "matched_skills": ["<required skill keys found in candidate skills dict>"],
  "missing_requirements": ["<required criteria not found>"],
  "bonus_matches": ["<nice-to-have skills found>"],
  "status": "done"
}"""


def build_user_prompt(candidate, job):
    """Format candidate profile and job posting into a prompt for the LLM."""
    return f"""Evaluate the following candidate against the job position and return only the JSON score object.

JOB:
{json.dumps(job, indent=2, ensure_ascii=False)}

CANDIDATE:
{json.dumps(candidate, indent=2, ensure_ascii=False)}"""


# ──────────────────────────────────────────────
# 4. Run evaluations
# ──────────────────────────────────────────────
model_short = MODEL.split("/")[-1]
results = []

print("=" * 60)
print(f"  MODEL: {model_short}")
print("=" * 60)

successes = 0
failures = 0
total_latency = 0
pair_num = 0
total_pairs = len(candidates) * len(jobs)

for candidate in candidates:
    for job in jobs:
        pair_num += 1
        label = f"[{pair_num}/{total_pairs}] {candidate['name']} x {job['title']}"
        print(f"  {label} ... ", end="", flush=True)

        if pair_num > 1:
            time.sleep(REQUEST_DELAY)

        user_prompt = build_user_prompt(candidate, job)
        start_time = time.time()

        try:
            response = completion(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                max_tokens=1024,
                timeout=300,
                temperature=0,
            )

            latency = time.time() - start_time
            total_latency += latency

            raw = response.choices[0].message.content
            score_data = json.loads(raw)

            # Clamp scores to their maximums
            details = score_data.get("score_details", {})
            caps = {"competences_techniques": 40, "experience": 25, "education": 15, "langues": 10, "bonus": 10}
            for key, max_val in caps.items():
                if key in details:
                    details[key] = min(int(details[key]), max_val)
            score_data["score_details"] = details
            score_data["score_total"] = sum(details.get(k, 0) for k in caps)

            score_data.update({
                "candidate_id": candidate["id"],
                "candidate_name": candidate["name"],
                "job_id": job["id"],
                "job_title": job["title"],
                "latency_s": latency,
                "status": "done",
            })
            results.append(score_data)
            print(f"SUCCESS — score: {score_data['score_total']} pts, latency: {latency:.2f}s")
            successes += 1
        except Exception as e:
            latency = time.time() - start_time
            total_latency += latency
            print(f"ERROR — {str(e)} (latency: {latency:.2f}s)")
            results.append({
                "candidate_id": candidate["id"],
                "candidate_name": candidate["name"],
                "job_id": job["id"],
                "job_title": job["title"],
                "score_total": 0,
                "score_details": {},
                "explanation": {},
                "missing_requirements": [],
                "latency_s": latency,
                "status": "failed",
                "error": str(e),
            })
            failures += 1

avg_latency = round(total_latency / pair_num, 2) if pair_num else 0
print(f"\n  Summary: {successes} OK / {failures} FAILED | Avg latency: {avg_latency}s\n")

# ──────────────────────────────────────────────
# 5. Save results to JSON
# ──────────────────────────────────────────────
output_path = os.path.join(BASE_DIR, "results.json")
with open(output_path, "w", encoding="utf-8") as f:
    json.dump({"model": MODEL, "results": results}, f, indent=2, ensure_ascii=False)
print(f"Results saved to {output_path}")

# ──────────────────────────────────────────────
# 6. Print structured results
# ──────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"  DETAILED RESULTS — {model_short}")
print("=" * 60)

for entry in results:
    print(f"\n  Candidate : {entry['candidate_name']}")
    print(f"  Job       : {entry['job_title']}")
    if entry["status"] == "failed":
        print("  Score     : ERR")
        print(f"  Error     : {entry.get('error', '')}")
    else:
        score = entry.get("score_total", 0)
        bar = "█" * (score // 5) + "░" * (20 - score // 5)
        print(f"  Score     : {score}/100  [{bar}]")
        details = entry.get("score_details", {})
        print(f"  Breakdown : Skills={details.get('competences_techniques','-')}/40 | Lang={details.get('langues','-')}/10 | Exp={details.get('experience','-')}/25 | Edu={details.get('education','-')}/15 | Bonus={details.get('bonus','-')}/10")
        expl = entry.get("explanation", {})
        if isinstance(expl, dict):
            for key, val in expl.items():
                print(f"    {key}: {val}")
        else:
            print(f"  Explanation: {expl}")
        matched = entry.get("matched_skills", [])
        missing = entry.get("missing_requirements", [])
        bonuses = entry.get("bonus_matches", [])
        if matched:
            print(f"  Matched   : {', '.join(matched)}")
        if missing:
            print(f"  Missing   : {', '.join(missing)}")
        if bonuses:
            print(f"  Bonuses   : {', '.join(bonuses)}")
    print("  " + "-" * 56)

print("\nDone!")