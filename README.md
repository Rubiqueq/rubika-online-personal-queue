# Rubika Online — personal-account queue (independent web server)

A Persian/right-to-left FastAPI web UI using [rubpy 7.3.5](https://pypi.org/project/rubpy/) for **personal-account** OTP login and Rubika message sending. Not a Bot API client. Rubpy itself is archived/unofficial; provider behavior can change. Based on publicly documented *interfaces*; this project does **not** vendor rubpy or copy Rubika's closed-source web client.

## Working features (server-side implementation)

- Admin-token protected web panel, sessionStorage token in browser, no developer bypass.
- Login request + OTP verification with rubpy `send_code`, `sign_in`, RSA session key and `register_device`; real test endpoint `get_me`.
- Import XLSX/CSV (`phone,name,consent`), deduplicate, normalize Iranian mobile numbers, require positive per-recipient consent; template download.
- Resolve user GUID using existing Rubika contacts, optionally explicitly add contacts to Rubika address book. Unknown GUIDs are **not** guessed and never sent messages.
- SQLite-persisted, single-worker sequential schedule with configurable >=60s spacing, START/PAUSE, auto-resume after container restart, server-acknowledged message IDs, failure report CSV.
- No silent retries of ambiguous submissions; after server crash in-flight jobs are flagged `review` while other pending tasks remain scheduled. On unknown provider failures the queue pauses for manual review.

## Limitations & deployment reality

- Live OTP and actual delivery **cannot be verified without the owner's phone, OTP, Rubika access and hosting**. The app has automated local tests but it is not a claim of live provider success.
- A `sent` status means Rubika's response contains a `message_id`. It is not proof of delivery/read.
- Rubika is not guaranteed to support discovery of every telephone number. If a number cannot be resolved to an actual `u0...` GUID the entry is `unresolved`.
- Runs as a persistent **server** with one Uvicorn worker. GitHub repository/Pages by themselves are not hosting for this Python+SQLite backend. Free hosting that sleeps, lacks persistent disk or blocks Rubika endpoints is unsuitable.
- Only for recipients who requested/consented to the communication. The app defaults to NOT adding imported numbers to the Rubika address book.
- Do not make port 8080 public without HTTPS/authenticated reverse proxy; use Caddy/Traefik/Nginx with TLS and restricted network access. Keep `/data` backed up and private.

## Setup using Docker on a persistent Linux server

```bash
cp .env.example .env
# Edit .env with a strong random ADMIN_TOKEN
# Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
docker compose up -d --build
```

Panel available at http://127.0.0.1:8080 on the host (use an SSH tunnel or HTTPS reverse proxy to reach remotely).

## First run

1. Enter the `ADMIN_TOKEN` in the panel.
2. Enter the personal Rubika phone, request OTP, then type the OTP into the **same app process**. If account password is required, enter it in the dedicated field and retry request.
3. Click “بررسی زنده اتصال”. This must actually return the logged-in account GUID.
4. Import XLSX/CSV with header `phone,name,consent` and `consent=بله` for recipients who agreed.
5. Write the message, choose a timezone-aware scheduled start (the browser converts local time), choose interval; optionally approve importing missing numbers to personal Rubika contacts.
6. Click “ایجاد صف” then START. Check response report; do not interpret server-accepted messages as delivered.

To stop: PAUSE. To recreate a queue with a new message: PAUSE, clear, create. Do not run multiple Docker service replicas against the same volume/database.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The unit tests use a fake provider and do not send Rubika messages. Actual end-to-end provider test must be performed with your own logged-in account and a consenting test recipient.

## Source references

- https://github.com/shayanheidari01/rubika (archived July 2026)
- https://rubpy.shayan-heidari.ir/client_methods/
- https://pypi.org/project/rubpy/
- https://github.com/EhsanAhmadzadeh/rubika-cli (alternative CLI investigated, not bundled)
