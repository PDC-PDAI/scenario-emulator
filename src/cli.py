from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from src.clients.langfuse.client import flush_langfuse
from src.services.scenario.service import ScenarioService
from src.settings import settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scenario-emulator",
        description="Executa a Frente A para AgentDebug-RH e RecruitSecBench.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Gera vaga, comandos 1:N e questionários.")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--brief", help="Briefing textual da vaga.")
    source.add_argument("--brief-file", type=Path, help="Arquivo UTF-8 com o briefing.")
    run.add_argument("--benign", type=int, default=3, help="Quantidade de comandos benignos.")
    run.add_argument("--malicious", type=int, default=3, help="Quantidade de comandos malignos.")
    run.add_argument(
        "--benign-responses",
        type=int,
        default=1,
        help="Respostas benignas por questionário gerado.",
    )
    run.add_argument(
        "--malicious-responses",
        type=int,
        default=1,
        help="Respostas com prompt injection por questionário gerado.",
    )
    run.add_argument("--output", type=Path, help="Arquivo JSON completo do cenário.")
    run.add_argument(
        "--jsonl",
        type=Path,
        help="Arquivo JSONL opcional com um BenchmarkRecord por trajetória.",
    )

    commands.add_parser("sync-prompts", help="Sincroniza os prompts locais com Langfuse.")

    export = commands.add_parser(
        "export-trace",
        help="Exporta um trace completo do Langfuse em um único JSON.",
    )
    export.add_argument("--trace-id", required=True, help="ID hexadecimal do trace.")
    export.add_argument("--output", type=Path, help="Arquivo JSON; sem ele, imprime no stdout.")

    serve = commands.add_parser("serve", help="Inicia a API HTTP com Swagger em /docs.")
    serve.add_argument("--host", default=settings.API_HOST, help="Interface de rede da API.")
    serve.add_argument("--port", type=int, default=settings.API_PORT, help="Porta da API.")
    serve.add_argument(
        "--reload",
        action=argparse.BooleanOptionalAction,
        default=settings.API_RELOAD,
        help="Recarrega a API ao alterar arquivos (apenas desenvolvimento).",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    brief = args.brief or args.brief_file.read_text(encoding="utf-8")
    result = await ScenarioService().run(
        brief,
        benign_count=args.benign,
        malicious_count=args.malicious,
        benign_response_count=args.benign_responses,
        malicious_response_count=args.malicious_responses,
    )
    rendered = result.model_dump_json(indent=2, by_alias=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Cenário salvo em {args.output}")
    else:
        print(rendered)
    if args.jsonl:
        args.jsonl.parent.mkdir(parents=True, exist_ok=True)
        lines = [record.model_dump_json(by_alias=True) for record in result.benchmark_records]
        args.jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Benchmark JSONL salvo em {args.jsonl}")
    passed = sum(item.benchmark_passed for item in result.executions)
    print(f"Resultado: {passed}/{len(result.executions)} trajetórias passaram no oráculo.")
    evaluation_passed = sum(
        bool(item.oracle and item.oracle.passed) for item in result.evaluation_executions
    )
    print(
        "Avaliações: "
        f"{evaluation_passed}/{len(result.evaluation_executions)} passaram no oráculo defensivo."
    )
    flush_langfuse()
    return 0


def _export_trace(args: argparse.Namespace) -> int:
    from src.services.observability.export import fetch_trace_export  # noqa: PLC0415

    rendered = json.dumps(
        fetch_trace_export(args.trace_id),
        ensure_ascii=False,
        indent=2,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Trace salvo em {args.output}")
    else:
        print(rendered)
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn  # noqa: PLC0415

    uvicorn.run(
        "src.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def main() -> int:
    args = _parser().parse_args()
    if args.command == "sync-prompts":
        from scripts.sync_langfuse_prompts import sync_prompts  # noqa: PLC0415

        return sync_prompts()
    if args.command == "export-trace":
        return _export_trace(args)
    if args.command == "serve":
        return _serve(args)
    try:
        return asyncio.run(_run(args))
    finally:
        flush_langfuse()
