# Repo Autodeployer – Agent Manual

This document captures recurring commands, project structure, and conventions used by this backend. It is the ground truth for day‑to‑day operations and future automation.

## Overview

- Purpose: Accept a natural language description and a GitHub repo URL, then provision an AWS EC2 instance and deploy the repo via Docker Compose, exposing the app on host port 8080.
- Core flow:
  1) Clone repo → analyze tree (depth 4) → verify it’s an HTTP-accessible service.
  2) Infer app internal port → generate/overwrite Dockerfile, docker-compose.yml, Makefile in repo.
  3) Prepare archive → generate Terraform via OpenAI (fallback template if needed).
  4) Terraform apply → provision t2.small (Ubuntu 24.04) in ca-central-1 → install Docker → transfer archive → run `make up`.

## Project structure

- app/
  - main.py: FastAPI app (routes: /request, /list, /job/{id})
  - queue.py: In-memory job queue, statuses, and per-job log capture
  - worker.py: End-to-end deployment worker (clone, analyze, dockerize, terraform)
  - openai_client.py: OpenAI chat completions wrapper for Terraform generation
- Dockerfile: API container (includes Terraform CLI)
- docker-compose.yml: Local dev/runtime for the API
- Makefile: Convenience targets for compose lifecycle
- .env.example: Required environment variables
- .github/workflows/markdownlint.yml: Markdown lint CI

## Environment variables

- OPENAI_API_KEY: OpenAI API key (required to call the LLM)
- OPENAI_MODEL: OpenAI model name (default: gpt-4o-mini)
- AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY: Used by Terraform provider
- AWS_DEFAULT_REGION: Defaults to ca-central-1
- MAX_CONCURRENT_JOBS: Thread pool size for job execution (default: 2)

## Commands

- Run the API (Docker):
  - `make up` → builds and runs the API on http://localhost:8000
  - `make logs` → follow API logs
  - `make down` → stop and remove containers/volumes
- Typecheck (Python):
  - `python -m py_compile app/*.py`
- Lint (Markdown):
  - `markdownlint **/*.md`
- Build container only:
  - `docker compose build`

## API endpoints

- POST /request
  - Body:
    - `description` (string): natural language deployment requirements
    - `repo_url` (string, URL): GitHub repository URL
  - Response: `{ "job_id": string, "status": "queued" }`
- GET /list
  - Response: array of jobs with id, status, logs (captured progressively)
- GET /job/{id}
  - Response: job details, status in {queued|running|completed|failed}, logs, workdir

## Job lifecycle and logging

- Statuses: queued → running → completed | failed
- Logging: per-job in-memory log handler; also emitted to stdout. All subprocess output (git, terraform, etc.) is streamed into logs.

## Deployment details

- Terraform targets:
  - AWS EC2 t2.small in ca-central-1 (Ubuntu 24.04 Canonical AMI)
  - SG allowing 22 and 8080
  - tls_private_key + aws_key_pair for SSH
  - file provisioner to upload the repo archive
  - remote-exec to install Docker and run `make up`
- Dockerization of target repos:
  - Dockerfile tailored with heuristics for Python/Node, exposes inferred internal port
  - docker-compose.yml maps host 8080 → internal port
  - Makefile with `up`, `logs`, `down`

## Conventions

- Language/runtime: Python 3.11, FastAPI
- Style: small, local changes > cross-file refactors; reuse conventions already present; add no new dependencies without approval.
- Logging: use Python `logging`; stream subprocess output into job logs.
- Security: never hardcode AWS credentials; rely on environment variables.
- Provisioning: non-interactive commands only; Terraform runs inside the API container.

## Verification gates (run in order)

1) Typecheck: `python -m py_compile app/*.py`
2) Lint (Markdown): `markdownlint **/*.md`
3) Tests: none yet (add when introducing tests)
4) Build: `docker compose build`

## Example usage

- Start API: `make up`
- Submit a job:
  - `curl -X POST http://localhost:8000/request -H 'Content-Type: application/json' -d '{"description":"Deploy the app and expose it.","repo_url":"https://github.com/owner/repo"}'`
- List jobs: `curl http://localhost:8000/list`
- Job details: `curl http://localhost:8000/job/<id>`

## Notes

- The worker denies repos that do not appear to run an HTTP-accessible server.
- If the model response is unusable for Terraform, a robust fallback `main.tf` is written to ensure successful provisioning.
