import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from openai import APIConnectionError, AsyncOpenAI

from scripts.budget_transport import BudgetExceeded, BudgetLedger, BudgetTransport, MeteredStream


def body():
    return {'model': 'qwen/qwen3.5-9b', 'temperature': 0, 'top_p': 1,
            'max_tokens': 8192, 'stream': True, 'reasoning': {'enabled': False},
            'provider': {'order': ['deepinfra/bf16'], 'allow_fallbacks': False,
                         'require_parameters': True,
                         'max_price': {'prompt': 0.1, 'completion': 0.15, 'request': 0}}}


def test_parallel_reservations_cannot_overspend_and_survive_restart(tmp_path):
    async def scenario():
        ledger = BudgetLedger(tmp_path / 'budget.json', '0.07')
        sent = []

        async def backend(request):
            sent.append(request)
            await asyncio.sleep(0)
            return httpx.Response(500, content=b'upstream error')

        async with httpx.AsyncClient(transport=BudgetTransport(
                ledger, httpx.MockTransport(backend))) as client:
            results = await asyncio.gather(*[
                client.post('https://openrouter.ai/api/v1/chat/completions', json=body())
                for _ in range(5)], return_exceptions=True)
        assert len(sent) == 2  # noqa: PLR2004
        assert sum(isinstance(x, BudgetExceeded) for x in results) == 3  # noqa: PLR2004
        assert ledger.committed == 2 * ledger.reserve_usd < Decimal('0.07')
        restored = BudgetLedger(tmp_path / 'budget.json', '0.07')
        with pytest.raises(BudgetExceeded):
            restored.reserve()
    asyncio.run(scenario())


def test_usage_settles_but_missing_usage_keeps_full_reservation(tmp_path):
    async def scenario():
        ledger = BudgetLedger(tmp_path / 'budget.json')
        replies = iter([
            b'data: {"usage":{"cost":0.0004}}\n\ndata: [DONE]\n\n',
            b'data: [DONE]\n\n',
            b'data: {"usage":{"cost":0.0001}}\n\n',  # interrupted: no DONE
        ])
        transport = BudgetTransport(ledger, httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(next(replies)))))
        async with httpx.AsyncClient(transport=transport) as client:
            for _ in range(3):
                await client.post('https://openrouter.ai/api/v1/chat/completions', json=body())
        assert ledger.committed == Decimal('0.00044') + 2 * ledger.reserve_usd
        assert ledger.data['reported_cost_usd'] == '0.0004'
    asyncio.run(scenario())


def test_missing_price_cap_never_reaches_network(tmp_path):
    async def scenario():
        ledger = BudgetLedger(tmp_path / 'budget.json')
        sent = []
        transport = BudgetTransport(ledger, httpx.MockTransport(sent.append))
        payload = body()
        del payload['provider']['max_price']
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError, match='price ceiling'):
                await client.post('https://openrouter.ai/api/v1/chat/completions', json=payload)
        assert sent == [] and ledger.committed == 0
    asyncio.run(scenario())


def test_fragmented_sse_and_unexpected_cost_halt(tmp_path):
    ledger = BudgetLedger(tmp_path / 'budget.json')
    stream = MeteredStream(None, ledger, ledger.reserve())
    for chunk in [b'data: {"usage":{"co', b'st":0.002}}\n\ndat', b'a: [DONE]\n\n']:
        stream.observe(chunk)
    stream.settle()
    stream.settle()
    assert ledger.committed == Decimal('0.00220')
    index = ledger.reserve()
    with pytest.raises(BudgetExceeded):
        ledger.settle(index, Decimal('5'))
    with pytest.raises(BudgetExceeded):
        ledger.reserve()
    assert json.loads((tmp_path / 'budget.json').read_text())['halted'] is True


def test_openai_sdk_retries_are_individually_budgeted(tmp_path):
    async def scenario():
        ledger = BudgetLedger(tmp_path / 'budget.json', '0.07')
        sent = []

        def backend(request):
            sent.append(request)
            return httpx.Response(503, json={'error': {'message': 'unavailable'}})

        http = httpx.AsyncClient(transport=BudgetTransport(ledger, httpx.MockTransport(backend)))
        async with AsyncOpenAI(api_key='test-only', base_url='https://openrouter.ai/api/v1',
                               max_retries=2, http_client=http) as client:
            client._platform = 'test'
            payload = body()
            extra = {key: payload.pop(key) for key in ('provider', 'reasoning')}
            with pytest.raises(APIConnectionError):
                await asyncio.wait_for(client.chat.completions.create(
                    messages=[{'role': 'user', 'content': 'test'}], extra_body=extra, **payload), 10)
        assert len(sent) == 2  # noqa: PLR2004
        assert ledger.committed == 2 * ledger.reserve_usd < Decimal('0.07')
    asyncio.run(scenario())
