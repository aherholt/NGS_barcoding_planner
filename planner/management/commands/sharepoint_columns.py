"""Print the SharePoint list columns (display name -> internal name) to fill config/sharepoint_fields.json."""

from django.core.management.base import BaseCommand, CommandError

from planner import sharepoint


class Command(BaseCommand):
    help = "List the internal column names of the SharePoint intake list."

    def handle(self, **_):
        if not sharepoint.configured():
            raise CommandError("SharePoint is not configured (see docs/SHAREPOINT_SYNC.md).")
        for col in sharepoint.GraphClient().columns():
            if col.get("readOnly") and col.get("name") not in ("Title",):
                continue
            self.stdout.write(f"{col.get('displayName', ''):45s} {col.get('name')}")
