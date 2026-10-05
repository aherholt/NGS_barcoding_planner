# Microsoft (Entra ID) login

Without this, people log in with a username/password created in Admin → Users. With it, they click
"Sign in with Microsoft" and use their M365 account. Sign-offs are then tied to the real identity.

1. Entra admin center → **App registrations → New registration** → `NGS planner login`.
   * Supported accounts: *this organizational directory only*.
   * Redirect URI (Web): `https://<your-planner-host>/oidc/callback/`
     (Microsoft requires HTTPS, except for `http://localhost`.)
2. **Certificates & secrets → New client secret**.
3. **Token configuration → Add optional claim → ID → email**.
4. In `.env`:
   ```
   ENTRA_TENANT_ID=<Directory (tenant) ID>
   OIDC_RP_CLIENT_ID=<Application (client) ID>
   OIDC_RP_CLIENT_SECRET=<secret value>
   DJANGO_HTTPS=1
   ```
5. Restart the app. Users are matched by e-mail — the same accounts the SharePoint sync creates
   from the "Responsible Person" columns.
6. Give yourself admin rights once: `python manage.py shell -c "from django.contrib.auth.models import User; u=User.objects.get(email='you@company.com'); u.is_staff=u.is_superuser=True; u.save()"`
