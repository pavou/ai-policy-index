# AI Policy Index

Tracks how open-source projects handle AI-generated contributions – whether they allow it, require disclosure, mandate human review, and so on.

## What it is

A growing number of projects have started publishing AI contribution policies. In this fast-paced era of LLMs, it is nice to have a place to see and compare the decisions made by major project. 
This project fetches those documents, classifies them on a few key dimensions, and presents them in a filterable table.

## How it works

- `data/projects.csv` the list of tracked projects and their policy document URLs
- `.github/scripts/generate_policies.py` A classification script calls an LLM to extract the relevant fields from each policy document
- `data/policies.json` the output, consumed by the frontend
- `index.html` a single-page app that renders and filters the table

## Adding a project

Add a row to `data/projects.csv` with the project name and a direct link to its AI or contributing policy document, then open a PR.

## Running locally

```bash
cd .github/scripts
python generate_policies.py
```

Requires three environment variables: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, and `AZURE_OPENAI_DEPLOYMENT`.

## Model

The classification pipeline uses Azure OpenAI (GPT-4o). GitHub Actions provides a small free quota of GPT-4o calls, which is enough for the weekly refresh given the incremental design — only projects whose policy content has changed since the last scan are sent to the model.