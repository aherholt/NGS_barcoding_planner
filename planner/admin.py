"""Admin back office: reference data, manual corrections and the full version history.

Every change made here is recorded by django-simple-history (button "History").
Locked plans/runs cannot be edited here either — reopen them in the app first.
"""

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    Deviation, Experiment, FlowcellType, IndexPrimer, IndexSet, Pool, RunPool, Sample, SequencingRun, SignOff,
    WellBarcode, WellBarcodeSet,
)


class UsedSetReadOnlyMixin:
    """Reference sets that are already used by an experiment/pool must not change."""

    def _in_use(self, obj):
        return False

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and self._in_use(obj))

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and self._in_use(obj))


class WellBarcodeInline(admin.TabularInline):
    model = WellBarcode
    extra = 0


@admin.register(WellBarcodeSet)
class WellBarcodeSetAdmin(UsedSetReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ["name", "size", "created"]
    inlines = [WellBarcodeInline]

    def _in_use(self, obj):
        return Experiment.objects.filter(well_barcode_set=obj).exists()


class IndexPrimerInline(admin.TabularInline):
    model = IndexPrimer
    extra = 0


@admin.register(IndexSet)
class IndexSetAdmin(UsedSetReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ["name"]
    inlines = [IndexPrimerInline]

    def _in_use(self, obj):
        return Pool.objects.filter(i7__index_set=obj).exists() or Pool.objects.filter(i5__index_set=obj).exists()


@admin.register(FlowcellType)
class FlowcellTypeAdmin(SimpleHistoryAdmin):
    list_display = ["name", "output_m_read_pairs", "price_eur", "active"]


class LockedExperimentMixin:
    def _experiment(self, obj):
        return obj.experiment

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and self._experiment(obj).plan_locked)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and self._experiment(obj).plan_locked)


@admin.register(Experiment)
class ExperimentAdmin(SimpleHistoryAdmin):
    list_display = ["code", "title", "library_type", "status", "requested_m_read_pairs", "lysis_date", "sharepoint_item_id"]
    list_filter = ["status", "library_type"]
    search_fields = ["code", "title"]
    readonly_fields = ["status", "planning_seed", "sharepoint_push_pending"]


@admin.register(Sample)
class SampleAdmin(LockedExperimentMixin, SimpleHistoryAdmin):
    list_display = ["sample_id", "experiment", "source_plate", "source_well", "pool", "well_barcode"]
    list_filter = ["experiment"]
    search_fields = ["sample_id"]


@admin.register(Pool)
class PoolAdmin(SimpleHistoryAdmin):
    """Indexes may be corrected here while no run containing the pool is locked."""

    list_display = ["pool_id", "experiment", "i7", "i5"]
    list_filter = ["experiment"]

    def _locked(self, obj):
        return obj.run_links.filter(run__status__in=SequencingRun.LOCKED_STATUSES).exists()

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and self._locked(obj))

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and (self._locked(obj) or obj.experiment.plan_locked))


@admin.register(SequencingRun)
class SequencingRunAdmin(SimpleHistoryAdmin):
    list_display = ["run_id", "flowcell_type", "status", "planned_submission_date"]
    readonly_fields = ["status"]

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and obj.locked)


@admin.register(RunPool)
class RunPoolAdmin(SimpleHistoryAdmin):
    list_display = ["run", "pool", "target_m_read_pairs"]

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and obj.run.locked)


class AppendOnlyAdmin(admin.ModelAdmin):
    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SignOff)
class SignOffAdmin(AppendOnlyAdmin):
    list_display = ["step", "experiment", "run", "state", "submitted_by", "submitted_at", "decided_by", "decided_at"]
    list_filter = ["step", "state"]

    def has_add_permission(self, request):
        return False


@admin.register(Deviation)
class DeviationAdmin(AppendOnlyAdmin):
    list_display = ["created_at", "experiment", "run", "created_by", "description"]
