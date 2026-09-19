"""Fail-closed USD accounting around every HTTP attempt, including SDK retries."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx


class BudgetExceeded(RuntimeError):
    pass


class BudgetLedger:
    # Full advertised context plus the unchanged completion limit, with 10% margin.
    # Provider max_price in each request prevents a price increase from bypassing this.
    reserve_usd = (Decimal(262144) * Decimal('0.0000001')
                   + Decimal(8192) * Decimal('0.00000015')) * Decimal('1.10')

    def __init__(self, path: Path, limit: str = '1.00'):
        self.path = path
        self.limit = Decimal(limit)
        self.data = json.loads(path.read_text()) if path.exists() else {
            'limit_usd': str(self.limit), 'requests': [],
            'context_bound': 262144, 'completion_bound': 8192,
            'margin': '1.10', 'provider': 'deepinfra/bf16',
            'pricing_source': 'https://openrouter.ai/api/v1/models/qwen/qwen3.5-9b/endpoints',
        }
        if Decimal(self.data['limit_usd']) != self.limit:
            raise ValueError('Cannot change the budget of an existing ledger.')

    @property
    def committed(self):
        return sum((Decimal(r['upper_bound_usd']) for r in self.data['requests']), Decimal(0))

    def save(self):
        self.data['committed_upper_bound_usd'] = str(self.committed)
        self.data['reported_cost_usd'] = str(sum(
            (Decimal(r.get('reported_cost_usd', '0')) for r in self.data['requests']), Decimal(0)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data, indent=2) + '\n')
        temporary.replace(self.path)

    def reserve(self):
        if self.data.get('halted') or self.committed + self.reserve_usd > self.limit:
            raise BudgetExceeded('USD budget exhausted; request blocked before network send.')
        record = {'upper_bound_usd': str(self.reserve_usd), 'state': 'reserved'}
        self.data['requests'].append(record)
        self.save()  # Durable before sending; a crash retains the entire reservation.
        return len(self.data['requests']) - 1

    def settle(self, index: int, cost: Decimal):
        record = self.data['requests'][index]
        if record['state'] == 'settled':
            return
        if not cost.is_finite() or cost < 0 or cost * Decimal('1.10') > self.reserve_usd:
            self.data['halted'] = True
            self.save()
            raise BudgetExceeded('Unexpected billing value; further requests blocked.')
        record.update(state='settled', reported_cost_usd=str(cost),
                      upper_bound_usd=str(cost * Decimal('1.10')))
        self.save()


class MeteredStream(httpx.AsyncByteStream):
    def __init__(self, inner, ledger: BudgetLedger, index: int):
        self.inner, self.ledger, self.index = inner, ledger, index
        self.buffer = b''
        self.cost = None
        self.done = False

    def observe(self, chunk):
        self.buffer += chunk
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            if not line.startswith(b'data:'):
                continue
            data = line[5:].strip()
            if data == b'[DONE]':
                self.done = True
                continue
            try:
                event = json.loads(data, parse_float=Decimal)
                usage = event.get('usage') or {}
                if usage.get('cost') is not None:
                    self.cost = Decimal(str(usage['cost']))
            except (ValueError, TypeError, AttributeError):
                continue

    def settle(self):
        # Missing final usage or interrupted streams retain the full worst-case cost.
        if self.done and self.cost is not None:
            self.ledger.settle(self.index, self.cost)

    async def __aiter__(self):
        try:
            async for chunk in self.inner:
                self.observe(chunk)
                yield chunk
        finally:
            self.settle()

    async def aclose(self):
        try:
            await self.inner.aclose()
        finally:
            self.settle()


class BudgetTransport(httpx.AsyncBaseTransport):
    def __init__(self, ledger: BudgetLedger, inner=None):
        self.ledger = ledger
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        body = json.loads(request.content)
        expected = {'model': 'qwen/qwen3.5-9b', 'temperature': 0, 'top_p': 1,
                    'max_tokens': 8192, 'stream': True}
        if (request.method != 'POST' or request.url.host != 'openrouter.ai'
                or request.url.path != '/api/v1/chat/completions'
                or any(body.get(k) != v for k, v in expected.items())
                or body.get('reasoning') != {'enabled': False}
                or body.get('plugins') or body.get('models')):
            raise ValueError('Request does not match the authorized fixed protocol.')
        provider = body.get('provider', {})
        if (provider.get('order') != ['deepinfra/bf16']
                or provider.get('allow_fallbacks') is not False
                or provider.get('require_parameters') is not True
                or provider.get('max_price') != {'prompt': 0.1, 'completion': 0.15, 'request': 0}):
            raise ValueError('Request is missing the authorized routing/price ceiling.')
        index = self.ledger.reserve()
        response = await self.inner.handle_async_request(request)
        response.stream = MeteredStream(response.stream, self.ledger, index)
        return response

    async def aclose(self):
        await self.inner.aclose()
