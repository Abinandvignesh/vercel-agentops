import unittest
from unittest.mock import Mock
from agentops.analysis import metrics,redact
from agentops.connectors.vercel import VercelConnector

class CoreTests(unittest.TestCase):
    def test_redaction(self):
        self.assertNotIn('abc123',redact('Authorization: Bearer abc123'))
        self.assertNotIn('person@example.com',redact('person@example.com'))
    def test_metrics(self):
        result=metrics([{'state':'READY'}],[{'type':'stderr','statusCode':500}])
        self.assertEqual(result['error_like_events'],1)
    def test_projects(self):
        s=Mock(); response=Mock(); response.json.return_value={'projects':[{'id':'prj_1','name':'demo','framework':'nextjs'}]}; s.get.return_value=response
        c=VercelConnector('fake',session=s)
        self.assertEqual(c.projects()[0]['name'],'demo')
        s.headers.update.assert_called_once()
    def test_invalid_deployment_id(self):
        with self.assertRaises(ValueError): VercelConnector('fake').deployment_events('../bad')
if __name__=='__main__': unittest.main()

