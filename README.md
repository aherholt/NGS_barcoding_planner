# NGS barcoding planner

A small web app to **plan and record NGS library barcoding** — from the SharePoint intake list to the
sample sheet for the sequencing provider — with an **audit trail** and **4-eyes sign-off**.

It replaces the Excel workbook (overview sheet + one sheet per run) and adds the Tag&Pool layer
(which well-barcode primer goes onto which lysate, and which lysates are pooled).

```
SharePoint list ──sync──►  Experiment ──► Samples ──► Pools (well barcodes)   ──► STAR robot CSV
 (intake, emails)                                         │
        ▲                                                 ▼
        └──── Status / run ID ◄────  Sequencing run ◄── i7/i5 sample indexes  ──► sample sheet,
                                     (checks, flow cell fit)                       barcode map, JSON
```

| Library type | Barcoding levels | What the app plans |
|---|---|---|
| **Tag&Pool** | well barcode (8 bp, start of R2) + i7/i5 per pool | pools, well barcodes, robot file, indexes, run |
| Bulk RNA-seq, snRNA-seq, CRISPR screens, other | i7/i5 per library | one library per sample, indexes, run |

---

## 1. Concepts in 2 minutes

* **Experiment** – one row of the SharePoint list (read structure, requested reads, people, dates).
* **Sample** – one lysate/sample, uploaded as a CSV/Excel list per experiment.
* **Pool** – Tag&Pool: up to 24 samples (one per well barcode) pooled after barcoded-primer annealing.
  For other library types each sample is its own indexed library ("pool").
* **Sequencing run** – one flow cell at the provider. All libraries on it must share the same
  read structure (R1 / i7 / i5 / R2 cycles).
* **Sign-off** – a recorded decision. Barcode plans and run plans need a **second person** to approve
  them (4-eyes principle). After approval the plan is **locked**; changing it needs "Reopen" with a
  reason, which is stored as a **deviation**.
* **Audit trail** – every version of every experiment, sample, pool and run is stored with user and
  time (`django-simple-history`). See "Audit trail" on each experiment page or "History" in Admin.

## 2. Workflow

| # | Who | Where | What happens | Status (→ SharePoint → email) |
|---|---|---|---|---|
| 1 | Scientist | SharePoint | adds the experiment | Submitted |
| 2 | NGS orga | Experiment page | checks details, **Accept** | Accepted |
| 3 | Technician | Experiment page | uploads sample list, **Plan pools & barcodes**, **Submit** | Barcode plan in review |
| 4 | 2nd person | Experiment page | **Approve** (or reject) | Barcode plan approved |
| 5 | Technician | STAR | downloads robot CSV, runs the protocol, **Record library prep** (+ `pool_manifest.csv` check) | Library prep done |
| 6 | Bioinformatician | Run page | creates run, adds experiments, **Assign indexes**, reviews checks, **Submit** | – |
| 7 | 2nd person | Run page | **Approve** | Run planned |
| 8 | Bioinformatician | Run page | downloads sample sheet, sends libraries, **Mark submitted** / **Mark delivered** | Submitted to provider / Data delivered |

Non-Tag&Pool experiments skip steps 3–4: after acceptance, upload the sample list (each sample becomes
one library) and record library prep.

### Run checks (shown live on the run page)

* identical read structure for all experiments in the run (error)
* every library has an index; no duplicate i7+i5 pair (error)
* i7 **or** i5 differ by ≥ 3 positions between any two libraries (error; setting `PLANNER_MIN_HAMMING`)
* shared i7 or i5 between libraries → **index hopping** risk on NovaSeq X (warning). With 12 i7 × 18 i5
  you can give up to 12 libraries a unique i7 *and* i5; the automatic assignment does this when possible
* 2-channel colour balance per index cycle (G = no signal) (error if a cycle is all-G, warning if weak)
* capacity: requested reads + PhiX (1 %, or 10 % if a low-complexity library is present) + 10 % margin
  must fit the selected flow cell (error); bars show how full each flow cell type would be and roughly
  how many more typical experiments still fit

## 3. Install on a Windows PC for testing (PowerShell)

You need **Python 3.11+** (from python.org, tick "Add python.exe to PATH") and **Git**.

```powershell
# 1. get the code
cd $HOME\Documents
git clone https://github.com/aherholt/NGS_barcoding_planner.git
cd NGS_barcoding_planner

# 2. create an isolated Python environment and install the packages
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements-dev.txt

# 3. create the database (a local file db.sqlite3) and demo data
python manage.py migrate
python manage.py load_demo            # users tech / bioinf / alex, password demo-pass-123

# 4. run the tests, then start the app
pytest
python manage.py runserver
```

Open <http://127.0.0.1:8000>, log in as `tech`, open **TP26-001**, upload
`data\templates\sample_list_template.csv` (or your own list), click **Plan pools & barcodes**, then log
in as `alex` in a private browser window to approve. That is the whole 4-eyes loop.

To start again from scratch: stop the server (Ctrl+C), `Remove-Item db.sqlite3`, repeat step 3.

## 4. Load your real reference data

1. **Well barcodes** – export your Excel sheet with columns `barcode_id, well, sequence`
   (`well` = position in the 96-well barcode stock plate, must match the STAR deck file):
   ```powershell
   python manage.py import_reference well-barcodes "WBC-24 v1" .\my_well_barcodes.xlsx
   ```
2. **Index primers** – columns `kind` (i7/i5), `primer_id`, `sequence`, optional `well`:
   ```powershell
   python manage.py import_reference indexes "Systasy index set v1" .\my_index_primers.xlsx
   ```
3. **Flow cells** – Admin → Flowcell types → add each option your provider sells, with the output
   in **million read pairs that the provider guarantees** (not the Illumina brochure value).
4. **Users** – `python manage.py createsuperuser` for yourself; add the others in Admin → Users
   (or let them sign in with Microsoft, see `docs/SETUP_ENTRA_ID.md`).

Sets are **versioned**: when you extend to 96 barcodes, import a new set ("WBC-96 v1"). Sets that are
in use cannot be edited, so old experiments stay reproducible.

> **i5 orientation:** sequences are stored as in your primer sheet. The run setting
> "i5 reverse complement" flips them in the sample sheet. Ask the provider which orientation they want
> for NovaSeq X, and check the first run's demultiplexing report.

## 5. Files the app produces

| File | For | Content |
|---|---|---|
| STAR robot sample sheet | `tag_and_pool_hamilton_star_sim` (`--sample-sheet`) | sample_id, source_plate, source_well, condition, pool_id, barcode_id |
| Barcode stock plate | STAR simulation `data/barcode_plate.csv` | barcode_id, well, sequence |
| Illumina sample sheet v2 | provider (BCL Convert) | pools as Sample_ID with i7/i5 |
| Library list | provider | pools, indexes, read targets, product length, complexity |
| Barcode map (CSV) | bioinformatics | sample → pool → i7/i5 → well barcode (R2, pos 1, 8 bp) |
| Run JSON | bioinformatics / pipelines | everything about the run incl. check results |

Files from plans that are not yet approved are prefixed `DRAFT_`.

## 6. Going live

* Server deployment with Docker + PostgreSQL: `docs/DEPLOYMENT.md`
* SharePoint sync and email notifications: `docs/SHAREPOINT_SYNC.md`
* Microsoft login: `docs/SETUP_ENTRA_ID.md`
* What still needs a decision: `docs/OPEN_POINTS.md`

## 7. Code map (for you or an AI coding agent)

| File | What it does |
|---|---|
| `planner/models.py` | data model (read this first) |
| `planner/services.py` | all rules: planning, locking, 4-eyes, run assignment, index assignment |
| `planner/checks.py` | run checks, pure Python |
| `planner/exports.py` | all file exports |
| `planner/sharepoint.py` | Microsoft Graph sync |
| `planner/views.py`, `templates/` | web pages |
| `config/settings.py` | settings; lab-specific values in `PLANNER = {...}` |
| `config/sharepoint_fields.json` | SharePoint column mapping |
| `tests/` | `pytest` — includes a test that feeds the exported CSV into the STAR simulation's validator |
