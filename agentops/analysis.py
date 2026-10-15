"""Deterministic first; AI optional. Never transmit raw Vercel log lines to an LLM."""
import json, os, re
from collections import Counter

SECRET_PATTERNS = [
    (re.compile(r'(?i)(bearer\s+)[\w.\-]+'), r'\1[REDACTED]'),
    (re.compile(r'(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+'), r'\1[REDACTED]'),
    (re.compile(r'(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b'), '[EMAIL]'),
]

def redact(s):
    for regex, replacement in SECRET_PATTERNS:
        s = regex.sub(replacement, s)
    return s


def metrics(deployments, events, drain=None):
    """Deterministic aggregate metrics. Drain summary is injected directly."""
    m = {
        'deployments_examined': len(deployments),
        'deployment_states': dict(Counter(d.get('state') or 'unknown' for d in deployments)),
        'events_examined': len(events),
        'event_types': dict(Counter(e.get('type') or 'unknown' for e in events)),
        'event_status_codes': dict(Counter(
            str(e['statusCode']) for e in events if e.get('statusCode') is not None
        )),
        'error_like_events': sum(
            e.get('type') in ('stderr', 'fatal') or
            (isinstance(e.get('statusCode'), int) and e['statusCode'] >= 400)
            for e in events
        ),
    }
    # Drain data now in metrics so LLM sees it in the same payload
    if drain:
        m['drain_total_logs'] = drain.get('total') or 0
        m['drain_errors']     = drain.get('errors') or 0
        m['drain_http4xx']    = drain.get('http4xx') or 0
    return m


def event_evidence(events, drain_entries=None, limit=15):
    """
    Extract bounded, redacted excerpts from both:
    1. Runtime Log Drain errors (500s, exceptions, stderr from production traffic)
    2. Build/Deployment events (compile errors, OOM, timeouts)
    """
    evidence_list = []

    # 1. Runtime Drain errors (highest diagnostic value for running apps)
    for r in (drain_entries or []):
        lvl = (r.get('level') or '').lower()
        code = r.get('status_code')
        msg = (r.get('message') or '').strip()
        path = r.get('path') or ''

        is_error = (
            lvl in ('error', 'fatal') or
            (isinstance(code, int) and code >= 400) or
            any(w in msg.lower() for w in ('error', 'fatal', 'exception', 'failed', 'traceback', 'prisma', 'econn', 'timeout', 'unhandled'))
        )
        if is_error and (msg or code):
            excerpt_text = f"[{path}] {msg}" if path else msg
            evidence_list.append({
                'source': 'runtime_drain',
                'created': r.get('ts') or r.get('received'),
                'type': r.get('level') or (f"http_{code}" if code else 'error'),
                'statusCode': code,
                'path': path or None,
                'excerpt': redact(excerpt_text)[:300].strip(),
            })

    # 2. Deployment/Build events
    for e in events:
        t = e.get('type')
        code = e.get('statusCode')
        text = str(e.get('text') or '').strip()
        is_err = t in ('stderr', 'fatal') or (isinstance(code, int) and code >= 400)
        if is_err:
            evidence_list.append({
                'source': 'build_event',
                'created': e.get('created'),
                'type': t,
                'statusCode': code,
                'path': None,
                'excerpt': redact(text)[:300].strip(),
            })

    # Prioritise non-empty excerpts over blank ones
    non_empty = [e for e in evidence_list if e['excerpt']]
    empty = [e for e in evidence_list if not e['excerpt']]
    combined = non_empty + empty

    return combined[:limit]


# ── Structured LLM prompt ─────────────────────────────────────────────────────
# Compact one-shot prompt that forces JSON output and keeps token count low.
_PROMPT_HEADER = (
    'You are an expert Vercel cloud deployment and runtime operations analyst.\n'
    'Analyze the provided deployment metrics and error evidence (including runtime log drain errors and endpoints).\n'
    'Respond ONLY with a single valid JSON object. No markdown fences, no text outside JSON.\n\n'
    'Required schema:\n'
    '{"severity":"low|medium|high|critical",'
    '"error_category":"string",'
    '"root_cause_hypothesis":"Concise, specific sentence identifying the exact error, failing path, or cause",'
    '"confidence":0.0,'
    '"key_evidence":["quoted excerpt, status code, or endpoint"],'
    '"recommended_steps":["actionable step 1","actionable step 2"],'
    '"needs_human_escalation":false,'
    '"analysis_notes":"string"}\n\n'
    'Rules: Identify failing API routes or components if present; distinguish runtime drain errors from build warnings; provide concrete fix steps.\n\n'
    'DATA:\n'
)

_FALLBACK: dict = {
    'severity': 'unknown',
    'error_category': 'unknown',
    'root_cause_hypothesis': 'Insufficient evidence',
    'confidence': 0.0,
    'key_evidence': [],
    'recommended_steps': ['Review deployment logs and error excerpts manually.'],
    'needs_human_escalation': True,
    'analysis_notes': 'Structured JSON parsing failed; falling back to deterministic metrics only.',
    'source': 'deterministic',
}


def _parse_structured(text: str) -> dict:
    """Parse LLM JSON response; return a safe fallback dict on any failure."""
    try:
        cleaned = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.M)
        cleaned = re.sub(r'```\s*$', '', cleaned.strip(), flags=re.M)
        result = json.loads(cleaned)
        required = {'severity', 'error_category', 'root_cause_hypothesis',
                    'confidence', 'recommended_steps'}
        if not required.issubset(result.keys()):
            return {**_FALLBACK, 'analysis_notes': 'JSON response missing required keys.'}
        return result
    except (json.JSONDecodeError, TypeError):
        return {**_FALLBACK, 'analysis_notes': f'JSON parse failed. Raw snippet: {text[:200]}'}


def ai_summary(summary: dict, provider: str, evidence: list | None = None,
               playbook_hint: dict | None = None,
               memory_context: list | None = None) -> dict:
    """
    Call the LLM with a compact structured prompt and return a parsed dict.

    Token-saving measures:
    - Returns a structured dict (not freeform markdown).
    - Compact JSON payload (no pretty-printing).
    - max_output_tokens capped at 400 (JSON is dense).
    - Playbook hint narrows the scope → shorter LLM response needed.
    - Up to 2 prior memory incidents injected so LLM can validate/refine.
    - Gemini 2.5 Flash Lite is the default (cheapest capable model).
    """
    if provider == 'Deterministic only':
        return {
            **_FALLBACK,
            'source': 'deterministic',
            'analysis_notes': 'AI disabled. Review metrics and evidence manually.',
        }

    # Build compact payload — no redundant whitespace
    payload: dict = {'metrics': summary, 'evidence': evidence or []}

    if memory_context:
        payload['prior_incidents'] = [
            {k: v for k, v in m.items()
             if k in ('error_categories', 'resolution_summary',
                      'recommended_steps', 'severity', 'outcome', 'hit_count')}
            for m in memory_context[:2]
        ]

    if playbook_hint:
        # Tell LLM the category we already suspect — shrinks its search space
        payload['playbook_hint'] = {
            'suspected_category': playbook_hint.get('category'),
            'candidate_steps':    playbook_hint.get('steps', []),
        }

    prompt = _PROMPT_HEADER + json.dumps(payload, separators=(',', ':'))
    raw = ''

    try:
        if provider == 'Gemini':
            from google import genai
            client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
            response = client.models.generate_content(
                model=os.getenv('GEMINI_MODEL', 'gemini-2.5-flash-lite'),
                contents=prompt,
            )
            raw = response.text or ''

        elif provider == 'OpenAI':
            from openai import OpenAI
            response = OpenAI().responses.create(
                model=os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'),
                input=prompt,
                max_output_tokens=400,   # structured JSON is compact
            )
            raw = response.output_text or ''

    except Exception as exc:
        return {
            **_FALLBACK,
            'source': 'error',
            'analysis_notes': f'LLM call failed: {type(exc).__name__}: {str(exc)[:150]}',
        }

    result = _parse_structured(raw)
    result['source'] = 'llm'
    result['raw'] = redact(raw)
    return result


# ── Report formatting ─────────────────────────────────────────────────────────
_SEVERITY_ICON = {
    'low': '🟢', 'medium': '🟡', 'high': '🔴',
    'critical': '🚨', 'unknown': '⚪',
}
_SOURCE_LABEL = {
    'memory':       '🧠 Memory — Known Pattern *(LLM skipped, 0 tokens used)*',
    'playbook':     '📚 Playbook — Rule Match *(LLM skipped, 0 tokens used)*',
    'llm':          '🤖 AI Analysis (Gemini / OpenAI)',
    'llm+playbook': '🤖 AI Analysis *(playbook-guided — reduced token usage)*',
    'deterministic':'📊 Deterministic Only *(AI disabled)*',
    'error':        '⚠️ Analysis Error',
}


def report(summary: dict, result: dict, evidence: list,
           deployment: dict | None, source: str,
           past: list | None = None) -> str:
    """Render the full markdown investigation report."""
    uid      = (deployment or {}).get('uid', 'unknown')
    sev      = result.get('severity', 'unknown')
    sev_icon = _SEVERITY_ICON.get(sev, '⚪')
    src_lbl  = _SOURCE_LABEL.get(source, source)
    conf_pct = int(result.get('confidence', 0) * 100)

    lines = [
        '# Deployment Investigation Report',
        '',
        f'**Deployment:** `{uid}`  ',
        f'**Severity:** {sev_icon} {sev.upper()}  ',
        f'**Analysis source:** {src_lbl}  ',
        '',
        '## Verified Metrics',
        '```json',
        json.dumps(summary, indent=2),
        '```',
        '',
        '## Error Evidence (redacted)',
        '```json',
        json.dumps(evidence, indent=2),
        '```',
        '',
    ]

    # ── Prior incidents from memory ──
    approved_past = [p for p in (past or []) if p.get('outcome') in ('approved', 'verified')]
    if approved_past:
        lines += [
            '## 🧠 Prior Incidents (from agent memory)',
            f'Found **{len(approved_past)}** previously approved occurrence(s) '
            f'with the same error fingerprint.',
            '',
        ]
        for p in approved_past[:2]:
            lines += [
                f'- **Resolution:** {p.get("resolution_summary", "N/A")}',
                f'  *Outcome: {p.get("outcome")} · Seen {p.get("hit_count", 1)}×*',
                '',
            ]

    # ── Root cause ──
    lines += [
        '## Root Cause Hypothesis',
        result.get('root_cause_hypothesis', 'Insufficient evidence'),
        '',
    ]
    if conf_pct > 0:
        lines += [f'**Confidence:** {conf_pct}%', '']

    key_ev = result.get('key_evidence', [])
    if key_ev:
        lines += ['**Key evidence:**', '']
        for ev in key_ev:
            lines.append(f'- {redact(str(ev))}')
        lines.append('')

    # ── Recommended steps ──
    steps = result.get('recommended_steps', [])
    if steps:
        lines += ['## Recommended Steps', '']
        for i, step in enumerate(steps, 1):
            lines.append(f'{i}. {step}')
        lines.append('')

    if result.get('needs_human_escalation'):
        lines += [
            '> ⚠️ **Human escalation recommended.** '
            'Automated analysis has low confidence for this pattern.',
            '',
        ]

    notes = result.get('analysis_notes', '')
    if notes:
        lines += ['## Analysis Notes', notes, '']

    lines += [
        '---',
        '*Scope: recent deployment events only. '
        'Runtime traffic, performance and error rates are not established by these data.*',
    ]
    return '\n'.join(lines)
