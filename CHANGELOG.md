# Changelog

## 0.2.0 – 2026-10-06
Workflow order changed: everything is planned and approved **before** library prep.
- New order: accept → plan pools/barcodes → add to NGS run → distribute sample indexes →
  final 4-eyes approval per run (barcodes of all experiments + indexes) → library prep → submit → data.
- Removed the separate per-experiment barcode-plan approval; the run's final approval covers it
  (frozen snapshot of run + all experiment plans).
- New experiment status "Assigned to NGS run"; statuses renamed ("Accepted – in planning",
  "Final plan in review", "Plan approved – ready for library prep"); "Run planned" removed.
- Only complete plans can be added to a run; removing an experiment from a run clears its indexes and
  unlocks its plan. Experiments can be added to a run directly from the experiment page.
- Robot file and library-prep recording only after final approval; run can be submitted to the provider
  only when library prep of all its experiments is recorded; final plan can be reopened only before the
  first library prep is recorded.
- Hold/cancel only for experiments not in a run.
- Migration 0002 converts existing records (old pending barcode-plan approvals become "superseded").
- SharePoint: update the Status choice values (docs/SHAREPOINT_SYNC.md).

## 0.1.0 – 2026-10-05
First version.
- Experiments (SharePoint intake fields), sample list upload (CSV/Excel), Tag&Pool barcode planner
  (even or explicit pool sizes, seeded shuffled barcode order), plan validation mirroring the STAR checks.
- Sign-off workflow with 4-eyes principle, plan snapshot hash, locking, reopen with reason, deviations.
- Audit trail via django-simple-history.
- Sequencing runs: add experiments, read-target split, automatic i7/i5 assignment, run checks
  (read structure, duplicates, Hamming distance, index hopping, 2-channel colour balance, capacity),
  flow cell fit bars.
- Exports: STAR sample sheet + barcode plate, BCL Convert v2 sample sheet, provider CSV, barcode map, run JSON.
- STAR `pool_manifest.csv` comparison when recording library prep.
- SharePoint sync via Microsoft Graph (untested against tenant), optional Entra ID login, Docker deployment.
