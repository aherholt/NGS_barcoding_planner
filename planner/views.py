from collections import OrderedDict
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import exports, services
from .forms import (
    AddExperimentForm, AssignIndexForm, CommentForm, DeviationForm, ExperimentForm, LibprepForm,
    LockedExperimentForm, PlanForm, ReasonForm, RunForm, UploadForm,
)
from .models import Experiment, Pool, Sample, SequencingRun, SignOff
from .services import PlannerError
from .tabular import read_table


def _download(content: str, filename: str, content_type="text/csv"):
    resp = HttpResponse(content, content_type=f"{content_type}; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def handles_planner_errors(view):
    """Turn PlannerError into a readable message and go back to the page the user came from."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PlannerError as e:
            for p in e.problems:
                messages.error(request, p)
            return redirect(request.META.get("HTTP_REFERER") or "dashboard")
    return login_required(wrapper)


# --------------------------------------------------------------------------- #
@login_required
def dashboard(request):
    exps = Experiment.objects.exclude(status__in=[Experiment.Status.DATA_DELIVERED, Experiment.Status.CANCELLED])
    pending = SignOff.objects.filter(state=SignOff.State.PENDING).exclude(submitted_by=request.user) \
        .select_related("experiment", "run", "submitted_by")
    return render(request, "planner/dashboard.html", {
        "experiments": exps.select_related("responsible_libprep", "responsible_ngs"),
        "runs": SequencingRun.objects.exclude(status__in=[SequencingRun.Status.DATA_DELIVERED, SequencingRun.Status.CANCELLED])
        .select_related("flowcell_type"),
        "pending": pending,
    })


@login_required
def experiment_list(request):
    return render(request, "planner/experiment_list.html", {"experiments": Experiment.objects.all()})


@login_required
def experiment_edit(request, pk=None):
    exp = get_object_or_404(Experiment, pk=pk) if pk else None
    form_cls = LockedExperimentForm if exp and exp.plan_locked else ExperimentForm
    form = form_cls(request.POST or None, instance=exp)
    if request.method == "POST" and form.is_valid():
        exp = form.save()
        messages.success(request, "Experiment saved.")
        return redirect(exp)
    return render(request, "planner/form.html", {
        "form": form, "title": f"Edit {exp.code}" if exp else "New experiment",
        "note": "The barcode plan is locked — only descriptive fields can be changed." if exp and exp.plan_locked else
        "Experiments normally come from the SharePoint list; create them here only for testing or if the sync is not set up.",
    })


@login_required
def experiment_detail(request, pk):
    exp = get_object_or_404(Experiment.objects.select_related("well_barcode_set"), pk=pk)
    pools = OrderedDict()
    for p in exp.pools.select_related("i7", "i5").order_by("pool_id"):
        pools[p] = []
    unassigned = []
    for s in exp.samples.select_related("pool", "well_barcode"):
        (pools[s.pool] if s.pool in pools else unassigned).append(s)
    problems = services.ready_for_run(exp) if exp.status == Experiment.Status.ACCEPTED and exp.samples.exists() else []
    amendment = None
    if exp.status == Experiment.Status.AMENDING and exp.samples.exists():
        amendment = services.preview_amendment(exp, reassign=request.GET.get("reassign") == "1")
    return render(request, "planner/experiment_detail.html", {
        "exp": exp, "pools": pools, "unassigned": unassigned, "problems": problems,
        "run": services.current_run(exp), "amendment": amendment,
        "pending_signoff": exp.signoffs.filter(state=SignOff.State.PENDING, step=SignOff.Step.AMENDMENT).first(),
        "open_runs": SequencingRun.objects.filter(status=SequencingRun.Status.PLANNING),
        "n_samples": exp.samples.count(),
        "signoffs": exp.signoffs.select_related("submitted_by", "decided_by"),
        "deviations": exp.deviations.select_related("created_by"),
        "upload_form": UploadForm(), "plan_form": PlanForm(initial={"shuffle": True}),
        "comment_form": CommentForm(), "reason_form": ReasonForm(), "libprep_form": LibprepForm(),
        "deviation_form": DeviationForm(),
    })


@require_POST
@handles_planner_errors
def experiment_upload(request, pk):
    exp = get_object_or_404(Experiment, pk=pk)
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        raise PlannerError("Choose a file.")
    f = form.cleaned_data["file"]
    try:
        rows = read_table(f.read(), f.name)
    except Exception as e:  # unreadable file
        raise PlannerError(f"Could not read the file: {e}")
    n = services.upload_samples(exp, rows, request.user)
    messages.success(request, f"{n} samples uploaded.")
    return redirect(exp)


@require_POST
@handles_planner_errors
def experiment_plan(request, pk):
    exp = get_object_or_404(Experiment, pk=pk)
    form = PlanForm(request.POST)
    if not form.is_valid():
        raise PlannerError([f"{k}: {', '.join(v)}" for k, v in form.errors.items()])
    d = form.cleaned_data
    pools = services.plan_barcodes(exp, pool_sizes=d["pool_sizes"], max_pool_size=d["max_pool_size"],
                                   shuffle=d["shuffle"], seed=d["seed"])
    exp.refresh_from_db()
    messages.success(request, f"Planned {len(pools)} pools" + (f" (seed {exp.planning_seed})." if exp.planning_seed else "."))
    return redirect(exp)


@require_POST
@handles_planner_errors
def experiment_action(request, pk, action):
    exp = get_object_or_404(Experiment, pk=pk)
    comment = request.POST.get("comment", "")
    if action == "accept":
        services.accept_experiment(exp, request.user, comment)
        messages.success(request, "Experiment accepted.")
    elif action == "reopen":
        services.reopen_experiment(exp, request.user, request.POST.get("reason", ""))
        messages.success(request, "Plan reopened for amendment. The other experiments of the run are not affected.")
    elif action == "submit_amendment":
        services.submit_amendment(exp, request.user, request.POST.get("reassign") == "on", comment)
        messages.success(request, "Amendment passed all run checks and was submitted. A second person must approve it.")
    elif action in ("approve_amendment", "reject_amendment"):
        so = get_object_or_404(SignOff, pk=request.POST.get("signoff"), experiment=exp, step=SignOff.Step.AMENDMENT)
        services.decide_amendment(so, request.user, action == "approve_amendment", comment)
        messages.success(request, "Amendment approved — library prep can start." if action == "approve_amendment"
                         else "Amendment rejected — the plan is open for changes again.")
    elif action == "add_to_run":
        run = get_object_or_404(SequencingRun, pk=request.POST.get("run"))
        services.add_experiment_to_run(run, exp)
        messages.success(request, f"{exp.code} added to run {run.run_id}. Distribute the sample indexes there.")
        return redirect(run)
    elif action == "libprep_done":
        rows = None
        if request.FILES.get("manifest"):
            f = request.FILES["manifest"]
            rows = read_table(f.read(), f.name)
        diffs = services.record_libprep_done(exp, request.user, comment, rows)
        if diffs:
            messages.warning(request, f"Recorded, but the robot manifest differs from the plan in {len(diffs)} places — saved as a deviation.")
        else:
            messages.success(request, "Library prep recorded." + (" Robot manifest matches the plan." if rows is not None else ""))
    elif action == "deviation":
        services.add_deviation(request.user, request.POST.get("description", ""), experiment=exp)
        messages.success(request, "Deviation recorded.")
    elif action in ("hold", "cancel", "resume"):
        target = {"hold": Experiment.Status.ON_HOLD, "cancel": Experiment.Status.CANCELLED,
                  "resume": Experiment.Status.SUBMITTED}[action]
        services.change_hold_status(exp, request.user, target, request.POST.get("reason", ""))
    else:
        raise PlannerError("Unknown action.")
    return redirect(exp)


@login_required
def experiment_download(request, pk, kind):
    exp = get_object_or_404(Experiment, pk=pk)
    if kind == "star":
        if exp.status not in (Experiment.Status.PLAN_APPROVED, Experiment.Status.LIBPREP_DONE,
                              Experiment.Status.SUBMITTED_TO_PROVIDER,
                              Experiment.Status.DATA_DELIVERED) and not request.GET.get("draft"):
            messages.error(request, "The robot file is only available once the final plan is approved (use the draft link for testing).")
            return redirect(exp)
        prefix = "" if not request.GET.get("draft") else "DRAFT_"
        return _download(exports.star_sample_sheet(exp), f"{prefix}{exp.code}_star_sample_sheet.csv")
    if kind == "barcode_plate" and exp.well_barcode_set:
        return _download(exports.star_barcode_plate(exp.well_barcode_set), f"barcode_plate_{exp.well_barcode_set.pk}.csv")
    if kind == "barcode_map":
        return _download(exports.barcode_map_experiment(exp), f"{exp.code}_barcode_map.csv")
    messages.error(request, "Unknown download.")
    return redirect(exp)


@login_required
def experiment_history(request, pk):
    exp = get_object_or_404(Experiment, pk=pk)
    records = []
    sources = [("Experiment", exp.history.all()),
               ("Sample", Sample.history.filter(experiment_id=exp.pk)),
               ("Pool", Pool.history.filter(experiment_id=exp.pk))]
    for label, qs in sources:
        for rec in qs.select_related("history_user"):
            changes = ""
            prev = rec.prev_record
            if rec.history_type == "~" and prev is not None:
                delta = rec.diff_against(prev)
                changes = "; ".join(f"{c.field}: {c.old} → {c.new}" for c in delta.changes)
            records.append({"when": rec.history_date, "who": rec.history_user, "what": label,
                            "obj": str(rec), "type": {"+": "created", "~": "changed", "-": "deleted"}[rec.history_type],
                            "changes": changes})
    records.sort(key=lambda r: r["when"], reverse=True)
    return render(request, "planner/history.html", {"exp": exp, "records": records[:1000]})


# --------------------------------------------------------------------------- #
@login_required
def run_list(request):
    return render(request, "planner/run_list.html", {"runs": SequencingRun.objects.select_related("flowcell_type")})


@login_required
def run_edit(request, pk=None):
    run = get_object_or_404(SequencingRun, pk=pk) if pk else None
    if run and run.locked:
        messages.error(request, "The run plan is locked. Reopen it to make changes.")
        return redirect(run)
    form = RunForm(request.POST or None, instance=run)
    if request.method == "POST" and form.is_valid():
        run = form.save()
        messages.success(request, "Run saved.")
        return redirect(run)
    return render(request, "planner/form.html", {"form": form, "title": f"Edit {run.run_id}" if run else "New sequencing run",
                                                  "note": "Read lengths are taken from the first experiment you add, if left empty."})


@login_required
def run_detail(request, pk):
    run = get_object_or_404(SequencingRun.objects.select_related("flowcell_type"), pk=pk)
    rps = list(run.run_pools.select_related("pool__experiment", "pool__i7", "pool__i5"))
    issues = services.check_run(run)
    done, open_ = services.libprep_progress(run)
    return render(request, "planner/run_detail.html", {
        "run": run, "run_pools": rps, "issues": issues,
        "errors": [i for i in issues if i.level == "error"],
        "experiments": run.experiments(),
        "flowcells": services.flowcell_options(run),
        "total_target": sum(rp.target_m_read_pairs for rp in rps),
        "add_form": AddExperimentForm(run=run), "index_form": AssignIndexForm(),
        "comment_form": CommentForm(), "reason_form": ReasonForm(), "deviation_form": DeviationForm(),
        "signoffs": run.signoffs.select_related("submitted_by", "decided_by"),
        "deviations": run.deviations.select_related("created_by"),
        "pending_signoff": run.signoffs.filter(state=SignOff.State.PENDING, step=SignOff.Step.RUN_PLAN).first(),
        "plan_problems": services.final_plan_problems(run) if run.status == SequencingRun.Status.PLANNING else [],
        "libprep_done": done, "libprep_open": open_,
        "amendments": run.amendments.all(),
        "pending_amendments": run.signoffs.filter(state=SignOff.State.PENDING, step=SignOff.Step.AMENDMENT)
        .select_related("experiment", "submitted_by"),
    })


@require_POST
@handles_planner_errors
def run_action(request, pk, action):
    run = get_object_or_404(SequencingRun, pk=pk)
    comment = request.POST.get("comment", "")
    if action == "add_experiment":
        form = AddExperimentForm(request.POST, run=run)
        if not form.is_valid():
            raise PlannerError("Choose an experiment that is ready for sequencing.")
        services.add_experiment_to_run(run, form.cleaned_data["experiment"])
        messages.success(request, f"{form.cleaned_data['experiment'].code} added.")
    elif action == "remove_experiment":
        services.remove_experiment_from_run(run, get_object_or_404(Experiment, pk=request.POST.get("experiment")))
        messages.success(request, "Experiment removed from run.")
    elif action == "targets":
        if run.locked:
            raise PlannerError("The run plan is locked.")
        for rp in run.run_pools.all():
            val = request.POST.get(f"target_{rp.pk}")
            if val not in (None, ""):
                try:
                    rp.target_m_read_pairs = max(float(val.replace(",", ".")), 0)
                except ValueError:
                    raise PlannerError(f"Not a number: {val}")
                rp.save()
        messages.success(request, "Read targets updated.")
    elif action == "assign_indexes":
        form = AssignIndexForm(request.POST)
        if not form.is_valid():
            raise PlannerError("Choose an index set.")
        n = services.assign_indexes(run, form.cleaned_data["index_set"], form.cleaned_data["overwrite"])
        messages.success(request, f"Indexes assigned to {n} libraries.")
    elif action == "submit":
        services.submit_run(run, request.user, comment)
        messages.success(request, "Final plan submitted. A second person must approve it before library prep starts.")
    elif action in ("approve", "reject"):
        so = get_object_or_404(SignOff, pk=request.POST.get("signoff"), run=run)
        services.decide_run(so, request.user, action == "approve", comment)
        messages.success(request, "Final plan approved — library prep can start." if action == "approve"
                         else "Final plan rejected and unlocked.")
    elif action == "reopen":
        services.reopen_run(run, request.user, request.POST.get("reason", ""))
        messages.success(request, "Final plan reopened.")
    elif action == "submitted":
        services.mark_run_submitted(run, request.user)
        messages.success(request, "Marked as submitted to the provider.")
    elif action == "delivered":
        services.mark_data_delivered(run, request.user, comment)
        messages.success(request, "Marked as data delivered.")
    elif action == "deviation":
        services.add_deviation(request.user, request.POST.get("description", ""), run=run)
        messages.success(request, "Deviation recorded.")
    else:
        raise PlannerError("Unknown action.")
    return redirect(run)


@login_required
def run_download(request, pk, kind):
    run = get_object_or_404(SequencingRun, pk=pk)
    final = run.status in (SequencingRun.Status.APPROVED, SequencingRun.Status.SUBMITTED, SequencingRun.Status.DATA_DELIVERED)
    draft = "" if final and not run.amendments.exists() else "DRAFT_"
    if kind == "samplesheet":
        return _download(exports.illumina_samplesheet_v2(run), f"{draft}{run.run_id}_SampleSheet.csv")
    if kind == "provider":
        return _download(exports.provider_csv(run), f"{draft}{run.run_id}_libraries.csv")
    if kind == "barcode_map":
        return _download(exports.barcode_map_run(run), f"{draft}{run.run_id}_barcode_map.csv")
    if kind == "json":
        return _download(exports.run_json(run), f"{draft}{run.run_id}.json", "application/json")
    messages.error(request, "Unknown download.")
    return redirect(run)

