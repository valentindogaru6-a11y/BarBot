import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.assistant.evaluation_budget import (
    BudgetedEvaluationTransport,PhysicalCallBudget,ProviderCallBudgetExhausted,
    EvaluationSafetyError,CALL_STAGE,CALL_ROLE,CALL_RETRY,
)
from app.services.assistant.providers import HttpResponse

URL = 'https://api.groq.com/openai/v1/chat/completions'


class Http:
    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure

    async def post_json(self,*args,**kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        if self.failure:
            raise self.failure
        return HttpResponse(200,b'{"usage":{"total_tokens":7}}')


async def call(transport):
    return await transport.post_json(URL,headers={},payload={'model':'synthetic-model'},
        timeout_seconds=1,max_response_bytes=1024)


def test_budget_counts_all_stages_fallback_retry_and_refuses_next_attempt():
    async def scenario():
        http = Http();budget = PhysicalCallBudget(4)
        transport = BudgetedEvaluationTransport(http,budget)
        for stage,role,retry in (('stage_1','primary',0),('stage_2','primary',0),
                                  ('stage_1','fallback',0),('stage_1','primary',1)):
            tokens = CALL_STAGE.set(stage),CALL_ROLE.set(role),CALL_RETRY.set(retry)
            try: await call(transport)
            finally:
                for variable,token in zip((CALL_STAGE,CALL_ROLE,CALL_RETRY),tokens):variable.reset(token)
        with pytest.raises(ProviderCallBudgetExhausted):await call(transport)
        report=budget.report()
        assert http.calls==report['attempted']==report['completed']==4
        assert report['by_stage']=={'stage_1':3,'stage_2':1}
        assert report['fallback_count']==report['retry_count']==1
        assert report['denied']==1
    asyncio.run(scenario())


def test_atomic_acquire_threads_and_concurrent_requests_cannot_exceed_budget():
    budget=PhysicalCallBudget(3)
    def acquire(_):
        try:budget.acquire('groq','synthetic','stage_1');return True
        except ProviderCallBudgetExhausted:return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(acquire,range(50)))==3
    async def scenario():
        budget=PhysicalCallBudget(3);http=Http();transport=BudgetedEvaluationTransport(http,budget)
        results=await asyncio.gather(*(call(transport) for _ in range(30)),return_exceptions=True)
        assert sum(isinstance(result,ProviderCallBudgetExhausted) for result in results)==27
        assert http.calls==budget.report()['attempted']==3
    asyncio.run(scenario())


@pytest.mark.parametrize('error,category',[(TimeoutError(),'timeout'),(RuntimeError('raw-sensitive'),'connection'),(asyncio.CancelledError(),'cancelled')])
def test_failed_and_cancelled_attempts_are_counted_without_exception_text(error,category):
    async def scenario():
        budget=PhysicalCallBudget(1);transport=BudgetedEvaluationTransport(Http(error),budget)
        with pytest.raises(type(error)):await call(transport)
        report=budget.report()
        assert report['attempted']==report['failed']==1
        assert report['calls'][0]['category']==category
        assert 'raw-sensitive' not in str(report)
    asyncio.run(scenario())


@pytest.mark.parametrize('url', ['https://api.telegram.org/bot-secret/getUpdates',
    'https://api.groq.com/openai/v1/audio/transcriptions','https://example.com',
    URL+'?key=secret','http://api.groq.com/openai/v1/chat/completions'])
def test_non_llm_endpoints_are_denied_without_transport(url):
    async def scenario():
        http=Http();budget=PhysicalCallBudget(1);transport=BudgetedEvaluationTransport(http,budget)
        with pytest.raises(EvaluationSafetyError):
            await transport.post_json(url,headers={},payload={'model':'synthetic'})
        assert http.calls==budget.report()['attempted']==0
    asyncio.run(scenario())


@pytest.mark.parametrize('maximum',[0,-1,None,True,1001])
def test_invalid_budget_is_rejected(maximum):
    with pytest.raises(ValueError):PhysicalCallBudget(maximum)
