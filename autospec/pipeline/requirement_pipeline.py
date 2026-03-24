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
