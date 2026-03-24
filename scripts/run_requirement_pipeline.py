#!/usr/bin/env python3
"""Run requirement -> spec -> code -> verification pipeline."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make script runnable without manually exporting PYTHONPATH.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from autospec.llm.openai_compatible import ChatConfig, OpenAICompatibleClient
from autospec.pipeline.requirement_pipeline import (
    EnhancedRequirementToCodePipeline,
    RequirementToCodePipeline,
    load_requirements,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate ACSL specs and C code from natural language requirements."
    )
    parser.add_argument(
        "--requirements-file",
        type=Path,
        default=Path("benchmarks/frama-c-problems/requirements/requirements_51.json"),
        help="Path to requirements JSON.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/req2code"),
        help="Directory to store generated specs/code/reports.",
    )
    parser.add_argument(
        "--endpoint",
        type=str,
        default="https://openrouter.ai/api/v1/chat/completions",
        help="OpenAI-compatible chat completion endpoint.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="deepseek/deepseek-v3.2",
        help="Model name sent to endpoint.",
    )
    parser.add_argument("--api-key", type=str, default=None, help="Direct API key.")
    parser.add_argument(
        "--api-key-env",
        type=str,
        default="OPENROUTER_API_KEY",
        help="Environment variable name holding API key.",
    )
    parser.add_argument(
        "--site-url",
        type=str,
        default=os.getenv("OPENROUTER_SITE_URL"),
        help="Optional HTTP-Referer header value.",
    )
    parser.add_argument(
        "--app-name",
        type=str,
        default=os.getenv("OPENROUTER_APP_NAME", "AutoSpec"),
        help="Optional X-Title header value.",
    )
    parser.add_argument("--temperature", type=float, default=0.1, help="Sampling temperature.")
    parser.add_argument("--max-tokens", type=int, default=2048, help="Max output tokens.")
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=120,
        help="Timeout per LLM request in seconds.",
    )
    parser.add_argument(
        "--verify-timeout",
        type=int,
        default=120,
        help="Timeout per Frama-C verification in seconds.",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Generate specs/code only, skip Frama-C verification.",
    )
    parser.add_argument(
        "--pipeline-variant",
        choices=["base", "enhanced"],
        default="base",
        help="Pipeline variant for ablation: base or enhanced.",
    )
    parser.add_argument(
        "--spec-self-check-rounds",
        type=int,
        default=1,
        help="Enhanced only: rounds for spec self-check/refinement.",
    )
    parser.add_argument(
        "--code-repair-max-iter",
        type=int,
        default=3,
        help="Enhanced only: max verification-guided code repair iterations.",
    )
    parser.add_argument(
        "--task-id",
        type=int,
        default=None,
        help="Run only one requirement item by id (e.g. 17).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = args.api_key or os.getenv(args.api_key_env) or os.getenv("OPENAI_API_KEY")
    if "openrouter.ai" in args.endpoint and not api_key:
        raise SystemExit(
            f"Missing API key for OpenRouter. Set {args.api_key_env} or pass --api-key."
        )

    requirements = load_requirements(args.requirements_file)
    if args.task_id is not None:
        requirements = [r for r in requirements if r.id == args.task_id]
        if not requirements:
            raise SystemExit(f"task id {args.task_id} not found in {args.requirements_file}")
    print(f"[INFO] Loaded {len(requirements)} requirements from {args.requirements_file}")

    llm_config = ChatConfig(
        model=args.model,
        endpoint=args.endpoint,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        api_key=api_key,
        site_url=args.site_url,
        app_name=args.app_name,
        timeout_seconds=args.request_timeout,
    )
    client = OpenAICompatibleClient(llm_config)
    if args.pipeline_variant == "enhanced":
        pipeline = EnhancedRequirementToCodePipeline(
            llm_client=client,
            output_dir=args.output_dir,
            verify_timeout=args.verify_timeout,
            skip_verify=args.skip_verify,
            logger=lambda msg: print(msg, flush=True),
            spec_self_check_rounds=args.spec_self_check_rounds,
            code_repair_max_iter=args.code_repair_max_iter,
        )
    else:
        pipeline = RequirementToCodePipeline(
            llm_client=client,
            output_dir=args.output_dir,
            verify_timeout=args.verify_timeout,
            skip_verify=args.skip_verify,
            logger=lambda msg: print(msg, flush=True),
        )
    outcome = pipeline.run(requirements)
    report = outcome["report"]
    print(
        "[INFO] Pipeline finished: "
        f"total={report['total']}, verified={report['verified']}, passed={report['passed']}"
    )
    print(f"[INFO] Pipeline variant: {args.pipeline_variant}")
    print(f"[INFO] Report: {outcome['report_path']}")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
