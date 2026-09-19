"""Resume only missing Qwen cases under the user's persistent $1 ceiling."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from scripts import run_fixed_dataset_with_provider as provider_run
from scripts.budget_transport import BudgetLedger, BudgetTransport

PRIVATE = Path('outputs/front-a-300/qwen_qwen3.5-9b/private')


async def run():
    ledger = BudgetLedger(PRIVATE / 'budget-usd1-20260919.json')
    ledger.save()
    clients = []
    original_model = provider_run.routed_model

    def model_factory(provider):
        model = original_model(provider)
        model.extra_body['provider']['max_price'] = {
            'prompt': 0.1, 'completion': 0.15, 'request': 0,
        }
        client = httpx.AsyncClient(transport=BudgetTransport(ledger))
        clients.append(client)
        model.http_client = client
        return model

    config_path = PRIVATE / 'run-config.json'
    config = json.loads(config_path.read_text())
    config['budget_control'] = {
        'command': '.venv/bin/python -m scripts.run_qwen_budgeted',
        'authorized_limit_usd': '1.00', 'ledger': 'private/budget-usd1-20260919.json',
        'per_http_attempt_reservation_usd': str(ledger.reserve_usd),
        'unsettled_requests_keep_full_reservation': True,
        'provider_max_price_per_million_tokens': {'prompt': 0.1, 'completion': 0.15},
        'provider_max_price_per_request': 0,
        'margin': '1.10', 'campaign_passes': 1,
        'scope': 'Only existing pending fixed case IDs; approved checkpoints reused.',
        'implementation_sha256': {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in ('scripts/budget_transport.py', 'scripts/run_qwen_budgeted.py')},
    }
    provider_run.write_json(config_path, config)
    status = {'status': 'running', 'pid': os.getpid(), 'attempt': 1,
              'provider': 'deepinfra/bf16', 'budget_usd': '1.00',
              'started_at': datetime.now(UTC).isoformat()}
    provider_run.write_json(PRIVATE / 'collection-status.json', status)
    args = SimpleNamespace(campaign=Path('configs/campaigns/front-a-fixed-300.yaml'),
                           model='qwen/qwen3.5-9b', provider='deepinfra/bf16',
                           max_parallel=5, limit=None, output_dir=None)
    try:
        with patch.object(provider_run, 'routed_model', model_factory):
            code = await provider_run.run(args)
        status.update(status='complete' if code == 0 else 'incomplete', exit_code=code)
        return code
    finally:
        for client in clients:
            await client.aclose()
        ledger.save()
        status.update(worker_running=False, finished_at=datetime.now(UTC).isoformat(),
                      committed_upper_bound_usd=str(ledger.committed),
                      reported_cost_usd=ledger.data.get('reported_cost_usd', '0'))
        if status['status'] == 'running':
            status['status'] = 'interrupted'
        provider_run.write_json(PRIVATE / 'collection-status.json', status)


if __name__ == '__main__':
    PRIVATE.mkdir(parents=True, exist_ok=True)
    with (PRIVATE / 'collection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raise SystemExit(asyncio.run(run()))
