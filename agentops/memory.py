"""
Episodic memory: store and retrieve past resolutions keyed by error fingerprint.

Every investigation writes a 'pending' entry. The dashboard operator's
Approve / Reject action updates the outcome. Approved entries are used in
future investigations to skip the LLM when the same fingerprint recurs.
"""
import json
from . import store

_TABLE = '''CREATE TABLE IF NOT EXISTS memory (
    id            INTEGER PRIMARY KEY,
    project_id    TEXT    NOT NULL,
    fingerprint   TEXT    NOT NULL,
    error_categories  TEXT,
    resolution_summary TEXT,
    recommended_steps  TEXT,   -- JSON array
    severity      TEXT,
    outcome       TEXT,        -- pending | approved | rejected | verified
    run_id        INTEGER,
    hit_count     INTEGER DEFAULT 1,
    created       TEXT,
    last_seen     TEXT
)'''
_INDEX_FP   = 'CREATE INDEX IF NOT EXISTS idx_memory_fp   ON memory(fingerprint)'
_INDEX_PROJ = 'CREATE INDEX IF NOT EXISTS idx_memory_proj ON memory(project_id, fingerprint)'


def _init(db) -> None:
    db.execute(_TABLE)
    db.execute(_INDEX_FP)
    db.execute(_INDEX_PROJ)


# ── Public API ────────────────────────────────────────────────────────────────

def lookup(project_id: str, fp: str, limit: int = 3) -> list[dict]:
    """
    Return past memory entries matching this fingerprint.
    Project-specific results are ranked first; approved/verified above pending/rejected.
    """
    with store.connect() as db:
        _init(db)
        rows = db.execute(
            '''SELECT * FROM memory WHERE fingerprint=?
               ORDER BY (project_id=?) DESC,
                        (outcome IN ('verified','approved')) DESC,
                        hit_count DESC, last_seen DESC
               LIMIT ?''',
            (fp, project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def upsert(project_id: str, fingerprint: str, error_categories: list,
           resolution_summary: str, recommended_steps: list,
           severity: str, outcome: str, run_id: int) -> None:
    """
    Insert a new memory entry or increment hit_count if same project+fingerprint
    already exists and update its resolution.
    """
    now = store.now()
    steps_json = json.dumps(recommended_steps)
    cats_str = json.dumps(error_categories)
    with store.connect() as db:
        _init(db)
        row = db.execute(
            'SELECT id FROM memory WHERE project_id=? AND fingerprint=?',
            (project_id, fingerprint),
        ).fetchone()
        if row:
            db.execute(
                '''UPDATE memory SET resolution_summary=?, recommended_steps=?,
                   error_categories=?, severity=?, outcome=?, run_id=?,
                   hit_count=hit_count+1, last_seen=? WHERE id=?''',
                (resolution_summary, steps_json, cats_str, severity, outcome,
                 run_id, now, row['id']),
            )
        else:
            db.execute(
                '''INSERT INTO memory
                   (project_id,fingerprint,error_categories,resolution_summary,
                    recommended_steps,severity,outcome,run_id,created,last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (project_id, fingerprint, cats_str, resolution_summary,
                 steps_json, severity, outcome, run_id, now, now),
            )


def approve(run_id: int) -> None:
    """Promote the memory entry linked to this run to 'approved'."""
    with store.connect() as db:
        _init(db)
        db.execute(
            "UPDATE memory SET outcome='approved' WHERE run_id=? AND outcome='pending'",
            (run_id,),
        )


def reject(run_id: int) -> None:
    """Mark memory entry as 'rejected' so it won't be used as a no-LLM shortcut."""
    with store.connect() as db:
        _init(db)
        db.execute(
            "UPDATE memory SET outcome='rejected' WHERE run_id=? AND outcome='pending'",
            (run_id,),
        )


def steps_from_entry(entry: dict) -> list[str]:
    """Safely parse recommended_steps from a memory row (stored as JSON)."""
    raw = entry.get('recommended_steps', '[]')
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def all_entries(project_id: str | None = None, limit: int = 100) -> list[dict]:
    with store.connect() as db:
        _init(db)
        if project_id:
            rows = db.execute(
                'SELECT * FROM memory WHERE project_id=? ORDER BY last_seen DESC LIMIT ?',
                (project_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                'SELECT * FROM memory ORDER BY last_seen DESC LIMIT ?', (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def stats() -> dict:
    """Summary counts for the knowledge-base dashboard panel."""
    with store.connect() as db:
        _init(db)
        r = db.execute(
            '''SELECT COUNT(*) AS total,
               SUM(outcome='approved') AS approved,
               SUM(outcome='rejected') AS rejected,
               SUM(outcome='pending')  AS pending,
               SUM(outcome='verified') AS verified
               FROM memory'''
        ).fetchone()
        return dict(r)
