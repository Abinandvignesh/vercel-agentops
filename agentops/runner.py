"""
Orchestrator for a single deployment investigation.

Decision tree (cheapest path first):
  1. Fetch data  (always, free)
  2. Deterministic metrics + fingerprint + categorise  (always, free)
  3. Memory lookup  → if approved/verified hit → skip LLM entirely  ✅ free
  4. Playbook match → if match in Deterministic mode → skip LLM      ✅ free
  5. LLM call       → last resort, guided by playbook + memory context
"""
from . import store
from .analysis import metrics, ai_summary, report, event_evidence
from . import fingerprint as fp_module
from . import memory as mem
from . import playbook as pb_module


def investigate(run_id, connector, project_id, deployment_id, provider):
    try:
        store.update(run_id, 'running')
        store.log(run_id, 'INFO', f'Fetching deployment {deployment_id}')

        # ── 1. Fetch ──────────────────────────────────────────────────────────
        deployments = connector.deployments(project_id)
        selected = next((d for d in deployments if d.get('uid') == deployment_id), None)
        if selected is None:
            raise ValueError('Selected deployment not present in recent project deployments')

        events = connector.deployment_events(deployment_id, limit=200)
        drain  = store.drain_summary(deployment_id)
        store.log(run_id, 'INFO',
                  f'Fetched {len(events)} events; drain_total={drain["total"] or 0}')

        # ── 2. Deterministic analysis ─────────────────────────────────────────
        # Drain is now folded into the metrics dict so the LLM sees it
        summary    = metrics([selected], events, drain)
        evidence   = event_evidence(events, limit=15)   # 15 not 30 → fewer tokens
        categories = fp_module.categorize(evidence, events)
        fingerprint = fp_module.compute(evidence, summary)

        store.log(run_id, 'INFO',
                  f'Fingerprint={fingerprint} | categories={categories}')
        store.update_run_meta(run_id, fingerprint, categories)

        # ── 3. Memory lookup ─────────────────────────────────────────────────
        past          = mem.lookup(project_id, fingerprint)
        approved_past = [p for p in past if p.get('outcome') in ('approved', 'verified')]

        if approved_past:
            # Known, human-approved pattern → reuse stored resolution, skip LLM
            best = approved_past[0]
            store.log(run_id, 'INFO',
                      f'Memory hit (fp={fingerprint}, seen {best["hit_count"]}×). '
                      'Skipping LLM — 0 tokens used.')
            result = {
                'severity':             best.get('severity', 'unknown'),
                'error_category':       str(best.get('error_categories', '')),
                'root_cause_hypothesis': best.get('resolution_summary', 'Known issue'),
                'confidence':           0.90,
                'key_evidence':         [],
                'recommended_steps':    mem.steps_from_entry(best),
                'needs_human_escalation': False,
                'analysis_notes':       (
                    f'Resolution recalled from agent memory '
                    f'(seen {best["hit_count"]}× previously, outcome={best["outcome"]}).'
                ),
                'source': 'memory',
            }
            source = 'memory'

        else:
            # ── 4. Playbook match ─────────────────────────────────────────────
            playbook = pb_module.match(categories)

            if playbook and provider == 'Deterministic only':
                store.log(run_id, 'INFO',
                          f'Playbook match: {playbook["category"]} (deterministic mode). '
                          'Skipping LLM — 0 tokens used.')
                result = {
                    'severity':             playbook['severity'],
                    'error_category':       playbook['category'],
                    'root_cause_hypothesis': playbook['summary'],
                    'confidence':           0.75,
                    'key_evidence':         [],
                    'recommended_steps':    playbook['steps'],
                    'needs_human_escalation': False,
                    'analysis_notes':       f'Matched playbook: {playbook["display"]}',
                    'source': 'playbook',
                }
                source = 'playbook'

            else:
                # ── 5. LLM (last resort, guided by context) ───────────────────
                store.log(run_id, 'INFO',
                          f'Calling LLM ({provider}). '
                          f'Playbook hint: {playbook["category"] if playbook else "none"} | '
                          f'Prior incidents: {len(past)}')
                result = ai_summary(
                    summary, provider, evidence,
                    playbook_hint=playbook,     # narrows scope → fewer tokens
                    memory_context=past,        # injects prior context
                )
                source = 'llm+playbook' if playbook else 'llm'
                store.log(run_id, 'INFO',
                          f'LLM done. severity={result.get("severity")} '
                          f'confidence={result.get("confidence")}')

        # ── 6. Persist to memory (pending until operator reviews) ─────────────
        mem.upsert(
            project_id=project_id,
            fingerprint=fingerprint,
            error_categories=categories,
            resolution_summary=result.get('root_cause_hypothesis', ''),
            recommended_steps=result.get('recommended_steps', []),
            severity=result.get('severity', 'unknown'),
            outcome='pending',
            run_id=run_id,
        )

        # ── 7. Generate final report ──────────────────────────────────────────
        rpt = report(summary, result, evidence, selected, source, past)
        store.update(run_id, 'awaiting_review', rpt)
        store.log(run_id, 'INFO', f'Report ready [{source}]. Awaiting operator review.')

    except Exception as exc:
        store.update(run_id, 'failed', error=f'{type(exc).__name__}: {str(exc)[:250]}')
        store.log(run_id, 'ERROR',
                  f'Investigation failed: {type(exc).__name__}: {str(exc)[:200]}')
