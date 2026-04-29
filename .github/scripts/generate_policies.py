"""
generate_policies.py
--------------------
Reads projects.csv, fetches each project's policy document, asks Azure OpenAI
to classify the AI contribution policy columns, and writes data/policies.json.

Incremental mode: existing records are reused when the policy content hash is
unchanged AND the record is younger than --rescan-days (default 7).  Each
record stores lastScanned (ISO 8601 UTC) and contentHash (SHA-256).

Required environment variables:
  AZURE_OPENAI_ENDPOINT    e.g. https://your-resource.openai.azure.com
  AZURE_OPENAI_API_KEY     your Azure OpenAI key
  AZURE_OPENAI_DEPLOYMENT  your deployment name, e.g. gpt-4o

Usage:
  python generate_policies.py
  python generate_policies.py --input projects.csv --output data/policies.json
  python generate_policies.py --rescan-days 14
"""

import argparse
import csv
import difflib
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

RESCAN_DAYS_DEFAULT = 7

# ── Text extraction ────────────────────────────────────────────────────────────

def extract_text(raw: str, url: str) -> str:
  """
  Return plain text from an HTML page or a Markdown/plain-text file.
  For HTML: strips all tags, decodes entities, and collapses whitespace.
  For everything else: returns the content as-is.
  """
  url_lower = url.lower().split("?")[0]

  is_html = (
      url_lower.endswith((".html", ".htm")) or
      bool(re.search(r"<\s*(!DOCTYPE|html|head|body)", raw[:200], re.IGNORECASE))
  )

  if not is_html:
    return raw

  text = re.sub(r"<[^>]+>", " ", raw)   # strip tags
  text = html.unescape(text)             # decode &amp; &lt; &#x27; etc.
  text = re.sub(r"\s+", " ", text).strip()
  return text


# ── Hashing & staleness ────────────────────────────────────────────────────────

def compute_hash(text: str) -> str:
  return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_ts(ts: str) -> datetime:
  """Parse an ISO 8601 string to an aware UTC datetime."""
  dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
  return dt


def is_stale(record: dict, current_hash: str, rescan_days: int) -> bool:
  """Return True if the record should be re-classified."""
  if record.get("contentHash") != current_hash:
    return True
  ts = record.get("lastScanned")
  if not ts:
    return True
  try:
    return datetime.now(timezone.utc) - _parse_ts(ts) > timedelta(days=rescan_days)
  except ValueError:
    return True


# ── Argument parsing ───────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--input",  default="../../data/projects.csv",
                    help="Path to input CSV file")
parser.add_argument("--output", default="../../data/policies.json",
                    help="Path to output JSON file")
parser.add_argument("--rescan-days", type=int, default=RESCAN_DAYS_DEFAULT,
                    dest="rescan_days",
                    help=f"Re-classify a project only if its content changed or "
                         f"this many days have passed (default: {RESCAN_DAYS_DEFAULT})")
parser.add_argument("--readme", default="../../README.md",
                    help="Path to output README.md (empty string to skip)")
args = parser.parse_args()

ENDPOINT   = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
API_KEY    = os.environ.get("AZURE_OPENAI_API_KEY", "")
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")

if not ENDPOINT or not API_KEY or not DEPLOYMENT:
  raise SystemExit(
    "ERROR: Missing environment variables.\n"
    "Set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, and AZURE_OPENAI_DEPLOYMENT."
  )

AZURE_URL = f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}/chat/completions?api-version=2025-04-01-preview"

SYSTEM_PROMPT = """
You are an expert in open-source software contribution policies.
You will be given the text of a project's contributing guide or AI policy document.
Classify the project's stance on AI tool usage, backing each classification with a
verbatim citation from the document.

── CITATION RULES ──────────────────────────────────────────────────────────────
1. A citation is a single sentence or clause copied CHARACTER-FOR-CHARACTER from
   the document. Preserve original capitalisation, punctuation, and spelling.
2. Choose the sentence that most DIRECTLY and SPECIFICALLY states the policy —
   not background context, not a general statement.
3. Do NOT paraphrase, summarise, shorten, or merge text from different sentences.
4. If no sentence in the document directly addresses a dimension, set that
   citation to null. Do not invent a citation.

── CLASSIFICATION VALUES ────────────────────────────────────────────────────────
aiCodeGen / aiCodeReview:
  "allowed"      — explicitly permitted with no major conditions
  "conditional"  — permitted but with notable caveats or requirements
  "restricted"   — discouraged, heavily limited, or prohibited
  "n/a"          — not mentioned or not applicable

signOff / attribution / humanReview:
  "required"     — the policy explicitly requires it
  "optional"     — allowed or mentioned but not required
  "n/a"          — not mentioned

maturity (describes the policy document itself — no citation needed):
  "stable"       — clearly written, specific, and enforced
  "draft"        — informal, vague, provisional, or work-in-progress
  "n/a"          — document does not address AI at all

── OUTPUT FORMAT ────────────────────────────────────────────────────────────────
Respond ONLY with a valid JSON object. Emit the citation BEFORE the classification
value for each dimension — this grounds your decision in evidence.

{
  "aiCodeGenCitation":    "verbatim sentence from document, or null",
  "aiCodeGen":            "allowed|conditional|restricted|n/a",
  "aiCodeReviewCitation": "verbatim sentence from document, or null",
  "aiCodeReview":         "allowed|conditional|restricted|n/a",
  "signOffCitation":      "verbatim sentence from document, or null",
  "signOff":              "required|optional|n/a",
  "attributionCitation":  "verbatim sentence from document, or null",
  "attribution":          "required|optional|n/a",
  "humanReviewCitation":  "verbatim sentence from document, or null",
  "humanReview":          "required|optional|n/a",
  "maturity":             "stable|draft|n/a"
}

── EXAMPLE ─────────────────────────────────────────────────────────────────────
Document contains: "Contributors must not submit AI-generated code without first
reviewing it line by line and understanding what it does."

CORRECT citation : "Contributors must not submit AI-generated code without first reviewing it line by line and understanding what it does."
WRONG (paraphrase): "AI-generated code must be reviewed before submission."
WRONG (fragment)  : "reviewing it line by line"
""".strip()


def user_prompt(project_name, policy_text):
  truncated = policy_text[:12000]
  if len(policy_text) > 12000:
    truncated += "\n\n[Document truncated for length]"
  return f"Project: {project_name}\n\nPolicy document:\n\n{truncated}"


def fetch_url(url, timeout=15):
  req = urllib.request.Request(url, headers={"User-Agent": "ai-policy-index/1.0"})
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    raw = resp.read()
    try:
      return raw.decode("utf-8")
    except UnicodeDecodeError:
      return raw.decode("latin-1")


def call_azure_openai(project_name, policy_text, retries=3):
  payload = json.dumps({
    "messages": [
      {"role": "system", "content": SYSTEM_PROMPT},
      {"role": "user",   "content": user_prompt(project_name, policy_text)}
    ],
    "temperature": 0,
    "max_completion_tokens": 800,
    "response_format": {"type": "json_object"}
  }).encode("utf-8")

  headers = {
    "Content-Type": "application/json",
    "api-key": API_KEY
  }

  for attempt in range(1, retries + 1):
    try:
      req = urllib.request.Request(AZURE_URL, data=payload, headers=headers, method="POST")
      with urllib.request.urlopen(req, timeout=30) as resp:
        data    = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)

    except urllib.error.HTTPError as e:
      body = e.read().decode("utf-8", errors="replace")
      print(f"    HTTP {e.code} on attempt {attempt}: {body[:200]}")
      if e.code in (429, 500, 503) and attempt < retries:
        wait = 5 * attempt
        print(f"    Retrying in {wait}s…")
        time.sleep(wait)
      else:
        raise

    except json.JSONDecodeError as e:
      print(f"    Could not parse LLM response as JSON on attempt {attempt}: {e}")
      if attempt < retries:
        time.sleep(3)
      else:
        raise

  raise RuntimeError(f"All {retries} attempts failed for project: {project_name}")


def verify_citations(classification: dict, policy_text: str) -> dict:
  """Null out any citation that cannot be found verbatim (or near-verbatim) in the source text."""
  text_lower = policy_text.lower()
  for key, val in classification.items():
    if not (key.endswith("Citation") and isinstance(val, str) and val):
      continue
    val_lower = val.lower()
    if val_lower in text_lower:
      continue
    # Fuzzy sliding-window match to catch minor punctuation / whitespace drift
    words     = text_lower.split()
    win_size  = len(val_lower.split()) + 4
    found     = any(
      difflib.SequenceMatcher(None, val_lower, " ".join(words[i:i + win_size])).ratio() >= 0.85
      for i in range(max(0, len(words) - win_size + 1))
    )
    if not found:
      classification[key] = None
  return classification


def generate_readme(results: list, path: str) -> None:
  LABELS = {
    "allowed":     "✅ Allowed",
    "required":    "✅ Required",
    "conditional": "⚠️ Conditional",
    "optional":    "⚠️ Optional",
    "restricted":  "❌ Restricted",
  }

  rows = []
  for p in results:
    rows.append("| {name} | {gen} | {rev} | {sign} | {attr} | {human} | {mat} |".format(
      name  = f"[{p['name']}]({p['url']})",
      gen   = LABELS.get(p.get("aiCodeGen"),    "—"),
      rev   = LABELS.get(p.get("aiCodeReview"), "—"),
      sign  = LABELS.get(p.get("signOff"),       "—"),
      attr  = LABELS.get(p.get("attribution"),   "—"),
      human = LABELS.get(p.get("humanReview"),   "—"),
      mat   = p.get("maturity") or "—",
    ))

  last_scanned = max(
    (p["lastScanned"] for p in results if p.get("lastScanned")),
    default="unknown"
  )[:10]

  lines = [
    "# AI Policy Index",
    "",
    "A community-maintained index of open-source projects' AI contribution policies,",
    "classified automatically from each project's official policy document.",
    "",
    "| Project | AI Codegen | AI Review | Author Sign-off | AI Disclosure | Human Oversight | Policy Status |",
    "|---------|-----------|-----------|-----------------|---------------|-----------------|---------------|",
    *rows,
    "",
    f"_Last updated: {last_scanned}. "
    "Classifications are AI-assisted — always consult the official policy before contributing._",
  ]

  os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
  with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")

  print(f"Wrote README to: {path}")


def error_record(name, url, reason):
  return {
    "name": name, "url": url,
    "aiCodeGen":   "n/a", "aiCodeGenCitation":    None,
    "aiCodeReview":"n/a", "aiCodeReviewCitation": None,
    "signOff":     "n/a", "signOffCitation":      None,
    "attribution": "n/a", "attributionCitation":  None,
    "humanReview": "n/a", "humanReviewCitation":  None,
    "maturity":    "draft"
  }


def main():
  # Load existing records for incremental updates
  existing: dict = {}
  if os.path.exists(args.output):
    try:
      with open(args.output, encoding="utf-8") as f:
        for rec in json.load(f):
          if rec.get("name"):
            existing[rec["name"]] = rec
      print(f"Loaded {len(existing)} cached record(s) from {args.output}.")
    except (json.JSONDecodeError, OSError) as e:
      print(f"WARN: Could not read existing data ({e}), starting fresh.")

  print(f"Reading projects from: {args.input}")
  projects = []
  with open(args.input, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
      name = row.get("name", "").strip()
      url  = row.get("policy_url", "").strip()
      if name and url:
        projects.append({"name": name, "url": url})

  if not projects:
    raise SystemExit(f"ERROR: No projects found in {args.input}.")

  print(f"Found {len(projects)} project(s). Rescan threshold: {args.rescan_days} day(s).\n")
  results = []

  for project in projects:
    name   = project["name"]
    url    = project["url"]
    cached = existing.get(name)
    print(f"[{name}]")

    # Fetch and extract
    try:
      print(f"  Fetching: {url}")
      raw_content  = fetch_url(url)
      policy_text  = extract_text(raw_content, url)
      content_hash = compute_hash(policy_text)
      print(f"  Fetched {len(raw_content)} chars → {len(policy_text)} chars after extraction.")
    except Exception as e:
      print(f"  WARN: Could not fetch policy — {e}")
      results.append(cached if cached else error_record(name, url, str(e)))
      continue

    # Skip LLM if content is fresh and unchanged
    if cached and not is_stale(cached, content_hash, args.rescan_days):
      age_days = (datetime.now(timezone.utc) - _parse_ts(cached["lastScanned"])).days
      print(f"  Skipping (content unchanged, last scanned {age_days}d ago).")
      results.append(cached)
      continue

    # Classify
    try:
      print("  Calling Azure OpenAI…")
      classification = call_azure_openai(name, policy_text)
      classification = verify_citations(classification, policy_text)
      print(f"  Done. aiCodeGen={classification.get('aiCodeGen')}, maturity={classification.get('maturity')}")
    except Exception as e:
      print(f"  WARN: LLM call failed — {e}")
      results.append(cached if cached else error_record(name, url, str(e)))
      continue

    record = {"name": name, "url": url}
    record.update(classification)
    record["lastScanned"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record["contentHash"] = content_hash
    results.append(record)

    time.sleep(1)

  os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
  with open(args.output, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

  print(f"\nDone. Wrote {len(results)} record(s) to: {args.output}")

  if args.readme:
    generate_readme(results, args.readme)


if __name__ == "__main__":
  main()
