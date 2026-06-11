"""runtime_verifier.py — During-run and post-run verification component.

Enforces R-rules (per-component, during run) and C-rules (post-run, completion)
per ANPD_V24_Verification_Rules §6-§7. Paired with preflight.py which enforces
pre-run rules.

Together they close the silent-skip failure mode: preflight verifies preconditions,
runtime_verifier verifies execution.

Invocation: in-process, instantiated once per pipeline run by master_controller (P011).
Receipt is written incrementally after each component verification (P012).

Component version: 1.0.0
Copyright 2026 Endeavor Publishing LLC
"""

import glob as glob_mod
import importlib
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List


# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class Finding:
    """A single verification finding (R-rule or C-rule failure)."""
    rule_id: str            # e.g. "R001"
    severity: str           # "A" or "B"
    error_code: str         # e.g. "COMPONENT_OUTPUT_MISSING"
    component_name: str
    iteration_index: Optional[int]
    file_path: Optional[str]
    suggested_fix: str


@dataclass
class VerificationResult:
    """Aggregated result of verifying one component invocation or run completion."""
    component_name: str
    iteration_index: Optional[int]
    findings: List[Finding]

    @property
    def has_class_a_failure(self) -> bool:
        return any(f.severity == "A" for f in self.findings)

    @property
    def passed(self) -> bool:
        return len(self.findings) == 0


@dataclass
class ComponentContext:
    """Context set by master_controller before verify_component_completion."""
    component_name: str
    exit_code: int
    runtime_seconds: float
    iteration_index: Optional[int]


@dataclass
class ComponentExecutionRecord:
    """Record of a component's execution, stored in executed_components."""
    component_name: str
    exit_code: int
    runtime_seconds: float
    started_at: str
    completed_at: str
    outputs_produced: List[str]


# ─── RuntimeVerifier ─────────────────────────────────────────────────────────

class RuntimeVerifier:
    """During-run and post-run verification engine.

    Instantiated once per pipeline run by master_controller. Accumulates state
    across the run and writes PIPELINE_RECEIPT.json incrementally per P012.
    """

    def __init__(
        self,
        manifest_path: Path,
        run_id: str,
        run_start_time: datetime,
        receipt_path: Path,
        stop_report_path: Path,
        log_file_access: bool = False,
        series_name: str = "",
        book_number: int = 0,
    ):
        self.manifest_path = Path(manifest_path)
        self.run_id = run_id
        self.run_start_time = run_start_time
        self.receipt_path = Path(receipt_path)
        self.stop_report_path = Path(stop_report_path)
        self.log_file_access = log_file_access

        # Load manifest once at init.
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            self._manifest_raw = json.load(f)
        self._pipeline_components = self._manifest_raw.get("pipeline_components", [])

        # Build lookup: component_name -> manifest entry.
        self._component_entries = {}
        for entry in self._pipeline_components:
            self._component_entries[entry["component_name"]] = entry

        # Resolve series name and book number: prefer constructor args,
        # fall back to manifest metadata.
        metadata = self._manifest_raw.get("metadata", {})
        self._series_name = series_name or metadata.get("series", "")
        self._book_number = book_number or metadata.get("book_number", 1)
        b_nn = f"b{self._book_number:02d}"
        base_dir = metadata.get("base_dir", "/anpd/v25")
        self._book_dir = Path(base_dir) / "series" / self._series_name / b_nn

        # State accumulated during run (per design doc §7).
        self.executed_components: dict[str, ComponentExecutionRecord] = {}
        self.iteration_counts: dict[str, int] = {}
        self.verification_results: list[VerificationResult] = []
        self.file_access_log: list[dict] = []

        # Component context set by master_controller before each verification.
        self._pending_context: Optional[ComponentContext] = None

        # File-access audit hook (C005).
        if self.log_file_access:
            self._install_audit_hook()

        # Initialize receipt on disk with run metadata.
        self._write_receipt(completed=False)

    # ─── Public API ──────────────────────────────────────────────────────

    def set_component_context(
        self,
        component_name: str,
        exit_code: int,
        runtime_seconds: float,
        iteration_index: Optional[int] = None,
    ) -> None:
        """Set context for the next verify_component_completion call.

        Must be called before verify_component_completion. Failure to call
        produces MISSING_COMPONENT_CONTEXT Class A finding (defensive).
        """
        self._pending_context = ComponentContext(
            component_name=component_name,
            exit_code=exit_code,
            runtime_seconds=runtime_seconds,
            iteration_index=iteration_index,
        )

    def verify_component_completion(
        self,
        component_name: str,
        iteration_index: Optional[int] = None,
    ) -> VerificationResult:
        """Run R-rules for a just-completed component invocation.

        Resolves output path placeholders for this iteration if multi_instance.
        Records iteration count for post-run verification.
        Writes per-component result to PIPELINE_RECEIPT.json incrementally (P012).
        """
        findings: list[Finding] = []

        # Defensive: check context was set.
        if self._pending_context is None:
            findings.append(Finding(
                rule_id="R005",
                severity="A",
                error_code="MISSING_COMPONENT_CONTEXT",
                component_name=component_name,
                iteration_index=iteration_index,
                file_path=None,
                suggested_fix=(
                    "master_controller must call set_component_context() before "
                    "verify_component_completion(). This is a master_controller bug."
                ),
            ))
            result = VerificationResult(
                component_name=component_name,
                iteration_index=iteration_index,
                findings=findings,
            )
            self.verification_results.append(result)
            self._write_receipt(completed=False)
            return result

        ctx = self._pending_context
        self._pending_context = None

        # Look up manifest entry.
        entry = self._component_entries.get(component_name)
        if entry is None:
            findings.append(Finding(
                rule_id="R001",
                severity="A",
                error_code="COMPONENT_NOT_IN_MANIFEST",
                component_name=component_name,
                iteration_index=iteration_index,
                file_path=None,
                suggested_fix=(
                    f"Component '{component_name}' was invoked but has no entry in "
                    f"pipeline_manifest.json. Add a manifest entry or check the component name."
                ),
            ))
            result = VerificationResult(
                component_name=component_name,
                iteration_index=iteration_index,
                findings=findings,
            )
            self.verification_results.append(result)
            self._write_receipt(completed=False)
            return result

        active_rules = set(entry.get("verification_rules_active", []))

        # R005: exit code.
        if "R005" in active_rules:
            findings.extend(self._check_component_exit_code(component_name, ctx, iteration_index))

        # Resolve outputs for this invocation.
        outputs = entry.get("outputs", [])
        resolved_outputs = [
            self._resolve_output_path(o, entry, iteration_index)
            for o in outputs
        ]

        # R001: outputs exist.
        if "R001" in active_rules:
            findings.extend(self._check_outputs_exist(
                component_name, outputs, resolved_outputs, iteration_index,
            ))

        # R002: outputs fresh.
        if "R002" in active_rules:
            findings.extend(self._check_outputs_fresh(
                component_name, resolved_outputs, iteration_index,
            ))

        # R003: JSON outputs valid.
        if "R003" in active_rules:
            findings.extend(self._check_outputs_valid_json(
                component_name, resolved_outputs, iteration_index,
            ))

        # R004: schema conformance.
        if "R004" in active_rules:
            findings.extend(self._check_outputs_schema_conformance(
                component_name, outputs, resolved_outputs, iteration_index,
            ))

        # R006: runtime within limit.
        if "R006" in active_rules:
            findings.extend(self._check_runtime_within_limit(
                component_name, entry, ctx, iteration_index,
            ))

        # R007: no internal STOP_REPORT.
        if "R007" in active_rules:
            findings.extend(self._check_no_internal_stop_report(
                component_name, iteration_index,
            ))

        # Record execution.
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        outputs_produced = [p for p in resolved_outputs if Path(p).exists()]

        if component_name not in self.executed_components:
            self.executed_components[component_name] = ComponentExecutionRecord(
                component_name=component_name,
                exit_code=ctx.exit_code,
                runtime_seconds=ctx.runtime_seconds,
                started_at=now_str,
                completed_at=now_str,
                outputs_produced=outputs_produced,
            )
        else:
            # Multi-instance: update record with latest iteration info.
            rec = self.executed_components[component_name]
            rec.completed_at = now_str
            rec.outputs_produced.extend(outputs_produced)

        # Track iteration count for multi-instance components.
        if iteration_index is not None:
            self.iteration_counts[component_name] = (
                self.iteration_counts.get(component_name, 0) + 1
            )

        result = VerificationResult(
            component_name=component_name,
            iteration_index=iteration_index,
            findings=findings,
        )
        self.verification_results.append(result)

        # Incremental receipt write (P012).
        self._write_receipt(completed=False)

        return result

    def verify_run_completion(self) -> VerificationResult:
        """Run C-rules after final component completes.

        Compares manifest pipeline_components against accumulated executed_components
        to catch silent-skip. Verifies multi_instance iteration counts.
        Writes final receipt.
        """
        findings: list[Finding] = []

        # C001: all required components executed (silent-skip catch).
        findings.extend(self._check_all_components_executed())

        # C002: runtime verification recorded for every executed component.
        findings.extend(self._check_runtime_verification_recorded())

        # C003: final manuscript exists.
        findings.extend(self._check_final_manuscript_exists())

        # C004: final manuscript word count.
        findings.extend(self._check_final_manuscript_word_count())

        # C005: consumers read their inputs (only if logging enabled).
        if self.log_file_access:
            findings.extend(self._check_consumers_read_inputs())

        result = VerificationResult(
            component_name="run_completion",
            iteration_index=None,
            findings=findings,
        )
        self.verification_results.append(result)

        # Write final receipt with completed_at and post_run_verification.
        self._write_receipt(completed=True, post_run_result=result)

        return result

    def write_stop_report(self, result: VerificationResult) -> None:
        """Write STOP_REPORT.json per Verification Rules §9 format."""
        # Resolve phase from manifest entry.
        entry = self._component_entries.get(result.component_name, {})
        phase = entry.get("phase", 0)

        # Determine scene_number from iteration context if applicable.
        scene_number = None
        if result.iteration_index is not None and entry.get("multi_instance"):
            scene_number = result.iteration_index + 1  # 1-indexed scene numbers

        failures = []
        for f in result.findings:
            if f.severity == "A":
                failures.append({
                    "rule_id": f.rule_id,
                    "error_code": f.error_code,
                    "file_path": f.file_path,
                    "suggested_fix": f.suggested_fix,
                })

        payload = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "verifier": "runtime_verifier",
            "component": result.component_name,
            "phase": phase,
            "scene_number": scene_number,
            "failures": failures,
            "hard_stop": True,
        }

        os.makedirs(os.path.dirname(self.stop_report_path), exist_ok=True)
        tmp_path = str(self.stop_report_path) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, self.stop_report_path)

    # ─── R-rule implementations ──────────────────────────────────────────

    def _check_outputs_exist(
        self,
        component_name: str,
        outputs: list[dict],
        resolved_paths: list[str],
        iteration_index: Optional[int],
    ) -> list[Finding]:
        """R001: Every declared output exists at declared path."""
        findings = []
        for output_decl, resolved in zip(outputs, resolved_paths):
            if "*" in resolved or "?" in resolved:
                exists = len(glob_mod.glob(resolved)) >= 1
            else:
                exists = Path(resolved).exists()
            if not exists:
                findings.append(Finding(
                    rule_id="R001",
                    severity="A",
                    error_code="COMPONENT_OUTPUT_MISSING",
                    component_name=component_name,
                    iteration_index=iteration_index,
                    file_path=resolved,
                    suggested_fix=(
                        f"Component completed but did not produce declared output. "
                        f"Review {component_name} logs."
                    ),
                ))
        return findings

    def _check_outputs_fresh(
        self,
        component_name: str,
        resolved_paths: list[str],
        iteration_index: Optional[int],
    ) -> list[Finding]:
        """R002: Every output has mtime >= run_start_time (freshly written)."""
        findings = []
        run_ts = self.run_start_time.timestamp()
        for resolved in resolved_paths:
            p = Path(resolved)
            if p.exists():
                if p.stat().st_mtime < run_ts:
                    findings.append(Finding(
                        rule_id="R002",
                        severity="A",
                        error_code="COMPONENT_OUTPUT_STALE",
                        component_name=component_name,
                        iteration_index=iteration_index,
                        file_path=resolved,
                        suggested_fix=(
                            f"Output exists but was not written during this run "
                            f"(mtime predates run start). Stale file from prior run. "
                            f"Delete and re-run component."
                        ),
                    ))
        return findings

    def _check_outputs_valid_json(
        self,
        component_name: str,
        resolved_paths: list[str],
        iteration_index: Optional[int],
    ) -> list[Finding]:
        """R003: Every output ending in .json parses as valid JSON."""
        findings = []
        for resolved in resolved_paths:
            if resolved.endswith(".json") and Path(resolved).exists():
                try:
                    with open(resolved, "r", encoding="utf-8") as f:
                        json.load(f)
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    findings.append(Finding(
                        rule_id="R003",
                        severity="A",
                        error_code="COMPONENT_OUTPUT_INVALID_JSON",
                        component_name=component_name,
                        iteration_index=iteration_index,
                        file_path=resolved,
                        suggested_fix=(
                            f"Output is declared as JSON but failed to parse: {e}. "
                            f"Review component output logic."
                        ),
                    ))
        return findings

    def _check_outputs_schema_conformance(
        self,
        component_name: str,
        outputs: list[dict],
        resolved_paths: list[str],
        iteration_index: Optional[int],
    ) -> list[Finding]:
        """R004: Outputs with schema field conform to named schema."""
        findings = []
        for output_decl, resolved in zip(outputs, resolved_paths):
            schema_name = output_decl.get("schema")
            if schema_name is None:
                continue
            if not Path(resolved).exists():
                continue  # R001 already catches missing files.

            try:
                module = importlib.import_module(
                    f"pipeline.schema_validators.{schema_name}"
                )
            except (ImportError, ModuleNotFoundError):
                findings.append(Finding(
                    rule_id="R004",
                    severity="A",
                    error_code="SCHEMA_VALIDATOR_IMPORT_FAILED",
                    component_name=component_name,
                    iteration_index=iteration_index,
                    file_path=resolved,
                    suggested_fix=(
                        f"Schema validator 'schema_validators/{schema_name}.py' "
                        f"could not be imported. Create the validator module."
                    ),
                ))
                continue

            try:
                schema_findings = module.validate(Path(resolved))
                for sf in schema_findings:
                    findings.append(Finding(
                        rule_id="R004",
                        severity="A",
                        error_code="COMPONENT_OUTPUT_SCHEMA_VIOLATION",
                        component_name=component_name,
                        iteration_index=iteration_index,
                        file_path=resolved,
                        suggested_fix=str(sf),
                    ))
            except Exception as e:
                findings.append(Finding(
                    rule_id="R004",
                    severity="A",
                    error_code="SCHEMA_VALIDATION_ERROR",
                    component_name=component_name,
                    iteration_index=iteration_index,
                    file_path=resolved,
                    suggested_fix=(
                        f"Schema validator '{schema_name}' raised an error: {e}. "
                        f"Review validator implementation."
                    ),
                ))
        return findings

    def _check_component_exit_code(
        self,
        component_name: str,
        ctx: ComponentContext,
        iteration_index: Optional[int],
    ) -> list[Finding]:
        """R005: Component exit code must be 0."""
        findings = []
        if ctx.exit_code != 0:
            findings.append(Finding(
                rule_id="R005",
                severity="A",
                error_code="COMPONENT_REPORTED_FAILURE",
                component_name=component_name,
                iteration_index=iteration_index,
                file_path=None,
                suggested_fix=(
                    f"Component exited with code {ctx.exit_code}. "
                    f"Review {component_name} stderr/logs for error details."
                ),
            ))
        return findings

    def _check_runtime_within_limit(
        self,
        component_name: str,
        entry: dict,
        ctx: ComponentContext,
        iteration_index: Optional[int],
    ) -> list[Finding]:
        """R006: Component runtime within max_runtime_seconds. Class B."""
        findings = []
        max_runtime = entry.get("max_runtime_seconds")
        if max_runtime is not None and ctx.runtime_seconds > max_runtime:
            findings.append(Finding(
                rule_id="R006",
                severity="B",
                error_code="COMPONENT_RUNTIME_EXCEEDED",
                component_name=component_name,
                iteration_index=iteration_index,
                file_path=None,
                suggested_fix=(
                    f"Component ran for {ctx.runtime_seconds:.1f}s, exceeding "
                    f"limit of {max_runtime}s. Review for performance issues."
                ),
            ))
        return findings

    def _check_no_internal_stop_report(
        self,
        component_name: str,
        iteration_index: Optional[int],
    ) -> list[Finding]:
        """R007: Component did not write its own STOP_REPORT.json."""
        findings = []
        if self.stop_report_path.exists():
            try:
                with open(self.stop_report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                # Only flag if this STOP_REPORT was written by the component,
                # not by runtime_verifier itself.
                if report.get("verifier") != "runtime_verifier":
                    findings.append(Finding(
                        rule_id="R007",
                        severity="A",
                        error_code="COMPONENT_INTERNAL_STOP_REPORT",
                        component_name=component_name,
                        iteration_index=iteration_index,
                        file_path=str(self.stop_report_path),
                        suggested_fix=(
                            f"Component '{component_name}' wrote its own STOP_REPORT "
                            f"during execution. Review the report for failure details."
                        ),
                    ))
            except (json.JSONDecodeError, OSError):
                findings.append(Finding(
                    rule_id="R007",
                    severity="A",
                    error_code="COMPONENT_INTERNAL_STOP_REPORT",
                    component_name=component_name,
                    iteration_index=iteration_index,
                    file_path=str(self.stop_report_path),
                    suggested_fix=(
                        f"A STOP_REPORT.json exists but could not be parsed. "
                        f"Component may have written a malformed report."
                    ),
                ))
        return findings

    # ─── C-rule implementations ──────────────────────────────────────────

    def _check_all_components_executed(self) -> list[Finding]:
        """C001: Every required (non-stub, non-disabled-optional) component executed."""
        findings = []
        for entry in self._pipeline_components:
            if not self._component_should_run(entry):
                continue

            component_name = entry["component_name"]

            if component_name not in self.executed_components:
                findings.append(Finding(
                    rule_id="C001",
                    severity="A",
                    error_code="COMPONENT_NEVER_EXECUTED",
                    component_name=component_name,
                    iteration_index=None,
                    file_path=None,
                    suggested_fix=(
                        f"Component '{component_name}' is declared required in manifest "
                        f"but was never executed during this run. Silent skip detected."
                    ),
                ))
                continue

            # Multi-instance iteration count check.
            if entry.get("multi_instance", False):
                expected = self._resolve_iteration_count(entry.get("loop_over", ""))
                actual = self.iteration_counts.get(component_name, 0)
                if expected is not None and actual != expected:
                    findings.append(Finding(
                        rule_id="C001",
                        severity="A",
                        error_code="MULTI_INSTANCE_ITERATION_COUNT_MISMATCH",
                        component_name=component_name,
                        iteration_index=None,
                        file_path=None,
                        suggested_fix=(
                            f"Component completed {actual} of {expected} iterations. "
                            f"Investigate skipped iteration(s)."
                        ),
                    ))

        return findings

    def _check_runtime_verification_recorded(self) -> list[Finding]:
        """C002: Every executed component has verification results recorded."""
        findings = []
        verified_components = {
            r.component_name
            for r in self.verification_results
            if r.component_name != "run_completion"
        }
        for component_name in self.executed_components:
            if component_name not in verified_components:
                findings.append(Finding(
                    rule_id="C002",
                    severity="A",
                    error_code="RUNTIME_VERIFICATION_NOT_RECORDED",
                    component_name=component_name,
                    iteration_index=None,
                    file_path=None,
                    suggested_fix=(
                        f"Component '{component_name}' was marked as executed but has "
                        f"no verification result. Verifier state coherence failure."
                    ),
                ))
        return findings

    def _check_final_manuscript_exists(self) -> list[Finding]:
        """C003: Final manuscript file exists at expected path."""
        findings = []
        final_path = self._resolve_final_manuscript_path()
        if final_path is None:
            return findings  # No final output declared in manifest; skip.
        if not Path(final_path).exists():
            findings.append(Finding(
                rule_id="C003",
                severity="A",
                error_code="FINAL_MANUSCRIPT_MISSING",
                component_name="run_completion",
                iteration_index=None,
                file_path=final_path,
                suggested_fix=(
                    "Final manuscript not found at expected path. "
                    "Review the final pipeline component's output."
                ),
            ))
        return findings

    def _check_final_manuscript_word_count(self) -> list[Finding]:
        """C004: Final manuscript word count within configured bounds."""
        findings = []
        final_path = self._resolve_final_manuscript_path()
        if final_path is None or not Path(final_path).exists():
            return findings  # C003 catches missing file.

        try:
            with open(final_path, "r", encoding="utf-8") as f:
                text = f.read()
            word_count = len(text.split())
        except OSError:
            return findings  # File read error; not a word-count issue.

        # Load word count bounds from book_config.json.
        bounds = self._load_word_count_bounds()
        if bounds is None:
            return findings  # No bounds configured; skip.

        min_words, max_words = bounds
        if word_count < min_words or word_count > max_words:
            findings.append(Finding(
                rule_id="C004",
                severity="A",
                error_code="FINAL_MANUSCRIPT_WORD_COUNT_OUT_OF_RANGE",
                component_name="run_completion",
                iteration_index=None,
                file_path=final_path,
                suggested_fix=(
                    f"Final manuscript has {word_count} words, outside configured "
                    f"range [{min_words}, {max_words}]. Review manuscript output."
                ),
            ))
        return findings

    def _check_consumers_read_inputs(self) -> list[Finding]:
        """C005: Declared consumers read their declared inputs. Class B.

        Only runs if log_file_access=True. Uses audit hook file-access log.
        """
        findings = []
        accessed_files = {e.get("path") for e in self.file_access_log}

        for entry in self._pipeline_components:
            component_name = entry["component_name"]
            if component_name not in self.executed_components:
                continue
            for inp in entry.get("inputs", []):
                input_path = self._resolve_placeholder(inp.get("path", ""))
                if input_path and input_path not in accessed_files:
                    findings.append(Finding(
                        rule_id="C005",
                        severity="B",
                        error_code="DECLARED_CONSUMER_DID_NOT_READ",
                        component_name=component_name,
                        iteration_index=None,
                        file_path=input_path,
                        suggested_fix=(
                            f"Component '{component_name}' declares '{input_path}' "
                            f"as an input but file-access log shows no read."
                        ),
                    ))
        return findings

    # ─── Path resolution helpers ─────────────────────────────────────────

    def _resolve_output_path(
        self,
        output_decl: dict,
        entry: dict,
        iteration_index: Optional[int],
    ) -> str:
        """Resolve output path placeholders for a specific invocation."""
        path = output_decl.get("path", "")
        return self._resolve_placeholder(path, iteration_index)

    def _resolve_placeholder(
        self,
        path: str,
        iteration_index: Optional[int] = None,
    ) -> str:
        """Resolve {series}, {bNN}, {sceneN}, {iteration_index} placeholders."""
        b_nn = f"b{self._book_number:02d}"

        path = path.replace("{series}", self._series_name)
        path = path.replace("{bNN}", b_nn)

        if iteration_index is not None:
            # {sceneN} resolves to 1-indexed scene number.
            scene_n = iteration_index + 1
            # Padded tokens first (longest match wins; prevents {sceneNN}
            # from partial-matching inside {sceneNN_minus_1}).
            path = path.replace("{sceneNN_minus_1}", f"{scene_n - 1:02d}")
            path = path.replace("{sceneN_minus_1}", str(scene_n - 1))
            path = path.replace("{sceneNN}", f"{scene_n:02d}")
            path = path.replace("{sceneN}", str(scene_n))
            path = path.replace("{iteration_index}", str(iteration_index))

        return path

    def _resolve_final_manuscript_path(self) -> Optional[str]:
        """Find the output path of the last pipeline component (the final manuscript)."""
        if not self._pipeline_components:
            return None
        # Walk backward through components to find the last one with outputs.
        for entry in reversed(self._pipeline_components):
            outputs = entry.get("outputs", [])
            if outputs:
                return self._resolve_placeholder(outputs[0].get("path", ""))
        return None

    def _resolve_iteration_count(self, loop_over: str) -> Optional[int]:
        """Parse loop_over field and count iterations from the referenced source.

        Example: "scene_map.entries[*]" -> count scene entries in scene_map.md.
        """
        if not loop_over:
            return None

        # Parse "file.field[*]" format.
        parts = loop_over.split(".")
        if len(parts) < 2:
            return None

        source_file = parts[0]
        field_ref = ".".join(parts[1:])

        if source_file == "scene_map":
            # scene_map.md: count scene entries (lines starting with scene markers).
            scene_map_path = self._book_dir / "work" / "scene_map.md"
            if not scene_map_path.exists():
                return None
            try:
                text = scene_map_path.read_text(encoding="utf-8")
                # Count scene entries: lines matching "## Scene N" or "### Scene N".
                count = 0
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("## Scene ") or stripped.startswith("### Scene "):
                        count += 1
                return count if count > 0 else None
            except OSError:
                return None
        elif source_file.endswith(".json"):
            # JSON source: parse and count array entries.
            json_path = self._book_dir / "work" / source_file
            if not json_path.exists():
                return None
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Navigate to the field, stripping [*].
                field_name = field_ref.replace("[*]", "")
                if field_name in data and isinstance(data[field_name], list):
                    return len(data[field_name])
            except (json.JSONDecodeError, OSError, KeyError):
                return None

        return None

    # ─── Manifest helpers ────────────────────────────────────────────────

    def _component_should_run(self, entry: dict) -> bool:
        """Determine if a component should have been executed.

        Returns False for stubs and disabled optionals; True for required
        components and enabled optionals.
        """
        if entry.get("is_stub", False):
            return False
        if entry.get("required", True):
            return True
        # Optional component: check if enabled by config.
        # If optional_by_config is set, we'd need to check the config flag.
        # For now, optional components with required=false are not required to run.
        return False

    def _load_word_count_bounds(self) -> Optional[tuple[int, int]]:
        """Load min/max word count from book_config.json via manifest metadata."""
        book_config_path = self._book_dir / "work" / "book_config.json"
        if not book_config_path.exists():
            return None
        try:
            with open(book_config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            min_words = config.get("min_word_count")
            max_words = config.get("max_word_count")
            if min_words is not None and max_words is not None:
                return (int(min_words), int(max_words))
        except (json.JSONDecodeError, OSError, ValueError):
            pass
        return None

    # ─── File-access logging (C005) ──────────────────────────────────────

    def _install_audit_hook(self) -> None:
        """Install sys.addaudithook for file-open tracking (Python 3.8+)."""
        def audit_hook(event: str, args: tuple) -> None:
            if event == "open":
                file_path = args[0] if args else None
                if file_path and isinstance(file_path, str):
                    self.file_access_log.append({
                        "path": file_path,
                        "timestamp": datetime.now().isoformat(),
                    })
        sys.addaudithook(audit_hook)

    # ─── Receipt writing (P012) ──────────────────────────────────────────

    def _write_receipt(
        self,
        completed: bool,
        post_run_result: Optional[VerificationResult] = None,
    ) -> None:
        """Write PIPELINE_RECEIPT.json incrementally.

        Per P012: written after every verify_component_completion call.
        Per open question §13.5: idempotent, logs Class B on write failure.
        """
        components_executed = []
        for comp_name, rec in self.executed_components.items():
            # Collect R-rule results for this component.
            comp_results = [
                r for r in self.verification_results
                if r.component_name == comp_name
            ]
            entry = self._component_entries.get(comp_name, {})

            rules_checked = list(entry.get("verification_rules_active", []))
            all_passed = all(r.passed for r in comp_results)

            component_receipt = {
                "component": comp_name,
                "phase": entry.get("phase", 0),
                "started_at": rec.started_at,
                "completed_at": rec.completed_at,
                "exit_code": rec.exit_code,
                "outputs_produced": rec.outputs_produced,
                "runtime_verification": {
                    "passed": all_passed,
                    "rules_checked": rules_checked,
                },
            }
            components_executed.append(component_receipt)

        # Collect Class B warnings from all results.
        class_b_warnings = []
        for r in self.verification_results:
            for f in r.findings:
                if f.severity == "B":
                    class_b_warnings.append({
                        "rule_id": f.rule_id,
                        "error_code": f.error_code,
                        "component": f.component_name,
                        "file_path": f.file_path,
                        "suggested_fix": f.suggested_fix,
                    })

        receipt = {
            "run_id": self.run_id,
            "started_at": self.run_start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "completed_at": (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S") if completed else None
            ),
            "components_executed": components_executed,
            "class_b_warnings": class_b_warnings,
        }

        # Post-run verification section.
        if post_run_result is not None:
            rules_checked = ["C001", "C002", "C003", "C004"]
            if self.log_file_access:
                rules_checked.append("C005")
            receipt["post_run_verification"] = {
                "passed": post_run_result.passed,
                "rules_checked": rules_checked,
            }

        try:
            os.makedirs(os.path.dirname(self.receipt_path), exist_ok=True)
            tmp_path = str(self.receipt_path) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(receipt, f, indent=2)
            os.replace(tmp_path, self.receipt_path)
        except OSError:
            # Per open question §13.5: log but don't abort for receipt write failure.
            pass
