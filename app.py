import os, threading
import streamlit as st
from dotenv import load_dotenv
load_dotenv()
from agentops import store
from agentops import memory as mem
from agentops.connectors.vercel import VercelConnector, VercelError
from agentops.runner import investigate

st.set_page_config(page_title='AgentOps Control Centre', page_icon='🛠️', layout='wide')
store.init()

# ── Pages ────────────────────────────────────────────────────────────────────
page = st.sidebar.selectbox('Navigate', ['🛠️ Control Centre', '🧠 Knowledge Base'])

# ── Sidebar: connection + engine ──────────────────────────────────────────────
with st.sidebar:
    st.header('Connection')
    st.write('Vercel:', 'Configured ✅' if os.getenv('VERCEL_TOKEN') else 'Not configured ❌')
    st.write('Team:', os.getenv('VERCEL_TEAM_ID') or 'Personal account')
    provider = st.selectbox(
        'Analysis engine',
        ['Deterministic only', 'Gemini', 'OpenAI'],
        help=(
            '**Deterministic only** — free, uses fingerprints + playbooks.\n\n'
            '**Gemini / OpenAI** — called only for novel patterns not in memory or playbooks.'
        ),
    )
    if provider == 'Gemini' and not os.getenv('GEMINI_API_KEY'):
        st.warning('Set GEMINI_API_KEY in .env')
    if provider == 'OpenAI' and not os.getenv('OPENAI_API_KEY'):
        st.warning('Set OPENAI_API_KEY in .env')

    # Live memory stats
    try:
        s = mem.stats()
        st.divider()
        st.caption('**Agent memory**')
        col1, col2 = st.columns(2)
        col1.metric('Known patterns', s.get('total') or 0)
        col2.metric('Approved', s.get('approved') or 0)
    except Exception:
        pass

    st.caption('AI receives only aggregate metrics + redacted excerpts — never raw log text.')
    st.warning('Local prototype: bind to localhost. Remote access via SSH tunnel only.')


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 1 — Control Centre
# ═══════════════════════════════════════════════════════════════════════════════
if page == '🛠️ Control Centre':
    st.title('🛠️ AgentOps Control Centre')
    st.caption(
        'Self-hosted Vercel deployment operations · read-only connector · '
        'human-reviewed reports · smart memory 🧠'
    )

    connector = None
    if os.getenv('VERCEL_TOKEN'):
        connector = VercelConnector(os.environ['VERCEL_TOKEN'], os.getenv('VERCEL_TEAM_ID'))

    if connector:
        try:
            projects = connector.projects()
            if not projects:
                st.info('No projects visible to this token. Check team scope.')
            else:
                names = {p['id']: p['name'] for p in projects if p.get('id')}
                project_id = st.selectbox('Vercel project', list(names),
                                          format_func=lambda p: names[p])
                deployments = connector.deployments(project_id)
                a, b, c = st.columns(3)
                a.metric('Projects accessible', len(projects))
                b.metric('Recent deployments', len(deployments))
                c.metric('Latest state',
                         deployments[0].get('state') or 'Unknown' if deployments else 'None')

                st.subheader('Recent deployments')
                st.dataframe(deployments, hide_index=True, use_container_width=True)

                deployment_ids = [d['uid'] for d in deployments if d.get('uid')]
                selected_deployment = (
                    st.selectbox(
                        'Deployment to investigate', deployment_ids,
                        format_func=lambda uid: next(
                            (f"{d.get('name', '')} | {uid} | {d.get('state', '')} | {d.get('target') or 'preview'}"
                             for d in deployments if d.get('uid') == uid),
                            uid,
                        ),
                    ) if deployment_ids else None
                )

                llm_blocked = (
                    (provider == 'Gemini' and not os.getenv('GEMINI_API_KEY')) or
                    (provider == 'OpenAI' and not os.getenv('OPENAI_API_KEY'))
                )
                if st.button('▶ Investigate selected deployment', type='primary',
                             disabled=not selected_deployment or llm_blocked):
                    run_id = store.create(project_id, names[project_id], selected_deployment)
                    threading.Thread(
                        target=investigate,
                        args=(run_id, connector, project_id, selected_deployment, provider),
                        daemon=True,
                    ).start()
                    st.success(f'Investigation #{run_id} started. Use Refresh to see progress.')

        except VercelError as exc:
            st.error(str(exc))
    else:
        st.info('Add VERCEL_TOKEN to .env and restart Streamlit to connect a real Vercel account.')

    st.divider()
    st.subheader('Execution history')
    if st.button('↻ Refresh'):
        st.rerun()

    runs = store.runs()
    if runs:
        st.dataframe(
            [{k: r[k] for k in ('id', 'project_name', 'status', 'started', 'ended')}
             for r in runs],
            hide_index=True, use_container_width=True,
        )
        run_id = st.selectbox(
            'Inspect run', [r['id'] for r in runs],
            format_func=lambda x: f'Run #{x}',
        )
        selected = next(r for r in runs if r['id'] == run_id)
        tab1, tab2 = st.tabs(['Report and approval', 'Execution logs'])

        with tab1:
            st.write('Status:', selected['status'])
            if selected['error']:
                st.error(selected['error'])
            if selected['report']:
                st.markdown(selected['report'])
            if selected['status'] == 'awaiting_review':
                left, right = st.columns(2)
                if left.button('✅ Approve report'):
                    store.review(run_id, 'approved')
                    mem.approve(run_id)   # promote memory entry → future LLM-skip eligible
                    st.rerun()
                if right.button('❌ Reject report'):
                    store.review(run_id, 'rejected')
                    mem.reject(run_id)    # mark as rejected so it won't be reused blindly
                    st.rerun()

        with tab2:
            st.dataframe(store.logs(run_id), hide_index=True, use_container_width=True)

    else:
        st.caption('No investigations yet.')

    st.caption(
        'V2: Memory + Playbook engine active. '
        'LLM is called only for novel patterns not already in memory or playbooks.'
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 2 — Knowledge Base
# ═══════════════════════════════════════════════════════════════════════════════
elif page == '🧠 Knowledge Base':
    st.title('🧠 Agent Knowledge Base')
    st.caption(
        'Every approved investigation is stored here keyed by error fingerprint. '
        'When the same fingerprint recurs, the agent skips the LLM and reuses the resolution — '
        'free of charge.'
    )

    try:
        s = mem.stats()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric('Total patterns', s.get('total') or 0)
        c2.metric('✅ Approved', s.get('approved') or 0,
                  help='Used for LLM-free resolution on next match')
        c3.metric('⏳ Pending review', s.get('pending') or 0)
        c4.metric('❌ Rejected', s.get('rejected') or 0,
                  help='Will not be reused; LLM re-runs for this fingerprint')

        st.divider()
        entries = mem.all_entries()
        if not entries:
            st.info('No patterns stored yet. Run and approve your first investigation above.')
        else:
            # Outcome colour coding
            _OUTCOME_BADGE = {
                'approved': '✅',
                'verified': '🏆',
                'pending':  '⏳',
                'rejected': '❌',
            }

            for entry in entries:
                badge = _OUTCOME_BADGE.get(entry.get('outcome', ''), '❓')
                cats  = entry.get('error_categories', '[]')
                sev   = entry.get('severity', 'unknown')
                hits  = entry.get('hit_count', 1)

                with st.expander(
                    f"{badge} [{sev.upper()}] {entry.get('resolution_summary', 'No summary')[:80]}  "
                    f"· seen {hits}×  · fp `{entry.get('fingerprint', '')}`",
                    expanded=False,
                ):
                    col_a, col_b, col_c = st.columns(3)
                    col_a.write(f"**Project:** `{entry.get('project_id', 'unknown')}`")
                    col_b.write(f"**Outcome:** {badge} {entry.get('outcome', 'N/A')}")
                    col_c.write(f"**Last seen:** {entry.get('last_seen', 'N/A')}")

                    st.write(f"**Categories:** `{cats}`")
                    st.write(f"**Fingerprint:** `{entry.get('fingerprint', '')}`")

                    steps = mem.steps_from_entry(entry)
                    if steps:
                        st.write('**Recommended steps:**')
                        for i, step in enumerate(steps, 1):
                            st.write(f'{i}. {step}')

                    run_link = entry.get('run_id')
                    if run_link:
                        st.caption(f'Source run: #{run_link}')

    except Exception as exc:
        st.error(f'Knowledge base unavailable: {exc}')
