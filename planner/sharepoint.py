"""SharePoint list <-> planner sync through Microsoft Graph.

Direction of data (each field has ONE owner):
  SharePoint → app : what the scientist enters (title, people, dates, read lengths, reads …)
  app → SharePoint : Status, NGS run ID, submission date  (Power Automate sends emails on Status changes)

Authentication: an Entra ID app registration with the application permission
`Sites.Selected` (granted to this one site) and a client secret. See docs/SHAREPOINT_SYNC.md.

NOTE: this module is written against the Graph API documentation but has not been
tested against your tenant yet. Start with `python manage.py sharepoint_columns` and
`python manage.py sync_sharepoint --dry-run`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.dateparse import parse_datetime

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
FIELD_MAP_FILE = Path(settings.BASE_DIR) / "config" / "sharepoint_fields.json"
TEXT_FIELDS = {"title", "description", "eln_assay_url", "eln_libprep_url"}


def configured() -> bool:
    sp = settings.SHAREPOINT
    return all(sp.get(k) for k in ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET", "SITE_ID", "LIST_ID"))


def field_map() -> dict:
    return json.loads(FIELD_MAP_FILE.read_text(encoding="utf-8"))


class GraphClient:
    def __init__(self):
        self.sp = settings.SHAREPOINT
        self._token, self._expires = None, 0.0

    def token(self) -> str:
        if not self._token or time.time() > self._expires - 60:
            r = requests.post(
                f"https://login.microsoftonline.com/{self.sp['TENANT_ID']}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": self.sp["CLIENT_ID"],
                      "client_secret": self.sp["CLIENT_SECRET"], "scope": "https://graph.microsoft.com/.default"},
                timeout=30)
            r.raise_for_status()
            body = r.json()
            self._token, self._expires = body["access_token"], time.time() + body.get("expires_in", 3600)
        return self._token

    def request(self, method: str, url: str, **kw):
        if not url.startswith("http"):
            url = GRAPH + url
        r = requests.request(method, url, headers={"Authorization": f"Bearer {self.token()}"}, timeout=30, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    def list_base(self) -> str:
        return f"/sites/{self.sp['SITE_ID']}/lists/{self.sp['LIST_ID']}"

    def columns(self) -> list[dict]:
        return self.request("GET", f"{self.list_base()}/columns").get("value", [])

    def items(self) -> list[dict]:
        url, out = f"{self.list_base()}/items?expand=fields&$top=200", []
        while url:
            body = self.request("GET", url)
            out += body.get("value", [])
            url = body.get("@odata.nextLink")
        return out

    def user_email(self, lookup_id) -> str:
        """Person columns come as '<Name>LookupId'; resolve via the site's hidden User Information List."""
        body = self.request("GET", f"/sites/{self.sp['SITE_ID']}/lists('User Information List')/items/{lookup_id}?expand=fields")
        return (body.get("fields") or {}).get("EMail", "")

    def update_fields(self, item_id: int, fields: dict):
        return self.request("PATCH", f"{self.list_base()}/items/{item_id}/fields", json=fields)


# --------------------------------------------------------------------------- #
def _user_for(client: GraphClient, fields: dict, internal: str, cache: dict):
    lookup = fields.get(f"{internal}LookupId")
    if not lookup:
        return None
    if lookup not in cache:
        email = client.user_email(lookup).lower()
        User = get_user_model()
        user = User.objects.filter(email__iexact=email).first() if email else None
        if email and not user:
            user = User.objects.create(username=email, email=email, is_active=True)
            user.set_unusable_password()
            user.save()
        cache[lookup] = user
    return cache[lookup]


def _code_for(title: str, item_id: int) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:16] or "EXP"
    return f"SP{item_id}-{slug}"


def item_to_values(client: GraphClient, item: dict, fmap: dict, user_cache: dict) -> dict:
    f = item.get("fields", {})
    fields = fmap["fields"]
    values = {}
    for app_field, internal in fields.items():
        if app_field.startswith("responsible_"):
            values[app_field] = _user_for(client, f, internal, user_cache)
            continue
        raw = f.get(internal)
        if isinstance(raw, dict):  # hyperlink columns
            raw = raw.get("Url", "")
        if app_field == "library_type":
            raw = fmap.get("library_type_choices", {}).get(raw, "other") if raw else "other"
        elif app_field == "library_complexity":
            raw = fmap.get("complexity_choices", {}).get(str(raw).lower(), "normal") if raw else "normal"
        elif app_field in ("lysis_date", "delivery_deadline") and raw:
            raw = parse_datetime(raw).date() if "T" in str(raw) else raw
        elif app_field in ("r1_length", "r2_length", "i7_length", "i5_length", "avg_product_length", "avg_insert_length") and raw not in (None, ""):
            raw = int(float(raw))
        elif app_field == "requested_m_read_pairs" and raw not in (None, ""):
            raw = float(raw)
        if raw in (None, ""):
            raw = "" if app_field in TEXT_FIELDS else None
        values[app_field] = raw
    return values


@transaction.atomic
def pull(dry_run: bool = False) -> list[str]:
    """Create/update experiments from the SharePoint list. Returns a log of what happened."""
    from .models import Experiment
    client, fmap, cache, report = GraphClient(), field_map(), {}, []
    for item in client.items():
        item_id = int(item["id"])
        values = item_to_values(client, item, fmap, cache)
        exp = Experiment.objects.filter(sharepoint_item_id=item_id).first()
        if exp is None:
            report.append(f"NEW  #{item_id} {values.get('title')}")
            if not dry_run:
                Experiment.objects.create(sharepoint_item_id=item_id, code=_code_for(values.get("title") or "", item_id),
                                          **{k: v for k, v in values.items() if v is not None})
            continue
        changed = {k: v for k, v in values.items() if v is not None and getattr(exp, k) != v}
        if exp.plan_locked:
            # after plan submission, intake changes must not silently alter a locked plan's context
            changed = {k: v for k, v in changed.items() if k in ("description", "eln_assay_url", "eln_libprep_url",
                                                                 "responsible_assay", "responsible_libprep", "responsible_ngs",
                                                                 "delivery_deadline")}
        if changed:
            report.append(f"UPD  #{item_id} {exp.code}: {', '.join(sorted(changed))}")
            if not dry_run:
                for k, v in changed.items():
                    setattr(exp, k, v)
                exp.save()
    return report


def push_experiment(experiment) -> None:
    from .models import RunPool
    wb = field_map()["write_back"]
    run = (RunPool.objects.filter(pool__experiment=experiment).exclude(run__status="cancelled")
           .select_related("run").order_by("-run__created").first())
    fields = {wb["status"]: experiment.get_status_display()}
    from .services import next_step
    label, owner = next_step(experiment)
    if wb.get("next_step"):
        fields[wb["next_step"]] = label
    if wb.get("next_step_owner_email"):
        fields[wb["next_step_owner_email"]] = (owner.email if owner else "") or ""
    if wb.get("next_step_owner_name"):
        fields[wb["next_step_owner_name"]] = (owner.get_full_name() or owner.username) if owner else ""
    if run:
        fields[wb["run_id"]] = run.run.run_id
        if run.run.planned_submission_date and wb.get("submission_date"):
            fields[wb["submission_date"]] = run.run.planned_submission_date.isoformat()
    GraphClient().update_fields(experiment.sharepoint_item_id, fields)


def push_experiment_safely(experiment) -> bool:
    """Called on every status change. Never breaks the app; failures stay 'pending' and are retried by sync_sharepoint."""
    if not configured() or not experiment.sharepoint_item_id:
        return False
    try:
        push_experiment(experiment)
    except Exception as exc:  # network, permissions, wrong column name …
        log.warning("SharePoint push failed for %s: %s", experiment, exc)
        return False
    type(experiment).objects.filter(pk=experiment.pk).update(sharepoint_push_pending=False)
    experiment.sharepoint_push_pending = False
    return True


def push_pending() -> list[str]:
    from .models import Experiment
    report = []
    for exp in Experiment.objects.filter(sharepoint_push_pending=True).exclude(sharepoint_item_id=None):
        ok = push_experiment_safely(exp)
        report.append(f"{'OK  ' if ok else 'FAIL'} {exp.code} → {exp.get_status_display()}")
    return report
