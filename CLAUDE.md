# Instructions for AI coding agents

Project: Django web app that plans Tag&Pool well barcodes, pools, i7/i5 indexes and NGS runs, with
audit trail (django-simple-history) and 4-eyes sign-offs. Users are lab scientists, not software
engineers: explain changes in plain language. The maintainer works in Windows PowerShell — give
PowerShell commands.

## Rules
- All data-changing logic goes through `planner/services.py`. Views and admin must not bypass it.
  Locking (approved plans/runs) and the 4-eyes rule live there — never weaken them.
- Run checks are pure functions in `planner/checks.py`; keep them free of database access.
- Lab parameters (Hamming distance, PhiX, margin, colour map, well-barcode position) live in
  `config/settings.py` → `PLANNER`. Do not hard-code them elsewhere.
- The STAR export columns must stay identical to `tag_and_pool_hamilton_star_sim/src/libprep/samplesheet.py`
  (`REQUIRED_COLUMNS`). `tests/test_workflow.py::test_star_export_passes_robot_validator` checks this
  when the simulation repo is checked out next to this one.
- Reference sets (well barcodes, index primers) are versioned: add a new set, never edit a used one.
- Every model change needs a migration (`python manage.py makemigrations`) — history tables too.
- Keep dependencies pinned in `requirements.txt`; do not upgrade unless asked.
- Add or update a test for every behaviour change. Run `pytest` and report the result.

## Commands (PowerShell)
- Setup: `python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements-dev.txt`
- DB + demo: `python manage.py migrate; python manage.py load_demo`
- Test: `pytest`
- Run: `python manage.py runserver`
