# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A static website that indexes open-source projects' AI contribution policies. Data is fetched and classified by an LLM pipeline, then rendered in a filterable table in the browser.

## Data flow

```
data/projects.csv  →  generate_policies.py  →  data/policies.json  →  index.html
```

1. `projects.csv` — source of truth for which projects to track (columns: `name`, `policy_url`)
2. `generate_policies.py` — fetches each policy URL, extracts plain text, calls Azure OpenAI to classify it, writes `policies.json`
3. `policies.json` — the only data source the frontend reads; consumed via `fetch('data/policies.json')`
4. `index.html` — self-contained single-page app (no build step, no bundler)

## Running the pipeline

```bash
cd .github/scripts
python generate_policies.py
# explicit paths / custom rescan window:
python generate_policies.py --input ../../data/projects.csv --output ../../data/policies.json --rescan-days 14
```

The script is incremental with two gates:

1. **TTL gate** — if `lastScanned` is within `--rescan-days` (default 7), the record is kept as-is and the URL is not fetched.
2. **Hash gate** — if the URL is fetched and the content hash matches the stored `contentHash`, `lastScanned` is updated but the LLM is not called.

Only when the hash changes does the script call Azure OpenAI to re-classify. If a fetch fails, the existing cached record is kept.

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
| `lastScanned` | ISO 8601 UTC timestamp of last scan |
| `contentHash` | SHA-256 of the extracted policy text |

Each classified field has a companion `*Citation` field containing a verbatim excerpt from the source policy document that supports the classification (or `null` if no passage was found). Citations are verified against the source text using fuzzy matching before being stored.

## Adding a new project

Add a row to `data/projects.csv`, then re-run `generate_policies.py`. The script is incremental — existing records within the rescan window are skipped entirely.

## GitHub Actions

`.github/workflows/generate_policies.yml` runs on:
- Push to `main` when `data/projects.csv` changes
- Weekly schedule (Mondays 03:00 UTC)
- Manual dispatch

It runs the pipeline and commits any changes to `data/policies.json` directly to `main`. Azure OpenAI credentials must be stored as repository secrets (`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`). The rescan window can be overridden without a commit by setting the `RESCAN_DAYS` repository variable under **Settings → Variables → Actions**.
