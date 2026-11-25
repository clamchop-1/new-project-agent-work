import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_core import AgentError
from research_service import FirecrawlKeylessProvider, ResearchAuthorizationService


class FakeProvider:
    provider_id = 'fixture_search'

    def __init__(self, *, results=3, fail_read=0):
        self.searches=[]; self.reads=[]; self.results=results; self.fail_read=fail_read

    def search(self, query, limit):
        self.searches.append((query,limit))
        return {'results':[{'url':f'https://public.example/page-{index}',
            'title':f'Page {index}', 'snippet':'untrusted snippet'}
            for index in range(min(self.results,limit))]}

    def read(self, url):
        self.reads.append(url)
        if len(self.reads) <= self.fail_read:
            raise AgentError('fixture source unavailable')
        text='Verified public source text for '+url
        return {'source_id':f'{len(self.reads):032x}', 'kind':'webpage', 'url':url,
            'title':'Verified '+url.rsplit('/',1)[-1],
            'retrieved_at':'2026-11-24T12:00:00+00:00','publication_date':None,
            'content':text,'content_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'untrusted_content':True}


class ResearchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.audit=Path(self.temp.name)
        self.provider=FakeProvider()
        self.service=ResearchAuthorizationService(self.audit,self.provider)
        self.conversation='a'*32;self.model='b'*64

    def tearDown(self):
        self.temp.cleanup()

    def proposal(self, objective='latest Python packaging guidance'):
        return self.service.propose(self.conversation,objective,self.model)

    def approved_session(self, objective='latest Python packaging guidance'):
        proposal=self.proposal(objective)
        decision=self.service.decide(proposal['proposal_id'],self.conversation,objective,
            self.model,'approved')
        return self.service.consume_resume(decision['resume_id'],self.conversation,objective,
            self.model,'approved')

    def test_approval_is_bound_to_conversation_objective_and_model_and_one_use(self):
        proposal=self.proposal()
        with self.assertRaises(AgentError):
            self.service.decide(proposal['proposal_id'],'c'*32,'latest Python packaging guidance',
                self.model,'approved')
        with self.assertRaises(AgentError):
            self.service.decide(proposal['proposal_id'],self.conversation,'different topic',
                self.model,'approved')
        proposal=self.proposal()
        decision=self.service.decide(proposal['proposal_id'],self.conversation,
            'latest Python packaging guidance',self.model,'approved')
        session=self.service.consume_resume(decision['resume_id'],self.conversation,
            'latest Python packaging guidance',self.model,'approved')
        self.assertEqual(session['search_requests'],1)
        with self.assertRaises(AgentError):
            self.service.consume_resume(decision['resume_id'],self.conversation,
                'latest Python packaging guidance',self.model,'approved')

    def test_pending_approval_is_bound_to_the_search_provider(self):
        proposal=self.proposal()
        self.service.provider=type('OtherProvider',(FakeProvider,),{'provider_id':'other_search'})()
        with self.assertRaises(AgentError):
            self.service.decide(proposal['proposal_id'],self.conversation,
                'latest Python packaging guidance',self.model,'approved')

    def test_firecrawl_adapter_posts_bounded_json_to_its_fixed_tls_host(self):
        payload=json.dumps({'success':True,'data':{'web':[{'title':'Python docs',
            'url':'https://docs.python.org/3/library/asyncio-task.html',
            'description':'Official task documentation'}]}}).encode()
        class Response:
            status=200
            def getheader(self,name,default=''):
                return 'application/json' if name=='Content-Type' else str(len(payload))
            def read(self,limit):return payload
        class Connection:
            def __init__(self,*args,**kwargs):self.request_args=None
            def request(self,*args,**kwargs):self.request_args=(args,kwargs)
            def getresponse(self):return Response()
            def close(self):pass
        connection=Connection()
        with patch('web_retrieval._safe_url',return_value=('api.firecrawl.dev','93.184.216.34',
                '/v2/search','https://api.firecrawl.dev/v2/search')), \
             patch('web_retrieval._PinnedHTTPSConnection',return_value=connection):
            result=FirecrawlKeylessProvider().search('Python asyncio documentation',5)
        args,kwargs=connection.request_args
        self.assertEqual(args[0:2],('POST','/v2/search'))
        request=json.loads(kwargs['body'])
        self.assertEqual(request,{'query':'Python asyncio documentation','limit':5,'sources':['web']})
        self.assertNotIn('Authorization',kwargs['headers'])
        self.assertEqual(result['results'][0]['url'],'https://docs.python.org/3/library/asyncio-task.html')
        self.assertEqual(result['provider'],'firecrawl_keyless')

    def test_denial_and_cancel_do_not_authorize_network(self):
        proposal=self.proposal()
        decision=self.service.decide(proposal['proposal_id'],self.conversation,
            'latest Python packaging guidance',self.model,'offline')
        with self.assertRaises(AgentError):
            self.service.consume_resume(decision['resume_id'],self.conversation,
                'latest Python packaging guidance',self.model,'approved')
        cancelled=self.proposal('latest Rust documentation')
        result=self.service.decide(cancelled['proposal_id'],self.conversation,
            'latest Rust documentation',self.model,'cancel')
        self.assertIsNone(result['resume_id'])
        self.assertEqual(self.provider.searches,[])

    def test_live_service_enforces_bounds_validates_sources_and_emits_progress(self):
        session=self.approved_session()
        progress=[];cancel=__import__('threading').Event()
        with patch('web_retrieval._safe_url',side_effect=lambda url:
                ('public.example','93.184.216.34','/'+url.rsplit('/',1)[-1],url)):
            result=self.service.execute(session,cancel,progress.append)
        self.assertEqual(len(self.provider.searches),1)
        self.assertEqual(len(self.provider.reads),3)
        self.assertEqual(len(result['sources']),3)
        self.assertLessEqual(sum(len(row['excerpt']) for row in result['context']),6000)
        self.assertTrue(result['sources'][0]['untrusted_content'])
        self.assertIn('Searching online',progress[0])
        self.assertTrue(any('Retrieving' in item for item in progress))
        metadata=self.service.source_for_retention(result['sources'][0]['source_id'],self.conversation)
        self.assertEqual(metadata['provider_id'],'fixture_search')
        with self.assertRaises(AgentError):
            self.service.source_for_retention(result['sources'][0]['source_id'],'c'*32)

    def test_failed_fetches_count_against_page_request_budget(self):
        provider=FakeProvider(results=5,fail_read=5)
        service=ResearchAuthorizationService(self.audit,provider)
        proposal=service.propose(self.conversation,'current Rust release',self.model)
        decision=service.decide(proposal['proposal_id'],self.conversation,
            'current Rust release',self.model,'approved')
        session=service.consume_resume(decision['resume_id'],self.conversation,
            'current Rust release',self.model,'approved')
        cancel=__import__('threading').Event()
        with patch('web_retrieval._safe_url',side_effect=lambda url:
                ('public.example','93.184.216.34','/'+url.rsplit('/',1)[-1],url)):
            result=service.execute(session,cancel,lambda _:None)
        self.assertEqual(len(provider.reads),3)
        self.assertEqual(result['sources'],[])

    def test_cancel_stops_before_next_request(self):
        class CancelAfterFirst(FakeProvider):
            def __init__(self): super().__init__(results=3);self.cancel=None
            def read(self,url):
                result=super().read(url)
                self.cancel.set()
                return result
        provider=CancelAfterFirst();service=ResearchAuthorizationService(self.audit,provider)
        proposal=service.propose(self.conversation,'current secure Python docs',self.model)
        decision=service.decide(proposal['proposal_id'],self.conversation,
            'current secure Python docs',self.model,'approved')
        session=service.consume_resume(decision['resume_id'],self.conversation,
            'current secure Python docs',self.model,'approved')
        import threading
        cancel=threading.Event();provider.cancel=cancel
        with patch('web_retrieval._safe_url',side_effect=lambda url:
                ('public.example','93.184.216.34','/'+url.rsplit('/',1)[-1],url)):
            with self.assertRaises(AgentError):
                service.execute(session,cancel,lambda _:None)
        self.assertEqual(len(provider.reads),1)

    def test_explicit_session_cache_is_conversation_scoped_and_audits_hashes_only(self):
        session=self.service.authorize_explicit(self.conversation,'research current Python wheels',self.model)
        import threading
        with patch('web_retrieval._safe_url',side_effect=lambda url:
                ('public.example','93.184.216.34','/'+url.rsplit('/',1)[-1],url)):
            first=self.service.execute(session,threading.Event(),lambda _:None)
            second_session=self.service.authorize_explicit(self.conversation,
                'research current Python wheels',self.model)
            second=self.service.execute(second_session,threading.Event(),lambda _:None)
        self.assertFalse(first['cached']);self.assertTrue(second['cached'])
        self.assertEqual(len(self.provider.searches),1)
        audit=(self.audit/'research-audit.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('research current Python wheels',audit)
        self.assertIn(hashlib.sha256(b'research current Python wheels').hexdigest(),audit)
        audit_rows=[json.loads(line) for line in audit.splitlines()]
        self.assertEqual(len(audit_rows),4)
        self.assertEqual(audit_rows[-1]['state'],'research_cache_hit')

    def test_external_request_interval_is_rate_limited_without_fallback(self):
        now=[100.0]
        service=ResearchAuthorizationService(provider=self.provider,clock=lambda:now[0])
        first=service.authorize_explicit(self.conversation,'research Python task groups',self.model)
        second=service.authorize_explicit(self.conversation,'research Python cancellation',self.model)
        import threading
        safe=patch('web_retrieval._safe_url',side_effect=lambda url:
            ('public.example','93.184.216.34','/'+url.rsplit('/',1)[-1],url))
        with safe:
            service.execute(first,threading.Event(),lambda _:None)
            with self.assertRaisesRegex(AgentError,'rate limited'):
                service.execute(second,threading.Event(),lambda _:None)
            self.assertEqual(len(self.provider.searches),1)
            now[0]+=2
            service.execute(second,threading.Event(),lambda _:None)
        self.assertEqual(len(self.provider.searches),2)

    def test_query_privacy_removes_code_and_paths_and_rejects_empty_result(self):
        from research_service import sanitize_research_query
        query=sanitize_research_query('Look up Python retry behavior at C:\\Users\\me\\repo\\a.py `token = abc`')
        self.assertNotIn('C:\\Users',query)
        self.assertNotIn('token',query)
        with self.assertRaises(AgentError):
            sanitize_research_query('C:\\Users\\me\\repo\\private.py')


if __name__=='__main__':
    unittest.main()
