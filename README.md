# AB Group staff app

A self-hosted PWA for AB Group staff. It replaces Frappe HR.

**Stage 1:** staff log in on their phone and see **only their own** shifts, in a list or calendar view. The shifts are read live from the roster Google Sheet. Admins also see the whole team's roster and a list of problems found in the sheet.

**Stage 2:** clock in/out with a PIN, on a personal phone (with GPS) or at an office kiosk. Works offline. Admins get a daily **Clock** view with late, early, missing and unrostered flags, plus corrections with an audit trail.

**Stage 3a:** a **Leave** tab for everyone. Offshore balances build up automatically. Staff apply in the app; admins approve or decline from the same tab, which shows a badge when requests are waiting. **Stage 3b:** onshore staff see their leave balances from **Xero Payroll**, read-only and synced every 6 hours.

Later stages: availability (4) and the pay report (5).

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

## Clock in / out

- **On a phone:** staff tap **Clock in** or **Clock out** on My shifts and enter their PIN. The app records the time and, if allowed, GPS location. A missing location is flagged but never blocks a punch.
- **At a kiosk:** staff tap **Clock in** or **Clock out** and enter their PIN. The PIN alone identifies the person, and the screen confirms with their first name.
- **Time:** the server's clock decides the time, not the phone's. Times are stored in UTC and compared with the roster in Melbourne time (daylight-saving aware), so offshore and onshore punches line up.
- **Offline:** a punch is saved on the device first, then sent. With no connection it's sent automatically later, with the phone's clock error corrected, and it's flagged "sent later".
- **No double counting:** re-sending the same punch stores it once. Tapping the same action twice within 60 seconds counts once. Clocking in twice in a row (forgot to clock out) keeps both, and the first is flagged "No clock-out".
- **Audit trail:** punches can't be edited or deleted, and the database itself refuses. Admin fixes add a new entry (with who and why) or mark one as not counting (with a reason).
- **Flags** on the admin **Clock** tab:
  - **Late:** clocked in more than 10 minutes after the rostered start.
  - **Left early:** clocked out more than 10 minutes before the rostered end.
  - **No clock-out:** still open 2 hours after the shift ended.
  - **Not clocked in:** rostered, but no punch.
  - **Not on the roster:** a punch with no matching shift.
  - **Information only:** sent later (offline), phone clock corrected, no location, entered by admin.
- **Settings** (`.env`): `LATE_GRACE_MINUTES`, `EARLY_GRACE_MINUTES` (both 10), `KIOSK_LOCATIONS` (`AB HQ:onshore,WrkPod:offshore`).

### Setting up a kiosk

1. On the tablet, open **https://hr.abgroup.au/kiosk** and add it to the home screen. It then opens full screen as "AB Kiosk".
2. An admin chooses the location (AB HQ or WrkPod), enters their Staff ID and password, and taps **Set up this kiosk**. The admin isn't left logged in. The device just becomes a kiosk for that office.
3. Plug the tablet in, and turn off auto-lock in its settings. The page also asks the screen to stay on.

A lost tablet or a kiosk being retired: `python -m app.cli list-kiosks`, then `remove-kiosk <number>`. That device stops working straight away.

**Limitation:** a kiosk can't check PINs while offline. If someone enters a wrong PIN with no connection, the punch can't be matched to anyone when it syncs, so it is dropped. Keep the kiosks on reliable Wi-Fi. Phones show the person an error if this happens.

## Leave

**Offshore policy:** the first 6 months are probation. Credits start at the **start of month 7** (the 6-month anniversary of the join date), then come on the same day each month after.
- **Vacation:** +1 day per month, carried over. The balance never goes above **10 days**: a credit that would take it over is only partly given, or not given at all.
- **Sick:** +0.5 day per month, carried over, with no limit.

Balances aren't stored. They're recalculated each time from the join date, approved leave and admin adjustments, so they can't drift or double-count.

**Applying:**
- Staff choose a type, the dates, and whether the first and last days are half days. Weekdays count; weekends don't. **Public holidays aren't excluded**, so the approver checks them.
- Overlapping requests are refused.
- Asking for more than the available balance is allowed, but the request is flagged for the approver.
- Staff can cancel a request while it's still waiting for approval. Approved leave can only be changed by an admin.

**Before offshore balances show:**
```bash
docker compose exec roster-app python -m app.cli set-join-date AB0003 2025-03-15
# Opening balance carried over from the old system (the final figure for that date):
docker compose exec roster-app python -m app.cli leave-set AB0003 vacation 4.5 --as-of 2026-10-01 --reason "Carried over from Frappe"
docker compose exec roster-app python -m app.cli leave-add AB0003 sick -1 --reason "Sick day 3 Oct, before the app"
docker compose exec roster-app python -m app.cli leave-balance AB0003     # check the result and recent history
```
Adjustments can't be edited or deleted. To correct one, add another.

**Onshore** staff can apply in the app. Their balances come from Xero (below). Xero stays the record, so update it when you approve leave.

### Xero (onshore balances)

The app only **reads** from Xero: payroll employees and their leave balances. It never writes to Xero.

1. **Create the Xero app.** At **developer.xero.com → My Apps → New app**:
   - Name: `AB Group Staff App`
   - Integration type: **Web app**
   - Company URL: `https://hr.abgroup.au`
   - Redirect URI: `https://hr.abgroup.au/api/admin/xero/callback`

   Then go to **Configuration**: copy the **Client id**, and use **Generate a secret** to get the secret. Save both in Passbolt.
2. **Add them to the server.** Add these lines to `.env`, then run `docker compose up -d`:
   ```
   XERO_CLIENT_ID=…
   XERO_CLIENT_SECRET=…
   ```
3. **Connect.** In the app, an admin goes to **Leave → Xero → Connect Xero**, signs in to Xero, chooses the AB organisation, and allows access.
4. **Check the matches.** Staff are matched to Xero employees automatically, by exact name or email. Anyone not matched shows "Not linked": pick their Xero name from the dropdown. If two Xero employees have the same name, the app won't guess, so you pick.

The access tokens are stored encrypted, using a key derived from `PIN_PEPPER`. The connection renews itself whenever it syncs. If it ever expires (for example, it was disconnected in Xero), the Leave tab shows **Reconnect**.

## Availability

For casual and contract staff (onshore; "Casual"/"Contract" in the staff file, `set-leave … no`, or casual in
Xero), at the bottom of My shifts. Everyone else applies for leave. Each person is one of two kinds — set
under **Team → Staff → Availability**:

- **Tells us when they can't work** (default, e.g. Jody): adds days/times they can't work, any time. Each
  one is emailed to the rosterer (`AVAILABILITY_EMAIL`, e.g. Dan) straight away, and removals too.
- **Submits monthly availability** (e.g. Craig): adds the days/times they CAN work for next month and
  presses Submit, by the 15th. Reminders go on the 1st, 10th and 14th (9am) to anyone who hasn't
  submitted and has an email. Each submission (and any update) is emailed to the rosterer; on the 16th
  (7am) the rosterer gets a summary of everyone's next month, including who hasn't submitted. Once a month
  is submitted, rostered shifts outside the given times show as clashes.

Admins see everyone's under **Team → Availability**, with the rostered shifts in the next 14 days that clash
with someone's availability or with approved/requested leave.

## Emails

The app sends email through a Google Workspace mailbox (e.g. `hr@abgroup.au`):

- staff: leave approved / declined (if they've added their email — tap your name at the top);
- admins: each new leave request, and a daily check at 7am (roster clashes, yesterday's missed clock-ins/outs,
  roster sheet errors) — only sent when there's something in it;
- `ALERT_EMAIL`: backup or off-site copy failed, no backup for 36 hours, Xero needs reconnecting,
  roster sheet unreadable for 30 minutes.

Set up (once): on the sending mailbox turn on 2-Step Verification, create an app password at
https://myaccount.google.com/apppasswords, then add to `.env` and restart:

```
SMTP_USER=hr@abgroup.au
SMTP_PASSWORD=the-16-letter-app-password
ALERT_EMAIL=brendan@...
```

```bash
docker compose up -d
docker compose exec roster-app python -m app.cli test-email you@example.com
docker compose exec roster-app python -m app.cli set-email AB0002 aaron@...   # or staff add their own in the app
docker compose exec roster-app python -m app.cli email-status                 # recent emails and whether they went
```

Emails are queued in the database and retried for several hours if Gmail can't be reached.

## Pay report

Fortnightly (Mon–Sun; the period ending Sun 4 Oct 2026 is the anchor; pay day is the Wednesday after).
Emailed to `PAY_REPORT_EMAIL` (or `ALERT_EMAIL`) at 6am the Monday after the fortnight ends, and viewable
any time under **Clock → Pay report** (including the current fortnight so far). It lists, per person, the
quantity for each Xero pay item — no dollar amounts — plus anything to check.

- **Fitness** (casual/part-time): pays rostered time, docked for lateness/leaving early, never extra time
  unless you approve it; each shift rounded to the nearest 15 min, minimum 1 h. Over 10 h/day or 38 h/week
  → Overtime. Public holidays → Public Holiday Hours. First Aid Allowance = days worked; a day with more than
  one shift = 1 Broken Shift + 1 Excess Fares.
- **First Aid** (casual): Sessional Teaching = roster Teaching Hours (min 2 per course, max 5 per day);
  Non-Teaching Duties = 0.5 h per course + travel time to client sites; Vehicle km for those trips;
  First Aid + Laundry allowances = 1 per week worked. Out-of-hours courses are flagged.
- **Contractors**: hours to check their invoices. **Offshore**: days worked, leave, problems.
- Directors / auto-clocked staff are left out.

Travel (First Aid, trainers with "First Aid travel paid" ticked under Team → Staff — Judy and Jody): per day,
HQ → each client site in order → HQ, benchmarked from `OFFICE_ADDRESS` (Unit 2, 7-9 Butler Way, Tullamarine VIC
3043), whatever route they actually drove. Travel hours to 2 decimals go into Non-Teaching Duties; km (0.1) into
Vehicle Allowance. Locations matching `CAMPUS_PATTERNS` (Tullamarine, Butler Way, HQ, Melton, Galactic,
39 Collins) are campuses automatically: gear is kept there, no travel.

Set the office address (`OFFICE_ADDRESS`) and, under Clock → Pay report → First Aid venues, mark each other roster
Location as a campus or a client site (with its address and one-way km/minutes, or let Google Maps look them up
if `GOOGLE_MAPS_API_KEY` is set). The 800-hour teaching tracker starts from each trainer's starting hours
("Set starting hours" in the same screen).

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

**All times are Melbourne time**, including offshore shifts. Offshore staff also see each shift in Philippines time underneath, worked out for that date so daylight saving is handled.

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
   chown 1000:1000 secrets/google-service-account.json  # the app runs as user 1000 in its container
   docker compose up -d --build
   docker compose logs -f roster-app                          # wait for "Application startup complete"
   ```
   The compose file assumes your Traefik uses the `websecure` entrypoint and the `mytlschallenge` cert resolver on the `n8n_default` network, like the n8n stack. If your entrypoint has a different name (e.g. `https`), change the one label in `docker-compose.yml`.
4. **Load staff** (one time). Copy `sample-roster/staff.example.csv`, fill it in, then:
   ```bash
   docker compose cp staff.csv roster-app:/tmp/staff.csv
   docker compose exec roster-app python -m app.cli seed-staff /tmp/staff.csv
   docker compose cp roster-app:/tmp/staff.credentials.csv .   # temporary passwords + PINs
   docker compose exec -u root roster-app rm -f /tmp/staff.csv /tmp/staff.credentials.csv
   ```
   Then make the admins (Brendan, Aaron, Dylan):
   ```bash
   for id in AB0001 AB0002 AB0005; do docker compose exec roster-app python -m app.cli set-role $id admin; done
   ```
   (Or include a `Role` column with `admin` in the staff CSV.)

   Hand out the logins privately, then **delete `staff.credentials.csv`**. Required columns: `Staff ID`, `Name`, `Location Type` (Onshore/Offshore). Optional: `Email`, `Department`, `Join Date`, `Employment Status`, `Notes`, `Role` (staff/admin), `PIN`, `Password`. Headers are matched ignoring case and spacing, so the `AB-Staff-Seed.csv` layout works as-is. If any row has a problem, nothing is imported and every error is listed. Offshore staff without a join date are imported with a warning; they need one before leave accrual (Stage 3). Re-running the import updates people by Staff ID and keeps their existing passwords, PINs and roles.
5. Open `https://hr.abgroup.au` on a phone, then use **Share → Add to Home Screen** (iOS) or **Install app** (Android).

### Admin commands

```bash
docker compose exec roster-app python -m app.cli list-staff
docker compose exec roster-app python -m app.cli set-role AB0004 admin    # or: ... set-role AB0004 staff
docker compose exec roster-app python -m app.cli reset-password AB0003   # new temp password, logs them out everywhere
docker compose exec roster-app python -m app.cli set-pin AB0003          # new random PIN (add --choose to type one)
docker compose exec roster-app python -m app.cli deactivate AB0003       # can't log in; their shifts get flagged
docker compose exec roster-app python -m app.cli list-kiosks
docker compose exec roster-app python -m app.cli remove-kiosk 2          # lost/retired kiosk device
docker compose exec roster-app python -m app.cli set-leave AB0012 no      # contractor: no leave, Leave tab hidden
docker compose exec roster-app python -m app.cli set-auto-clock AB0001 yes  # clocked in/out at rostered start/end automatically
```

Auto-clock (AB0001 and AB0002 by default) adds punches marked "automatic" at each rostered shift start
and end, every few minutes. They show on the Clock page like any punch and can be removed or corrected.
If the roster changes after a shift has started, check that day's punches.

### Backups

`deploy/backup.sh` dumps the database to `/root/backups/ab-roster/` (compressed, private to root), checks the dump isn't empty, and keeps 30 days. Install the nightly run once:

```bash
cd ~/ab-roster && chmod 700 deploy/backup.sh
./deploy/backup.sh                       # run once now; should print "backup OK"
(crontab -l 2>/dev/null; echo "15 2 * * * $PWD/deploy/backup.sh >> /root/backups/ab-roster/backup.log 2>&1") | crontab -
crontab -l                               # confirm the line is there
```

Check it's running: `tail /root/backups/ab-roster/backup.log`. Backups on the same server don't survive the server being lost, so also turn on Hostinger's VPS backups or snapshots, or copy the folder off-site.

**Off-server copy (Google Shared Drive).** Each night's backup is also **encrypted** (AES-256, with a passphrase only you hold) and uploaded to a Shared drive. Copies are kept for 90 days, and the newest 7 are always kept. To set it up:
1. In the Google Cloud project (`ab-roster`), enable the **Google Drive API**: APIs & Services → Library → Google Drive API → Enable.
1. In Google Drive, go to **Shared drives → New**, e.g. "AB Roster Backups". Open **Manage members** and add `ab-roster-reader@ab-roster.iam.gserviceaccount.com` as **Content manager**.
2. Copy the drive ID from its URL (`https://drive.google.com/drive/folders/<ID>`) and add `BACKUP_DRIVE_FOLDER_ID=<ID>` to `.env`.
3. Create the passphrase: `openssl rand -base64 32 > secrets/backup-passphrase && chmod 600 secrets/backup-passphrase`. **Save it in Passbolt.** Without it the Drive copies can't be opened.
4. Run `docker compose up -d --build`, then `docker compose exec roster-app python -m app.offsite check`, then `./deploy/backup.sh`. You should see `offsite copy OK`.

**Restore from a Drive copy**: download the `.gpg` file to the VPS, then decrypt it:
```bash
gpg --batch --pinentry-mode loopback --passphrase-file secrets/backup-passphrase -d roster-….sql.gz.gpg > restore.sql.gz
```
Then restore `restore.sql.gz` as below.

**Restore** (replaces the current data with the backup):
```bash
cd ~/ab-roster
gunzip -c /root/backups/ab-roster/roster-YYYY-MM-DD-HHMM.sql.gz | docker compose exec -T roster-db psql -U roster -q roster
docker compose restart roster-app
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

## Branding

`static/logo.png` (header and login screen) and `static/icons/*` (home-screen icon, favicon) are generated from the AB Group logo SVG, always on black. The supplied SVG wraps a ~435 px image, so if a true vector version turns up, regenerate these from it at higher resolution. After changing any of them, bump `VERSION` in `static/sw.js` so installed apps pick up the new files.

## Expenses

Everyone has an **Expenses** tab (admins: Leave → Expenses): date, category (parking/tolls, accommodation &
meals, equipment/supplies, other), amount (AUD; PHP for offshore staff), what it was for, and a photo/PDF of the
receipt. Only the approvers in `EXPENSE_APPROVERS` (default AB0001, Brendan) can approve — and get the email for
each claim; other admins can see them. Nobody approves their own. Approved claims appear in the next pay report as
**Reimbursement** for that person and are marked paid once that fortnight's report goes out, so they're never
paid twice.

**Incentives (offshore staff):** their tab is called **Claims** and also has **Claim an incentive** with a type:
**Monthly incentive** (month + a bookings table — paste it straight from the booking sheet, including the
"Total amount: Php …" line; link columns Blue / TD Portal / Booking Spreadsheet take URLs) or **Client retention** /
**Other** (client, incentive and reason, date achieved). Amount in pesos, optional evidence. Brendan approves (no receipt needed);
approved incentives go into the pay report as **Incentive (PHP)**, separate from reimbursements.

## Still to do

- **Onboarding new staff** — an admin screen (possibly on the kiosk) to add a new employee: details,
  Staff ID, temporary password and PIN, instead of the `seed-staff` CSV. Not needed yet.
- **Xero draft pay run** — once a couple of fortnights' reports have been checked by hand, push the quantities
  to Xero as timesheets / a draft pay run for approval.
