"""Import well barcodes and index primers from your Excel/CSV primer sheets."""

from __future__ import annotations

import re

from django.db import transaction

from .models import IndexPrimer, IndexSet, WellBarcode, WellBarcodeSet
from .services import PlannerError

WELL_RE = re.compile(r"^[A-H](?:[1-9]|1[0-2])$")
SEQ_RE = re.compile(r"^[ACGT]+$")


@transaction.atomic
def import_well_barcodes(set_name: str, rows: list[dict], description: str = "") -> WellBarcodeSet:
    """Columns: barcode_id, well, sequence. Creates a NEW set; existing sets are never changed."""
    if WellBarcodeSet.objects.filter(name=set_name).exists():
        raise PlannerError(f"Set '{set_name}' already exists. Use a new name (e.g. with v2) — used sets must not change.")
    problems = []
    for i, r in enumerate(rows, start=2):
        for col in ("barcode_id", "well", "sequence"):
            if not r.get(col):
                problems.append(f"row {i}: '{col}' empty or column missing")
        if r.get("well") and not WELL_RE.match(r["well"].upper()):
            problems.append(f"row {i}: invalid well {r['well']}")
        if r.get("sequence") and not SEQ_RE.match(r["sequence"].upper().replace(" ", "")):
            problems.append(f"row {i}: sequence has non-ACGT characters")
    seqs = [r.get("sequence", "").upper() for r in rows]
    if len({len(s) for s in seqs if s}) > 1:
        problems.append("barcode sequences have different lengths")
    for col in ("barcode_id", "well", "sequence"):
        vals = [r.get(col, "").upper() for r in rows]
        dup = sorted({v for v in vals if v and vals.count(v) > 1})
        if dup:
            problems.append(f"duplicate {col}: {dup}")
    if problems:
        raise PlannerError(problems)
    bset = WellBarcodeSet.objects.create(name=set_name, description=description)
    for r in rows:
        WellBarcode.objects.create(barcode_set=bset, barcode_id=r["barcode_id"], well=r["well"], sequence=r["sequence"])
    return bset


@transaction.atomic
def import_index_primers(set_name: str, rows: list[dict], description: str = "") -> IndexSet:
    """Columns: kind (i7/i5), primer_id, sequence, optional well."""
    if IndexSet.objects.filter(name=set_name).exists():
        raise PlannerError(f"Index set '{set_name}' already exists. Use a new name.")
    problems = []
    for i, r in enumerate(rows, start=2):
        if r.get("kind", "").lower() not in ("i7", "i5"):
            problems.append(f"row {i}: kind must be i7 or i5")
        if not r.get("primer_id"):
            problems.append(f"row {i}: primer_id empty")
        if not SEQ_RE.match(r.get("sequence", "").upper().replace(" ", "")):
            problems.append(f"row {i}: sequence missing or has non-ACGT characters")
    keys = [(r.get("kind", "").lower(), r.get("primer_id")) for r in rows]
    dup = sorted({f"{k}:{p}" for k, p in keys if keys.count((k, p)) > 1})
    if dup:
        problems.append(f"duplicate primers: {dup}")
    if problems:
        raise PlannerError(problems)
    iset = IndexSet.objects.create(name=set_name, description=description)
    for r in rows:
        IndexPrimer.objects.create(index_set=iset, kind=r["kind"].lower(), primer_id=r["primer_id"],
                                   sequence=r["sequence"], well=r.get("well", ""))
    return iset
