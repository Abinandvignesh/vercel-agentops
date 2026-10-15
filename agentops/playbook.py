"""
Rule-based playbook engine.
For known error categories, produces a structured resolution dict without any LLM call.
The LLM can still be invoked *on top of* a playbook match to validate or refine steps.
"""

_PLAYBOOKS: dict[str, dict] = {
    'build_oom': {
        'display': 'Build: Out of Memory (exit 137 / OOM)',
        'severity': 'high',
        'summary': 'Build process was killed due to memory exhaustion (exit code 137 or OOM signal).',
        'steps': [
            'Set `NODE_OPTIONS=--max-old-space-size=4096` in Vercel environment variables (project → Settings → Env Vars)',
            'Profile build memory locally: `node --max-old-space-size=4096 node_modules/.bin/next build`',
            'Disable production source maps if not needed: `productionBrowserSourceMaps: false` in `next.config.js`',
            'Consider splitting large pages or disabling static generation for heavy data-fetching pages',
        ],
    },
    'missing_env_var': {
        'display': 'Missing Environment Variable',
        'severity': 'medium',
        'summary': 'Build or runtime failed due to a required environment variable not being set in Vercel.',
        'steps': [
            'Go to Vercel Dashboard → Project → Settings → Environment Variables',
            'Ensure the variable is set for the correct scope (Production / Preview / Development)',
            'Check for typos between the name used in code and the name set in the dashboard',
            'For monorepos: verify the variable is scoped to the correct subproject / root',
        ],
    },
    'build_timeout': {
        'display': 'Build Timeout',
        'severity': 'high',
        'summary': 'Build exceeded the maximum allowed duration.',
        'steps': [
            'Enable Vercel build caching: verify `.next/cache` and `node_modules/.cache` are in cache directories',
            'Profile the slow step locally: `NEXT_TELEMETRY_DISABLED=1 time next build`',
            'Move heavy build-time data fetching to runtime using ISR or on-demand revalidation',
            'Check `postinstall` and custom build scripts for runaway or hanging processes',
        ],
    },
    'module_not_found': {
        'display': 'Module / File Not Found',
        'severity': 'medium',
        'summary': 'Build failed because a required module or file could not be resolved.',
        'steps': [
            'Run `npm install` / `pnpm install` locally and commit the updated lockfile',
            'Check for case-sensitivity issues (Linux filesystem is case-sensitive; macOS is not)',
            'Verify the package is in `dependencies`, not only in `devDependencies`, if needed at runtime',
            'Check `tsconfig.json` / `jsconfig.json` path aliases match actual file locations',
        ],
    },
    'syntax_error': {
        'display': 'Syntax / Parse Error',
        'severity': 'medium',
        'summary': 'Build failed due to a JavaScript or TypeScript syntax error.',
        'steps': [
            'Run `npx tsc --noEmit` locally to get the exact file and line number',
            'Review the most recent commits for the offending file (`git log --diff-filter=M -n 5 -- <file>`)',
            'Add ESLint and `tsc` to your CI pipeline to catch these before a deploy',
        ],
    },
    'type_error': {
        'display': 'Runtime TypeError',
        'severity': 'medium',
        'summary': 'A TypeError was raised, typically from accessing a property on null or undefined.',
        'steps': [
            'Review the error excerpt for the exact property and object that is null/undefined',
            'Add null/undefined guards or optional chaining (`?.`) at the relevant call sites',
            'Check whether an upstream API response schema changed (external data may now be nullable)',
        ],
    },
    'db_error': {
        'display': 'Database Connection / Query Error',
        'severity': 'high',
        'summary': 'Runtime failure due to a database connectivity or schema issue.',
        'steps': [
            'Verify `DATABASE_URL` / connection string is correct in Vercel env vars for this environment',
            'Ensure the database is accessible from Vercel serverless regions (allowlist IPs if required)',
            'For Prisma: confirm `prisma generate` runs at build time via a `postinstall` script',
            'Monitor for connection pool exhaustion — serverless functions can spike concurrent connections rapidly',
        ],
    },
    'network_error': {
        'display': 'Network / External API Failure',
        'severity': 'medium',
        'summary': 'Build or runtime failed because an external network call timed out or was refused.',
        'steps': [
            'Check whether the external service blocks Vercel serverless IP ranges',
            'Implement retry logic with exponential backoff on the failing client',
            'Move build-time fetches that can fail to runtime, with graceful fallbacks',
            'Review Vercel function timeout settings — default is 10 s (Pro: 60 s)',
        ],
    },
    'rate_limit': {
        'display': 'Rate Limit (HTTP 429)',
        'severity': 'medium',
        'summary': 'Requests to an external API were throttled.',
        'steps': [
            'Implement exponential backoff with jitter on the rate-limited API client',
            'Cache API responses where possible to reduce call frequency',
            'Audit whether build-time calls are redundant (e.g., same data fetched per page)',
            'Consider upgrading to a higher API tier if load is legitimate',
        ],
    },
    'auth_error': {
        'display': 'Authentication / Authorisation Error (401 / 403)',
        'severity': 'high',
        'summary': 'A request was rejected due to invalid or missing credentials.',
        'steps': [
            'Verify the API token / secret is set correctly in Vercel env vars and has not expired',
            'Check token scopes — the token may lack permission for the requested resource',
            'Rotate the credential and redeploy if a leak is suspected',
        ],
    },
    'import_error': {
        'display': 'Import / Module Resolution Error',
        'severity': 'medium',
        'summary': 'A module could not be imported at build or runtime.',
        'steps': [
            'Verify the package is listed in `requirements.txt` / `package.json`',
            'Check that optional dependencies are wrapped in conditional imports',
            'Ensure the build environment matches the runtime Python/Node version',
        ],
    },
}


def match(categories: list[str]) -> dict | None:
    """
    Return the first matching playbook dict for the given category list, or None.
    """
    for cat in categories:
        pb = _PLAYBOOKS.get(cat)
        if pb:
            return {'category': cat, **pb}
    return None


def format_markdown(pb: dict) -> str:
    """Render a playbook as a markdown block for embedding in reports."""
    lines = [f"### {pb['display']}", '', pb['summary'], '', '**Recommended steps:**', '']
    for i, step in enumerate(pb['steps'], 1):
        lines.append(f'{i}. {step}')
    return '\n'.join(lines)
