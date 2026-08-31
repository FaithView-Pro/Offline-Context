"""
FaithView Pro — local license system test rig
===============================================

Simulates the four-stage flow end to end:
  1. payment          -> POST /dummy/payment
  2. key generation    -> (happens automatically inside step 1)
  3. device activation -> POST /activate
  4. device-switch enforcement -> built into /activate + /admin/devices/{id}/deactivate

Run:
    python gen_keys.py        # once, creates server_private.pem / server_public.pem
    uvicorn server:app --reload --port 8000

Then open http://127.0.0.1:8000/admin in a browser to watch it live.
"""
import sqlite3
import secrets
import base64
import json
import time
import os
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "license_test.db")
PRIVATE_KEY_PATH = os.path.join(BASE_DIR, "server_private.pem")

GRACE_DAYS = 21  # mid-point of your 14-30 day grace window

app = FastAPI(title="FaithView Pro License Test Server")

# ---------------------------------------------------------------------------
# Ed25519 signing key (loaded once at startup)
# ---------------------------------------------------------------------------
if not os.path.exists(PRIVATE_KEY_PATH):
    raise RuntimeError(
        "server_private.pem not found. Run `python gen_keys.py` first."
    )

with open(PRIVATE_KEY_PATH, "rb") as f:
    PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)


def sign_token(payload: dict) -> str:
    """Build a compact signed offline token: base64(payload).base64(signature)"""
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = PRIVATE_KEY.sign(payload_bytes)
    token = (
        base64.urlsafe_b64encode(payload_bytes).decode()
        + "."
        + base64.urlsafe_b64encode(signature).decode()
    )
    return token


# ---------------------------------------------------------------------------
# DB helpers (SQLite standin for the Postgres schema)
# ---------------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT UNIQUE NOT NULL,
                email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unactivated',  -- unactivated | active | revoked
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_id INTEGER NOT NULL REFERENCES licenses(id),
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',  -- active | deactivated
                activated_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                token_expires_at TEXT NOT NULL
            )
        """)


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def gen_license_key():
    # FVP-XXXX-XXXX-XXXX-XXXX
    groups = [secrets.token_hex(2).upper() for _ in range(4)]
    return "FVP-" + "-".join(groups)


# ---------------------------------------------------------------------------
# Stage 1+2: dummy payment -> license key generation
# ---------------------------------------------------------------------------
class PaymentRequest(BaseModel):
    email: str


@app.post("/dummy/payment")
def dummy_payment(req: PaymentRequest):
    """
    Stands in for your real payment webhook (Stripe/Paddle/whatever later).
    'Confirms' payment unconditionally and issues a license key + email login.
    """
    key = gen_license_key()
    with db() as conn:
        conn.execute(
            "INSERT INTO licenses (license_key, email, status, created_at) VALUES (?, ?, 'unactivated', ?)",
            (key, req.email, now_iso()),
        )
    return {
        "status": "payment_confirmed",
        "email": req.email,
        "license_key": key,
        "message": "License key generated. Use it with POST /activate from a device.",
    }


# ---------------------------------------------------------------------------
# Stage 3+4: device activation with one-device-at-a-time enforcement
# ---------------------------------------------------------------------------
class ActivateRequest(BaseModel):
    license_key: str
    device_fingerprint: str
    device_label: str = ""


@app.post("/activate")
def activate(req: ActivateRequest):
    with db() as conn:
        lic = conn.execute(
            "SELECT * FROM licenses WHERE license_key = ?", (req.license_key,)
        ).fetchone()
        if lic is None:
            raise HTTPException(404, "License key not found")
        if lic["status"] == "revoked":
            raise HTTPException(403, "License has been revoked")

        active_device = conn.execute(
            "SELECT * FROM devices WHERE license_id = ? AND status = 'active'",
            (lic["id"],),
        ).fetchone()

        if active_device is not None:
            if active_device["fingerprint"] == req.device_fingerprint:
                # Same device re-activating (e.g. app restart) — just refresh token
                pass
            else:
                # Different device trying to activate -> blocked, this is the
                # "device-switching enforcement" stage. Admin has to release it.
                raise HTTPException(
                    409,
                    f"License already active on another device (fingerprint "
                    f"{active_device['fingerprint'][:12]}...). Deactivate it from "
                    f"/admin before activating here.",
                )

        expires_at = datetime.now(timezone.utc) + timedelta(days=GRACE_DAYS)

        if active_device is None:
            conn.execute(
                """INSERT INTO devices
                   (license_id, fingerprint, status, activated_at, last_seen, token_expires_at)
                   VALUES (?, ?, 'active', ?, ?, ?)""",
                (lic["id"], req.device_fingerprint, now_iso(), now_iso(), expires_at.isoformat()),
            )
            conn.execute("UPDATE licenses SET status = 'active' WHERE id = ?", (lic["id"],))
        else:
            conn.execute(
                "UPDATE devices SET last_seen = ?, token_expires_at = ? WHERE id = ?",
                (now_iso(), expires_at.isoformat(), active_device["id"]),
            )

    payload = {
        "license_key": req.license_key,
        "device_fingerprint": req.device_fingerprint,
        "issued_at": now_iso(),
        "expires_at": expires_at.isoformat(),
    }
    token = sign_token(payload)
    return {
        "status": "activated",
        "token": token,
        "expires_at": payload["expires_at"],
        "note": "Client stores this token and verifies it offline with server_public.pem "
                "on every launch (see verify_token.py). No server call needed until it "
                f"expires in {GRACE_DAYS} days.",
    }


class DeviceHeartbeat(BaseModel):
    license_key: str
    device_fingerprint: str


@app.post("/heartbeat")
def heartbeat(req: DeviceHeartbeat):
    """Optional: client can ping this whenever it does have connectivity,
    purely so the admin dashboard shows fresh 'last seen' times."""
    with db() as conn:
        lic = conn.execute(
            "SELECT * FROM licenses WHERE license_key = ?", (req.license_key,)
        ).fetchone()
        if lic is None:
            raise HTTPException(404, "License key not found")
        result = conn.execute(
            "UPDATE devices SET last_seen = ? WHERE license_id = ? AND fingerprint = ? AND status = 'active'",
            (now_iso(), lic["id"], req.device_fingerprint),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Device not found or not active")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Admin: monitoring dashboard
# ---------------------------------------------------------------------------
@app.get("/api/state")
def api_state():
    with db() as conn:
        licenses = conn.execute("SELECT * FROM licenses ORDER BY id DESC").fetchall()
        out = []
        for lic in licenses:
            devices = conn.execute(
                "SELECT * FROM devices WHERE license_id = ? ORDER BY id DESC", (lic["id"],)
            ).fetchall()
            out.append({
                "id": lic["id"],
                "license_key": lic["license_key"],
                "email": lic["email"],
                "status": lic["status"],
                "created_at": lic["created_at"],
                "devices": [dict(d) for d in devices],
            })
    return JSONResponse(out)


@app.post("/admin/devices/{device_id}/deactivate")
def deactivate_device(device_id: int):
    with db() as conn:
        dev = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if dev is None:
            raise HTTPException(404, "Device not found")
        conn.execute("UPDATE devices SET status = 'deactivated' WHERE id = ?", (device_id,))
        # license goes back to 'unactivated' so a new device can claim it
        active_left = conn.execute(
            "SELECT COUNT(*) c FROM devices WHERE license_id = ? AND status = 'active'",
            (dev["license_id"],),
        ).fetchone()["c"]
        if active_left == 0:
            conn.execute(
                "UPDATE licenses SET status = 'unactivated' WHERE id = ?", (dev["license_id"],)
            )
    return {"status": "deactivated", "device_id": device_id}


@app.post("/admin/licenses/{license_id}/revoke")
def revoke_license(license_id: int):
    with db() as conn:
        conn.execute("UPDATE licenses SET status = 'revoked' WHERE id = ?", (license_id,))
        conn.execute(
            "UPDATE devices SET status = 'deactivated' WHERE license_id = ? AND status = 'active'",
            (license_id,),
        )
    return {"status": "revoked", "license_id": license_id}


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard():
    with open(os.path.join(BASE_DIR, "dashboard.html")) as f:
        return f.read()
