import os, sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB = Path(os.getenv('AGENTOPS_DB') or 'data/agentops.sqlite3')

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=15000')
    return conn

def init():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, project_id TEXT, project_name TEXT, deployment_id TEXT, status TEXT, started TEXT, ended TEXT, report TEXT, error TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, run_id INTEGER, created TEXT, level TEXT, message TEXT)')
    _migrate()


def _migrate():
    """Idempotent schema migrations. Adds new columns and tables without breaking existing data."""
    with connect() as db:
        existing_cols = {row[1] for row in db.execute('PRAGMA table_info(runs)')}
        for col, typedef in [
            ('fingerprint',     'TEXT'),
            ('error_categories','TEXT'),
            ('severity',        'TEXT'),
        ]:
            if col not in existing_cols:
                db.execute(f'ALTER TABLE runs ADD COLUMN {col} {typedef}')

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

def create(project_id, project_name, deployment_id):
    with connect() as db:
        cur = db.execute('INSERT INTO runs(project_id,project_name,deployment_id,status,started) VALUES (?,?,?,?,?)', (project_id,project_name,deployment_id,'queued',now()))
        return cur.lastrowid

def update(run_id, status, report=None, error=None):
    with connect() as db:
        db.execute('UPDATE runs SET status=?, ended=?, report=COALESCE(?,report), error=COALESCE(?,error) WHERE id=?', (status,now() if status in ('awaiting_review','approved','rejected','failed') else None,report,error,run_id))


def update_run_meta(run_id, fingerprint, error_categories, severity=None):
    """Persist fingerprint and categorisation metadata onto the run row."""
    import json
    with connect() as db:
        db.execute(
            'UPDATE runs SET fingerprint=?, error_categories=?, severity=? WHERE id=?',
            (fingerprint, json.dumps(error_categories), severity, run_id)
        )

def log(run_id, level, message):
    with connect() as db:
        db.execute('INSERT INTO events(run_id,created,level,message) VALUES (?,?,?,?)', (run_id,now(),level,str(message)[:1200]))

def runs():
    with connect() as db: return [dict(x) for x in db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 100')]

def logs(run_id):
    with connect() as db: return [dict(x) for x in db.execute('SELECT created,level,message FROM events WHERE run_id=? ORDER BY id', (run_id,))]

def review(run_id, action):
    if action not in ('approved','rejected'): raise ValueError('Invalid review action')
    with connect() as db:
        cur=db.execute('UPDATE runs SET status=?, ended=? WHERE id=? AND status=?',(action,now(),run_id,'awaiting_review'))
        if cur.rowcount != 1: raise ValueError('Run not awaiting review')
    log(run_id,'INFO',f'Report {action} by dashboard operator')


def init_drain():
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS drain_logs (
            id TEXT NOT NULL, deployment_id TEXT NOT NULL, project_id TEXT,
            project_name TEXT, ts INTEGER, source TEXT, level TEXT, event_type TEXT,
            status_code INTEGER, path TEXT, message TEXT, received TEXT NOT NULL,
            PRIMARY KEY (deployment_id, id)
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_drain_deploy_time ON drain_logs(deployment_id, ts DESC)')

def insert_drain_logs(rows):
    init_drain()
    with connect() as db:
        before = db.total_changes
        db.executemany('''INSERT OR IGNORE INTO drain_logs
        (id,deployment_id,project_id,project_name,ts,source,level,event_type,status_code,path,message,received)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
        return db.total_changes - before

def drain_logs(deployment_id, limit=300):
    init_drain()
    with connect() as db:
        return [dict(r) for r in db.execute(
            'SELECT * FROM drain_logs WHERE deployment_id=? ORDER BY ts DESC LIMIT ?',
            (deployment_id, min(limit,1000)))]

def drain_summary(deployment_id):
    init_drain()
    with connect() as db:
        r=db.execute('''SELECT COUNT(*) AS total,
          SUM(CASE WHEN level IN ('error','fatal') OR status_code >= 500 THEN 1 ELSE 0 END) AS errors,
          SUM(CASE WHEN status_code >= 400 AND status_code < 500 THEN 1 ELSE 0 END) AS http4xx,
          MIN(ts) AS first_ts, MAX(ts) AS last_ts
          FROM drain_logs WHERE deployment_id=?''',(deployment_id,)).fetchone()
        return dict(r)
