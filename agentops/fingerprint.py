"""Canonical error fingerprinting and rule-based categorization. Zero LLM calls."""
import hashlib, re

# Patterns whose values are volatile and should be normalised away
_VOLATILE = [
    (re.compile(r'\bdpl_[A-Za-z0-9]+\b'), 'DEPLOY'),
    (re.compile(r'\bprj_[A-Za-z0-9]+\b'), 'PROJECT'),
    (re.compile(r'\b[0-9a-f]{12,}\b', re.I), 'ID'),          # long hex IDs
    (re.compile(r'\d{4}-\d{2}-\d{2}T[\d:.Z+-]+'), 'TS'),     # ISO timestamps
    (re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'), 'IP'),
    (re.compile(r'\b\d+ms\b'), 'NMS'),
    (re.compile(r'line \d+', re.I), 'lineN'),
    (re.compile(r'at offset \d+', re.I), 'offsetN'),
    (re.compile(r'chunk-[A-Za-z0-9]+'), 'CHUNK'),
]

def _normalize(text: str) -> str:
    for pat, rep in _VOLATILE:
        text = pat.sub(rep, text)
    return text.lower().strip()


def compute(evidence: list[dict], metrics: dict | None = None) -> str:
    """
    Return a stable 16-char hex fingerprint for a set of error evidence.
    Ignores volatile fields (IDs, timestamps, IPs) so the same class of error
    produces the same fingerprint across different deployments.
    """
    parts = sorted(
        _normalize(f"{e.get('type', '')} {e.get('statusCode', '')} {e.get('excerpt', '')}")
        for e in evidence
    )
    if metrics:
        # Bucket state names so 1 READY == 2 READY
        parts.append('states:' + str(sorted(metrics.get('deployment_states', {}).keys())))
    combined = '\n'.join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


# Rule table: (category_id, keywords_to_match_in_lowercased_text)
_RULES: list[tuple[str, list[str]]] = [
    ('build_oom',        ['exit code 137', 'killed', 'out of memory', 'oom', 'heap out', 'javascript heap']),
    ('missing_env_var',  ['env var', 'environment variable', 'is not defined', 'undefined env', 'process.env']),
    ('build_timeout',    ['timeout', 'timed out', 'build exceeded', 'took too long', 'max duration']),
    ('module_not_found', ['cannot find module', 'module not found', 'no such file or directory', "can't resolve"]),
    ('type_error',       ['typeerror', 'is not a function', 'cannot read properties', 'cannot read property']),
    ('syntax_error',     ['syntaxerror', 'unexpected token', 'unexpected identifier', 'parse error']),
    ('network_error',    ['econnrefused', 'enotfound', 'network error', 'fetch failed', 'socket hang up', 'etimedout']),
    ('auth_error',       ['401', 'unauthorized', 'invalid token', 'authentication failed', 'forbidden']),
    ('rate_limit',       ['429', 'rate limit', 'too many requests', 'quota exceeded']),
    ('db_error',         ['connection refused', 'prisma', 'pg error', 'mongodb', 'database error', 'econnreset', 'neon']),
    ('import_error',     ['importerror', 'cannot import', "can't import", 'no module named']),
    ('auth_error',       ['403', 'forbidden']),
]

def categorize(evidence: list[dict], events: list[dict]) -> list[str]:
    """
    Fast rule-based categorization with no LLM.
    Returns a list of matched category names (no duplicates, ordered by rule priority).
    """
    text = ' '.join(
        (e.get('excerpt', '') + ' ' + e.get('text', '')).lower()
        for e in evidence + events
    )
    seen: set[str] = set()
    found: list[str] = []
    for cat, kws in _RULES:
        if cat not in seen and any(kw in text for kw in kws):
            found.append(cat)
            seen.add(cat)
    return found or ['unknown']
