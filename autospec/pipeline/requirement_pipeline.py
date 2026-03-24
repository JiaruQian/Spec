"""Pipeline: requirement -> ACSL specification -> C code -> verification."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..llm.openai_compatible import OpenAICompatibleClient
from ..verifier.frama_c import FramaCVerifier


SPEC_SYSTEM_PROMPT = """You are an expert in ACSL specification design for C code.
Return strict JSON only."""

SPEC_USER_PROMPT_TEMPLATE = """Given the requirement below, design a minimal but useful ACSL function specification.

Requirement:
{requirement}

Output constraints (IMPORTANT):
- Return valid JSON only (no markdown fences, no explanation).
- JSON schema:
  {{
    "function_signature": "C function signature without body, e.g. int foo(int *a, int n);",
    "acsl_block": "A full ACSL block in /*@ ... */ format for the function.",
    "notes": "Very short rationale."
  }}
- The signature must be compatible with Frama-C/WP and plain C.
- Include requires/assigns/ensures in the ACSL block.
"""


CODE_SYSTEM_PROMPT = """You are an expert C developer writing verification-friendly code.
Output C code only."""

CODE_USER_PROMPT_TEMPLATE = """Implement a single C function from the requirement and ACSL specification.

Requirement:
{requirement}

Function signature:
{function_signature}

ACSL block (must remain directly above the function):
{acsl_block}

Output constraints:
- Output only complete C source code (no markdown fences, no explanation).
- Keep exactly one function implementation that matches the signature.
- Keep the ACSL block directly above the function.
- Do not add main().
- Use simple loops/branches and avoid advanced library dependencies.
"""

CONSTRAINT_SYSTEM_PROMPT = """You are an expert in translating requirements into verification constraints.
Return strict JSON only."""

CONSTRAINT_USER_PROMPT_TEMPLATE = """Extract structured verification constraints from the requirement.

Requirement:
{requirement}

Output constraints (IMPORTANT):
- Return valid JSON only (no markdown fences, no explanation).
- JSON schema:
  {{
    "function_signature": "Best-effort C function signature without body; end with ';'. Use empty string if unknown.",
    "preconditions": ["..."],
    "postconditions": ["..."],
    "invariants": ["..."],
    "notes": "Very short rationale."
  }}
- Keep each constraint atomic and testable.
- Prefer concise mathematical/logical wording.
"""

CONSTRAINT_TO_SPEC_SYSTEM_PROMPT = """You are an expert ACSL specification engineer.
Convert constraints into a complete ACSL contract. Return strict JSON only."""

CONSTRAINT_TO_SPEC_USER_PROMPT_TEMPLATE = """Create ACSL spec JSON using requirement + structured constraints.

Requirement:
{requirement}

Structured constraints (JSON):
{constraints_json}

Function signature hint (can be empty):
{signature_hint}

Output constraints (IMPORTANT):
- Return valid JSON only (no markdown fences, no explanation).
- JSON schema:
  {{
    "function_signature": "C function signature without body, e.g. int foo(int *a, int n);",
    "acsl_block": "A full ACSL block in /*@ ... */ format for the function.",
    "notes": "Very short rationale."
  }}
- Keep ACSL as complete as possible and aligned with constraints.
- Include requires/assigns/ensures in ACSL.
"""

SPEC_CHECK_SYSTEM_PROMPT = """You are a strict requirement-spec alignment reviewer.
Return strict JSON only."""

SPEC_CHECK_USER_PROMPT_TEMPLATE = """Check whether generated specification misses requirement constraints.

Requirement:
{requirement}

Structured constraints (JSON):
{constraints_json}

Generated specification (JSON):
{spec_json}

Output constraints (IMPORTANT):
- Return valid JSON only.
- JSON schema:
  {{
    "is_aligned": true,
    "missing_constraints": ["..."],
    "inconsistent_items": ["..."],
    "refinement_hints": ["..."],
    "notes": "Very short rationale."
  }}
- If everything is covered, use empty arrays.
"""

SPEC_REFINE_SYSTEM_PROMPT = """You refine ACSL specs to improve requirement alignment.
Return strict JSON only."""

SPEC_REFINE_USER_PROMPT_TEMPLATE = """Refine the generated specification based on alignment findings.

Requirement:
{requirement}

Structured constraints (JSON):
{constraints_json}

Current specification (JSON):
{spec_json}

Alignment findings (JSON):
{check_json}

Output constraints (IMPORTANT):
- Return valid JSON only.
- Same schema:
  {{
    "function_signature": "...",
    "acsl_block": "/*@ ... */",
    "notes": "..."
  }}
"""

CODE_REPAIR_SYSTEM_PROMPT = """You are an expert C developer fixing code to satisfy ACSL verification.
Output C code only."""

CODE_REPAIR_USER_PROMPT_TEMPLATE = """Fix the C function so that it satisfies requirement and ACSL specification.

Requirement:
{requirement}

Function signature:
{function_signature}

ACSL block:
{acsl_block}

Current code:
{code}

Verification result:
- type: {verdict_type}
- message: {verdict_message}
- details:
{verdict_details}

Output constraints:
- Output only complete C source code (no markdown fences, no explanation).
- Keep exactly one function implementation matching the signature.
- Keep ACSL block directly above the function.
- Prefer minimal, verification-friendly fixes.
"""


@dataclass
class RequirementItem:
    """Single requirement unit."""

    id: int
    path: str
    requirement: str


def _extract_json_object(text: str) -> Dict[str, Any]:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", stripped)
    if not match:
        raise ValueError(f"No JSON object found in model output: {stripped[:500]}")
    return json.loads(match.group(0))


def _extract_c_code(text: str) -> str:
    stripped = text.strip()
    fenced = re.search(r"```c\n([\s\S]*?)\n```", stripped)
    if fenced:
        return fenced.group(1).strip()
    if stripped.startswith("```"):
        generic = re.search(r"```\n([\s\S]*?)\n```", stripped)
        if generic:
            return generic.group(1).strip()
    return stripped


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        return lowered in {"true", "yes", "1"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _as_list_of_str(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _truncate_for_prompt(text: str, max_chars: int = 6000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]..."


def load_requirements(path: Path) -> List[RequirementItem]:
    """Load requirement entries from JSON."""
    raw = json.loads(path.read_text())
    items: List[RequirementItem] = []
    for obj in raw:
        if "id" not in obj or "path" not in obj:
            raise ValueError(f"Invalid requirement entry: {obj}")
        req_text = obj.get("requirement_zh") or obj.get("requirement_en") or obj.get("requirement")
        if not req_text:
            raise ValueError(f"Requirement text missing for entry: {obj}")
        items.append(RequirementItem(id=int(obj["id"]), path=str(obj["path"]), requirement=str(req_text)))
    return items


class RequirementToCodePipeline:
    """End-to-end requirement-driven generation pipeline."""

    def __init__(
        self,
        llm_client: OpenAICompatibleClient,
        output_dir: Path,
        verify_timeout: int = 120,
        skip_verify: bool = False,
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.llm_client = llm_client
        self.output_dir = output_dir
        self.skip_verify = skip_verify
        self.verifier = FramaCVerifier(timeout=verify_timeout)
        self.logger = logger

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)

    def run(self, requirements: List[RequirementItem]) -> Dict[str, Any]:
        """Run generation+verification for all requirements."""
        specs_dir = self.output_dir / "specs"
        code_dir = self.output_dir / "code"
        reports_dir = self.output_dir / "reports"
        specs_dir.mkdir(parents=True, exist_ok=True)
        code_dir.mkdir(parents=True, exist_ok=True)
        reports_dir.mkdir(parents=True, exist_ok=True)

        results: List[Dict[str, Any]] = []
        total = len(requirements)
        for idx, item in enumerate(requirements, start=1):
            self._log("=" * 72)
            self._log(f"[TASK {idx}/{total}] id={item.id} path={item.path}")
            self._log(f"[TASK {idx}/{total}] requirement: {item.requirement}")
            result: Dict[str, Any] = {
                "id": item.id,
                "path": item.path,
                "requirement": item.requirement,
                "status": "ok",
            }
            try:
                result.update(self._run_one(item, specs_dir, code_dir))
                self._log(f"[TASK {idx}/{total}] status=ok")
            except Exception as exc:  # keep batch processing resilient
                result["status"] = "error"
                result["error"] = str(exc)
                self._log(f"[TASK {idx}/{total}] status=error error={exc}")
            results.append(result)

            # Write an incremental report after each task so progress is visible/recoverable.
            interim_passed = sum(
                1 for r in results if r.get("verification", {}).get("valid") is True
            )
            interim_attempted = sum(1 for r in results if "verification" in r)
            interim_report = {
                "total": total,
                "processed": idx,
                "verified": interim_attempted,
                "passed": interim_passed,
                "results": results,
            }
            interim_report_path = reports_dir / "results.partial.json"
            interim_report_path.write_text(json.dumps(interim_report, ensure_ascii=False, indent=2))
            self._log(
                f"[TASK {idx}/{total}] partial_report={interim_report_path} "
                f"(processed={idx}, verified={interim_attempted}, passed={interim_passed})"
            )

        passed = sum(1 for r in results if r.get("verification", {}).get("valid") is True)
        attempted = sum(1 for r in results if "verification" in r)
        report = {
            "total": total,
            "processed": len(results),
            "verified": attempted,
            "passed": passed,
            "results": results,
        }
        report_path = reports_dir / "results.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        self._log("=" * 72)
        self._log(
            f"[DONE] total={report['total']} processed={report['processed']} "
            f"verified={report['verified']} passed={report['passed']}"
        )
        self._log(f"[DONE] report={report_path}")
        return {"report_path": str(report_path), "report": report}

    def _run_one(self, item: RequirementItem, specs_dir: Path, code_dir: Path) -> Dict[str, Any]:
        self._log(f"[id={item.id}] stage=spec_generation start")
        spec_prompt = SPEC_USER_PROMPT_TEMPLATE.format(requirement=item.requirement)
        spec_raw = self.llm_client.chat(SPEC_SYSTEM_PROMPT, spec_prompt)
        spec_json = _extract_json_object(spec_raw)

        function_signature = str(spec_json["function_signature"]).strip()
        acsl_block = str(spec_json["acsl_block"]).strip()
        notes = str(spec_json.get("notes", "")).strip()

        if not function_signature.endswith(";"):
            raise ValueError(f"function_signature must end with ';': {function_signature}")
        if "/*@" not in acsl_block or "*/" not in acsl_block:
            raise ValueError("acsl_block must be a full /*@ ... */ block.")

        spec_out = {
            "id": item.id,
            "path": item.path,
            "requirement": item.requirement,
            "function_signature": function_signature,
            "acsl_block": acsl_block,
            "notes": notes,
            "raw_model_output": spec_raw,
        }
        spec_file = specs_dir / Path(item.path).with_suffix(".json")
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(json.dumps(spec_out, ensure_ascii=False, indent=2))
        self._log(f"[id={item.id}] stage=spec_generation done spec_file={spec_file}")

        self._log(f"[id={item.id}] stage=code_generation start")
        code_prompt = CODE_USER_PROMPT_TEMPLATE.format(
            requirement=item.requirement,
            function_signature=function_signature,
            acsl_block=acsl_block,
        )
        code_raw = self.llm_client.chat(CODE_SYSTEM_PROMPT, code_prompt)
        code_text = _extract_c_code(code_raw)
        code_file = code_dir / Path(item.path)
        code_file.parent.mkdir(parents=True, exist_ok=True)
        code_file.write_text(code_text)
        self._log(f"[id={item.id}] stage=code_generation done code_file={code_file}")

        one_result: Dict[str, Any] = {
            "spec_file": str(spec_file),
            "code_file": str(code_file),
        }
        if self.skip_verify:
            self._log(f"[id={item.id}] stage=verify skipped")
            return one_result

        self._log(f"[id={item.id}] stage=verify start")
        verdict = self.verifier.verify(code_file)
        one_result["verification"] = {
            "valid": verdict.is_valid(),
            "type": verdict.verdict_type.value,
            "message": verdict.message,
        }
        if verdict.details:
            verify_log = self.output_dir / "reports" / Path(item.path).with_suffix(".verify.log")
            verify_log.parent.mkdir(parents=True, exist_ok=True)
            verify_log.write_text(verdict.details)
            one_result["verification"]["details_file"] = str(verify_log)
        self._log(
            f"[id={item.id}] stage=verify done valid={one_result['verification']['valid']} "
            f"type={one_result['verification']['type']}"
        )
        return one_result


class EnhancedRequirementToCodePipeline(RequirementToCodePipeline):
    """Incremental enhancement over baseline pipeline for ablation-friendly comparison."""

    def __init__(
        self,
        llm_client: OpenAICompatibleClient,
        output_dir: Path,
        verify_timeout: int = 120,
        skip_verify: bool = False,
        logger: Optional[Callable[[str], None]] = None,
        spec_self_check_rounds: int = 1,
        code_repair_max_iter: int = 3,
    ):
        super().__init__(
            llm_client=llm_client,
            output_dir=output_dir,
            verify_timeout=verify_timeout,
            skip_verify=skip_verify,
            logger=logger,
        )
        self.spec_self_check_rounds = max(0, spec_self_check_rounds)
        self.code_repair_max_iter = max(0, code_repair_max_iter)

    def _extract_constraints(self, requirement: str) -> Dict[str, Any]:
        prompt = CONSTRAINT_USER_PROMPT_TEMPLATE.format(requirement=requirement)
        raw = self.llm_client.chat(CONSTRAINT_SYSTEM_PROMPT, prompt)
        data = _extract_json_object(raw)
        return {
            "function_signature": str(data.get("function_signature", "")).strip(),
            "preconditions": _as_list_of_str(data.get("preconditions")),
            "postconditions": _as_list_of_str(data.get("postconditions")),
            "invariants": _as_list_of_str(data.get("invariants")),
            "notes": str(data.get("notes", "")).strip(),
            "raw_model_output": raw,
        }

    def _constraints_to_spec(
        self,
        requirement: str,
        constraints: Dict[str, Any],
        signature_hint: str = "",
    ) -> Dict[str, Any]:
        prompt = CONSTRAINT_TO_SPEC_USER_PROMPT_TEMPLATE.format(
            requirement=requirement,
            constraints_json=json.dumps(constraints, ensure_ascii=False, indent=2),
            signature_hint=signature_hint,
        )
        raw = self.llm_client.chat(CONSTRAINT_TO_SPEC_SYSTEM_PROMPT, prompt)
        data = _extract_json_object(raw)
        data["raw_model_output"] = raw
        return data

    def _check_and_refine_spec(
        self,
        requirement: str,
        constraints: Dict[str, Any],
        spec_json: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        rounds: List[Dict[str, Any]] = []
        current_spec = spec_json
        final_aligned = False

        for round_idx in range(1, self.spec_self_check_rounds + 1):
            check_prompt = SPEC_CHECK_USER_PROMPT_TEMPLATE.format(
                requirement=requirement,
                constraints_json=json.dumps(constraints, ensure_ascii=False, indent=2),
                spec_json=json.dumps(current_spec, ensure_ascii=False, indent=2),
            )
            check_raw = self.llm_client.chat(SPEC_CHECK_SYSTEM_PROMPT, check_prompt)
            check = _extract_json_object(check_raw)
            is_aligned = _to_bool(check.get("is_aligned"))
            missing_constraints = _as_list_of_str(check.get("missing_constraints"))
            inconsistent_items = _as_list_of_str(check.get("inconsistent_items"))
            refinement_hints = _as_list_of_str(check.get("refinement_hints"))
            round_info: Dict[str, Any] = {
                "round": round_idx,
                "is_aligned": is_aligned,
                "missing_constraints": missing_constraints,
                "inconsistent_items": inconsistent_items,
                "refinement_hints": refinement_hints,
                "notes": str(check.get("notes", "")).strip(),
                "raw_check_output": check_raw,
            }

            if is_aligned or (not missing_constraints and not inconsistent_items):
                final_aligned = True
                rounds.append(round_info)
                break

            refine_prompt = SPEC_REFINE_USER_PROMPT_TEMPLATE.format(
                requirement=requirement,
                constraints_json=json.dumps(constraints, ensure_ascii=False, indent=2),
                spec_json=json.dumps(current_spec, ensure_ascii=False, indent=2),
                check_json=json.dumps(check, ensure_ascii=False, indent=2),
            )
            refine_raw = self.llm_client.chat(SPEC_REFINE_SYSTEM_PROMPT, refine_prompt)
            refined_spec = _extract_json_object(refine_raw)
            refined_spec["raw_model_output"] = refine_raw
            current_spec = refined_spec
            round_info["refined"] = True
            rounds.append(round_info)

        return current_spec, {
            "rounds": rounds,
            "final_aligned": final_aligned,
            "max_rounds": self.spec_self_check_rounds,
        }

    def _validate_spec_fields(self, spec_json: Dict[str, Any], signature_fallback: str = "") -> tuple[str, str, str]:
        function_signature = str(spec_json.get("function_signature", "")).strip() or signature_fallback.strip()
        acsl_block = str(spec_json.get("acsl_block", "")).strip()
        notes = str(spec_json.get("notes", "")).strip()

        if not function_signature:
            raise ValueError("function_signature is empty after generation/refinement.")
        if not function_signature.endswith(";"):
            raise ValueError(f"function_signature must end with ';': {function_signature}")
        if "/*@" not in acsl_block or "*/" not in acsl_block:
            raise ValueError("acsl_block must be a full /*@ ... */ block.")
        return function_signature, acsl_block, notes

    def _run_one(self, item: RequirementItem, specs_dir: Path, code_dir: Path) -> Dict[str, Any]:
        self._log(f"[id={item.id}] stage=constraint_extraction start")
        constraints = self._extract_constraints(item.requirement)
        self._log(f"[id={item.id}] stage=constraint_extraction done")

        self._log(f"[id={item.id}] stage=constraint_to_spec start")
        initial_spec = self._constraints_to_spec(
            requirement=item.requirement,
            constraints=constraints,
            signature_hint=constraints.get("function_signature", ""),
        )
        self._log(f"[id={item.id}] stage=constraint_to_spec done")

        self._log(f"[id={item.id}] stage=spec_self_check start")
        final_spec, alignment_info = self._check_and_refine_spec(
            requirement=item.requirement,
            constraints=constraints,
            spec_json=initial_spec,
        )
        self._log(
            f"[id={item.id}] stage=spec_self_check done final_aligned={alignment_info.get('final_aligned')}"
        )

        function_signature, acsl_block, notes = self._validate_spec_fields(
            final_spec,
            signature_fallback=constraints.get("function_signature", ""),
        )

        spec_out = {
            "id": item.id,
            "path": item.path,
            "requirement": item.requirement,
            "function_signature": function_signature,
            "acsl_block": acsl_block,
            "notes": notes,
            "constraints": constraints,
            "alignment_check": alignment_info,
            "raw_model_output": final_spec.get("raw_model_output", ""),
        }
        spec_file = specs_dir / Path(item.path).with_suffix(".json")
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(json.dumps(spec_out, ensure_ascii=False, indent=2))
        self._log(f"[id={item.id}] stage=spec_generation done spec_file={spec_file}")

        self._log(f"[id={item.id}] stage=code_generation start")
        code_prompt = CODE_USER_PROMPT_TEMPLATE.format(
            requirement=item.requirement,
            function_signature=function_signature,
            acsl_block=acsl_block,
        )
        code_raw = self.llm_client.chat(CODE_SYSTEM_PROMPT, code_prompt)
        code_text = _extract_c_code(code_raw)
        code_file = code_dir / Path(item.path)
        code_file.parent.mkdir(parents=True, exist_ok=True)
        code_file.write_text(code_text)
        self._log(f"[id={item.id}] stage=code_generation done code_file={code_file}")

        one_result: Dict[str, Any] = {
            "spec_file": str(spec_file),
            "code_file": str(code_file),
            "enhanced": {
                "spec_self_check_rounds": self.spec_self_check_rounds,
                "code_repair_max_iter": self.code_repair_max_iter,
            },
        }
        if self.skip_verify:
            self._log(f"[id={item.id}] stage=verify skipped")
            return one_result

        verify_report_dir = self.output_dir / "reports"
        verify_report_dir.mkdir(parents=True, exist_ok=True)
        repair_history: List[Dict[str, Any]] = []

        self._log(f"[id={item.id}] stage=verify start")
        verdict = self.verifier.verify(code_file)

        for attempt in range(1, self.code_repair_max_iter + 1):
            if verdict.is_valid():
                break
            details = verdict.details or ""
            detail_path = (
                verify_report_dir / Path(item.path).with_suffix(f".repair{attempt - 1}.verify.log")
            )
            if details:
                detail_path.parent.mkdir(parents=True, exist_ok=True)
                detail_path.write_text(details)

            self._log(
                f"[id={item.id}] stage=code_repair attempt={attempt}/{self.code_repair_max_iter} "
                f"trigger_type={verdict.verdict_type.value}"
            )
            repair_prompt = CODE_REPAIR_USER_PROMPT_TEMPLATE.format(
                requirement=item.requirement,
                function_signature=function_signature,
                acsl_block=acsl_block,
                code=code_text,
                verdict_type=verdict.verdict_type.value,
                verdict_message=verdict.message,
                verdict_details=_truncate_for_prompt(details, max_chars=5000),
            )
            repaired_raw = self.llm_client.chat(CODE_REPAIR_SYSTEM_PROMPT, repair_prompt)
            repaired_code = _extract_c_code(repaired_raw)
            code_file.write_text(repaired_code)
            code_text = repaired_code
            repair_history.append(
                {
                    "attempt": attempt,
                    "before_type": verdict.verdict_type.value,
                    "before_message": verdict.message,
                    "details_file": str(detail_path) if details else None,
                }
            )

            verdict = self.verifier.verify(code_file)
            self._log(
                f"[id={item.id}] stage=code_repair attempt={attempt} "
                f"result_valid={verdict.is_valid()} result_type={verdict.verdict_type.value}"
            )

        verification = {
            "valid": verdict.is_valid(),
            "type": verdict.verdict_type.value,
            "message": verdict.message,
            "repair_attempts": len(repair_history),
        }
        if repair_history:
            verification["repair_history"] = repair_history
        if verdict.details:
            verify_log = verify_report_dir / Path(item.path).with_suffix(".verify.log")
            verify_log.parent.mkdir(parents=True, exist_ok=True)
            verify_log.write_text(verdict.details)
            verification["details_file"] = str(verify_log)

        one_result["verification"] = verification
        self._log(
            f"[id={item.id}] stage=verify done valid={verification['valid']} "
            f"type={verification['type']} repairs={verification['repair_attempts']}"
        )
        return one_result
