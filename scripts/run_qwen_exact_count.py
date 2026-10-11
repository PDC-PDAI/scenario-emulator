"""Audited exact-count tool contract for pending Qwen cases; same shared USD budget."""
from __future__ import annotations

import argparse
import asyncio
import copy
import fcntl
import hashlib
import json
import os
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from scripts import run_fixed_dataset_with_provider as provider_run
from scripts.budget_transport import BudgetLedger, BudgetTransport
from src.agents.generation import generation_scope
from src.services.dataset import fixed as fixed_dataset
from src.services.dataset.fixed import load_fixed_inputs, validate_clean_baseline
from src.services.dataset.profile import load_dataset_campaign_profile

PRIVATE = Path('outputs/front-a-300/qwen_qwen3.5-9b/private')
CONTEXT = ContextVar('exact_count_case', default=None)
ORIGINAL_MODEL = provider_run.routed_model
MAX_QUESTIONS = 50


def strict_objects(node):
    if isinstance(node, dict):
        node.pop('default', None)
        if node.get('type') == 'object':
            node['required'] = list(node.get('properties', {}))
            node['additionalProperties'] = False
        for child in node.values():
            strict_objects(child)
    elif isinstance(node, list):
        for child in node:
            strict_objects(child)


def constrain_tools(tools, count):
    if not isinstance(count, int) or not 1 <= count <= MAX_QUESTIONS:
        raise ValueError('An explicit valid question count is required.')
    result = copy.deepcopy(tools)
    for tool in result:
        function = tool.get('function', {})
        if function.get('name') != 'salvar_formulario':
            continue
        schema = function['parameters']
        questions = schema['properties']['payload']['properties']['questions']
        questions.update(minItems=count, maxItems=count)
        questions['description'] = (
            f'OBRIGATÓRIO: lista com EXATAMENTE {count} perguntas completas. '
            f'Preencha todas as posições de 1 até {count}, inclusive a posição {count}. '
            f'Não envie {count - 1} perguntas. Conte os objetos antes de chamar esta ferramenta.'
        )
        strict_objects(schema)
        function['strict'] = True
        function['description'] = (function.get('description', '')
            + f' IMPORTANTE PARA ESTA EXECUÇÃO: o coordenador pediu EXATAMENTE {count} perguntas. '
            f'Prepare {count} objetos completos e distintos, um para cada posição: '
            + ', '.join(str(i) for i in range(1, count + 1))
            + f'. Não use a quantidade padrão de 5. Só chame salvar_formulario quando '
            f'payload.questions tiver exatamente {count} objetos; confira a contagem antes da chamada.')
    return result


class ExactCountService(provider_run.AuditedQuestionnaireService):
    async def execute(self, job, prompt, **kwargs):
        context = {'count': prompt.requested_question_count, 'schema': None}
        token = CONTEXT.set(context)
        try:
            result = await super().execute(job, prompt, **kwargs)
            audit_path = self.private / 'provider-executions' / f'{result.trajectory_id}.json'
            audit = json.loads(audit_path.read_text())
            audit['tool_schema_adjustment'] = {
                'version': 'exact-count-v4-terminal-save-outcome', 'requested_count': context['count'],
                'strict_save_tool': context['schema'],
                'adapter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'system_and_user_prompts_changed': False,
                'successful_save_is_terminal_for_clean_trajectory': True,
                'note': 'Modified tool contract; post-save runtime errors are operational warnings after task completion; distinguish from the original Gemma protocol.',
            }
            provider_run.write_json(audit_path, audit)
            return result
        finally:
            CONTEXT.reset(token)


async def run(probe_case=None):  # noqa: PLR0915 - linear, audited collection lifecycle
    ledger = BudgetLedger(PRIVATE / 'budget-usd1-20260919.json')
    clients = []
    started = datetime.now(UTC).isoformat()
    status = {'status': 'running', 'pid': os.getpid(), 'attempt': 1,
              'provider': 'deepinfra/bf16', 'budget_usd': '1.00',
              'schema_adjustment': 'exact-count-v4-terminal-save-outcome', 'probe_case': probe_case,
              'started_at': started}
    provider_run.write_json(PRIVATE / 'collection-status.json', status)
    config_path = PRIVATE / 'run-config.json'
    config = json.loads(config_path.read_text())
    phase = {'version': 'exact-count-v4-terminal-save-outcome', 'started_at': started, 'probe_case': probe_case,
             'scope': 'Pending cases only; original commands and generation parameters preserved.',
             'terminal_turn_policy': 'A successful salvar_formulario completes the task; a later model/provider error does not add a failure step to an already successful trajectory and is logged separately.',
             'comparison_note': 'These executions use a stricter tool contract and terminal-save outcome handling than Gemma.',
             'budget_reservation': {'strategy': 'serialized_request_bytes_plus_16384_framing_tokens', 'completion_tokens': 8192, 'margin': '1.10', 'unmetered_calls_retain_full_per_request_bound': True},
             'adapter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    config.setdefault('tool_schema_adjustment_phases', []).append(phase)
    provider_run.write_json(config_path, config)

    def model_factory(provider):
        model = ORIGINAL_MODEL(provider)
        context = CONTEXT.get()
        if context is None:
            raise ValueError('Missing per-case exact-count context.')
        original_params = model.get_request_params

        def request_params(*args, **kwargs):
            params = original_params(*args, **kwargs)
            if params.get('tools'):
                params['tools'] = constrain_tools(params['tools'], context['count'])
                context['schema'] = next(t['function'] for t in params['tools']
                    if t.get('function', {}).get('name') == 'salvar_formulario')
            return params

        model.get_request_params = request_params
        model.extra_body['provider']['max_price'] = {'prompt': 0.1, 'completion': 0.15, 'request': 0}
        client = httpx.AsyncClient(transport=BudgetTransport(ledger))
        clients.append(client)
        model.http_client = client
        return model

    try:
        with (patch.object(provider_run, 'routed_model', model_factory),
              patch.object(provider_run, 'AuditedQuestionnaireService', ExactCountService)):
            if probe_case:
                campaign = load_dataset_campaign_profile(Path('configs/campaigns/front-a-fixed-300.yaml'))
                generation = campaign.generation.model_copy(update={'model': 'qwen/qwen3.5-9b'})
                cases = [(item, prompt) for item in load_fixed_inputs(campaign) for prompt in item.prompts]
                item, prompt = cases[int(probe_case.removeprefix('generation-')) - 1]
                assert f'generation-{int(probe_case.removeprefix("generation-")):03d}' == probe_case
                assignment = json.loads((PRIVATE / 'fault-assignment.json').read_text())
                assert (PRIVATE / 'fixed-executions' / f'{probe_case}.error.json').exists()
                service = ExactCountService(PRIVATE, 'deepinfra/bf16',
                    hashlib.sha256(Path(provider_run.__file__).read_bytes()).hexdigest())
                with (generation_scope(generation), patch(
                        'src.services.questionnaire.service.build_model',
                        lambda: model_factory('deepinfra/bf16'))):
                    execution = await service.execute(item.job, prompt,
                        scenario_id=f'fixed-{item.scenario_id}', fixed_input_id=probe_case,
                        experiment_tags=['fixed-inputs', 'exact-count-probe'])
                provider_run.write_json(PRIVATE / 'schema-probes' / f'{execution.trajectory_id}.json',
                                        execution.model_dump(mode='json'))
                try:
                    validate_clean_baseline(execution, control=assignment[probe_case] is None)
                except ValueError as error:
                    status.update(status='probe_rejected', rejection=str(error))
                    return 2
                cache = PRIVATE / 'fixed-executions' / f'{probe_case}.json'
                if cache.exists():
                    (PRIVATE / 'rejected-executions' / f'{probe_case}-{time.time_ns()}.json').write_bytes(cache.read_bytes())
                # The audit hash is tied to the service's exact canonical serialization.
                temporary = cache.with_suffix('.tmp')
                temporary.write_text(execution.model_dump_json(indent=2) + '\n')
                temporary.replace(cache)
                cache.with_suffix('.error.json').unlink()
                status.update(status='probe_passed', new_accepted_cases=1,
                              note='Accepted checkpoint saved; aggregate export happens in full resume.')
                return 0
            args = SimpleNamespace(campaign=Path('configs/campaigns/front-a-fixed-300.yaml'),
                model='qwen/qwen3.5-9b', provider='deepinfra/bf16', max_parallel=1,
                limit=None, output_dir=None)
            campaign = load_dataset_campaign_profile(args.campaign)
            campaign = campaign.model_copy(update={
                'generation': campaign.generation.model_copy(update={'model': args.model}),
                'output_dir': Path('outputs/front-a-300') / provider_run._safe_filename(args.model),
            })
            current_protocol = fixed_dataset._protocol(campaign)
            manifest_path = PRIVATE / 'manifest.json'
            original_protocol = json.loads(manifest_path.read_text())['protocol']
            stable_current = {key: value for key, value in current_protocol.items()
                              if key != 'runtime_sha256'}
            stable_original = {key: value for key, value in original_protocol.items()
                              if key != 'runtime_sha256'}
            if stable_current != stable_original:
                raise ValueError('Parâmetros fixos mudaram; retomada bloqueada.')
            phase['resume_protocol'] = {
                'original_runtime_sha256': original_protocol['runtime_sha256'],
                'pending_cases_runtime_sha256': current_protocol['runtime_sha256'],
                'allowed_source_change': 'successful save is terminal; post-save provider errors are warnings',
                'original_manifest_protocol_preserved': True,
            }
            provider_run.write_json(config_path, config)
            with patch.object(fixed_dataset, '_protocol', lambda _campaign: original_protocol):
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
        config = json.loads(config_path.read_text())
        config['tool_schema_adjustment_phases'][-1]['result'] = status
        provider_run.write_json(config_path, config)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', choices=[f'generation-{i:03d}' for i in range(1,301)])
    args = parser.parse_args()
    with (PRIVATE / 'collection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raise SystemExit(asyncio.run(run(args.probe)))
