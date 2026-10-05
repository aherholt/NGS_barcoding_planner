from django import template

register = template.Library()

OK = {"plan_approved", "libprep_done", "run_planned", "submitted_to_provider", "data_delivered",
      "approved", "submitted"}
WARN = {"plan_in_review", "in_review", "on_hold", "pending"}
ERR = {"cancelled", "rejected"}


@register.filter
def status_class(status: str, kind: str = "") -> str:
    # experiment status "submitted" means "new request", a run's "submitted" means "at provider"
    if status == "submitted" and kind == "experiment":
        return ""
    return "ok" if status in OK else "warn" if status in WARN else "err" if status in ERR else ""


@register.filter
def get(d, key):
    return d.get(key) if hasattr(d, "get") else None
