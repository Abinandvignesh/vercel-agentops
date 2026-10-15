"""Read-only Vercel connector. Events endpoint is deployment/build events, not a full runtime-log archive."""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = 'https://api.vercel.com'

class VercelError(Exception):
    pass

class VercelConnector:
    def __init__(self, token, team_id=None, session=None):
        if not token:
            raise ValueError('VERCEL_TOKEN is required')
        self.team_id = team_id or None
        self.session = session or requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
        if session is None:
            retry = Retry(total=2, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'])
            self.session.mount('https://', HTTPAdapter(max_retries=retry))

    def _get(self, path, **params):
        if self.team_id:
            params['teamId'] = self.team_id
        try:
            response = self.session.get(API + path, params=params, timeout=(5, 20))
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else '?'
            raise VercelError(f'Vercel API HTTP {status}; check token, team scope and rate limits') from exc
        except (requests.RequestException, ValueError) as exc:
            raise VercelError(f'Vercel API request failed: {type(exc).__name__}') from exc

    def projects(self, limit=50):
        data = self._get('/v9/projects', limit=min(limit, 100))
        return [{'id': x.get('id'), 'name': x.get('name'), 'framework': x.get('framework')} for x in data.get('projects', [])]

    def deployments(self, project_id, limit=20):
        data = self._get('/v6/deployments', projectId=project_id, limit=min(limit, 100))
        return [{'uid': x.get('uid') or x.get('id'), 'name': x.get('name'), 'state': x.get('state') or x.get('readyState'), 'created': x.get('created'), 'url': x.get('url'), 'target': x.get('target')} for x in data.get('deployments', [])]

    def deployment_events(self, deployment_id, limit=100):
        if not deployment_id or not deployment_id.startswith('dpl_'):
            raise ValueError('Invalid deployment identifier')
        data = self._get(f'/v3/deployments/{deployment_id}/events', limit=min(limit, 200), direction='backward', follow=0)
        if not isinstance(data, list):
            return []
        return [{'type': e.get('type'), 'created': e.get('created'), 'text': str((e.get('payload') or {}).get('text') or '')[:1000], 'statusCode': (e.get('payload') or {}).get('statusCode')} for e in data]
