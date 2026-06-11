# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify master_controller.py to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
ANPD V24 Pipeline Receipt Writer (Commit 3 — full markdown rendering)

Writes PIPELINE_RECEIPT.json (canonical, per Data Standards §4.5 + V24
extensions) and pipeline_receipt.md (companion human-readable summary).

Called from master_controller._finalize_receipt() at end of every run,
including hard-stop runs.

Commit 3 upgrade from Commit 1 stub: full markdown rendering with gate
verdicts table, components_called summary, cost breakdown, invocation
timeline, capsule paths, output validation.
"""

from __future__ import annotations

import json
import os


INTERNAL_FIELDS = ("_args", "_started_at_epoch")


def receipt_json_path(book_dir: str) -> str:
    return os.path.join(book_dir, "out", "reports", "PIPELINE_RECEIPT.json")


def receipt_md_path(book_dir: str) -> str:
    return os.path.join(book_dir, "out", "reports", "pipeline_receipt.md")


def _strip_internal_fields(pipeline_state: dict) -> dict:
    return {k: v for k, v in pipeline_state.items() if k not in INTERNAL_FIELDS}


def write_receipt(pipeline_state: dict, book_dir: str) -> tuple[str, str]:
    """Write canonical JSON receipt + companion markdown."""
    json_path = receipt_json_path(book_dir)
    md_path = receipt_md_path(book_dir)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)

    receipt = _strip_internal_fields(pipeline_state)

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=2)

    _write_receipt_md(receipt, md_path)
    return json_path, md_path


def _write_receipt_md(receipt: dict, md_path: str) -> None:
    """Render PIPELINE_RECEIPT into a human-readable markdown summary."""
    lines: list[str] = []
    lines.append("# ANPD V24 Pipeline Receipt")
    lines.append("")

    lines.extend(_render_header(receipt))
    lines.append("")
    lines.extend(_render_status(receipt))
    lines.append("")
    lines.extend(_render_gate_verdicts(receipt))
    lines.append("")
    lines.extend(_render_components_summary(receipt))
    lines.append("")

    timeline = receipt.get("invocation_timeline", [])
    if timeline:
        lines.extend(_render_timeline(timeline))
        lines.append("")

    cost_log = receipt.get("cost_log", [])
    if cost_log:
        lines.extend(_render_cost(cost_log))
        lines.append("")

    lines.extend(_render_capsule(receipt))
    lines.append("")
    lines.extend(_render_output(receipt))

    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _render_header(receipt: dict) -> list[str]:
    return [
        f"- **Run timestamp:** {receipt.get('run_timestamp', 'unknown')}",
        f"- **Git commit:** `{receipt.get('git_commit_hash', 'unknown')}`",
        f"- **Mode:** {receipt.get('pipeline_mode', 'unknown')}",
        f"- **Series:** {receipt.get('series', 'unknown')}",
        f"- **Book number:** {receipt.get('book_number', 'unknown')}",
        f"- **Title:** {receipt.get('title', 'unknown')}",
    ]


def _render_status(receipt: dict) -> list[str]:
    hard_stop = receipt.get("hard_stop", False)
    output_valid = receipt.get("output_valid", False)
    class_a = receipt.get("class_a_failures", 0)
    class_b = receipt.get("class_b_violations", 0)

    if hard_stop:
        banner = "⚠ HARD STOP — pipeline halted"
    elif output_valid:
        banner = "✓ Pipeline complete — output valid"
    else:
        banner = "○ Pipeline ran without halt; output validation pending"

    return [
        "## Status",
        "",
        banner,
        "",
        f"- Class A failures: **{class_a}**",
        f"- Class B violations: **{class_b}**",
    ]


def _render_gate_verdicts(receipt: dict) -> list[str]:
    verdicts = receipt.get("gate_verdicts", {})
    rows = [
        "## Gate Verdicts",
        "",
        "| Gate | Verdict |",
        "|---|---|",
    ]
    for gate in ("synopsis", "character_profiles", "manuscript"):
        v = verdicts.get(gate, "not_yet_run")
        rows.append(f"| {gate} | {_verdict_marker(v)} {v} |")
    return rows


def _verdict_marker(verdict: str) -> str:
    return {
        "pass": "✓",
        "fail": "✗",
        "stubbed": "○",
        "skipped": "—",
    }.get(verdict, "·")


def _render_components_summary(receipt: dict) -> list[str]:
    components = receipt.get("components_called", {})
    invoked = sorted(name for name, called in components.items() if called)
    not_invoked = sorted(name for name, called in components.items() if not called)

    rows = ["## Components", ""]
    if invoked:
        rows.append("**Invoked:**")
        for name in invoked:
            rows.append(f"- {name}")
    else:
        rows.append("_No components invoked._")
    rows.append("")
    if not_invoked:
        rows.append("**Not invoked:**")
        for name in not_invoked:
            rows.append(f"- {name}")
    return rows


def _render_timeline(timeline: list[dict]) -> list[str]:
    rows = [
        "## Invocation Timeline",
        "",
        "| Component | Status | Started | Findings | STOP_REPORT |",
        "|---|---|---|---|---|",
    ]
    for entry in timeline:
        component = entry.get("component", "?")
        status = entry.get("status", "?")
        started = entry.get("started_at", "?")
        finding_count = entry.get("finding_count", 0)
        stop_written = "yes" if entry.get("stop_report_written") else "no"
        rows.append(
            f"| {component} | {_status_marker(status)} {status} | {started} | {finding_count} | {stop_written} |"
        )
    return rows


def _status_marker(status: str) -> str:
    return {
        "succeeded": "✓",
        "failed": "✗",
        "stubbed": "○",
        "skipped": "—",
    }.get(status, "·")


def _render_cost(cost_log: list[dict]) -> list[str]:
    rows = [
        "## Cost Summary",
        "",
        "| Component | Model | Input tokens | Output tokens | USD est. |",
        "|---|---|---|---|---|",
    ]
    total_input = 0
    total_output = 0
    total_usd = 0.0
    for entry in cost_log:
        in_tok = entry.get("input_tokens", 0)
        out_tok = entry.get("output_tokens", 0)
        usd = entry.get("usd_estimate", 0.0)
        total_input += in_tok
        total_output += out_tok
        total_usd += usd
        rows.append(
            f"| {entry.get('component', '?')} | {entry.get('model', '?')} | "
            f"{in_tok:,} | {out_tok:,} | ${usd:.4f} |"
        )
    rows.append(
        f"| **TOTAL** | — | **{total_input:,}** | **{total_output:,}** | **${total_usd:.4f}** |"
    )
    return rows


def _render_capsule(receipt: dict) -> list[str]:
    capsule_paths = receipt.get("capsule_paths", {})
    forward = capsule_paths.get("forward")
    rows = ["## Capsule", ""]
    if forward:
        rows.append(f"- Forward capsule: `{forward}`")
    else:
        rows.append("- Forward capsule: _not written (capsule_writer stubbed or not invoked)_")
    return rows


def _render_output(receipt: dict) -> list[str]:
    rows = ["## Output Validation", ""]
    rows.append(f"- Output valid: **{'yes' if receipt.get('output_valid', False) else 'no'}**")
    rows.append(f"- Scenes generated: {receipt.get('scenes_generated', 0)}")
    rows.append(f"- Scenes audited: {receipt.get('scenes_audited', 0)}")
    rows.append(f"- Scenes corrected: {receipt.get('scenes_corrected', 0)}")
    rows.append(f"- Correction rate: {receipt.get('correction_rate', 0.0):.2%}")
    return rows
