# FaithView Pro — License System Local Test Rig

A working local version of your 4-stage license flow:
`payment → key generation → device activation → device-switching enforcement`,
plus an admin dashboard to watch devices in real time.

Uses SQLite instead of Postgres so there's zero setup — same schema shape,
swap the connection later in `db()` when you're ready to point at real Postgres.

## 1. Install & set up

```bash
cd license_test
pip install -r requirements.txt --break-system-packages   # drop the flag if not needed on your machine
python gen_keys.py    # creates server_private.pem + server_public.pem, once
```

`server_private.pem` = stays on your license server only.
`server_public.pem` = what gets embedded in the real FaithView Pro client later for offline verification.

## 2. Run the server

```bash
uvicorn server:app --reload --port 8000
```

## 3. Watch the dashboard

Open **http://127.0.0.1:8000/admin** — it polls every 3 seconds, so leave it open
in a tab while you fire test requests from a terminal and watch licenses/devices
appear live.

## 4. Test the flow

**Simulate a payment (issues a license key):**
```bash
curl -X POST http://127.0.0.1:8000/dummy/payment \
  -H "Content-Type: application/json" \
  -d '{"email":"pastor@examplechurch.org"}'
```
Copy the `license_key` from the response for the next step.

**Activate a device:**
```bash
curl -X POST http://127.0.0.1:8000/activate \
  -H "Content-Type: application/json" \
  -d '{"license_key":"FVP-XXXX-XXXX-XXXX-XXXX","device_fingerprint":"macA1-hostname-diskserial-hash"}'
```
Returns a signed offline token good for 21 days (edit `GRACE_DAYS` in `server.py`
to match whatever you land on in the 14–30 day range).

**Try activating a second device on the same key** — this should get blocked
with a 409, proving the one-device-at-a-time enforcement works:
```bash
curl -X POST http://127.0.0.1:8000/activate \
  -H "Content-Type: application/json" \
  -d '{"license_key":"FVP-XXXX-XXXX-XXXX-XXXX","device_fingerprint":"different-device-hash"}'
```

**Verify the token completely offline** (no network call — this is what the
real client does on every launch):
```bash
python verify_token.py "<paste the token string here>"
```

**Release a device from the dashboard** — click "Deactivate" on a device card,
or:
```bash
curl -X POST http://127.0.0.1:8000/admin/devices/1/deactivate
```
The license flips back to `unactivated` and can be claimed by a new device —
this is your device-switching flow.

**Revoke a license entirely** (kills all its devices too):
```bash
curl -X POST http://127.0.0.1:8000/admin/licenses/1/revoke
```

## What's faked vs. real

| Piece | This rig | Real system |
|---|---|---|
| Payment | Unconditional dummy endpoint | Stripe/Paddle webhook with signature verification |
| Database | SQLite | Postgres (same table shapes — `licenses`, `devices`) |
| Token signing | Real Ed25519, real keys | Same — this part is production-ready logic |
| Device fingerprint | Whatever string you pass in | MAC/hostname/disk-serial hash generated client-side |
| Admin auth | None — dashboard is wide open | Needs real auth before this touches the internet |

## Next steps toward production

1. Swap `sqlite3` calls in `server.py` for `psycopg` against your six-table Postgres schema.
2. Replace `/dummy/payment` with a real Stripe (or similar) webhook handler that verifies the event signature before generating a key.
3. Add auth in front of `/admin` and the `/admin/*` endpoints — right now anyone who finds the URL can deactivate or revoke.
4. Move `verify_token.py`'s logic into the actual FaithView Pro client codebase, embedding `server_public.pem` at build time.
5. Decide the real fingerprint recipe (MAC + hostname + disk serial, hashed) and wire it into the client's activation call.
