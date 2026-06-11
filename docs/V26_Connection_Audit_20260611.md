# V26 Connection Audit — 20260611

## Component Connection Matrix

Legend:
- **Manifest**: Registered in pipeline_manifest.json (`pipeline_components` = P, `non_pipeline_components` = N)
- **COMP dict**: In master_controller.py COMPONENTS dict (subprocess-invocable)
- **Runtime import**: Imported by phase_handlers / manuscript_orchestrator at runtime (file:line)
- **Run evidence**: Evidence of invocation in real V25 runs (CSAR airmen b01, black_tide b01)
- **Downstream**: Output consumed downstream (actionable) or report-only
- **Verdict**: WIRED / ORPHANED / STUB / REPORT-ONLY-UNREAD / SUPPORT-LIB

### Control plane

| Component | Manifest | COMP dict | Runtime import | Run evidence | Downstream | Verdict |
|---|---|---|---|---|---|---|
| master_controller | N | n/a (IS the dict) | Entry point — not imported | No direct log (orchestrates phases) | Actionable (drives pipeline) | WIRED |
| phase_handlers | N | no | phase_handlers.py:40 `import master_controller as mc` | Implicit via master_controller | Actionable (phase orchestration) | WIRED |
| manuscript_orchestrator | no | no | Standalone entry point | orchestrator_run_20260530_0133.log (100 scenes generated) | Actionable (scene generation + assembly) | WIRED |
| preflight | P | yes | phase_handlers.py:60 `COMPONENTS["preflight"]` subprocess | csar_run logs: "Step 1: Validating intake..." | Actionable (gates pipeline start) | WIRED |
| runtime_verifier | N | no | Not imported at runtime | No run evidence | Report-only | REPORT-ONLY-UNREAD |

### Synopsis pipeline

| Component | Manifest | COMP dict | Runtime import | Run evidence | Downstream | Verdict |
|---|---|---|---|---|---|---|
| synopsis_generator | P | yes (via COMP) | phase_handlers.py:149 subprocess | csar_synopsis logs: 100 chapters generated, receipt written | Actionable (produces synopsis.md) | WIRED |
| synopsis_auditor | P | yes (via COMP) | phase_handlers.py:173 subprocess | "Step 7: Running synopsis_auditor" in logs; synopsis_audit_report.json exists | Actionable (gates synopsis quality) | WIRED |
| synopsis_summarizer | P | yes (via COMP) | phase_handlers.py:795 subprocess | Mandate_*.docx files in black_tide/b01/out/reviews/ | Actionable (produces .docx summaries) | WIRED |
| outline_comparator | P | yes (via COMP) | phase_handlers.py referenced in synopsis phase | outline_comparison_findings.json consumed by master_controller | Actionable (gates outline drift) | WIRED |

### Character pipeline

| Component | Manifest | COMP dict | Runtime import | Run evidence | Downstream | Verdict |
|---|---|---|---|---|---|---|
| character_generator | P | yes (via COMP) | phase_handlers.py:257 subprocess | character_profiles.json exists in airmen/ | Actionable (produces character profiles) | WIRED |
| character_profile_auditor | P | yes (via COMP) | Imported by character_generator.py:70 | Implicit via character_generator | Actionable (gates profile quality) | WIRED |
| character_profile_merge | N | no | Imported by character_profile_auditor.py:57 | Implicit via character_profile_auditor | Support library | SUPPORT-LIB |

### Scene pipeline

| Component | Manifest | COMP dict | Runtime import | Run evidence | Downstream | Verdict |
|---|---|---|---|---|---|---|
| scene_writer | P | yes (via COMP) | phase_handlers.py:423 subprocess; manuscript_orchestrator.py:30 import | orchestrator log: 100 scenes written | Actionable (produces scene prose) | WIRED |
| scene_auditor | P | yes (via COMP) | manuscript_orchestrator.py:31 import | orchestrator log: per-scene PASS/FAIL gates | Actionable (gates scene quality) | WIRED |
| state_tracker | P | yes (via COMP) | phase_handlers.py:1188 `COMPONENTS["state_tracker"]` subprocess | No direct log evidence in airmen (invoked per-scene by phase 5) | Actionable (produces state snapshots) | WIRED |
| scene_formatter | P | yes (via COMP) | phase_handlers.py:558 subprocess | No direct log evidence (post-assembly formatting) | Actionable (formats scene files) | WIRED |

### Manuscript pipeline

| Component | Manifest | COMP dict | Runtime import | Run evidence | Downstream | Verdict |
|---|---|---|---|---|---|---|
| manuscript_assembler | P | yes (via COMP) | manuscript_orchestrator.py:32 import | manuscript_receipt.json exists | Actionable (assembles chapters) | WIRED |
| manuscript_auditor_v25 | no | no | Imported by fixer_runner.py:20 | audit_20260530 logs: 16 MA checks run | Actionable (audits manuscript) | WIRED |
| manuscript_summarizer | P | yes (via COMP) | Not directly imported (subprocess via phase_handlers) | No direct evidence in airmen (invoked post-assembly) | Actionable (produces summary) | WIRED |
| formatter | N (unlisted) | no | phase_handlers.py:741 subprocess | .docx files in black_tide/b01/out/ | Actionable (produces .docx) | WIRED |

### Audit checks + fixer stack

| Component | Manifest | COMP dict | Runtime import | Run evidence | Downstream | Verdict |
|---|---|---|---|---|---|---|
| audit_checks/ (package) | no | no | Imported by manuscript_auditor_v25, fixer_preflight, fixer_runner, manuscript_fixer | audit logs: MA-001 through MA-047 checks run | Actionable (check modules) | SUPPORT-LIB |
| manifest_auditor | P | yes (via COMP) | phase_handlers referenced | manifest_audit_20260514_*.json in out/ | Report-only (manifest consistency) | WIRED |
| fixer_preflight | N | no | Imported by manuscript_fixer.py:21 | No direct run evidence | Actionable (gates fixer) | SUPPORT-LIB |
| fixer_runner | N | no | Standalone entry point | No direct run evidence in airmen | Actionable (drives fix loop) | WIRED |
| manuscript_fixer | N | no | Imported by fixer_runner.py:21 | No direct run evidence | Actionable (applies fixes) | SUPPORT-LIB |
| audit_existing_manuscript | N | no | Standalone entry point | audit_20260530 logs (3 audit runs) | Actionable (standalone auditor) | WIRED |

### Support libraries

| Component | Manifest | COMP dict | Runtime import | Run evidence | Downstream | Verdict |
|---|---|---|---|---|---|---|
| llm_client | N | no | Imported by synopsis_summarizer, manuscript_summarizer | Implicit via LLM-calling components | Support library | SUPPORT-LIB |
| findings | N | no | Imported by synopsis_auditor, character_generator, character_profile_auditor | Implicit via auditor components | Support library | SUPPORT-LIB |
| config_resolver | N | no | Imported by master_controller, synopsis_auditor, character_generator, character_profile_auditor | Implicit via consumers | Support library | SUPPORT-LIB |
| intake_validator | N | no | Imported by synopsis_generator.py:36 | Implicit via synopsis_generator | Support library | SUPPORT-LIB |
| outline_parser | N | no | Imported by synopsis_generator.py:37, outline_comparator | Implicit via consumers | Support library | SUPPORT-LIB |
| synopsis_parser | N | no | Imported by manuscript_orchestrator, scene_writer tests, audit_existing_manuscript | Implicit via consumers | Support library | SUPPORT-LIB |
| principles_loader | N | no | Imported by synopsis_generator.py:38, audit_existing_manuscript | Implicit via consumers | Support library | SUPPORT-LIB |
| entity_ledger_builder | N | no | Standalone entry point | entity_ledger.json + entity_ledger_arm001_*.json exist | Actionable (produces entity ledger) | WIRED |
| publish_gate | N | no | Imported by anpd_export.py:37 | No direct run evidence | Support library | SUPPORT-LIB |
| pipeline_receipt_writer | N | no | Not imported by any transferred component | No run evidence | Unused | ORPHANED |
| book_archive | N (unlisted) | no | Not imported by any transferred component | _book_archive_btd001/ exists in black_tide | Standalone operator tool | WIRED |
| anpd_export | N (unlisted) | no | Standalone operator tool | No direct evidence | Standalone operator tool | WIRED |

### Orphaned / disconnected

| Component | Manifest | COMP dict | Runtime import | Run evidence | Downstream | Verdict |
|---|---|---|---|---|---|---|
| library_loader | N | no | Not imported by any component | No run evidence; default path `/anpd/v25/libraries` does not exist | None | **ORPHANED** |

### Reclassified (corrected 20260611)

| Component | Previous | Corrected | Evidence |
|---|---|---|---|
| pipeline_receipt_writer | ORPHANED | **WIRED** | Lazy inline import in `master_controller._finalize_receipt()`: `from pipeline_receipt_writer import write_receipt`. Called at every pipeline exit point (normal completion, hard stop, phase failure). Missed by initial audit which only checked top-level imports. **Note for future audits:** grep function-level / inline imports, not just module-level. |

### Dead references (not components — runtime data)

| Reference | Location | Status |
|---|---|---|
| `/anpd/v25/shared/banned_ai_phrases.json` | phase_handlers.py:1135 | Restored to /anpd/v26/shared/ from v24 source (Dispatch 2, Part 3) |
| `/anpd/v25/libraries` | library_loader.py:125 (default) | Directory does not exist on disk (component ORPHANED) |

---

## Ranked Disconnected Quality Components

Priority order for wiring review (highest value / most disconnected first):

1. ~~**pipeline_receipt_writer**~~ — **RECLASSIFIED as WIRED** (Dispatch 2). Lazy inline import in `master_controller._finalize_receipt()`.

2. **library_loader** — ORPHANED. Written, manifested as non-pipeline, but never imported. Default path points to non-existent `/anpd/v25/libraries/`. Real library assets exist at `/anpd/v24/libraries/` (twist, action_scene, voice). Integration into synopsis_generator or scene_writer was planned but never completed.

3. ~~**runtime_verifier**~~ — **RESOLVED** (Dispatch 2). Wired as startup gate in master_controller (S001 importability check). R-rules active during run. C-rules at completion.

4. ~~**manuscript_auditor (V24)**~~ — **RESOLVED** (Dispatch 1). COMPONENTS dict key "manuscript_auditor" repointed to manuscript_auditor_v25. Gate 3 stub deleted and replaced with real invocation.

5. ~~**formatter**~~ — **RESOLVED** (Dispatch 1). Added to COMPONENTS dict and manifest.

6. **book_archive** — WIRED but unlisted. Not in manifest. Standalone operator tool with evidence of use (_book_archive_btd001/ in black_tide). Should be added to manifest.

7. **anpd_export** — WIRED but unlisted. Not in manifest. Standalone operator tool. Should be added to manifest.
