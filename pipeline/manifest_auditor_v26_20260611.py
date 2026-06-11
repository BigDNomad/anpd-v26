# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify master_controller.py to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
manifest_auditor.py — V25 Manifest Audit Component

Prevents the V24 failure mode where master_controller had components
commented out, deleted, or quietly bypassed. Runs as the FIRST step
of master_controller's preflight phase.
"""

import argparse
import ast
import io
import json
import os
import subprocess
import sys
import tokenize
from datetime import datetime, timezone
from pathlib import Path

PIPELINE_DIR = "/anpd/v26/pipeline"

HARDCODED_CRITICAL_COMPONENTS = [
    "synopsis_generator",
    "synopsis_auditor",
    "scene_writer",
    "scene_auditor",
    "manuscript_auditor",
    "manifest_auditor",
]

REQUIRED_TOP_LEVEL_FIELDS = [
    "manifest_version",
    "schema_version_required",
    "last_updated",
    "metadata",
    "pipeline_components",
    "non_pipeline_components",
]

FINAL_ARTIFACT_PATTERNS = [
    "PIPELINE_RECEIPT",
    "STOP_REPORT",
    "manuscript",
    ".docx",
]


# ---------------------------------------------------------------------------
# Finding helpers
# ---------------------------------------------------------------------------

def _finding(check: str, severity: str, message: str) -> dict:
    return {"check": check, "severity": severity, "message": message}


def _a(check: str, message: str) -> dict:
    return _finding(check, "A", message)


def _b(check: str, message: str) -> dict:
    return _finding(check, "B", message)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _c001_manifest_loads(manifest_path: str) -> tuple:
    """C001 — manifest_loads: json.load succeeds, manifest_version present."""
    findings_a = []
    manifest = None
    version = None

    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        findings_a.append(_a("C001", f"Failed to load manifest: {exc}"))
        return findings_a, manifest, version

    if "manifest_version" not in manifest:
        findings_a.append(
            _a("C001", "manifest_version field missing from manifest")
        )
    else:
        version = manifest["manifest_version"]

    return findings_a, manifest, version


def _c002_required_top_level_fields(manifest: dict) -> list:
    """C002 — required_top_level_fields."""
    findings = []
    for field in REQUIRED_TOP_LEVEL_FIELDS:
        if field not in manifest:
            findings.append(
                _a("C002", f"Required top-level field missing: {field}")
            )
    return findings


def _all_components(manifest: dict) -> list:
    """Return combined list of pipeline + non-pipeline component entries."""
    pipeline = manifest.get("pipeline_components", [])
    non_pipeline = manifest.get("non_pipeline_components", [])
    return list(pipeline) + list(non_pipeline)


def _c003_every_component_file_exists(manifest: dict) -> list:
    """C003 — every_component_file_exists."""
    findings = []
    for entry in _all_components(manifest):
        fp = entry.get("file_path", "")
        if not fp:
            findings.append(
                _a(
                    "C003",
                    f"Component {entry.get('component_name', '???')} "
                    f"has no file_path",
                )
            )
            continue
        if not os.path.isfile(fp):
            findings.append(
                _a(
                    "C003",
                    f"File not found for component "
                    f"{entry.get('component_name', '???')}: {fp}",
                )
            )
    return findings


def _module_name_from_path(file_path: str) -> str:
    """Derive module name from file_path basename without .py."""
    return Path(file_path).stem


def _c004_every_component_importable(manifest: dict) -> list:
    """C004 — every_component_importable via subprocess."""
    findings = []
    for entry in _all_components(manifest):
        fp = entry.get("file_path", "")
        if not fp:
            continue
        mod = _module_name_from_path(fp)
        result = subprocess.run(
            ["python3", "-c", f"import {mod}"],
            cwd=PIPELINE_DIR,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            findings.append(
                _a(
                    "C004",
                    f"Component {entry.get('component_name', '???')} "
                    f"({mod}) failed to import: {stderr[:300]}",
                )
            )
    return findings


def _c005_every_entry_function_exists(manifest: dict) -> list:
    """C005 — every_entry_function_exists via subprocess."""
    findings = []
    for entry in _all_components(manifest):
        func = entry.get("entry_function", "")
        if not func:
            continue
        fp = entry.get("file_path", "")
        if not fp:
            continue
        mod = _module_name_from_path(fp)
        snippet = (
            f"from {mod} import {func}; assert callable({func})"
        )
        result = subprocess.run(
            ["python3", "-c", snippet],
            cwd=PIPELINE_DIR,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            findings.append(
                _a(
                    "C005",
                    f"Entry function {func} not callable in {mod}: "
                    f"{stderr[:300]}",
                )
            )
    return findings


def _parse_components_dict_keys(master_controller_path: str) -> set | None:
    """Parse master_controller.py AST for a COMPONENTS dict assignment.

    Returns the set of string keys, or None if not found.
    """
    try:
        with open(master_controller_path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        return None

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    keys = set()

    def _extract_dict_keys(dict_node: ast.Dict) -> None:
        for key in dict_node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
            elif isinstance(key, ast.Str):  # Python 3.7 compat
                keys.add(key.s)

    for node in ast.walk(tree):
        # Look for: COMPONENTS = { ... }
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "COMPONENTS":
                    if isinstance(node.value, ast.Dict):
                        _extract_dict_keys(node.value)
        # Also handle: COMPONENTS: dict[str, str] = { ... }
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "COMPONENTS":
                if node.value is not None and isinstance(node.value, ast.Dict):
                    _extract_dict_keys(node.value)

    return keys if keys else None


def _c006_master_controller_imports_required(
    manifest: dict, master_controller_path: str
) -> list:
    """C006 — master_controller_imports_required_components."""
    findings = []
    keys = _parse_components_dict_keys(master_controller_path)
    if keys is None:
        findings.append(
            _a(
                "C006",
                "Could not parse COMPONENTS dict from "
                f"{master_controller_path}",
            )
        )
        return findings

    # Only check pipeline_components — non_pipeline_components are library
    # utilities that master_controller imports indirectly, not via COMPONENTS.
    for entry in manifest.get("pipeline_components", []):
        name = entry.get("component_name", "")
        if not name:
            continue
        if name not in keys:
            findings.append(
                _a(
                    "C006",
                    f"Pipeline component {name} is registered in manifest but "
                    f"missing from COMPONENTS dict in master_controller",
                )
            )
    return findings


def _tokenize_master_controller(master_controller_path: str):
    """Tokenize master_controller.py and return (comment_tokens, non_comment_source).

    Returns:
        comment_tokens: list of token strings that are COMMENT type
        non_comment_source: concatenated string of all non-comment tokens
    """
    try:
        with open(master_controller_path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        return None, None

    comment_tokens = []
    non_comment_parts = []

    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                comment_tokens.append(tok.string)
            elif tok.type not in (
                tokenize.NEWLINE,
                tokenize.NL,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENCODING,
                tokenize.ENDMARKER,
            ):
                non_comment_parts.append(tok.string)
    except tokenize.TokenError:
        return None, None

    non_comment_source = " ".join(non_comment_parts)
    return comment_tokens, non_comment_source


def _c007_no_components_commented_out(
    manifest: dict, master_controller_path: str
) -> list:
    """C007 — no_components_commented_out_in_master_controller."""
    findings = []
    comment_tokens, non_comment_source = _tokenize_master_controller(
        master_controller_path
    )

    if comment_tokens is None:
        findings.append(
            _a(
                "C007",
                f"Could not tokenize {master_controller_path}",
            )
        )
        return findings

    for entry in _all_components(manifest):
        name = entry.get("component_name", "")
        if not name:
            continue

        in_comment = any(name in ct for ct in comment_tokens)
        in_source = name in non_comment_source

        if in_comment and not in_source:
            findings.append(
                _a(
                    "C007",
                    f"Component {name} appears in a comment but NOT in "
                    f"active source code in master_controller — "
                    f"possible commented-out bypass",
                )
            )

    return findings


def _c008_no_dependency_cycles(manifest: dict) -> list:
    """C008 — no_dependency_cycles via topological sort."""
    findings = []
    pipeline = manifest.get("pipeline_components", [])
    if not pipeline:
        return findings

    # Build adjacency: edge from A -> B if A in B's consumed_by
    # Actually spec says: edge from A to B if A appears in B's consumed_by
    # i.e. A is consumed by B, so A -> B (A must run before B)
    graph: dict[str, list[str]] = {}
    all_names = set()

    for entry in pipeline:
        name = entry.get("component_name", "")
        if name:
            all_names.add(name)
            if name not in graph:
                graph[name] = []

    for entry in pipeline:
        name = entry.get("component_name", "")
        consumed_by = entry.get("consumed_by", [])
        if not name:
            continue
        for consumer in consumed_by:
            if consumer in all_names:
                # Edge: name -> consumer (name feeds into consumer)
                graph.setdefault(name, []).append(consumer)
                graph.setdefault(consumer, [])

    # Kahn's algorithm for topological sort / cycle detection
    in_degree: dict[str, int] = {n: 0 for n in graph}
    for node in graph:
        for neighbor in graph[node]:
            in_degree[neighbor] = in_degree.get(neighbor, 0) + 1

    queue = [n for n in graph if in_degree[n] == 0]
    visited_count = 0

    while queue:
        current = queue.pop(0)
        visited_count += 1
        for neighbor in graph.get(current, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited_count != len(graph):
        cycle_nodes = [n for n in graph if in_degree[n] > 0]
        findings.append(
            _a(
                "C008",
                f"Dependency cycle detected among: "
                f"{', '.join(sorted(cycle_nodes))}",
            )
        )

    return findings


def _registered_names(manifest: dict) -> set:
    """Return set of all registered component_name values."""
    names = set()
    for entry in _all_components(manifest):
        name = entry.get("component_name", "")
        if name:
            names.add(name)
    return names


def _c009_every_produced_by_resolves(manifest: dict) -> list:
    """C009 — every_produced_by_resolves."""
    findings = []
    names = _registered_names(manifest)

    for entry in _all_components(manifest):
        inputs = entry.get("inputs", [])
        if not inputs:
            continue
        for inp in inputs:
            produced_by = inp.get("produced_by", "")
            if not produced_by:
                continue
            if produced_by == "operator":
                continue
            if produced_by not in names:
                findings.append(
                    _a(
                        "C009",
                        f"Component {entry.get('component_name', '???')} "
                        f"input produced_by={produced_by!r} does not resolve "
                        f"to any registered component",
                    )
                )

    return findings


VALID_TERMINAL_CONSUMERS = {"operator", "master_controller"}


def _c010_every_consumed_by_resolves(manifest: dict) -> list:
    """C010 — every_consumed_by_resolves."""
    findings = []
    names = _registered_names(manifest)

    for entry in manifest.get("non_pipeline_components", []):
        consumed_by = entry.get("consumed_by", [])
        if not consumed_by:
            continue
        for consumer in consumed_by:
            if consumer in VALID_TERMINAL_CONSUMERS:
                continue
            if consumer not in names:
                findings.append(
                    _a(
                        "C010",
                        f"Component {entry.get('component_name', '???')} "
                        f"consumed_by={consumer!r} does not resolve "
                        f"to any registered component",
                    )
                )

    return findings


def _c011_hardcoded_critical_components(manifest: dict) -> list:
    """C011 — hardcoded_critical_components_required."""
    findings = []
    names = _registered_names(manifest)

    for required in HARDCODED_CRITICAL_COMPONENTS:
        if required not in names:
            findings.append(
                _a(
                    "C011",
                    f"Hardcoded critical component missing: {required}",
                )
            )

    return findings


def _c012_manifest_auditor_self_registered(manifest: dict) -> list:
    """C012 — manifest_auditor must be in non_pipeline_components."""
    findings = []
    non_pipeline = manifest.get("non_pipeline_components", [])
    names = {
        e.get("component_name", "") for e in non_pipeline
    }
    if "manifest_auditor" not in names:
        findings.append(
            _a(
                "C012",
                "manifest_auditor is not registered in "
                "non_pipeline_components",
            )
        )
    return findings


def _c013_unconsumed_outputs(manifest: dict) -> list:
    """C013 — class_b_unconsumed_outputs."""
    findings = []
    pipeline = manifest.get("pipeline_components", [])

    for entry in pipeline:
        outputs = entry.get("outputs", [])
        if not outputs:
            continue
        for output in outputs:
            consumed_by = output.get("consumed_by", [])
            if consumed_by:
                continue
            # Check if this is a final-artifact pattern
            output_name = output.get("name", "")
            output_path = output.get("file_path", "")
            combined = f"{output_name} {output_path}"

            is_final = any(pat in combined for pat in FINAL_ARTIFACT_PATTERNS)
            if not is_final:
                findings.append(
                    _b(
                        "C013",
                        f"Output {output_name!r} from "
                        f"{entry.get('component_name', '???')} "
                        f"is produced but has no consumers",
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Main audit runner
# ---------------------------------------------------------------------------

def run_manifest_audit(
    manifest_path: str = "/anpd/v26/pipeline/pipeline_manifest.json",
    master_controller_path: str = "/anpd/v26/pipeline/master_controller.py",
    report_dir: str = "/anpd/v26/out/manifest_audits",
) -> dict:
    """Run all manifest audit checks. Returns a findings dict.

    Returns:
        {
            "passed": bool,
            "class_a_findings": [list of finding dicts],
            "class_b_findings": [list of finding dicts],
            "report_path": str (path to written audit report JSON),
            "manifest_version": str,
            "checked_at": ISO timestamp,
        }
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    class_a: list[dict] = []
    class_b: list[dict] = []

    # C001 — manifest loads
    c001_findings, manifest, version = _c001_manifest_loads(manifest_path)
    class_a.extend(c001_findings)

    if manifest is None:
        # Cannot continue without a loaded manifest
        report = _write_report(
            checked_at=checked_at,
            manifest_version=None,
            manifest_path=manifest_path,
            master_controller_path=master_controller_path,
            class_a=class_a,
            class_b=class_b,
            report_dir=report_dir,
        )
        _print_summary(class_a, class_b)
        return {
            "passed": False,
            "class_a_findings": class_a,
            "class_b_findings": class_b,
            "report_path": report,
            "manifest_version": None,
            "checked_at": checked_at,
        }

    # C002 — required top-level fields
    class_a.extend(_c002_required_top_level_fields(manifest))

    # C003 — every component file exists
    class_a.extend(_c003_every_component_file_exists(manifest))

    # C004 — every component importable
    class_a.extend(_c004_every_component_importable(manifest))

    # C005 — every entry function exists
    class_a.extend(_c005_every_entry_function_exists(manifest))

    # C006 — master_controller imports required components
    class_a.extend(
        _c006_master_controller_imports_required(
            manifest, master_controller_path
        )
    )

    # C007 — no components commented out in master_controller
    class_a.extend(
        _c007_no_components_commented_out(manifest, master_controller_path)
    )

    # C008 — no dependency cycles
    class_a.extend(_c008_no_dependency_cycles(manifest))

    # C009 — every produced_by resolves
    class_a.extend(_c009_every_produced_by_resolves(manifest))

    # C010 — every consumed_by resolves
    class_a.extend(_c010_every_consumed_by_resolves(manifest))

    # C011 — hardcoded critical components required
    class_a.extend(_c011_hardcoded_critical_components(manifest))

    # C012 — manifest_auditor self-registered
    class_a.extend(_c012_manifest_auditor_self_registered(manifest))

    # C013 — unconsumed outputs (Class B)
    class_b.extend(_c013_unconsumed_outputs(manifest))

    # Write report
    report = _write_report(
        checked_at=checked_at,
        manifest_version=version,
        manifest_path=manifest_path,
        master_controller_path=master_controller_path,
        class_a=class_a,
        class_b=class_b,
        report_dir=report_dir,
    )

    _print_summary(class_a, class_b)

    passed = len(class_a) == 0

    return {
        "passed": passed,
        "class_a_findings": class_a,
        "class_b_findings": class_b,
        "report_path": report,
        "manifest_version": version,
        "checked_at": checked_at,
    }


# ---------------------------------------------------------------------------
# Report writing and output
# ---------------------------------------------------------------------------

def _write_report(
    checked_at: str,
    manifest_version: str | None,
    manifest_path: str,
    master_controller_path: str,
    class_a: list[dict],
    class_b: list[dict],
    report_dir: str,
) -> str:
    """Write audit findings to a timestamped JSON file. Returns the path."""
    os.makedirs(report_dir, exist_ok=True)

    timestamp_label = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename = f"manifest_audit_{timestamp_label}.json"
    report_path = os.path.join(report_dir, filename)

    report = {
        "checked_at": checked_at,
        "manifest_version": manifest_version,
        "manifest_path": manifest_path,
        "master_controller_path": master_controller_path,
        "passed": len(class_a) == 0,
        "class_a_findings": class_a,
        "class_b_findings": class_b,
    }

    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    return report_path


def _print_summary(class_a: list[dict], class_b: list[dict]) -> None:
    """Print human-readable summary to stdout."""
    total_checks = 13
    num_a = len(class_a)
    num_b = len(class_b)

    print(
        f"[manifest_auditor] {total_checks} checks run, "
        f"{num_a} Class A findings, {num_b} Class B findings"
    )

    if num_a == 0:
        print("[manifest_auditor] PASS")
    else:
        print("[manifest_auditor] FAIL")
        for finding in class_a[:3]:
            print(f"  {finding['check']}: {finding['message']}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="V25 Manifest Auditor — pipeline integrity checker"
    )
    parser.add_argument(
        "--manifest",
        default="/anpd/v26/pipeline/pipeline_manifest.json",
        help="Path to pipeline_manifest.json",
    )
    parser.add_argument(
        "--master-controller",
        default="/anpd/v26/pipeline/master_controller.py",
        help="Path to master_controller.py",
    )
    parser.add_argument(
        "--report-dir",
        default="/anpd/v26/out/manifest_audits",
        help="Directory for audit report output",
    )

    args = parser.parse_args()

    try:
        result = run_manifest_audit(
            manifest_path=args.manifest,
            master_controller_path=args.master_controller,
            report_dir=args.report_dir,
        )
    except Exception as exc:
        print(f"[manifest_auditor] FATAL: {exc}", file=sys.stderr)
        sys.exit(2)

    if result["passed"]:
        sys.exit(0)
    else:
        sys.exit(1)
