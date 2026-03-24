# Requirement-to-Code Pipeline (Minimal V1)

This document records the new paradigm built on top of AutoSpec:

1. Natural language requirement input
2. LLM generates ACSL specification skeleton
3. LLM generates C code using that specification
4. Frama-C/WP verifies the generated code+spec

This V1 is intentionally minimal to establish a complete end-to-end loop.

## Execution mode (task-by-task in terminal)

The pipeline now runs **strictly one requirement at a time** and prints stage logs in terminal:

1. load one requirement
2. generate ACSL spec
3. generate C code
4. verify with Frama-C
5. move to next requirement

Example terminal flow:
- `[TASK 3/51] id=17 path=miscellaneous/array_max_advanced.c`
- `[id=17] stage=spec_generation start`
- `[id=17] stage=spec_generation done ...`
- `[id=17] stage=code_generation start`
- `[id=17] stage=verify start`
- `[id=17] stage=verify done valid=true type=valid`

## Why this change

The old workflow starts from already-correct C code and asks LLM to infer ACSL.
The new workflow targets a more realistic synthesis setting:

- build code and formal spec together from requirements;
- keep verification as a hard gate;
- prepare for later requirement-level evaluation.

## What was added

- `autospec/llm/openai_compatible.py`
  - lightweight OpenAI-compatible chat client (OpenRouter/vLLM/etc.).
- `autospec/pipeline/requirement_pipeline.py`
  - core orchestration for:
    - requirement loading,
    - spec generation,
    - code generation,
    - Frama-C verification,
    - report export.
- `scripts/run_requirement_pipeline.py`
  - CLI entry script for batch execution.
- `benchmarks/frama-c-problems/requirements/requirements_51.json`
  - 51 requirement entries aligned with `test-inputs` paths.

## Data format

Requirements JSON (array):

```json
[
  {
    "id": 1,
    "path": "pointers/swap.c",
    "requirement_en": "Given pointers to two integers, swap their stored values in place."
  }
]
```

Supported text fields in loader priority:
- `requirement_zh`
- `requirement_en`
- `requirement`

## Prompting protocol (V1)

For each requirement:

1) Spec stage (JSON output):
- asks model for:
  - `function_signature`
  - `acsl_block`
  - `notes`

2) Code stage (C source output):
- feeds requirement + generated signature + ACSL block
- asks model for one complete C function file.

3) Verify stage:
- run existing `FramaCVerifier`.

## Outputs

Given `--output-dir outputs/req2code`, pipeline writes:

- `outputs/req2code/specs/<relative_path>.json`
- `outputs/req2code/code/<relative_path>.c`
- `outputs/req2code/reports/results.json`
- `outputs/req2code/reports/results.partial.json` (updated after each task)
- `outputs/req2code/reports/<relative_path>.verify.log` (when verifier emits details)

## Run

```bash
PYTHONPATH=. python3 scripts/run_requirement_pipeline.py \
  --requirements-file benchmarks/frama-c-problems/requirements/requirements_51.json \
  --output-dir outputs/req2code \
  --endpoint https://openrouter.ai/api/v1/chat/completions \
  --model deepseek/deepseek-v3.2 \
  --api-key-env OPENROUTER_API_KEY \
  --verify-timeout 120
```

Run only one task (recommended for debugging/prompt iteration):

```bash
PYTHONPATH=. python3 scripts/run_requirement_pipeline.py \
  --task-id 17 \
  --requirements-file benchmarks/frama-c-problems/requirements/requirements_51.json \
  --output-dir outputs/req2code
```

If you only want generation without verification:

```bash
PYTHONPATH=. python3 scripts/run_requirement_pipeline.py \
  --skip-verify
```

## Current limitations (expected)

- No correction loop yet (unlike `scripts/gen_specs.py` final repair loop).
- No requirement-vs-spec semantic scoring yet.
- No synthesis-time decomposition yet.
- Relies on LLM output formatting discipline (JSON/C parsing with light fallback).

## Planned next upgrades

- Add verification-guided correction loop for both ACSL and code.
- Add requirement-to-spec compliance checks.
- Add decomposition-aware synthesis prompts.
- Add richer dataset fields (`io_examples`, `constraints`, `difficulty`).
