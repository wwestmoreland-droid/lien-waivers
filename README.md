# Buildout Pros: Lien Waivers & Cost to Complete

This app replaces the Zapier + Smartsheet lien waiver automation. It keeps its own database as the source of record. Smartsheet is input only, and **the app never writes to Smartsheet**: the client only issues GET requests and refuses any other verb.

## Status: Phases 1–4a of 5

| Phase | Scope | State |
|---|---|---|
| 1 | Smartsheet sync, Projects picker, data model, vendor matching + Unmatched queue, CTC engine + report, read-only dashboard, Google sign-in, Railway deploy files | **Done** |
| 2 | Waiver decision engine, template registry, PDF filling + `calibrate`, Waivers page | **Done** |
| 3 | Mailboxes (Google sign-in per mailbox), Email center, outbound requests, inbound matching, verification, reminders | **Done** |
| 4a | Contact logging (contracts@, ap@ via wwestmoreland@), New contacts queue, Contacts page, billing-email suggestions, 12-month backfill | **Done** |
| 4b | Supplier disclosure for major trades | Next |
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

## How it works (Phase 3: email)

- **Mailboxes** (Settings → Mailboxes): click **Connect a Google mailbox as Sender** and sign in *as* contracts@. Click **Connect a read-only source** and sign in as wwestmoreland@, then set "Only read mail addressed to" to `ap@buildoutpros.com`, so only forwarded ap@ mail is read. Tokens are stored encrypted in the database (key from `FERNET_KEY`, or derived from `SECRET_KEY`). Changing the Sender moves new mail to the new mailbox; threads already in progress keep replying from the mailbox that started them ("Listening"). If Google stops accepting a mailbox's sign-in, a red banner appears on every page and outgoing mail queues until it's reconnected.
- **Outbound.** Each new waiver gets a request email drafted in the **Outbox** with the PDF attached:
  - To: the sub's billing email. CC: the sub's PM and estimator (from All Subcontracts).
  - Subject ends in `[LW-{id}]`.
  - Nothing goes out until someone clicks **Send** or **Send all selected**. Settings → Email can switch individual types to auto-send.
  - Threads the app sends get the Gmail label `Lien Waivers`. Nothing is archived, moved or deleted.
- **Templates** (`templates/email/`) are the fixed texts from the spec. Edits made on Settings → Email are stored in the database and checked against the writing rules. Payment gating is warn-only, so the `[if gate]` "payment is on hold" lines never appear. The conditional final keeps "We'll release payment as soon as we have everything."
- **Inbound** (every sync interval, plus **Check mail now**). Each mailbox is read incrementally (Gmail history). Waiver-related mail is kept: replies in known threads, `[LW-…]` tokens, and mail from a vendor on a tracked job that has an attachment or mentions a lien or waiver. Matching goes by thread, then token, then sender when the vendor has exactly one waiver waiting; anything else goes to the **Unmatched inbox**.
  - A PDF or image reply marks the waiver Received and runs the automatic check.
  - Out-of-office replies and replies with no attachment are logged only.
  - A bounce marks the email Bounced, flags the vendor, and opens a review item listing the vendor's other addresses.
- **Read-only source.** Mail there with a token, or a waiver-looking attachment from a tracked vendor, shows as **Found in ap@**. A person links it and clicks **File as signed copy**. The mailbox itself is never changed.
- **Verification.** When `ANTHROPIC_API_KEY` is set, Claude (`claude-opus-5-5`, with the server-side refusal fallback on) reads the returned PDF or image and reports what it found: signed, notarized, form type, amount, through date, project. The app compares that with the request. The result appears in Queues → Verification and on the waiver page as a guide. A person always clicks Accept, or Reject, which drafts the "please correct" reply in the same thread.
- **Reminders** run at 8:00 AM Central on weekdays only (anything due on a weekend goes out Monday). By default: day 7 (waiver attached again) and day 10 go to the sub in the same thread, and day 13 is an internal escalation to the job's PM and estimator from INTAKE. Checks are mailed, so subs get a week before the first nudge. The days can be changed on Settings → Email. All go to the Outbox unless set to auto-send. **Daily digest**: `/digest`.
- **Email center** (`/email`): Inbox, Outbox, Sent; filters, search, read/handled per user; reply, reply all and forward with a template picker; **see all contracts@ mail** reads the whole mailbox live from Gmail, and any message can be brought in and linked.

### Contacts (Phase 4a)
- Every new message in a connected mailbox is checked for contacts: names from From, Reply-To, To and Cc; phone, mobile, title and company from the sender's signature (rules first; Claude only when the rules can't read a new or changed signature). The email body isn't kept for contacts.
- Skipped: @buildoutpros.com, no-reply and notification addresses, bulk mail (List-Unsubscribe), calendar invites, auto-replies and bounces. For a read-only source with "only mail addressed to" set, only that mail is read.
- Vendor match order: address already on file (auto-confirmed), the waiver thread, the company's email domain (never gmail.com and the like), the company name, else Unassigned. Everything else waits in **Queues → New contacts**.
- Contacts in All Subcontracts are added as confirmed on each sync.
- On a sub's page, a confirmed billing contact that differs from where waivers go shows as a suggested update; one click switches waiver emails to it (Smartsheet isn't changed).
- **Backfill**: Settings → Mailboxes → "Start tonight" reads the last 12 months, headers first, a slice per minute under Gmail's quota, 8 PM to 6 AM by default. It saves progress and resumes after a restart.

### Gmail setup (one time, in the same Google Cloud project as sign-in)

1. **APIs & Services → Library → Gmail API → Enable** (or `gcloud services enable gmail.googleapis.com`).
2. **Google Auth Platform → Data access → Add or remove scopes**: add `.../auth/gmail.modify`, `.../auth/gmail.send` and `.../auth/gmail.readonly`. The app is Internal, so Google doesn't review these.
3. If the Workspace admin restricts third-party access to Gmail, trust the client ID once in admin.google.com (Security → Access and data control → API controls → Manage Third-Party App Access).
4. In Railway, add `ANTHROPIC_API_KEY` to turn on the automatic check (optional; without it, returned copies are checked by eye).

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
3. **Add a volume for the PDFs.** On the project canvas (not inside the service), right-click empty space → **Volume**, or press ⌘K / Ctrl+K and type "volume". Attach it to the app service (not Postgres) with mount path `/data`, then click **Deploy**. Generated and signed waiver PDFs are stored there; without the volume they'd be lost on every deploy. No variable is needed: the Dockerfile already sets `DATA_DIR=/data`.
4. App service → **Settings → Networking → Public Networking → Generate Domain**. When asked for the port, enter **8080**: Railway sets `PORT` (8080) and the start script listens on it. Set `APP_BASE_URL` to the generated URL with `https://` and no trailing slash, e.g. `https://lien-waivers-production.up.railway.app`. Check `https://<domain>/health` returns `{"status":"ok", ...}`.
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
