# Buildout Pros: Lien Waivers & Cost to Complete

This app replaces the Zapier + Smartsheet lien waiver automation. It keeps its own database as the source of record. Smartsheet is input only, and **the app never writes to Smartsheet**: the client only issues GET requests and refuses any other verb.

## Status: Phases 1 and 2 of 5

| Phase | Scope | State |
|---|---|---|
| 1 | Smartsheet sync, Projects picker, data model, vendor matching + Unmatched queue, CTC engine + report, read-only dashboard, Google sign-in, Railway deploy files | **Done** |
| 2 | Waiver decision engine, template registry, PDF filling + `calibrate`, Waivers page | **Done** |
| 3 | Mailboxes (Google sign-in per mailbox), Email center, outbound requests, inbound matching, verification, reminders | Next |
| 4 | Contact logging (contracts@, ap@ via wwestmoreland@), supplier disclosure for major trades | |
| 5 | Payment status view (warn only), auto-send switches, deployment runbook | |

## How it works (Phase 1)

- **Sync** (every 15 minutes by default, plus **Sync now**). It reads INTAKE and Archived Projects in full so the picker stays current. Subcontracts, change orders and AR/AP payables are stored **only for tracked projects**. Every run is logged on the Sync log page.
- **Columns are matched by ID** (`app/smartsheet/columns.yaml`). If a column is deleted or recreated, the sync stops with an error naming it.
- **Projects page** lists INTAKE projects in status 7, 8 or 9, plus anything already tracked. Tracking is off by default. Turning it on loads the project's history as a *backfill*: status changes already in place are recorded as history and will never trigger emails. Every track and untrack goes in the audit log.
- **Vendor matching.** Each payable is resolved to a subcontract by (vendor, project number). It is not joined on the "Vendor - Project" text. The project name is cut off the end of that text, so a vendor name that contains a hyphen survives. A near match on the project name is accepted for typos like "CCDPC". Names are normalized for case, punctuation and Inc/LLC/Co. Known variants (Ianuzzi/Iannuzzi, Crown/Crowne Coverings) are seeded as aliases. Anything that doesn't match exactly one subcontract goes to the **Unmatched queue**, where a person can:
  - map the spelling to a vendor (saved as an alias for all future rows),
  - assign the row to one subcontract (when a vendor has two on a job), or
  - mark the vendor as a **direct purchase** on that job or all jobs. This is for material houses billed with no subcontract; those rows don't need waivers.
- **Status changes** (approved, paid, hold, rejected) are recorded as `payable_events`. Each is unique per payable and kind, so re-running a sync never duplicates them. Phase 2's waiver engine reads these instead of "Paid Date = today".
- **CTC** is computed by the app, not taken from the Smartsheet formulas. It uses only approved Internal COs. Hold and Rejected rows are excluded, and Hold is reported separately. It can be computed **as of any payable** (by date received, then row ID), which the waiver decision needs. Over-billed and zero-contract subs go to the Review queue, assigned to the default reviewer.
- **Report**: by job, by sub and by PM, exported as CSV or XLSX (one tab per grouping).

### Checked against live data

The CTC engine was run on real Smartsheet rows for P--IL-102125-1 (IMAGE Studios, Palatine), pulled read-only. It matched Smartsheet's Cost to Complete on 11 of 12 subs. The 12th, Iannuzzi Millwork, shows **$0** in the app and **$6,764** in Smartsheet. The subcontract is spelled "Ianuzzi" and the AP row "Iannuzzi", so Smartsheet's formula never counted the $6,764 that was billed and paid. That job's 16 other AP rows (BP Environmental Services, Palatine Builders Supply, Armorshield, GPRS, Foundation Building Materials) have no subcontract, and the Unmatched queue lists them until each vendor is marked once as a direct purchase. One approved Internal CO ($649.53, "Additional Lighting Controls") has no sub selected in Smartsheet, so it also shows in the Unmatched queue.

## How it works (Phase 2)

- **Triggers.** After each sync, every new `approved` or `paid` event on a tracked, matched payable runs through the rules. Events loaded as history (backfill) never create waivers. Events on unmatched rows wait until the row is mapped, so nothing is lost.
- **Rules** (`app/waivers/decision.py`, CTC as of that payable):
  - Approved, CTC after > 0: nothing yet (conditional partials are off). Turning them on needs a conditional partial template.
  - Approved, CTC after = 0: **Conditional final** for that invoice. Pending Internal COs and invoices on Hold for the sub are listed as EXCEPTIONS, and a review item asks the PM to confirm.
  - Paid: **Unconditional partial** for the amount paid, or **Unconditional final** if the CTC after it is 0.
  - Approved and paid in the same sync: only the unconditional waiver.
  - Retention sub hits $0 on an invoice that isn't the retention invoice: **Review** (retention check), no waiver. Marking the invoice as the retention invoice on the sub's page re-runs the rules.
  - Over-billed, no contract value, or invoice on Hold: **Review**, no waiver.
  - Each (payable, type) can have only one waiver, so re-running never duplicates.
- **Templates** (`templates/pdf/registry.yaml`) are keyed by (state, type). **Every state uses the same three Buildout Pros forms for now** (`statutory_states: []`). The state mechanism is kept for later: list a state under `statutory_states` and waivers on its jobs are **blocked** ("Statutory form required for {STATE}; add template.") until a form is registered for it. To add one, put the PDF in `templates/pdf/<STATE>/`, add an entry, tune the positions with `lw calibrate`, and click **Reload registry and recheck** on Settings → Waiver templates.
- **PDFs** are filled by drawing on top of the flat forms (reportlab + pypdf). The app fills: project name, project address, amount, through date (partials), exceptions (conditional final), today's date, the sub's company name, and "Buildout Pros LLC" on the unconditional final, where the form left it blank. The sub's address, signature, printed name and the notary block stay blank for the sub.
- **Waivers page**: filter by status, project and type. Preview the PDF, change the through date (it defaults to Date Received and is flagged as a fallback), edit exceptions, regenerate, mark sent, upload the signed copy, accept or reject, void. Edits are only allowed before sending. Every change is added to the waiver's history; nothing is overwritten.

## Running locally

```bash
uv venv -p 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
cp .env.example .env     # set SMARTSHEET_TOKEN; leave GOOGLE_CLIENT_ID empty to use DEV_AUTH_EMAIL
.venv/bin/lw migrate
.venv/bin/uvicorn app.main:app --reload
.venv/bin/pytest
```

Or with Docker and Postgres: `docker compose up --build`.

CLI (`lw`):

| Command | What it does |
|---|---|
| `lw migrate` | Create or upgrade the schema |
| `lw sync` | One sync now |
| `lw track P--IL-102125-1 ...` | Turn tracking on from the command line |
| `lw backfill [--project N]` | Reload history for tracked projects (no waivers, no emails), print CTC per sub, and list waivers the rules say were owed but that neither the app nor Smartsheet's "Waiver Created" column has |
| `lw calibrate [key] [--out-dir .]` | Write test fills with a coordinate grid for tuning field positions |
| `lw report --out ctc.xlsx` | Write the CTC report |

## Deploying to Railway

1. Push this repo to GitHub. In Railway: **New Project → Deploy from GitHub repo**. Railway builds from the `Dockerfile` (`railway.toml` sets the health check to `/health` and the service to one replica).
2. **+ New → Database → PostgreSQL**. In the app service's Variables, add `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`.
3. App service → **Settings → Volumes → Add volume**, mount path `/data`. Generated and signed waiver PDFs are stored there; without the volume they'd be lost on every deploy.
4. App service → **Settings → Networking → Generate domain** (or add a custom domain). Set `APP_BASE_URL` to that URL, e.g. `https://lienwaivers.up.railway.app`.
5. Variables: `SECRET_KEY`, `SMARTSHEET_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `TIMEZONE=America/Chicago`, and in later phases `ANTHROPIC_API_KEY` and `FERNET_KEY`. Do **not** set `DEV_AUTH_EMAIL` in production.
6. Deploy. The container runs `lw migrate` and then starts the web server, and the scheduler runs inside it. Keep the service at one replica; a database lock also stops two syncs from running at once.

## Google sign-in setup (one time)

Phase 1 only needs sign-in (`openid email profile`). In Phase 3 the same OAuth client is reused to connect mailboxes with Gmail scopes.

With the gcloud CLI, signed in as a Workspace admin or a user allowed to create projects:

```bash
gcloud projects create buildoutpros-lienwaivers --name="Lien Waivers" --organization=<ORG_ID>   # gcloud organizations list
gcloud config set project buildoutpros-lienwaivers
gcloud services enable gmail.googleapis.com     # needed in Phase 3; harmless now
```

Then in the console (these steps have no gcloud equivalent):

1. Open https://console.cloud.google.com/auth/overview?project=buildoutpros-lienwaivers and click **Get started**.
   - App name: `Buildout Pros Lien Waivers`. Support email: your address. Click **Next**.
   - Audience: **Internal**. Only @buildoutpros.com accounts can sign in, and Google's app verification isn't needed.
   - Contact email: your address. Agree and click **Create**.
2. **Clients → Create client**. Type **Web application**, name `lien-waivers-web`.
   - Authorized redirect URIs: `https://<your Railway domain>/auth/callback` and `http://localhost:8000/auth/callback`.
   - Click **Create**, then copy the client ID and secret into `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
3. **Data access → Add or remove scopes**: `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`. Phase 3 adds `gmail.modify`, `gmail.send` and `gmail.readonly` here.

If the Workspace admin restricts third-party apps, trust the client once. In https://admin.google.com go to **Security → Access and data control → API controls → Manage Third-Party App Access → Add app → OAuth App Name Or Client ID**. Paste the client ID, select the app, choose **All users in buildoutpros.com** (or the right OU), set **Trusted**, and click **Finish**.

## Roles

Anyone with a @buildoutpros.com Google account can sign in. Accounts in `ADMIN_EMAILS` start as **Admin**; everyone else starts as **PM**, which is read only in Phase 1. An Admin can change roles on the Settings page. **AP** and **Admin** can track projects, run Sync now and work the queues. Only Admin can change settings.

## Layout

```
app/
  smartsheet/   client.py (read only), columns.yaml (column IDs), sync.py
  matching/     normalize.py, resolver.py (aliases, direct purchases, Unmatched queue)
  ctc/          engine.py (point-in-time CTC), report.py (CSV/XLSX)
  services/     projects, audit, settings, locks, sync runner
  jobs/         scheduler.py (APScheduler, in process)
  web/          routes.py, templates/, static/
  auth.py       Google sign-in, roles
  cli.py        lw command
alembic/        migrations
tests/          fixtures built from real rows with names anonymized
```
