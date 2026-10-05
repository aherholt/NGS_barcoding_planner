"""File exports.

* STAR robot input  — same columns as tag_and_pool_hamilton_star_sim/data/*.csv
* Barcode map       — for the bioinformatician: sample → pool → sample index → well barcode
* Provider sheets   — Illumina BCL Convert v2 sample sheet + a simple CSV (pools as Sample_ID)
* Run JSON          — everything about a run in one machine-readable file
"""

from __future__ import annotations

import csv
import io
import json

from django.conf import settings

from .models import Experiment, SequencingRun, WellBarcodeSet

COMPLEMENT = str.maketrans("ACGTN", "TGCAN")


def revcomp(seq: str) -> str:
    return seq.translate(COMPLEMENT)[::-1]


def _csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


# --- STAR robot ---------------------------------------------------------------
STAR_COLUMNS = ["sample_id", "source_plate", "source_well", "condition", "pool_id", "barcode_id"]


def star_sample_sheet(experiment: Experiment) -> str:
    rows = [[s.sample_id, s.source_plate, s.source_well, s.condition,
             s.pool.pool_id if s.pool else "", s.well_barcode.barcode_id if s.well_barcode else ""]
            for s in experiment.samples.select_related("pool", "well_barcode")]
    return _csv(STAR_COLUMNS, rows)


def star_barcode_plate(barcode_set: WellBarcodeSet) -> str:
    return _csv(["barcode_id", "well", "sequence"],
                [[b.barcode_id, b.well, b.sequence] for b in barcode_set.barcodes.all()])


# --- Bioinformatics -------------------------------------------------------------
BARCODE_MAP_COLUMNS = [
    "sample_id", "experiment", "condition", "source_plate", "source_well", "pool_id",
    "i7_id", "i7_seq", "i5_id", "i5_seq",
    "well_barcode_id", "well_barcode_seq", "well_barcode_read", "well_barcode_start", "well_barcode_length",
]


def _barcode_map_rows(samples) -> list[list]:
    cfg = settings.PLANNER
    rows = []
    for s in samples:
        p = s.pool
        tp = s.experiment.is_tag_and_pool and s.well_barcode
        rows.append([
            s.sample_id, s.experiment.code, s.condition, s.source_plate, s.source_well, p.pool_id if p else "",
            p.i7.primer_id if p and p.i7 else "", p.i7.sequence if p and p.i7 else "",
            p.i5.primer_id if p and p.i5 else "", p.i5.sequence if p and p.i5 else "",
            s.well_barcode.barcode_id if tp else "", s.well_barcode.sequence if tp else "",
            cfg["WELL_BARCODE_READ"] if tp else "", cfg["WELL_BARCODE_START"] if tp else "",
            cfg["WELL_BARCODE_LENGTH"] if tp else "",
        ])
    return rows


def barcode_map_experiment(experiment: Experiment) -> str:
    samples = experiment.samples.select_related("experiment", "pool__i7", "pool__i5", "well_barcode")
    return _csv(BARCODE_MAP_COLUMNS, _barcode_map_rows(samples))


def barcode_map_run(run: SequencingRun) -> str:
    from .models import Sample
    samples = Sample.objects.filter(pool__run_links__run=run).select_related(
        "experiment", "pool__i7", "pool__i5", "well_barcode").order_by("experiment__code", "pool__pool_id", "order")
    return _csv(BARCODE_MAP_COLUMNS, _barcode_map_rows(samples))


# --- Provider -------------------------------------------------------------------
def _i5(run: SequencingRun, seq: str) -> str:
    seq = seq[: run.i5_length or len(seq)]
    return revcomp(seq) if run.i5_reverse_complement else seq


def illumina_samplesheet_v2(run: SequencingRun) -> str:
    """BCL Convert v2 sample sheet. Check with your provider whether they use it as is."""
    lines = [
        "[Header]", "FileFormatVersion,2", f"RunName,{run.run_id}", "InstrumentPlatform,NovaSeqXSeries", "",
        "[Reads]", f"Read1Cycles,{run.r1_length or ''}", f"Read2Cycles,{run.r2_length or ''}",
        f"Index1Cycles,{run.i7_length or ''}",
    ]
    if run.i5_length:
        lines.append(f"Index2Cycles,{run.i5_length}")
    lines += ["", "[BCLConvert_Data]", "Sample_ID,Index,Index2" if run.i5_length else "Sample_ID,Index"]
    for rp in run.run_pools.select_related("pool__i7", "pool__i5"):
        p = rp.pool
        i7 = p.i7.sequence[: run.i7_length or None] if p.i7 else ""
        row = [p.pool_id, i7]
        if run.i5_length:
            row.append(_i5(run, p.i5.sequence) if p.i5 else "")
        lines.append(",".join(row))
    return "\n".join(lines) + "\n"


def provider_csv(run: SequencingRun) -> str:
    rows = []
    for rp in run.run_pools.select_related("pool__experiment", "pool__i7", "pool__i5"):
        p, e = rp.pool, rp.pool.experiment
        rows.append([p.pool_id, e.code, p.i7.primer_id if p.i7 else "", p.i7.sequence[: run.i7_length or None] if p.i7 else "",
                     p.i5.primer_id if p.i5 else "", _i5(run, p.i5.sequence) if p.i5 else "",
                     rp.target_m_read_pairs, e.avg_product_length or "", e.get_library_complexity_display()])
    return _csv(["Sample_ID", "Experiment", "i7_ID", "Index", "i5_ID", "Index2", "Target_M_read_pairs",
                 "Avg_product_length_bp", "Library_complexity"], rows)


def run_json(run: SequencingRun) -> str:
    from .services import check_run
    data = {
        "run_id": run.run_id, "provider": run.provider, "status": run.status,
        "flowcell": run.flowcell_type.name if run.flowcell_type else None,
        "read_structure": {"R1": run.r1_length, "i7": run.i7_length, "i5": run.i5_length, "R2": run.r2_length},
        "i5_reverse_complement_in_samplesheet": run.i5_reverse_complement,
        "well_barcode": {k.lower(): v for k, v in settings.PLANNER.items() if k.startswith("WELL_BARCODE")},
        "checks": [vars(i) for i in check_run(run)],
        "libraries": [],
    }
    for rp in run.run_pools.select_related("pool__experiment", "pool__i7", "pool__i5"):
        p, e = rp.pool, rp.pool.experiment
        data["libraries"].append({
            "pool_id": p.pool_id, "experiment": e.code, "library_type": e.library_type,
            "target_m_read_pairs": rp.target_m_read_pairs,
            "i7": {"id": p.i7.primer_id, "seq": p.i7.sequence} if p.i7 else None,
            "i5": {"id": p.i5.primer_id, "seq": p.i5.sequence} if p.i5 else None,
            "samples": [{"sample_id": s.sample_id, "condition": s.condition,
                         "well_barcode": ({"id": s.well_barcode.barcode_id, "seq": s.well_barcode.sequence}
                                          if s.well_barcode else None)}
                        for s in p.samples.select_related("well_barcode")],
        })
    return json.dumps(data, indent=2, default=str)
