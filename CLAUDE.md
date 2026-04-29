# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A static website that indexes open-source projects' AI contribution policies. Data is fetched and classified by an LLM pipeline, then rendered in a filterable table in the browser and a generated `README.md` at the repo root.

## Data flow

```
data/projects.csv  →  generate_policies.py  →  data/policies.json  →  index.html
                                            →  README.md
```

1. `projects.csv` — source of truth for which projects to track (columns: `name`, `policy_url`)
2. `generate_policies.py` — fetches each policy URL, extracts plain text, calls Azure OpenAI to classify it, writes `policies.json` and `README.md`
3. `policies.json` — the only data source the frontend reads; consumed via `fetch('data/policies.json')`
4. `index.html` — self-contained single-page app (no build step, no bundler)
5. `README.md` — generated markdown table at the repo root; the GitHub landing page for the project

## Running the pipeline

```bash
cd .github/scripts
python generate_policies.py
# explicit paths / custom rescan window / skip README:
python generate_policies.py --input ../../data/projects.csv --output ../../data/policies.json --rescan-days 14 --readme ""
```

The script is incremental: it loads existing `policies.json` on startup, fetches each URL to compute a SHA-256 hash of the extracted text, and skips the LLM call when the hash is unchanged **and** the record is younger than `--rescan-days` (default 7). Each record stores `lastScanned` (ISO 8601 UTC) and `contentHash` for this check. If a fetch fails, the existing cached record is kept rather than overwriting with an error placeholder.

Required environment variables:

| Variable | Example |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | `https://your-resource.openai.azure.com` |
| `AZURE_OPENAI_API_KEY` | your key |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o` |

The script has no third-party Python dependencies — stdlib only.

## Viewing the frontend

Serve `index.html` from a local HTTP server (required for the `fetch()` call to `data/policies.json`):

```bash
python -m http.server 8080
# then open http://localhost:8080
```

Opening `index.html` directly as a `file://` URL will fail with a CORS error.

## Policy schema

Each record in `policies.json` has these classified fields:

| Field | Values |
|---|---|
| `aiCodeGen` | `allowed` / `conditional` / `restricted` / `n/a` |
| `aiCodeReview` | `allowed` / `conditional` / `restricted` / `n/a` |
| `signOff` | `required` / `optional` / `n/a` |
| `attribution` | `required` / `optional` / `n/a` |
| `humanReview` | `required` / `optional` / `n/a` |
| `maturity` | `stable` / `draft` / `n/a` |
| `lastScanned` | ISO 8601 UTC timestamp of last classification |
| `contentHash` | SHA-256 of the extracted policy text |

Each classified field has a companion `*Citation` field containing a verbatim excerpt from the source policy document that supports the classification (or `null` if no passage was found). Citations are verified against the source text using fuzzy matching before being stored.

## Adding a new project

Add a row to `data/projects.csv`, then re-run `generate_policies.py`. The script is incremental — existing records whose content hash is unchanged and are within the rescan window are skipped.

## GitHub Actions

`.github/workflows/generate_policies.yml` runs on:
- Push to `main` when `data/projects.csv` changes
- Weekly schedule (Mondays 03:00 UTC)
- Manual dispatch

It runs the pipeline on a new branch (`policy-updates/YYYY-MM-DD`), commits `data/policies.json` and `README.md`, then opens a PR to `main` for review. Azure OpenAI credentials must be stored as repository secrets (`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`).
