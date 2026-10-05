# Changelog

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
