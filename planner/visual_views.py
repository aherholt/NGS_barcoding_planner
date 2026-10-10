"""Visual pages: plates & well barcodes, library structure, sample-index grid (with drag & drop).

Drag & drop in the browser sends small JSON requests to the *_dnd endpoints; all rules are
enforced in services.py, the browser only displays. After a successful change the page reloads.
"""

import json
from collections import Counter

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.html import escape
from django.views.decorators.http import require_POST

from . import library, plates, services
from .models import Experiment, IndexPrimer, Pool, Sample, SequencingRun, WellBarcode
from .services import PlannerError


def _json_body(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        raise PlannerError("Invalid request.")


def _json_endpoint(fn):
    @login_required
    @require_POST
    def wrapper(request, *args, **kwargs):
        try:
            return JsonResponse({"ok": True, **(fn(request, *args, **kwargs) or {})})
        except PlannerError as e:
            return JsonResponse({"ok": False, "errors": e.problems}, status=400)
        except (Sample.DoesNotExist, Pool.DoesNotExist, WellBarcode.DoesNotExist, IndexPrimer.DoesNotExist):
            return JsonResponse({"ok": False, "errors": ["Item not found — reload the page."]}, status=400)
    wrapper.__name__ = fn.__name__
    return wrapper


# --------------------------------------------------------------------------- #
# Plates & well barcodes
# --------------------------------------------------------------------------- #
@login_required
def experiment_plates(request, pk):
    exp = get_object_or_404(Experiment.objects.select_related("well_barcode_set"), pk=pk)
    samples = list(exp.samples.select_related("pool", "well_barcode"))
    pools = list(exp.pools.order_by("pool_id"))
    pool_colour = {p.pk: (plates.colour(i), plates.colour_dark(i)) for i, p in enumerate(pools)}

    def cell(s):
        c = pool_colour.get(s.pool_id, ("#ffffff", "#667085"))
        return {"s": s, "bg": c[0], "fg": c[1]}

    # culture plates in order of first appearance
    culture = []
    for name in dict.fromkeys(s.source_plate for s in samples if s.source_plate):
        members = [s for s in samples if s.source_plate == name]
        fmt = members[0].plate_format
        culture.append({"name": name, "format": fmt,
                        "rows": plates.grid(fmt, {s.source_well: cell(s) for s in members}),
                        "cols": range(1, plates.rows_cols(fmt)[1] + 1)})

    # 96-well PCR plates, as the STAR protocol fills them
    layout = plates.pcr_layout([{"sample_id": s.sample_id, "pool_id": s.pool.pool_id if s.pool else None,
                                 "source_plate": s.source_plate, "source_well": s.source_well,
                                 "plate_format": s.plate_format} for s in samples if s.pool])
    by_id = {s.sample_id: s for s in samples}
    pcr = []
    for plate in sorted({p.plate for p in layout.values()}):
        filled = {pos.well: cell(by_id[sid]) for sid, pos in layout.items() if pos.plate == plate}
        pcr.append({"name": plate, "rows": plates.grid(96, filled), "cols": range(1, 13)})

    # barcode stock plate
    stock = None
    if exp.well_barcode_set:
        bcs = list(exp.well_barcode_set.barcodes.all())
        stock = {"rows": plates.grid(96, {b.well: b for b in bcs}), "cols": range(1, 13)}

    pool_list = []
    for p in pools:
        members = sorted((s for s in samples if s.pool_id == p.pk),
                         key=lambda s: (s.source_plate, plates.well_index(s.plate_format, s.source_well)))
        pool_list.append({"pool": p, "samples": members, "bg": pool_colour[p.pk][0], "fg": pool_colour[p.pk][1],
                          "free": (exp.well_barcode_set.size - len(members)) if exp.well_barcode_set else 0})
    editable = exp.is_tag_and_pool and not exp.plan_locked
    return render(request, "planner/experiment_plates.html", {
        "exp": exp, "culture": culture, "pcr": pcr, "stock": stock, "pools": pool_list,
        "unassigned": [s for s in samples if not s.pool], "editable": editable,
        "problems": services.validate_plan(exp) if samples else [],
        "warnings": services.plan_warnings(exp), "tab": "plates",
    })


@_json_endpoint
def experiment_dnd(request, pk):
    exp = get_object_or_404(Experiment, pk=pk)
    d = _json_body(request)
    op = d.get("op")
    if op == "assign_barcode":
        msg = services.assign_barcode(exp.samples.get(pk=d["sample"]),
                                      WellBarcode.objects.get(pk=d["barcode"]))
    elif op == "move_sample":
        sample = exp.samples.get(pk=d["sample"])
        if d.get("target_sample"):
            msg = services.move_sample(sample, target_sample=exp.samples.get(pk=d["target_sample"]))
        elif d.get("pool") == "new":
            msg = services.move_sample(sample, new_pool=True)
        else:
            msg = services.move_sample(sample, target_pool=exp.pools.get(pk=d["pool"]))
    else:
        raise PlannerError("Unknown operation.")
    return {"message": msg}


# --------------------------------------------------------------------------- #
# Library structure
# --------------------------------------------------------------------------- #
def _analysis_html(exp, layout) -> str:
    an = services.library_analysis(exp, layout)
    parts = [library.svg(an)]
    parts.append('<table class="cov"><tr><th>Read</th><th>Covers (read bases)</th></tr>')
    for rd in an.reads:
        cov = " · ".join(f'<b>{escape(c["label"])}</b> {c["first"]}–{c["last"]}' for c in rd.covers)
        parts.append(f"<tr><td>{escape(rd.name)} ({rd.cycles})</td><td>{cov or '–'}</td></tr>")
    parts.append("</table>")
    for lvl, text in an.messages:
        cls = {"ok": "info", "info": "info"}.get(lvl, lvl)
        parts.append(f'<div class="issue {cls}">{"✓ " if lvl == "ok" else ""}{escape(text)}</div>')
    parts.append(f'<p class="help">Total product ≈ {an.total} bp (insert drawn with '
                 f'{"the experiment’s average insert length" if exp.avg_insert_length else "100 bp — set the average insert length on the experiment"}).</p>')
    return "".join(parts)


@login_required
def experiment_library(request, pk):
    exp = get_object_or_404(Experiment, pk=pk)
    saved = bool(exp.library_layout)
    layout = exp.library_layout or services.library_template(exp.library_type)
    return render(request, "planner/experiment_library.html", {
        "exp": exp, "layout_json": json.dumps(layout), "saved": saved,
        "segment_types": [{"type": t, "label": v[0], "length": v[1], "colour": v[2]} for t, v in library.SEGMENT_TYPES.items()],
        "segment_types_json": json.dumps({t: {"label": v[0], "length": v[1], "colour": v[2]} for t, v in library.SEGMENT_TYPES.items()}),
        "analysis_html": _analysis_html(exp, layout), "editable": not exp.plan_locked, "tab": "library",
    })


@_json_endpoint
def experiment_library_api(request, pk, action):
    exp = get_object_or_404(Experiment, pk=pk)
    raw = _json_body(request).get("layout")
    if action == "preview":
        try:
            layout = library.clean_layout(raw)
        except (ValueError, TypeError) as e:
            raise PlannerError(str(e))
        return {"html": _analysis_html(exp, layout)}
    if action == "save":
        services.save_library_layout(exp, raw)
        return {"message": "Library structure saved."}
    if action == "template":
        services.save_library_template(exp.library_type, raw)
        return {"message": f"Saved as template for {exp.get_library_type_display()}."}
    if action == "load_template":
        return {"layout": services.library_template(exp.library_type)}
    raise PlannerError("Unknown action.")


# --------------------------------------------------------------------------- #
# Sample-index grid (i7 × i5)
# --------------------------------------------------------------------------- #
@login_required
def run_indexes(request, pk):
    run = get_object_or_404(SequencingRun, pk=pk)
    iset = services.grid_index_set(run, request.GET.get("set"))
    scope = services.index_scope(run)
    exp_ids = list(dict.fromkeys(p.experiment_id for p in scope))
    exp_colour = {e: (plates.colour(i), plates.colour_dark(i)) for i, e in enumerate(exp_ids)}
    def short(p):
        pre = p.experiment.code + "_"
        return p.pool_id[len(pre):] if p.pool_id.startswith(pre) else p.pool_id

    tiles = {p.pk: {"pool": p, "short": short(p), "bg": exp_colour[p.experiment_id][0], "fg": exp_colour[p.experiment_id][1],
                    "editable": services.index_editable(run, p),
                    "amending": p.experiment.status == Experiment.Status.AMENDING} for p in scope}
    i7s = list(iset.primers.filter(kind="i7")) if iset else []
    i5s = list(iset.primers.filter(kind="i5")) if iset else []
    cells: dict[tuple, list] = {}
    for p in scope:
        if p.i7_id:
            cells.setdefault((p.i7_id, p.i5_id), []).append(tiles[p.pk])
    used7 = Counter(p.i7_id for p in scope if p.i7_id)
    used5 = Counter(p.i5_id for p in scope if p.i5_id)
    rows = [{"i7": a, "used": used7.get(a.pk, 0),
             "cells": [{"i5": b, "tiles": cells.get((a.pk, b.pk), [])} for b in i5s]} for a in i7s]
    foreign = [tiles[p.pk] for p in scope if p.i7_id and iset and p.i7.index_set_id != iset.pk]
    amend_previews = [(e, services.preview_amendment(e)) for e in run.amendments.filter(status=Experiment.Status.AMENDING)]
    from .models import IndexSet
    return render(request, "planner/run_indexes.html", {
        "run": run, "iset": iset, "index_sets": IndexSet.objects.all(), "rows": rows, "i5s": i5s,
        "used5": {b.pk: used5.get(b.pk, 0) for b in i5s},
        "unassigned": [tiles[p.pk] for p in scope if not p.i7_id], "foreign": foreign,
        "experiments": [(Experiment.objects.get(pk=e), exp_colour[e]) for e in exp_ids],
        "issues": services.check_run(run), "amend_previews": amend_previews,
        "any_editable": any(t["editable"] for t in tiles.values()), "tab": "indexes",
    })


@_json_endpoint
def run_dnd(request, pk):
    run = get_object_or_404(SequencingRun, pk=pk)
    d = _json_body(request)
    pool = Pool.objects.select_related("experiment").get(pk=d["pool"])
    if pool not in services.index_scope(run):
        raise PlannerError("This pool is not part of the run.")
    if d.get("op") == "set_index":
        i7 = IndexPrimer.objects.get(pk=d["i7"])
        i5 = IndexPrimer.objects.get(pk=d["i5"]) if d.get("i5") else None
        msg = services.set_pool_index(run, pool, i7, i5)
    elif d.get("op") == "clear_index":
        msg = services.clear_pool_index(run, pool)
    else:
        raise PlannerError("Unknown operation.")
    return {"message": msg}
