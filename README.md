# AB Group staff app

A self-hosted PWA for AB Group staff. It replaces Frappe HR.

**Stage 1 (this build):** staff log in on their phone and see **only their own** shifts, in a list or calendar view. The shifts are read live from the roster Google Sheet. Admins also see the whole team's roster and a list of problems found in the sheet.

Later stages: clock in/out with PIN and GPS (Stage 2), leave (3), availability (4), and the pay report (5).

```
hr.abgroup.au ─► Traefik (existing) ─► roster-app (FastAPI + PWA) ─► roster-db (Postgres, private network)
                                              └──► Google Sheets API (read-only)
```

## How it works

| Thing | Where it lives |
|---|---|
| Shifts | Google Sheet, one tab per month (`10/2026` or `2026-10`). The app reads this month's and next month's tabs. It never writes to the sheet. |
| Staff, logins, PINs | Postgres. Loaded once from a CSV, then the database is the source of truth. |

- **Login** uses **Staff ID** (e.g. `AB0010`) or email, plus a password, and lets staff see their own roster on their phone. New staff get a temporary password and must change it the first time they log in. Five wrong attempts lock the account for 15 minutes.
- **PIN** is the clock-in credential, issued now and used from Stage 2. Each PIN is unique to one person, so a kiosk can tell who is clocking in from the PIN alone.
- **Privacy:** `/api/roster/me` filters to the logged-in Staff ID on the server. Staff never receive anyone else's shifts or the sheet issues list. Tests enforce this.
- **Fail loud:** if a sheet row has an unknown Staff ID, a bad date or time, or a missing field, that row shows up on the admin **Sheet issues** tab with its tab name and row number. It is never silently dropped.
- **Caching and offline:** the sheet is cached for 2 minutes, and **Refresh** forces a re-read (at most once every 20 seconds). If Google can't be reached, the app shows the last good copy with a banner. If the phone is offline, it shows the last roster it loaded. Logging out wipes that copy.

## The roster sheet

- **One tab per month**, named `MM/YYYY` (e.g. `10/2026`) or `YYYY-MM` (e.g. `2026-10`). Pick one style and stick to it. Two tabs for the same month are flagged and neither is read. Other tabs (e.g. a `Template` tab) are ignored. Copy each new month from the template tab so the columns stay consistent.
- Each shift goes in **its own month's tab only**. A row dated outside its tab's month, or the same shift in both tabs, is flagged on Sheet issues.
- **Row 1 is the header row** (one or two title rows above it are fine). **Column order doesn't matter.** The app matches columns by header name.

| Column | Required | Accepted values |
|---|---|---|
| `Date` | ✅ | `2026-10-28` or `28/10/2026` (day first). A real date cell also works. |
| `Staff ID` | ✅ | `AB0003`. This is the join key, and it must match a staff record. |
| `Shift Start` | ✅ | `05:45`, `17:30`, `5:45 pm` |
| `Shift End` | ✅ | Same as Shift Start. An end time earlier than the start is treated as finishing after midnight, and flagged for checking. |
| `Staff Name` | | Shown to admins only. Matching never uses it. |
| `Department` | | `Gym` / `First Aid` / `Offshore`. Set per row, so Brendan and Aaron can have shifts in both. |
| `Class / Role` | | |
| `Location` | | |
| `Scheduled Hours` | | A decimal, e.g. `1.00`. If blank, it's worked out from the times. It can be less than the shift length (an unpaid break) but not more. |
| `Teaching Hours` | | First Aid only. Can't be more than Scheduled Hours. |
| `Notes` | | Shown to the staff member. |

A month tab with no rows (or no rows for someone) shows **"No shifts scheduled yet"**. Examples are in `sample-roster/`.

**Tip for whoever maintains the sheet:** add a `Staff` tab listing Staff ID and Name. Make the roster's `Staff ID` column a dropdown from that list (**Data → Data validation**), and fill `Staff Name` with a lookup, e.g. `=IFERROR(VLOOKUP(B2, Staff!A:B, 2, FALSE), "")`. Then nobody types IDs by hand, and the name shows who the ID belongs to.

## Deploying on the VPS

1. **DNS:** point `hr.abgroup.au` (A record) at the VPS.
2. **Google service account** (read-only access to the sheet):
   1. Go to [console.cloud.google.com](https://console.cloud.google.com). Create or choose a project and enable the **Google Sheets API**.
   2. Under **IAM & Admin → Service accounts → Create**, name it e.g. `ab-roster-reader`. It doesn't need any roles.
   3. Open the account, go to **Keys → Add key → JSON**, and download the file. Store it in Passbolt.
   4. In the roster sheet, click **Share** and add the service account's email (`…@….iam.gserviceaccount.com`) as a **Viewer**.
3. **On the VPS:**
   ```bash
   git clone git@github.com:brendan524/Roster-PWA.git ~/ab-roster && cd ~/ab-roster
   cp .env.example .env && nano .env                          # values from Passbolt
   mkdir -p secrets && nano secrets/google-service-account.json  # paste the key
   chmod 600 .env secrets/*
   docker compose up -d --build
   docker compose logs -f roster-app                          # wait for "Application startup complete"
   ```
   The compose file assumes your Traefik uses the `websecure` entrypoint and the `mytlschallenge` cert resolver on the `n8n_default` network, like the n8n stack. If your entrypoint has a different name (e.g. `https`), change the one label in `docker-compose.yml`.
4. **Load staff** (one time). Copy `sample-roster/staff.example.csv`, fill it in, then:
   ```bash
   docker compose cp staff.csv roster-app:/tmp/staff.csv
   docker compose exec roster-app python -m app.cli seed-staff /tmp/staff.csv
   docker compose cp roster-app:/tmp/staff.credentials.csv .   # temporary passwords + PINs
   docker compose exec roster-app rm /tmp/staff.csv /tmp/staff.credentials.csv
   ```
   Then make the admins: `docker compose exec roster-app python -m app.cli set-role AB0001 admin`.

   Hand out the logins privately, then **delete `staff.credentials.csv`**. Required columns: `Staff ID`, `Name`, `Location Type` (Onshore/Offshore). Optional: `Email`, `Department`, `Join Date`, `Employment Status`, `Notes`, `Role` (staff/admin), `PIN`, `Password`. Headers are matched ignoring case and spacing, so the `AB-Staff-Seed.csv` layout works as-is. If any row has a problem, nothing is imported and every error is listed. Offshore staff without a join date are imported with a warning; they need one before leave accrual (Stage 3). Re-running the import updates people by Staff ID and keeps their existing passwords, PINs and roles.
5. Open `https://hr.abgroup.au` on a phone, then use **Share → Add to Home Screen** (iOS) or **Install app** (Android).

### Admin commands

```bash
docker compose exec roster-app python -m app.cli list-staff
docker compose exec roster-app python -m app.cli set-role AB0004 admin    # or: ... set-role AB0004 staff
docker compose exec roster-app python -m app.cli reset-password AB0003   # new temp password, logs them out everywhere
docker compose exec roster-app python -m app.cli set-pin AB0003          # new random PIN (add --choose to type one)
docker compose exec roster-app python -m app.cli deactivate AB0003       # can't log in; their shifts get flagged
```

### Backups

Postgres holds staff now, and will hold clock-ins from Stage 2. Set up a nightly `pg_dump` **before Stage 2 goes live**, e.g.:
```bash
docker compose exec -T roster-db pg_dump -U roster roster | gzip > /backups/roster-$(date +%F).sql.gz
```

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
# Postgres on localhost:5433 with user/db "roster" (and a "roster_test" db for tests), then:
export DATABASE_URL=postgresql+psycopg://roster:roster@localhost:5433/roster
export PIN_PEPPER=dev COOKIE_SECURE=false ROSTER_SOURCE=csv ROSTER_CSV_DIR=./sample-roster
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli seed-staff sample-roster/staff.example.csv
.venv/bin/uvicorn app.main:app --reload       # http://localhost:8000
.venv/bin/pytest                              # uses roster_test
```
`ROSTER_SOURCE=csv` reads `sample-roster/YYYY-MM.csv` files instead of Google, so you can try the app without any Google credentials.

## Placeholders to replace

- `static/icons/*`: a generated "AB" mark. Send the real AB logo and these will be regenerated.
