"""Pull new/changed experiments from the SharePoint list and push pending status changes.

Run it every few minutes (Windows Task Scheduler or a cron job in Docker):
  python manage.py sync_sharepoint            # pull + push
  python manage.py sync_sharepoint --dry-run  # show what would change, change nothing
  python manage.py sync_sharepoint --push-only
"""

from django.core.management.base import BaseCommand, CommandError

from planner import sharepoint


class Command(BaseCommand):
    help = "Sync experiments with the SharePoint intake list (Microsoft Graph)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--push-only", action="store_true")

    def handle(self, dry_run=False, push_only=False, **_):
        if not sharepoint.configured():
            raise CommandError("SharePoint is not configured. Set GRAPH_* and SHAREPOINT_* variables (see docs/SHAREPOINT_SYNC.md).")
        if not push_only:
            for line in sharepoint.pull(dry_run=dry_run) or ["no changes"]:
                self.stdout.write(f"pull: {line}")
        if not dry_run:
            for line in sharepoint.push_pending() or ["nothing pending"]:
                self.stdout.write(f"push: {line}")
