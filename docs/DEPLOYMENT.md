# Deployment on the lab server

## What runs

| Container | Purpose |
|---|---|
| `db` | PostgreSQL database (all data + audit trail) |
| `app` | the web app (gunicorn, port 8000) |
| `sharepoint-sync` | pulls the intake list / pushes statuses every 5 minutes |

## Steps (server with Docker installed; PowerShell or Linux shell)

```powershell
git clone https://github.com/aherholt/NGS_barcoding_planner.git
cd NGS_barcoding_planner
Copy-Item .env.example .env          # then edit .env: secret key, password, host name
# generate a secret key:
python -c "import secrets; print(secrets.token_urlsafe(50))"
docker compose up -d --build
docker compose exec app python manage.py createsuperuser
docker compose exec app python manage.py import_reference well-barcodes "WBC-24 v1" /app/data/well_barcodes.csv
```

(Put the primer files into `data/` before `--build`, or copy them in with `docker compose cp`.)

## HTTPS

Microsoft login needs HTTPS. Put a reverse proxy in front of port 8000 — e.g. IIS with URL Rewrite on
Windows, or Caddy/nginx — with your internal certificate, and set `DJANGO_HTTPS=1`,
`DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` to the host name.

## Backups (required for the audit trail)

Daily database dump, kept off the server:

```powershell
docker compose exec -T db pg_dump -U planner planner > "backup_$(Get-Date -Format yyyy-MM-dd).sql"
```

Schedule it with Windows Task Scheduler (or cron) and copy the file to your backup location.
Test a restore once: `Get-Content backup.sql | docker compose exec -T db psql -U planner planner`.

## Updates

```powershell
git pull
docker compose up -d --build        # migrations run automatically on start
```

Check `CHANGELOG.md` first; run `pytest` on a test machine before updating production.
