# AgentOps for Vercel — self-hosted MVP

Generic deployment-operations control centre, extracted conceptually from the BudgetFly prototype. BudgetFly-specific logic is isolated under `agentops/plugins/budgetfly.py` and is **not loaded** by the core.

## What works
- Vercel token authentication to **read-only GET** endpoints (projects, deployments, deployment/build events).
- Dashboard for projects, deployments, background investigation runs, persistent SQLite history, logs and human report approval.
- Deterministic deployment-event metrics; optional Gemini or OpenAI summaries of **aggregate metrics only**.
- Docker Compose deployment, bound to localhost by default.

## Setup on Ubuntu

```bash
unzip vercel-agentops-mvp.zip
cd vercel-agentops
cp .env.example .env
chmod 600 .env
nano .env  # set VERCEL_TOKEN; optionally VERCEL_TEAM_ID and AI keys
sudo docker compose up --build -d
```

Open http://localhost:8501 on the Dell. From a Mac, use an SSH tunnel:

```bash
ssh -L 8501:127.0.0.1:8501 YOUR_USER@DELL_LAN_IP
```

Then open http://localhost:8501 on the Mac. Docker Compose requires Docker Engine and the Compose plugin; if unavailable, use Python directly:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py --server.address 127.0.0.1
```

## Vercel credentials
Create a token in Vercel account settings, scoped as narrowly as Vercel permits. For team projects, set `VERCEL_TEAM_ID` and ensure the token has that team's access. Never paste credentials into chat or commit `.env`.

## Data limitations
`GET /v3/deployments/{id}/events` supplies deployment/build events. It is **not** a comprehensive production runtime log stream. Future versions can add supported runtime log integrations or log drains. Vercel API limits and available history vary. This is a read-only prototype, not a production SaaS.

## Commercial hardening backlog
- OAuth onboarding, multi-tenant user authentication and tenant isolation.
- Encrypted per-tenant secrets, key rotation and audit trail.
- Queue workers and reliable cancellation instead of in-process threads.
- Pagination, cursor-based sync, backoff and durable checkpoints.
- Runtime log ingestion with data minimisation and retention controls.
- Per-customer billing and provider-specific token accounting.
- Automated tests with API fixtures, license, versioned schema migrations and CI.

## Run tests
```bash
python -m unittest discover -s tests -v
```

Vercel API docs: https://vercel.com/docs/rest-api
