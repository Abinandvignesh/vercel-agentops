"""Signed Vercel JSON log drain ingestion. No public read endpoints."""
import hashlib
import hmac
import json
import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from . import store
from .analysis import redact

app = FastAPI(title='AgentOps Ingest', docs_url=None, redoc_url=None, openapi_url=None)
MAX_BODY = 1024 * 1024  # 1 MiB; reject oversized batches
MAX_EVENTS = 1000

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.post('/ingest/vercel/logs')
async def ingest_logs(request: Request):
    secret = os.getenv('VERCEL_DRAIN_SECRET', '')
    if not secret:
        raise HTTPException(503, 'Drain secret not configured')
    signature = request.headers.get('x-vercel-signature', '')
    if len(signature) != 40:
        raise HTTPException(403, 'Invalid signature')
    # Read incrementally to avoid unbounded request bodies.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            raise HTTPException(413, 'Payload too large')
    expected = hmac.new(secret.encode('utf-8'), bytes(body), hashlib.sha1).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise HTTPException(403, 'Invalid signature')
    try:
        events = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Invalid JSON')
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise HTTPException(400, 'Expected JSON array of up to 1000 events')
    rows=[]
    for e in events:
        if not isinstance(e, dict):
            raise HTTPException(400, 'Invalid log event')
        event_id=e.get('id')
        deployment_id=e.get('deploymentId')
        if not isinstance(event_id,str) or not event_id or not isinstance(deployment_id,str) or not deployment_id.startswith('dpl_'):
            raise HTTPException(400, 'Missing log id or deploymentId')
        def bounded(k, n=300):
            value=e.get(k)
            return redact(str(value))[:n] if value is not None else None
        timestamp=e.get('timestamp')
        if not isinstance(timestamp,(int,float)) or isinstance(timestamp,bool):timestamp=None
        status=e.get('statusCode')
        if not isinstance(status,int) or isinstance(status,bool):status=None
        rows.append((event_id[:200],deployment_id[:150],bounded('projectId'),bounded('projectName'),
                     int(timestamp) if timestamp is not None else None,bounded('source',50),
                     bounded('level',30),bounded('type',30),status,bounded('path',500),
                     bounded('message',4000),store.now()))
    try:
        inserted=store.insert_drain_logs(rows)
    except Exception:
        # Do not log request bodies or leak internal DB errors.
        raise HTTPException(503, 'Storage unavailable')
    return JSONResponse({'accepted': len(rows), 'inserted': inserted})
