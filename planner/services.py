"""Business logic: everything that changes data goes through these functions.

Views, admin actions and management commands call these functions instead of
editing models directly, so the rules (locking, 4-eyes, validation) are
enforced in ONE place. Each function raises `PlannerError` with a list of
human-readable problems if something is not allowed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import random
from itertools import product

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import checks, library, plates
from .models import (
    Deviation, Experiment, FlowcellType, IndexPrimer, IndexSet, Pool, RunPool, Sample,
    SequencingRun, SignOff, WellBarcode,
)

log = logging.getLogger(__name__)

MAX_POOLS_PER_STAR_RUN = 24  # one 24 deep-well pool plate on the deck
MAX_SOURCE_PLATES = 10


class PlannerError(ValueError):
    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


def _cfg():
    return settings.PLANNER


def set_status(experiment: Experiment, status: str):
    """Change status and mark it for SharePoint write-back (which triggers the emails)."""
    if experiment.status != status:
        experiment.status = status
        experiment.sharepoint_push_pending = True
        experiment.save()
        from .sharepoint import push_experiment_safely
        push_experiment_safely(experiment)


# --------------------------------------------------------------------------- #
# Hand-over: who does the next step?
#
# Every workflow step accepts `assignee`: a user, None (nobody in particular) or AUTO (sensible
# default, e.g. the experiment's "Responsible person library prep" after acceptance).
# Steps on the run (indexes, final approval, submission, data delivery) are owned by the run's
# assignee; steps on the experiment (accept, planning, library prep, amendment) by the experiment's.
# --------------------------------------------------------------------------- #
AUTO = object()

EXPERIMENT_LEVEL = {"submitted", "accepted", "plan_approved", "amending", "on_hold"}


def _run_of(experiment: Experiment):
    return experiment.amendment_run or SequencingRun.objects.filter(run_pools__pool__experiment=experiment).first()


def _has_pending_amendment(experiment: Experiment) -> bool:
    return experiment.signoffs.filter(step=SignOff.Step.AMENDMENT, state=SignOff.State.PENDING).exists()


def next_step(experiment: Experiment) -> tuple[str, object]:
    """(what has to happen next, who is responsible) for an experiment — shown in the app and sent to SharePoint."""
    st = experiment.status
    run = _run_of(experiment)
    rid = f" (run {run.run_id})" if run else ""
    if st == "plan_in_review" and _has_pending_amendment(experiment):
        return "Approve amendment", experiment.assignee
    labels = {
        "submitted": "Accept experiment",
        "accepted": "Plan samples/pools and add to an NGS run",
        "in_run": f"Distribute sample indexes and submit final plan{rid}",
        "plan_in_review": f"Approve final plan{rid}",
        "plan_approved": "Library prep",
        "amending": "Amend plan and submit amendment",
        "libprep_done": f"Submit run to provider{rid}",
        "submitted_to_provider": f"Record data delivery{rid}",
        "on_hold": "Resume or cancel",
    }
    label = labels.get(st, "")
    if not label:
        return "", None
    owner = experiment.assignee if (st in EXPERIMENT_LEVEL or run is None) else run.assignee
    if st == "submitted" and owner is None:
        owner = experiment.responsible_ngs  # new requests: the NGS organiser accepts them
    return label, owner


def next_step_on_run(experiment: Experiment) -> bool:
    """True if the experiment's next step is done on its run page (owned by the run's assignee)."""
    return (experiment.status not in EXPERIMENT_LEVEL and _run_of(experiment) is not None
            and not (experiment.status == "plan_in_review" and _has_pending_amendment(experiment)))


def run_next_step(run: SequencingRun) -> str:
    return {"planning": "Distribute sample indexes and submit final plan", "in_review": "Approve final plan",
            "approved": "Library prep of all experiments, then submit to provider",
            "submitted": "Record data delivery"}.get(run.status, "")


def assign(target, user, by=None):
    """Set who does the next step on an experiment or run (and tell SharePoint/Power Automate)."""
    if getattr(target, "assignee_id", None) == (user.pk if user else None):
        return
    target.assignee = user
    target.save()
    from .sharepoint import push_experiment_safely
    exps = [target] if isinstance(target, Experiment) else list(set(target.experiments()) | set(target.amendments.all()))
    for e in exps:
        type(e).objects.filter(pk=e.pk).update(sharepoint_push_pending=True)
        e.refresh_from_db()
        push_experiment_safely(e)


# --------------------------------------------------------------------------- #
# Sample list upload
# --------------------------------------------------------------------------- #
SAMPLE_COLUMNS_REQUIRED = ["sample_id"]
SAMPLE_COLUMNS_OPTIONAL = ["source_plate", "source_well", "plate_format", "condition", "pool_id", "barcode_id"]
PLATE_FORMAT_ALIASES = ("plate_format", "plate format", "format", "well_format", "plate_type")


def read_csv_text(text: str) -> list[dict]:
    """Read CSV exported by Excel: handles BOM, ';' or ',' as separator."""
    text = text.lstrip("﻿")
    first = text.splitlines()[0] if text else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    rows = []
    for row in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in row.items()})
    return rows


@transaction.atomic
def upload_samples(experiment: Experiment, rows: list[dict], user) -> int:
    """Replace the sample list of an experiment.

    Optional columns pool_id + barcode_id import a hand-made plan; otherwise use
    `plan_barcodes` afterwards. For non-Tag&Pool experiments every sample becomes
    its own indexed library ("pool").
    """
    if experiment.plan_locked:
        raise PlannerError("The plan is locked (in review or approved). Reopen it first.")
    if any(rp for p in experiment.pools.all() for rp in p.run_links.all()):
        raise PlannerError("Pools of this experiment are already in a sequencing run.")
    problems = []
    if not rows:
        raise PlannerError("The file contains no rows.")
    missing = [c for c in SAMPLE_COLUMNS_REQUIRED if c not in rows[0]]
    if missing:
        raise PlannerError(f"Missing column(s): {missing}. Required: sample_id; optional: {SAMPLE_COLUMNS_OPTIONAL}")
    ids = [r["sample_id"] for r in rows]
    if any(not i for i in ids):
        problems.append("Empty sample_id in at least one row.")
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        problems.append(f"sample_id used more than once: {dup}")
    formats = []
    for i, r in enumerate(rows, start=2):
        raw = next((r[k] for k in PLATE_FORMAT_ALIASES if r.get(k)), "")
        try:
            formats.append(plates.parse_format(raw) or plates.DEFAULT_FORMAT)
        except ValueError as e:
            problems.append(f"row {i}: {e}")
    if problems:
        raise PlannerError(problems)

    experiment.samples.all().delete()
    experiment.pools.all().delete()
    has_plan = all(r.get("pool_id") and r.get("barcode_id") for r in rows) and experiment.is_tag_and_pool
    barcodes = {}
    if has_plan:
        if not experiment.well_barcode_set:
            raise PlannerError("Select a well-barcode set before importing a plan with barcode_id.")
        barcodes = {b.barcode_id: b for b in experiment.well_barcode_set.barcodes.all()}
        unknown = sorted({r["barcode_id"] for r in rows} - set(barcodes))
        if unknown:
            raise PlannerError(f"barcode_id not in set {experiment.well_barcode_set}: {unknown}")

    pools: dict[str, Pool] = {}
    for i, r in enumerate(rows):
        s = Sample(experiment=experiment, sample_id=r["sample_id"], order=i,
                   source_plate=r.get("source_plate", ""), source_well=r.get("source_well", "").upper(),
                   plate_format=formats[i], condition=r.get("condition", ""))
        if has_plan:
            pid = r["pool_id"]
            if pid not in pools:
                pools[pid] = Pool.objects.create(experiment=experiment, pool_id=pid)
            s.pool = pools[pid]
            s.well_barcode = barcodes[r["barcode_id"]]
        elif not experiment.is_tag_and_pool:
            pid = f"{experiment.code}_{r['sample_id']}"
            s.pool = Pool.objects.create(experiment=experiment, pool_id=pid)
        s.save()
    return len(rows)


# --------------------------------------------------------------------------- #
# Barcode plan (Tag&Pool)
# --------------------------------------------------------------------------- #
def even_pool_sizes(n_samples: int, max_pool_size: int) -> list[int]:
    """Split n samples into the fewest pools of at most max_pool_size, sizes differing by ≤ 1."""
    if n_samples <= 0:
        return []
    n_pools = -(-n_samples // max_pool_size)
    base, extra = divmod(n_samples, n_pools)
    return [base + 1 if i < extra else base for i in range(n_pools)]


def _sample_order_key(plate_order: dict):
    def key(s: Sample):
        return plate_order.get(s.source_plate, 0), plates.well_index(s.plate_format, s.source_well), s.order
    return key


@transaction.atomic
def plan_barcodes(experiment: Experiment, pool_sizes: list[int] | None = None, max_pool_size: int | None = None,
                  shuffle: bool = True, seed: int | None = None) -> list[Pool]:
    """Group samples into pools and give each sample a well barcode.

    * Samples are taken in plate order (culture plate, then column-wise A1,B1,C1,D1,A2…),
      exactly like the STAR protocol walks the plates.
    * pool_sizes: explicit sizes, e.g. [24, 24, 12]; or max_pool_size for an even split.
    * shuffle: each pool gets a different random barcode order (seeded and stored so the
      layout can be reproduced), so a barcode is not always tied to the same plate position.
    """
    if not experiment.is_tag_and_pool:
        raise PlannerError("Barcode planning is only for Tag&Pool experiments.")
    if experiment.plan_locked:
        raise PlannerError("The plan is locked (in review or approved). Reopen it first.")
    if not experiment.well_barcode_set:
        raise PlannerError("Select a well-barcode set first.")
    if any(rp for p in experiment.pools.all() for rp in p.run_links.all()):
        raise PlannerError("Pools of this experiment are already in a sequencing run.")
    samples = list(experiment.samples.all())
    if not samples:
        raise PlannerError("Upload a sample list first.")
    barcodes = list(experiment.well_barcode_set.barcodes.order_by("barcode_id"))
    set_size = len(barcodes)
    if pool_sizes is None:
        pool_sizes = even_pool_sizes(len(samples), min(max_pool_size or set_size, set_size))
    problems = []
    if sum(pool_sizes) != len(samples):
        problems.append(f"Pool sizes add up to {sum(pool_sizes)}, but there are {len(samples)} samples.")
    if any(s > set_size for s in pool_sizes):
        problems.append(f"A pool can hold at most {set_size} samples (one per well barcode in {experiment.well_barcode_set}).")
    if any(s <= 0 for s in pool_sizes):
        problems.append("Pool sizes must be positive.")
    if problems:
        raise PlannerError(problems)

    if seed is None:
        seed = random.SystemRandom().randint(1, 10**6)
    plate_order = {p: i for i, p in enumerate(dict.fromkeys(s.source_plate for s in sorted(samples, key=lambda s: s.order)))}
    samples.sort(key=_sample_order_key(plate_order))

    experiment.pools.all().delete()  # samples keep existing; their pool FK is set to NULL
    pools, pos = [], 0
    for p_idx, size in enumerate(pool_sizes, start=1):
        pool = Pool.objects.create(experiment=experiment, pool_id=f"{experiment.code}_P{p_idx:02d}")
        order = barcodes[:]
        if shuffle:
            random.Random(seed * 1000 + p_idx).shuffle(order)
        for s, bc in zip(samples[pos:pos + size], order):
            s.pool, s.well_barcode = pool, bc
            s.save()
        pos += size
        pools.append(pool)
    experiment.planning_seed = seed if shuffle else None
    experiment.save()
    return pools


def validate_plan(experiment: Experiment) -> list[str]:
    """All rules the plan must satisfy before it can be submitted (mirrors the STAR sample-sheet checks)."""
    problems = []
    samples = list(experiment.samples.select_related("pool", "well_barcode"))
    if not samples:
        return ["No samples."]
    if experiment.is_tag_and_pool:
        if not experiment.well_barcode_set:
            problems.append("No well-barcode set selected.")
        unassigned = [s.sample_id for s in samples if not s.pool or not s.well_barcode]
        if unassigned:
            problems.append(f"{len(unassigned)} samples without pool/barcode, e.g. {unassigned[:5]}.")
        seen = {}
        for s in samples:
            if s.pool and s.well_barcode:
                k = (s.pool_id, s.well_barcode_id)
                if k in seen:
                    problems.append(f"Barcode {s.well_barcode.barcode_id} used twice in {s.pool.pool_id} ({seen[k]}, {s.sample_id}).")
                seen[k] = s.sample_id
                if experiment.well_barcode_set and s.well_barcode.barcode_set_id != experiment.well_barcode_set_id:
                    problems.append(f"{s.sample_id}: barcode from another set.")
        n_pools = experiment.pools.count()
        if n_pools > MAX_POOLS_PER_STAR_RUN:
            problems.append(f"{n_pools} pools, the STAR pool plate has {MAX_POOLS_PER_STAR_RUN} wells (split into several robot runs).")
        bad_wells = sorted({f"{s.source_well} ({s.plate_format}-well)" for s in samples
                            if s.source_well and s.source_well not in plates.wells(s.plate_format)})
        if bad_wells:
            problems.append(f"source_well does not exist in the plate format: {bad_wells}")
        fmt_of: dict[str, set] = {}
        for s in samples:
            fmt_of.setdefault(s.source_plate, set()).add(s.plate_format)
        mixed = sorted(p for p, f in fmt_of.items() if len(f) > 1)
        if mixed:
            problems.append(f"Culture plate(s) with more than one plate format in the sample list: {mixed}")
        missing_pos = [s.sample_id for s in samples if not s.source_plate or not s.source_well]
        if missing_pos:
            problems.append(f"{len(missing_pos)} samples without source_plate/source_well (needed by the robot).")
        pos = [(s.source_plate, s.source_well) for s in samples if s.source_plate and s.source_well]
        dups = sorted({f"{p}:{w}" for p, w in pos if pos.count((p, w)) > 1})
        if dups:
            problems.append(f"Same source well used twice: {dups}")
        n_plates = len({s.source_plate for s in samples})
        if n_plates > MAX_SOURCE_PLATES:
            problems.append(f"{n_plates} culture plates, the deck holds {MAX_SOURCE_PLATES}.")
    for field in ("r1_length", "r2_length", "i7_length", "i5_length"):
        if getattr(experiment, field) is None:
            problems.append(f"{field} not set.")
    if experiment.requested_m_read_pairs is None:
        problems.append("Requested data output not set.")
    return problems


def plan_warnings(experiment: Experiment) -> list[str]:
    """Things worth knowing that do not block the plan."""
    warn = []
    if experiment.is_tag_and_pool:
        fmts = sorted(set(experiment.samples.values_list("plate_format", flat=True)) - plates.STAR_SUPPORTED_FORMATS)
        if fmts:
            warn.append(f"The STAR protocol currently loads 24-well culture plates only; this plan uses "
                        f"{', '.join(f'{f}-well' for f in fmts)} plates (robot file must be adapted or prepared by hand).")
    if experiment.library_layout:
        an = library_analysis(experiment)
        warn += [f"Library structure: {t}" for lvl, t in an.messages if lvl in ("error", "warning")]
    return warn


def library_analysis(experiment: Experiment, layout: list[dict] | None = None):
    cfg = _cfg()
    return library.analyse(
        layout if layout is not None else experiment.library_layout,
        experiment.r1_length, experiment.r2_length, experiment.i7_length, experiment.i5_length,
        experiment.avg_insert_length,
        (cfg["WELL_BARCODE_READ"], cfg["WELL_BARCODE_START"], cfg["WELL_BARCODE_LENGTH"]) if experiment.is_tag_and_pool else None)


def plan_snapshot(experiment: Experiment) -> dict:
    snap = {
        "experiment": experiment.code,
        "barcode_set": str(experiment.well_barcode_set) if experiment.well_barcode_set else None,
        "seed": experiment.planning_seed,
        "samples": [
            {"sample_id": s.sample_id, "source_plate": s.source_plate, "source_well": s.source_well,
             "pool_id": s.pool.pool_id if s.pool else None,
             "barcode_id": s.well_barcode.barcode_id if s.well_barcode else None,
             "barcode_seq": s.well_barcode.sequence if s.well_barcode else None}
            for s in experiment.samples.select_related("pool", "well_barcode")
        ],
    }
    # added in v0.4 — only included when used, so older approvals keep their fingerprint
    formats = dict(experiment.samples.values_list("sample_id", "plate_format"))
    if any(f != plates.DEFAULT_FORMAT for f in formats.values()):
        snap["plate_formats"] = formats
    if experiment.library_layout:
        snap["library_layout"] = experiment.library_layout
    return snap


def run_snapshot(run: SequencingRun) -> dict:
    return {
        "run": run.run_id,
        "flowcell": str(run.flowcell_type) if run.flowcell_type else None,
        "read_structure": run.read_structure,
        "i5_reverse_complement": run.i5_reverse_complement,
        "libraries": [
            {"pool_id": rp.pool.pool_id, "experiment": rp.pool.experiment.code, "target_m": rp.target_m_read_pairs,
             "i7": rp.pool.i7.primer_id if rp.pool.i7 else None, "i7_seq": rp.pool.i7.sequence if rp.pool.i7 else None,
             "i5": rp.pool.i5.primer_id if rp.pool.i5 else None, "i5_seq": rp.pool.i5.sequence if rp.pool.i5 else None}
            for rp in run.run_pools.select_related("pool__experiment", "pool__i7", "pool__i5")
        ],
    }


def _hash(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Experiment workflow
#
#   Submitted → Accepted (in planning) → Assigned to NGS run → Final plan in review
#   → Plan approved (ready for library prep) → Library prep done → Submitted → Data delivered
#
# Planning (samples, pools, well barcodes) happens while "Accepted". The experiment is then
# added to a run; sample indexes are distributed on the run, and ONE final 4-eyes sign-off on
# the run approves well barcodes + indexes of all its experiments. Only then can lab work start.
# --------------------------------------------------------------------------- #
@transaction.atomic
def accept_experiment(experiment: Experiment, user, comment: str = "", assignee=AUTO):
    if experiment.status != Experiment.Status.SUBMITTED:
        raise PlannerError("Only submitted experiments can be accepted.")
    SignOff.objects.create(experiment=experiment, step=SignOff.Step.ACCEPT, state=SignOff.State.APPROVED,
                           submitted_by=user, decided_by=user, decided_at=timezone.now(), comment=comment)
    set_status(experiment, Experiment.Status.ACCEPTED)
    assign(experiment, experiment.responsible_libprep if assignee is AUTO else assignee)


def _decide(signoff: SignOff, user, approve: bool, comment: str, current_snapshot: dict):
    if signoff.state != SignOff.State.PENDING:
        raise PlannerError("This sign-off is not waiting for a decision.")
    if signoff.submitted_by_id == user.pk:
        raise PlannerError("4-eyes principle: the person who submitted cannot approve or reject.")
    if approve and _hash(current_snapshot) != signoff.snapshot_hash:
        raise PlannerError("The plan changed after it was submitted. Reject it and submit again.")
    signoff.state = SignOff.State.APPROVED if approve else SignOff.State.REJECTED
    signoff.decided_by, signoff.decided_at = user, timezone.now()
    if comment:
        signoff.comment = (signoff.comment + "\n" if signoff.comment else "") + f"[{user}] {comment}"
    signoff.save()


def compare_manifest(experiment: Experiment, rows: list[dict]) -> list[str]:
    """Compare the STAR run's pool_manifest.csv with the approved plan. Returns differences."""
    plan = {s.sample_id: (s.pool.pool_id if s.pool else None, s.well_barcode.barcode_id if s.well_barcode else None)
            for s in experiment.samples.select_related("pool", "well_barcode")}
    done = {r.get("sample_id"): (r.get("pool_id"), r.get("barcode_id")) for r in rows}
    diffs = []
    for sid, (pool, bc) in plan.items():
        if sid not in done:
            diffs.append(f"{sid}: missing in robot manifest")
        elif done[sid] != (pool, bc):
            diffs.append(f"{sid}: planned {pool}/{bc}, robot {done[sid][0]}/{done[sid][1]}")
    for sid in sorted(set(done) - set(plan)):
        diffs.append(f"{sid}: in robot manifest but not in plan")
    return diffs


@transaction.atomic
def record_libprep_done(experiment: Experiment, user, comment: str = "", manifest_rows: list[dict] | None = None,
                        assignee=AUTO) -> list[str]:
    """Library prep (well barcodes AND sample-index PCR) finished for this experiment."""
    if experiment.status != Experiment.Status.PLAN_APPROVED:
        raise PlannerError("Library prep can only be recorded after the final plan (barcodes + indexes) "
                           "of its sequencing run has been approved.")
    diffs = compare_manifest(experiment, manifest_rows) if manifest_rows is not None else []
    if diffs:
        Deviation.objects.create(experiment=experiment, created_by=user,
                                 description="Robot manifest differs from plan:\n" + "\n".join(diffs))
    SignOff.objects.create(experiment=experiment, step=SignOff.Step.LIBPREP, state=SignOff.State.APPROVED,
                           submitted_by=user, decided_by=user, decided_at=timezone.now(), comment=comment)
    set_status(experiment, Experiment.Status.LIBPREP_DONE)
    assign(experiment, None)
    run = current_run(experiment)
    if run is not None and assignee is not AUTO:
        assign(run, assignee)  # who submits the run to the provider
    return diffs


def add_deviation(user, description: str, experiment=None, run=None) -> Deviation:
    if not description.strip():
        raise PlannerError("Describe the deviation.")
    return Deviation.objects.create(experiment=experiment, run=run, created_by=user, description=description)


@transaction.atomic
def change_hold_status(experiment: Experiment, user, target: str, reason: str):
    """Put on hold / cancel / resume. Not possible while the experiment is in a sequencing run."""
    if not reason.strip():
        raise PlannerError("A reason is required.")
    if experiment.pools.filter(run_links__isnull=False).exists() or experiment.amendment_run_id:
        raise PlannerError("The experiment is in a sequencing run. Remove it from the run first "
                           "(possible while the run is in planning).")
    if target == Experiment.Status.SUBMITTED and experiment.status not in (Experiment.Status.ON_HOLD, Experiment.Status.CANCELLED):
        raise PlannerError("Only experiments on hold or cancelled can be resumed.")
    add_deviation(user, f"Status → {Experiment.Status(target).label}: {reason}", experiment=experiment)
    if target == Experiment.Status.SUBMITTED and experiment.signoffs.filter(step=SignOff.Step.ACCEPT).exists():
        target = Experiment.Status.ACCEPTED  # was accepted before → back to planning
    set_status(experiment, target)


# --------------------------------------------------------------------------- #
# Sequencing runs
# --------------------------------------------------------------------------- #
def _check_run_unlocked(run: SequencingRun):
    if run.locked:
        raise PlannerError("The run plan is locked (in review or approved). Reopen it first.")


def ready_for_run(experiment: Experiment) -> list[str]:
    """Problems that prevent adding the experiment to a run (empty list = ready)."""
    if experiment.status != Experiment.Status.ACCEPTED:
        return [f"Status is '{experiment.get_status_display()}' — only accepted experiments in planning can be added."]
    problems = validate_plan(experiment)
    if not experiment.pools.exists():
        problems.append("No pools/libraries yet" + (" — create the barcode plan." if experiment.is_tag_and_pool else
                                                     " — upload the sample list."))
    return problems


@transaction.atomic
def add_experiment_to_run(run: SequencingRun, experiment: Experiment, assignee=AUTO):
    """Add all pools of a fully planned experiment; the requested reads are split by samples per pool."""
    _check_run_unlocked(run)
    if run.status != SequencingRun.Status.PLANNING:
        raise PlannerError("Experiments can only be added to runs in planning.")
    problems = ready_for_run(experiment)
    if problems:
        raise PlannerError([f"{experiment.code} is not ready for a run:"] + problems)
    pools = list(experiment.pools.all())
    if run.r1_length is None and not run.run_pools.exists():
        run.r1_length, run.r2_length = experiment.r1_length, experiment.r2_length
        run.i7_length, run.i5_length = experiment.i7_length, experiment.i5_length
        run.save()
    total_samples = sum(max(p.samples.count(), 1) for p in pools)
    for p in pools:
        share = max(p.samples.count(), 1) / total_samples
        RunPool.objects.update_or_create(run=run, pool=p, defaults={
            "target_m_read_pairs": round((experiment.requested_m_read_pairs or 0) * share, 1)})
    set_status(experiment, Experiment.Status.IN_RUN)
    if assignee is not AUTO:
        assign(run, assignee)
    elif run.assignee_id is None:
        assign(run, experiment.responsible_ngs)


@transaction.atomic
def remove_experiment_from_run(run: SequencingRun, experiment: Experiment):
    """Back to planning: indexes are cleared, the barcode plan can be edited again."""
    _check_run_unlocked(run)
    links = RunPool.objects.filter(run=run, pool__experiment=experiment)
    if not links.exists():
        raise PlannerError(f"{experiment.code} is not in this run.")
    for rp in links.select_related("pool"):
        rp.pool.i7 = rp.pool.i5 = None
        rp.pool.save()
    links.delete()
    set_status(experiment, Experiment.Status.ACCEPTED)


def run_libraries(run: SequencingRun) -> list[checks.Library]:
    libs = []
    for rp in run.run_pools.select_related("pool__experiment", "pool__i7", "pool__i5"):
        p, e = rp.pool, rp.pool.experiment
        libs.append(checks.Library(
            name=p.pool_id, experiment=e.code, target_m=rp.target_m_read_pairs,
            i7_id=p.i7.primer_id if p.i7 else "", i7_seq=p.i7.sequence if p.i7 else "",
            i5_id=p.i5.primer_id if p.i5 else "", i5_seq=p.i5.sequence if p.i5 else "",
            r1=e.r1_length, r2=e.r2_length, i7_len=e.i7_length, i5_len=e.i5_length,
            low_complexity=e.library_complexity == Experiment.Complexity.LOW))
    return libs


def run_spec(run: SequencingRun) -> checks.RunSpec:
    return checks.RunSpec(
        r1=run.r1_length, r2=run.r2_length, i7_len=run.i7_length, i5_len=run.i5_length,
        flowcell_output_m=run.flowcell_type.output_m_read_pairs if run.flowcell_type else None,
        phix_percent=run.phix_percent,
        safety_margin_percent=run.safety_margin_percent if run.safety_margin_percent is not None else _cfg()["SAFETY_MARGIN_PERCENT"])


def check_run(run: SequencingRun) -> list[checks.Issue]:
    return checks.check_run(run_libraries(run), run_spec(run), _cfg())


def flowcell_options(run: SequencingRun) -> list[dict]:
    libs = run_libraries(run)
    spec = run_spec(run)
    phix = checks.effective_phix(libs, spec, _cfg()["DEFAULT_PHIX_PERCENT"], _cfg()["LOW_COMPLEXITY_PHIX_PERCENT"])
    recent = sorted(e.requested_m_read_pairs for e in Experiment.objects.exclude(requested_m_read_pairs=None).order_by("-created")[:20])
    typical = recent[len(recent) // 2] if recent else None
    rows = checks.flowcell_fit(sum(l.target_m for l in libs), phix, spec.safety_margin_percent,
                               [(f.name, f.output_m_read_pairs) for f in FlowcellType.objects.filter(active=True)], typical)
    for r in rows:
        r["selected"] = bool(run.flowcell_type and run.flowcell_type.name == r["name"])
        r["phix"] = phix
        r["typical_m"] = typical
        r["fill_pct"] = min(r["fill"] * 100, 100)
    return rows


@transaction.atomic
def assign_indexes(run: SequencingRun, index_set: IndexSet, overwrite: bool = False) -> int:
    """Give every library without index an i7/i5 pair from `index_set`.

    Greedy choice, best first: (1) pair not used yet in this run, (2) i7 and i5 not
    shared with other libraries (less index-hopping risk), (3) no index cycle where all
    libraries read G (no signal on 2-channel instruments), (4) largest minimum
    Hamming distance to the libraries already placed. Always review the run checks afterwards.
    """
    _check_run_unlocked(run)
    return _assign_missing_indexes(run, index_set, overwrite)


def _assign_missing_indexes(run: SequencingRun, index_set: IndexSet, overwrite: bool = False) -> int:
    n7, n5 = run.i7_length or 0, run.i5_length or 0
    i7s = [p for p in index_set.primers.filter(kind="i7") if len(p.sequence) >= n7]
    i5s = [p for p in index_set.primers.filter(kind="i5") if len(p.sequence) >= n5] if n5 else [None]
    if not i7s or not i5s:
        raise PlannerError(f"Index set {index_set} has no primers long enough for {n7}/{n5} index cycles.")
    rps = list(run.run_pools.select_related("pool__i7", "pool__i5").order_by("pool__experiment__code", "pool__pool_id"))
    if overwrite:
        for rp in rps:
            if rp.pool.run_links.exclude(run=run).filter(run__status__in=SequencingRun.LOCKED_STATUSES).exists():
                continue  # already indexed and sequenced/approved elsewhere — keep
            rp.pool.i7 = rp.pool.i5 = None
            rp.pool.save()
    placed = [(rp.pool.i7.sequence[:n7], rp.pool.i5.sequence[:n5] if (n5 and rp.pool.i5) else "")
              for rp in rps if rp.pool.i7]
    used_pairs = set(placed)
    count = 0
    for rp in rps:
        if rp.pool.i7:
            continue
        best, best_score = None, None
        for a, b in product(i7s, i5s):
            k = (a.sequence[:n7], b.sequence[:n5] if b else "")
            if k in used_pairs:
                continue
            shared = sum(k[0] == x[0] for x in placed) + (sum(k[1] == x[1] for x in placed) if n5 else 0)
            dist = min((max(checks.hamming(k[0], x[0]), checks.hamming(k[1], x[1]) if n5 else 0) for x in placed), default=99)
            # index cycles in which every library would read a dark base (G) — no signal at all
            dark = sum(all(seq[c] == "G" for seq in [k[0]] + [x[0] for x in placed]) for c in range(n7)) + \
                sum(all(seq[c] == "G" for seq in [k[1]] + [x[1] for x in placed]) for c in range(n5))
            score = (shared, dark, -dist, a.primer_id, b.primer_id if b else "")
            if best_score is None or score < best_score:
                best, best_score = (a, b), score
        if best is None:
            raise PlannerError(f"Not enough unused index pairs in {index_set} for all libraries.")
        rp.pool.i7, rp.pool.i5 = best
        rp.pool.save()
        k = (best[0].sequence[:n7], best[1].sequence[:n5] if best[1] else "")
        placed.append(k)
        used_pairs.add(k)
        count += 1
    return count


def _set_run_experiments_status(run: SequencingRun, status: str):
    for e in run.experiments():
        set_status(e, status)


def final_plan_snapshot(run: SequencingRun) -> dict:
    """What the final sign-off approves: run settings, indexes, AND every experiment's barcode plan."""
    return {"run": run_snapshot(run),
            "experiments": [plan_snapshot(e) for e in run.experiments().order_by("code")]}


def final_plan_problems(run: SequencingRun) -> list[str]:
    problems = [i.message for i in check_run(run) if i.level == "error"]
    for e in run.experiments().order_by("code"):
        problems += [f"{e.code}: {p}" for p in validate_plan(e)]
    if not run.run_pools.exists():
        problems.append("No experiments in this run.")
    return problems


@transaction.atomic
def submit_run(run: SequencingRun, user, comment: str = "", assignee=AUTO) -> SignOff:
    """Submit the final plan (well barcodes of all experiments + sample indexes) for 4-eyes review."""
    if run.status != SequencingRun.Status.PLANNING:
        raise PlannerError("Only runs in planning can be submitted for review.")
    problems = final_plan_problems(run)
    if problems:
        raise PlannerError(problems)
    snap = final_plan_snapshot(run)
    so = SignOff.objects.create(run=run, step=SignOff.Step.RUN_PLAN, submitted_by=user, comment=comment,
                                snapshot=snap, snapshot_hash=_hash(snap))
    if assignee is not AUTO and assignee is not None and assignee.pk == user.pk:
        raise PlannerError("4-eyes principle: choose a different person to approve your plan.")
    run.status = SequencingRun.Status.IN_REVIEW
    run.save()
    _set_run_experiments_status(run, Experiment.Status.PLAN_IN_REVIEW)
    assign(run, None if assignee is AUTO else assignee)  # reviewer (empty = any second person)
    return so


@transaction.atomic
def decide_run(signoff: SignOff, user, approve: bool, comment: str = "", assignee=AUTO):
    run = signoff.run
    _decide(signoff, user, approve, comment, final_plan_snapshot(run))
    run.status = SequencingRun.Status.APPROVED if approve else SequencingRun.Status.PLANNING
    run.save()
    _set_run_experiments_status(run, Experiment.Status.PLAN_APPROVED if approve else Experiment.Status.IN_RUN)
    if approve:  # library prep of each experiment; the run itself goes back to the person who planned it
        for e in run.experiments():
            assign(e, e.responsible_libprep if assignee is AUTO else assignee)
        assign(run, signoff.submitted_by)
    else:
        assign(run, signoff.submitted_by if assignee is AUTO else assignee)


@transaction.atomic
def reopen_run(run: SequencingRun, user, reason: str, assignee=AUTO):
    """Unlock the final plan. Only possible before any library prep of this run was recorded."""
    if not reason.strip():
        raise PlannerError("A reason is required.")
    if run.status not in (SequencingRun.Status.IN_REVIEW, SequencingRun.Status.APPROVED):
        raise PlannerError("Only runs in review or approved can be reopened.")
    if run.amendments.exists():
        raise PlannerError("Finish or reject the open experiment amendment(s) first.")
    started = [e.code for e in run.experiments() if e.status != Experiment.Status.PLAN_APPROVED
               and e.status != Experiment.Status.PLAN_IN_REVIEW]
    if started:
        raise PlannerError(f"Library prep has already been recorded for {', '.join(started)} — "
                           "the plan can no longer be reopened. Record a deviation instead.")
    Deviation.objects.create(run=run, created_by=user, description=f"Final plan reopened: {reason}")
    run.signoffs.filter(step=SignOff.Step.RUN_PLAN, state__in=[SignOff.State.PENDING, SignOff.State.APPROVED]) \
        .update(state=SignOff.State.SUPERSEDED)
    run.status = SequencingRun.Status.PLANNING
    run.save()
    _set_run_experiments_status(run, Experiment.Status.IN_RUN)
    assign(run, user if assignee is AUTO else assignee)


def libprep_progress(run: SequencingRun) -> tuple[list[Experiment], list[Experiment]]:
    exps = sorted(set(run.experiments()) | set(run.amendments.all()), key=lambda e: e.code)
    done = [e for e in exps if e.status in (Experiment.Status.LIBPREP_DONE, Experiment.Status.SUBMITTED_TO_PROVIDER,
                                            Experiment.Status.DATA_DELIVERED)]
    return done, [e for e in exps if e not in done]


@transaction.atomic
def mark_run_submitted(run: SequencingRun, user, assignee=AUTO):
    if run.status != SequencingRun.Status.APPROVED:
        raise PlannerError("Approve the final plan first.")
    if run.amendments.exists():
        raise PlannerError("Open amendment(s): " + ", ".join(e.code for e in run.amendments.all())
                           + " — they must be approved before the run is submitted.")
    _, open_ = libprep_progress(run)
    if open_:
        raise PlannerError(f"Library prep not yet recorded for: {', '.join(e.code for e in open_)}.")
    run.status = SequencingRun.Status.SUBMITTED
    if not run.planned_submission_date:
        run.planned_submission_date = timezone.localdate()
    run.save()
    _set_run_experiments_status(run, Experiment.Status.SUBMITTED_TO_PROVIDER)
    assign(run, user if assignee is AUTO else assignee)  # records the data delivery


@transaction.atomic
def mark_data_delivered(run: SequencingRun, user, comment: str = ""):
    if run.status != SequencingRun.Status.SUBMITTED:
        raise PlannerError("The run must be submitted to the provider first.")
    SignOff.objects.create(run=run, step=SignOff.Step.DATA_DELIVERED, state=SignOff.State.APPROVED,
                           submitted_by=user, decided_by=user, decided_at=timezone.now(), comment=comment)
    run.status = SequencingRun.Status.DATA_DELIVERED
    run.save()
    _set_run_experiments_status(run, Experiment.Status.DATA_DELIVERED)
    assign(run, None)
    for e in run.experiments():
        assign(e, None)


# --------------------------------------------------------------------------- #
# Amendments: change ONE experiment inside an approved run
#
# Other experiments of the run keep their approval and their library prep can continue.
#   reopen_experiment   Plan approved → Plan reopened – amendment
#                       (pools leave the run; their sample indexes are kept; plan becomes editable)
#   submit_amendment    pools re-join the run, missing indexes are assigned, ALL run checks
#                       (index uniqueness/distance across the entire run …) must pass → 4-eyes review
#   decide_amendment    approve → Plan approved again; reject → back to amendment
# --------------------------------------------------------------------------- #
def current_run(experiment: Experiment) -> SequencingRun | None:
    return experiment.amendment_run or SequencingRun.objects.filter(run_pools__pool__experiment=experiment).first()


@transaction.atomic
def reopen_experiment(experiment: Experiment, user, reason: str, assignee=AUTO):
    if not reason.strip():
        raise PlannerError("A reason is required.")
    run = current_run(experiment)
    if experiment.status != Experiment.Status.PLAN_APPROVED or run is None or run.status != SequencingRun.Status.APPROVED:
        raise PlannerError("Only experiments with an approved plan whose library prep has not been recorded can be "
                           "reopened. If library prep is already done, record a deviation instead.")
    Deviation.objects.create(experiment=experiment, run=run, created_by=user,
                             description=f"Plan of {experiment.code} reopened for amendment in run {run.run_id}: {reason}")
    RunPool.objects.filter(run=run, pool__experiment=experiment).delete()
    experiment.amendment_run = run
    set_status(experiment, Experiment.Status.AMENDING)
    assign(experiment, user if assignee is AUTO else assignee)


def _run_index_set(run: SequencingRun) -> IndexSet | None:
    pool = Pool.objects.filter(run_links__run=run, i7__isnull=False).select_related("i7__index_set").first()
    return pool.i7.index_set if pool else None


def _relink_amendment(experiment: Experiment, reassign: bool = False) -> SequencingRun:
    """Put the amended experiment's pools back into its run and fill in missing indexes."""
    run = experiment.amendment_run
    problems = validate_plan(experiment)
    if not experiment.pools.exists():
        problems.append("No pools/libraries.")
    if problems:
        raise PlannerError(problems)
    index_set = _run_index_set(run)
    pools = list(experiment.pools.all())
    if reassign:
        for p in pools:
            p.i7 = p.i5 = None
            p.save()
    total = sum(max(p.samples.count(), 1) for p in pools)
    for p in pools:
        RunPool.objects.create(run=run, pool=p, target_m_read_pairs=round(
            (experiment.requested_m_read_pairs or 0) * max(p.samples.count(), 1) / total, 1))
    if any(p.i7_id is None for p in experiment.pools.all()):
        if index_set is None:
            raise PlannerError("Cannot assign indexes: no index set in use in this run.")
        _assign_missing_indexes(run, index_set)
    return run


class _Rollback(Exception):
    pass


def preview_amendment(experiment: Experiment, reassign: bool = False) -> dict:
    """Dry run of submit_amendment: what the indexes and run checks WOULD be. Changes nothing."""
    result = {"problems": [], "issues": [], "libraries": []}
    try:
        with transaction.atomic():
            run = _relink_amendment(experiment, reassign)
            result["issues"] = check_run(run)
            result["problems"] = final_plan_problems(run)
            result["libraries"] = [(p.pool_id, p.i7, p.i5) for p in experiment.pools.select_related("i7", "i5")]
            raise _Rollback
    except _Rollback:
        pass
    except PlannerError as e:
        result["problems"] = e.problems
    return result


@transaction.atomic
def submit_amendment(experiment: Experiment, user, reassign: bool = False, comment: str = "", assignee=AUTO) -> SignOff:
    if experiment.status != Experiment.Status.AMENDING:
        raise PlannerError("The experiment is not being amended.")
    run = _relink_amendment(experiment, reassign)
    problems = final_plan_problems(run)
    if problems:
        raise PlannerError(["Run checks fail with the amended plan (nothing was saved):"] + problems)
    snap = final_plan_snapshot(run)
    so = SignOff.objects.create(experiment=experiment, run=run, step=SignOff.Step.AMENDMENT, submitted_by=user,
                                comment=comment, snapshot=snap, snapshot_hash=_hash(snap))
    if assignee is not AUTO and assignee is not None and assignee.pk == user.pk:
        raise PlannerError("4-eyes principle: choose a different person to approve the amendment.")
    set_status(experiment, Experiment.Status.PLAN_IN_REVIEW)
    assign(experiment, None if assignee is AUTO else assignee)
    return so


@transaction.atomic
def decide_amendment(signoff: SignOff, user, approve: bool, comment: str = "", assignee=AUTO):
    exp, run = signoff.experiment, signoff.run
    _decide(signoff, user, approve, comment, final_plan_snapshot(run))
    if approve:
        exp.amendment_run = None
        set_status(exp, Experiment.Status.PLAN_APPROVED)
        assign(exp, exp.responsible_libprep if assignee is AUTO else assignee)
    else:
        RunPool.objects.filter(run=run, pool__experiment=exp).delete()
        set_status(exp, Experiment.Status.AMENDING)
        assign(exp, signoff.submitted_by if assignee is AUTO else assignee)


# --------------------------------------------------------------------------- #
# Drag & drop editing (well barcodes, pools, sample indexes, library structure)
#
# Same rules as everywhere else: only while the plan is open (planning, run in planning,
# or amendment). Every change is recorded by the audit trail (history tables).
# --------------------------------------------------------------------------- #
def _check_barcodes_editable(experiment: Experiment):
    if not experiment.is_tag_and_pool:
        raise PlannerError("Well barcodes are only used for Tag&Pool experiments.")
    if experiment.plan_locked:
        raise PlannerError("The plan is locked. To change it: remove the experiment from its run (run in planning) "
                           "or reopen it as an amendment.")


def _mark_manual(experiment: Experiment):
    if experiment.planning_seed is not None:
        experiment.planning_seed = None  # layout no longer reproducible from a seed
        experiment.save()


def _drop_empty_pool(pool: Pool | None):
    if pool is not None and not pool.samples.exists() and not pool.run_links.exists():
        pool.delete()


def _next_pool_id(experiment: Experiment) -> str:
    existing = set(experiment.pools.values_list("pool_id", flat=True))
    n = 1
    while f"{experiment.code}_P{n:02d}" in existing:
        n += 1
    return f"{experiment.code}_P{n:02d}"


@transaction.atomic
def assign_barcode(sample: Sample, barcode: WellBarcode) -> str:
    """Give `sample` this barcode; if another sample in the same pool has it, the two swap."""
    exp = sample.experiment
    _check_barcodes_editable(exp)
    if barcode.barcode_set_id != exp.well_barcode_set_id:
        raise PlannerError(f"{barcode.barcode_id} is not in the barcode set of this experiment.")
    if sample.pool is None:
        raise PlannerError(f"{sample.sample_id} is not in a pool yet — drag it into a pool first.")
    other = Sample.objects.filter(pool=sample.pool, well_barcode=barcode).exclude(pk=sample.pk).first()
    msg = f"{sample.sample_id} → {barcode.barcode_id}"
    if other:
        other.well_barcode = sample.well_barcode
        other.save()
        msg += f" (swapped with {other.sample_id}, now {other.well_barcode.barcode_id if other.well_barcode else '–'})"
    sample.well_barcode = barcode
    sample.save()
    _mark_manual(exp)
    return msg


@transaction.atomic
def move_sample(sample: Sample, target_pool: Pool | None = None, target_sample: Sample | None = None,
                new_pool: bool = False) -> str:
    """Drop a sample onto another sample (swap) or onto a pool (take a free barcode there)."""
    exp = sample.experiment
    _check_barcodes_editable(exp)
    if target_sample is not None:
        if target_sample.experiment_id != exp.pk or target_sample.pk == sample.pk:
            raise PlannerError("Drop onto another sample of the same experiment.")
        a_pool, a_bc = sample.pool, sample.well_barcode
        sample.pool, sample.well_barcode = target_sample.pool, target_sample.well_barcode
        target_sample.pool, target_sample.well_barcode = a_pool, a_bc
        sample.save()
        target_sample.save()
        _mark_manual(exp)
        where = "barcodes swapped" if sample.pool_id == target_sample.pool_id else "pool and barcode swapped"
        return f"{sample.sample_id} ⇄ {target_sample.sample_id}: {where}"

    if new_pool:
        target_pool = Pool.objects.create(experiment=exp, pool_id=_next_pool_id(exp))
    if target_pool is None or target_pool.experiment_id != exp.pk:
        raise PlannerError("Unknown target pool.")
    if target_pool.pk == sample.pool_id:
        return "No change."
    used = set(target_pool.samples.values_list("well_barcode_id", flat=True))
    if sample.well_barcode_id and sample.well_barcode_id not in used:
        bc = sample.well_barcode  # keep its barcode if it is free in the target pool
    else:
        bc = exp.well_barcode_set.barcodes.exclude(pk__in=[u for u in used if u]).order_by("barcode_id").first()
    if bc is None:
        raise PlannerError(f"{target_pool.pool_id} is full (all {exp.well_barcode_set.size} barcodes used) — "
                           "drop onto a sample in that pool to swap instead.")
    old_pool = sample.pool
    sample.pool, sample.well_barcode = target_pool, bc
    sample.save()
    _drop_empty_pool(old_pool)
    _mark_manual(exp)
    return f"{sample.sample_id} → {target_pool.pool_id} with {bc.barcode_id}"


def index_scope(run: SequencingRun) -> list[Pool]:
    """Pools shown in the run's index grid: pools in the run + pools of open amendments."""
    ids = set(run.run_pools.values_list("pool_id", flat=True))
    ids |= set(Pool.objects.filter(experiment__amendment_run=run,
                                   experiment__status=Experiment.Status.AMENDING).values_list("pk", flat=True))
    return list(Pool.objects.filter(pk__in=ids).select_related("experiment", "i7", "i5")
                .order_by("experiment__code", "pool_id"))


def index_editable(run: SequencingRun, pool: Pool) -> bool:
    if run.status == SequencingRun.Status.PLANNING:
        return pool.run_links.filter(run=run).exists()
    return pool.experiment.amendment_run_id == run.pk and pool.experiment.status == Experiment.Status.AMENDING


@transaction.atomic
def set_pool_index(run: SequencingRun, pool: Pool, i7: IndexPrimer, i5: IndexPrimer | None) -> str:
    """Assign an i7/i5 pair to a pool. If another pool of the run has this pair, the two swap."""
    if not index_editable(run, pool):
        raise PlannerError(f"{pool.pool_id} is locked (final plan in review/approved). Reopen the run or amend the experiment.")
    if i7.kind != "i7" or (i5 is not None and i5.kind != "i5"):
        raise PlannerError("Invalid primer kinds.")
    if i5 is not None and i5.index_set_id != i7.index_set_id:
        raise PlannerError("i7 and i5 must come from the same index set.")
    other = next((p for p in index_scope(run) if p.pk != pool.pk and p.i7_id == i7.pk
                  and (p.i5_id == (i5.pk if i5 else None))), None)
    msg = f"{pool.pool_id} → {i7.primer_id} + {i5.primer_id if i5 else '–'}"
    if other:
        if not index_editable(run, other):
            raise PlannerError(f"This pair is used by {other.pool_id}, which is locked.")
        other.i7, other.i5 = pool.i7, pool.i5
        other.save()
        msg += f" (swapped with {other.pool_id})"
    pool.i7, pool.i5 = i7, i5
    pool.save()
    return msg


@transaction.atomic
def clear_pool_index(run: SequencingRun, pool: Pool) -> str:
    if not index_editable(run, pool):
        raise PlannerError(f"{pool.pool_id} is locked.")
    pool.i7 = pool.i5 = None
    pool.save()
    return f"{pool.pool_id}: index removed"


def grid_index_set(run: SequencingRun, requested_pk: int | None = None) -> IndexSet | None:
    if requested_pk:
        return IndexSet.objects.filter(pk=requested_pk).first()
    used = next((p.i7.index_set for p in index_scope(run) if p.i7_id), None)
    return used or IndexSet.objects.order_by("name").first()


@transaction.atomic
def save_library_layout(experiment: Experiment, raw) -> list[dict]:
    if experiment.plan_locked:
        raise PlannerError("The plan is locked — the library structure can only be changed while the plan is open.")
    try:
        layout = library.clean_layout(raw)
    except (ValueError, TypeError) as e:
        raise PlannerError(str(e))
    experiment.library_layout = layout
    experiment.save()
    return layout


@transaction.atomic
def save_library_template(library_type: str, raw) -> None:
    from .models import LibraryTemplate
    try:
        layout = library.clean_layout(raw)
    except (ValueError, TypeError) as e:
        raise PlannerError(str(e))
    LibraryTemplate.objects.update_or_create(library_type=library_type, defaults={"layout": layout})


def library_template(library_type: str) -> list[dict]:
    from .models import LibraryTemplate
    t = LibraryTemplate.objects.filter(library_type=library_type).first()
    return t.layout if t else library.default_layout(library_type)
