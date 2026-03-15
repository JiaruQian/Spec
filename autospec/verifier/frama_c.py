"""Frama-C WP verification wrapper"""
import subprocess
import re
from pathlib import Path
from typing import Optional, List
from .verdict import Verdict, VerdictType
from ..config import (
    FRAMA_C_COMMAND,
    FRAMA_C_TIMEOUT,
    FRAMA_C_WP_TIMEOUT,
)


class FramaCVerifier:
    """Wrapper for Frama-C WP verification"""
    
    def __init__(self, timeout: int = FRAMA_C_TIMEOUT):
        self.timeout = timeout
        self.frama_c_cmd = FRAMA_C_COMMAND
        
    def verify(self, c_file: Path) -> Verdict:
        """Run Frama-C WP on a C file and return verdict"""
        if not c_file.exists():
            return Verdict(
                verdict_type=VerdictType.UNKNOWN,
                message=f"File not found: {c_file}"
            )
        
        try:
            # Run Frama-C with WP plugin. Newer Frama-C versions support
            # `-generated-spec-custom terminates:skip`; older versions (e.g. 26.x)
            # do not. Try with the flag first, then transparently retry without it.
            result = self._run_frama_c(
                c_file,
                wp_timeout=FRAMA_C_WP_TIMEOUT,
                run_timeout=self.timeout,
            )
            return self._parse_output(result.stdout, result.stderr, result.returncode)
            
        except subprocess.TimeoutExpired:
            return Verdict(
                verdict_type=VerdictType.TIMEOUT,
                message=f"Verification timed out after {self.timeout}s"
            )
        except FileNotFoundError:
            return Verdict(
                verdict_type=VerdictType.UNKNOWN,
                message=f"Frama-C not found. Please install Frama-C and ensure '{self.frama_c_cmd}' is in PATH"
            )
        except Exception as e:
            return Verdict(
                verdict_type=VerdictType.UNKNOWN,
                message=f"Verification error: {str(e)}"
            )

    def _build_cmd(
        self,
        c_file: Path,
        wp_timeout: int,
        include_terminates_skip: bool = True
    ) -> List[str]:
        """Build Frama-C command line."""
        cmd = [self.frama_c_cmd]
        if include_terminates_skip:
            cmd.extend(["-generated-spec-custom", "terminates:skip"])
        cmd.extend([
            "-wp",
            f"-wp-timeout={wp_timeout}",
            "-wp-prover=alt-ergo",
            str(c_file),
        ])
        return cmd

    def _run_frama_c(
        self,
        c_file: Path,
        wp_timeout: int,
        run_timeout: int
    ) -> subprocess.CompletedProcess:
        """Run Frama-C and retry without unsupported flags if needed."""
        first_result = subprocess.run(
            self._build_cmd(c_file, wp_timeout=wp_timeout, include_terminates_skip=True),
            capture_output=True,
            text=True,
            timeout=run_timeout
        )
        combined = f"{first_result.stdout}\n{first_result.stderr}".lower()
        if "generated-spec-custom" in combined and "unknown" in combined:
            return subprocess.run(
                self._build_cmd(c_file, wp_timeout=wp_timeout, include_terminates_skip=False),
                capture_output=True,
                text=True,
                timeout=run_timeout
            )
        return first_result
    
    def _parse_output(self, stdout: str, stderr: str, returncode: int) -> Verdict:
        """Parse Frama-C output to determine verdict"""
        output = stdout + stderr
        
        # Check for "Proved goals: X / X" pattern (all goals proven)
        proved_match = re.search(r'\[wp\] Proved goals:\s+(\d+)\s*/\s*(\d+)', output)
        if proved_match:
            proved = int(proved_match.group(1))
            total = int(proved_match.group(2))
            
            if proved == total and total > 0:
                return Verdict(
                    verdict_type=VerdictType.VALID,
                    message=f"All proof obligations verified ({proved}/{total} goals proven)",
                    details=output
                )
            elif proved < total:
                # Check if the failure is due to timeout
                timeout_count = len(re.findall(r'\[Timeout\]', output))
                if timeout_count > 0:
                    return Verdict(
                        verdict_type=VerdictType.TIMEOUT,
                        message=f"Verification incomplete: {timeout_count} goal(s) timed out ({proved}/{total} goals proven)",
                        details=output
                    )
                else:
                    return Verdict(
                        verdict_type=VerdictType.INVALID,
                        message=f"Some proof obligations failed ({proved}/{total} goals proven)",
                        details=output
                    )
        
        # Look for "Valid" in output (older Frama-C versions or different output format)
        if "Valid" in output and returncode == 0:
            valid_count = len(re.findall(r"Valid", output))
            return Verdict(
                verdict_type=VerdictType.VALID,
                message=f"All proof obligations verified ({valid_count} valid)",
                details=output
            )
        
        # Check for timeouts or unknown results
        if "Timeout" in output or "timeout" in output.lower():
            return Verdict(
                verdict_type=VerdictType.TIMEOUT,
                message="Verification timed out",
                details=output
            )
        
        if "Unknown" in output:
            return Verdict(
                verdict_type=VerdictType.UNKNOWN,
                message="Some proof obligations could not be verified",
                details=output
            )
        
        # Check for invalid results
        if "Invalid" in output or (returncode != 0 and "error" in output.lower()):
            return Verdict(
                verdict_type=VerdictType.INVALID,
                message="Verification failed",
                details=output
            )
        
        # Default case
        return Verdict(
            verdict_type=VerdictType.UNKNOWN,
            message="Could not determine verification result",
            details=output
        )

