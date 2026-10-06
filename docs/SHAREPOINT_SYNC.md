# SharePoint sync and email notifications

## Principle: every field has one owner

| Data | Entered in | Copied to |
|---|---|---|
| Title, people, ELN links, dates, library type, read lengths, requested reads, product/insert length, complexity | SharePoint (scientist) | app (every 5 min) |
| Status, NGS run ID, date of submission | app | SharePoint |

Emails are sent by **Power Automate** when the **Status** column changes. The app never sends mail
itself, so recipients and texts are maintained in one place.

## 1. Recommended changes to the SharePoint list

| Column | Change |
|---|---|
| Requested read length (R1 and R2) – multiple lines of text | replace by four **Number** columns: `R1 length`, `R2 length`, `i7 length`, `i5 length` |
| Requested Data Output (in M Reads) | keep; column description: "total for the experiment, **million read pairs**" |
| NGS run ID – Choice | change to **Single line of text**; written by the app |
| Status – Choice | choices exactly as the app labels: Submitted, Accepted – in planning, Assigned to NGS run, Final plan in review, Plan approved – ready for library prep, Plan reopened – amendment, Library prep done, Submitted to provider, Data delivered, On hold, Cancelled |
| Sample number (per pool), Number of pools, Total | keep as planning estimates; the real pools live in the app |
| new: Data delivery deadline – Date | used for run planning |

## 2. App registration (done once by an M365 admin)

1. Entra admin center → **App registrations → New registration** → name `NGS planner sync`.
2. **API permissions → Microsoft Graph → Application permissions → `Sites.Selected`** → *Grant admin consent*.
   `Sites.Selected` gives access to **only the sites you allow**, not the whole tenant.
3. Allow the app on your site (admin, once — e.g. in Graph Explorer as admin):
   `POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions` with body
   `{"roles":["write"],"grantedToIdentities":[{"application":{"id":"<client-id>","displayName":"NGS planner sync"}}]}`
4. **Certificates & secrets → New client secret** — copy the value (shown once).
5. Find the IDs (Graph Explorer, signed in as you):
   * site: `GET https://graph.microsoft.com/v1.0/sites/<tenant>.sharepoint.com:/sites/<SiteName>` → `id`
   * list: `GET https://graph.microsoft.com/v1.0/sites/<site-id>/lists?$select=id,displayName` → `id`

Put the values into `.env`: `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`,
`SHAREPOINT_SITE_ID`, `SHAREPOINT_LIST_ID`.

## 3. Map the columns

SharePoint shows display names, but the API uses **internal names** (e.g. spaces become `_x0020_`,
and names renamed after creation keep their original internal name).

```powershell
python manage.py sharepoint_columns
```

Copy the internal names into `config/sharepoint_fields.json`. Also check the Choice values of
"Library type" and "Library complexity" in that file.

## 4. Test, then switch on

```powershell
python manage.py sync_sharepoint --dry-run   # shows NEW/UPD lines, changes nothing
python manage.py sync_sharepoint             # pull + push pending statuses
```

On the server, the `sharepoint-sync` container in `docker-compose.yml` runs this every 5 minutes.
If a status write-back fails (network, permissions) the experiment stays "push pending" and is
retried on the next sync.

## 5. Power Automate flow (emails)

1. Trigger: **When an item is created or modified** (your list).
2. Action: **Get changes for an item or a file (properties only)** — Since: *Trigger Window Start Token*.
3. Condition: `Has Column Changed: Status` is true. (Without this, every edit would send an email.)
4. Switch on **Status**:
   * Submitted → email Library prep + NGS orga
   * Accepted – in planning → email scientist (Responsible Person_Assay) + Library prep
   * Assigned to NGS run → email NGS orga (+ link to run ID)
   * Final plan in review → email NGS orga + Library prep ("please approve")
   * Plan approved – ready for library prep → email Library prep ("start library prep")
   * Plan reopened – amendment → email Library prep + NGS orga ("do not start library prep of this experiment")
   * Library prep done → email NGS orga
   * Submitted to provider / Data delivered → email scientist (+ run ID)
5. Enable versioning on the list (List settings → Versioning) — required for "Get changes".

## Not done yet

The sync has been written against the Graph documentation but not tested against your tenant.
Expect to adjust the column mapping on the first try; the dry run is safe.
