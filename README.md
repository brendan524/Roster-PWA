# AB Group staff app

A self-hosted PWA for AB Group staff. It replaces Frappe HR.

**Stage 1:** staff log in on their phone and see **only their own** shifts, in a list or calendar view. The shifts are read live from the roster Google Sheet. Admins also see the whole team's roster and a list of problems found in the sheet.

**Stage 2:** clock in/out with a PIN, on a personal phone (with GPS) or at an office kiosk. Works offline. Admins get a daily **Clock** view with late, early, missing and unrostered flags, plus corrections with an audit trail.

Later stages: leave (3), availability (4), and the pay report (5).

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
```

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
