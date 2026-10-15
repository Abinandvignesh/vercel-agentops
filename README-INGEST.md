# AgentOps Vercel drain ingestion

1. Back up existing project. Copy `agentops/ingest.py`, `agentops/store.py`, `agentops/runner.py`, `app.py`, `requirements.txt`, `docker-compose.yml` into your existing project. Preserve your `.env` and `data/`.
2. Set `VERCEL_DRAIN_SECRET` in `.env` to the Vercel destination signature verification secret. Set `AGENTOPS_DB=/app/data/agentops.sqlite3`.
3. Run `docker-compose up --build -d` (Compose v1).
4. Verify `curl -i http://127.0.0.1:8000/health` gives 200. The ingest endpoint rejects unsigned POST requests with 403.
5. Run `ngrok http 8000` on the Dell host, not in Docker.
6. In Vercel select **Logs**, JSON, project `flightbooking-axt1`, and `https://YOUR-NGROK-DOMAIN/ingest/vercel/logs` as the endpoint. Use the same signature secret. Click Test and confirm a successful response, then Create Drain.
7. Run `docker-compose logs --tail=40 ingest` and inspect the dashboard section 'Vercel drained logs'.

## Security / scope
- HMAC-SHA1 signature is checked against raw request bytes using constant-time comparison per Vercel documentation.
- 1 MiB request limit, bounded event count, SQLite deduplication by deploymentId + id, basic regex redaction, no public read endpoint.
- Basic redaction is not comprehensive. Vercel application logs may contain personal information or tokens. Review what your application emits, use short retention, and do not treat this as production-hardened multi-tenant infrastructure.
- ngrok tunnel must forward port 8000 only. Keep dashboard 8501 localhost-only.
- SQLite WAL is enabled for ingest; backup SQLite consistently (SQLite backup API or stop services first).
- Historical logs are not automatically backfilled. New events are correlated using deploymentId.
- Dashboard AI analysis still uses deployment build events; drain metrics are appended separately and are not yet passed into the AI reasoning engine.
