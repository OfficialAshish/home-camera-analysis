"""Offline, time-limited licences for the desktop app.

A licence key is ``CCTV1.<payload>.<signature>`` (both base64url): the payload is JSON
(customer, issued/expiry dates, plan, features, optional machine binding) and the signature is
Ed25519 over the payload bytes. The vendor signs with a private key that never leaves the
vendor's machine (``tools/license_tool.py``); the app only holds the public key
(``license_public_key.pem`` next to this file), so keys cannot be forged from the app.

Fails closed: no public key, no licence, a bad signature, an expired licence, another
machine, or a system clock moved back all give ``valid=False`` and the app runs in trial mode.
This module gates the packaged GUI; the Python library / CLI are the open-source developer
interface and are not licence-gated (see docs/DECISIONS.md).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import platform
import uuid
from dataclasses import asdict, dataclass, field
from typing import List, Optional

PREFIX = "CCTV1"
PUBLIC_KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "license_public_key.pem")
TRIAL_MAX_VIDEOS = 5
CLOCK_SKEW_DAYS = 2


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def machine_id() -> str:
    """Stable, non-reversible id of this computer (host name + primary MAC + OS/arch)."""
    raw = f"{platform.node()}|{uuid.getnode():012x}|{platform.system()}|{platform.machine()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20].upper()


@dataclass
class LicenseStatus:
    valid: bool
    state: str                       # LICENSED | TRIAL | EXPIRED | INVALID | NOT_CONFIGURED | CLOCK_TAMPERED
    reason: str
    customer: str = ""
    plan: str = ""
    expires: str = ""
    days_left: Optional[int] = None
    features: List[str] = field(default_factory=list)
    machine_bound: bool = False

    def public(self) -> dict:
        """What the UI may show (no licence id, no signature)."""
        return asdict(self)


def sign(payload: dict, private_key_pem: bytes) -> str:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key = load_pem_private_key(private_key_pem, password=None)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{PREFIX}.{_b64e(body)}.{_b64e(key.sign(body))}"


def _load_public(pem: Optional[bytes]):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    if pem is None:
        if not os.path.exists(PUBLIC_KEY_PATH):
            return None
        with open(PUBLIC_KEY_PATH, "rb") as fh:
            pem = fh.read()
    key = load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("licence public key is not Ed25519")
    return key


def verify(key_text: str, public_key_pem: Optional[bytes] = None, today: Optional[dt.date] = None,
           this_machine: Optional[str] = None, last_seen: Optional[dt.date] = None) -> LicenseStatus:
    from cryptography.exceptions import InvalidSignature
    try:
        pub = _load_public(public_key_pem)
    except Exception as e:
        return LicenseStatus(False, "NOT_CONFIGURED", f"licence public key unreadable: {e}")
    if pub is None:
        return LicenseStatus(False, "NOT_CONFIGURED",
                             f"Trial mode (up to {TRIAL_MAX_VIDEOS} videos per job): this copy has no licence "
                             f"verification key yet – vendor: run tools/license_tool.py init")
    key_text = (key_text or "").strip()
    if not key_text:
        return LicenseStatus(False, "TRIAL", f"Trial mode (up to {TRIAL_MAX_VIDEOS} videos per job): no licence installed")
    parts = key_text.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        return LicenseStatus(False, "INVALID", "not a licence key")
    try:
        body, sig = _b64d(parts[1]), _b64d(parts[2])
        pub.verify(sig, body)
        p = json.loads(body)
    except InvalidSignature:
        return LicenseStatus(False, "INVALID", "licence signature does not match (edited or forged key)")
    except Exception:
        return LicenseStatus(False, "INVALID", "licence key is damaged")
    today = today or dt.date.today()
    try:
        exp = dt.date.fromisoformat(p["expires"])
        issued = dt.date.fromisoformat(p["issued"])
    except Exception:
        return LicenseStatus(False, "INVALID", "licence has no valid dates")
    base = dict(customer=p.get("customer", ""), plan=p.get("plan", ""), expires=exp.isoformat(),
                features=list(p.get("features") or []), machine_bound=bool(p.get("machine")))
    if last_seen and today < last_seen - dt.timedelta(days=CLOCK_SKEW_DAYS):
        return LicenseStatus(False, "CLOCK_TAMPERED", f"system date {today} is before a date already seen "
                                                      f"({last_seen}); fix the computer clock", **base)
    if today < issued - dt.timedelta(days=CLOCK_SKEW_DAYS):
        return LicenseStatus(False, "CLOCK_TAMPERED", f"system date {today} is before the licence issue date", **base)
    if p.get("machine") and p["machine"] != (this_machine or machine_id()):
        return LicenseStatus(False, "INVALID", "licence was issued for another computer", **base)
    left = (exp - today).days
    if left < 0:
        return LicenseStatus(False, "EXPIRED", f"licence expired on {exp} – contact your vendor to renew",
                             days_left=left, **base)
    return LicenseStatus(True, "LICENSED", f"licensed to {base['customer']} until {exp}", days_left=left, **base)


class LicenseStore:
    """Installed licence + last-seen date, in the app's data folder."""

    def __init__(self, folder: str):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.key_path = os.path.join(folder, "license.key")
        self.seen_path = os.path.join(folder, "license.seen")

    def key(self) -> str:
        if os.path.exists(self.key_path):
            with open(self.key_path, encoding="utf-8") as fh:
                return fh.read().strip()
        return ""

    def last_seen(self) -> Optional[dt.date]:
        try:
            with open(self.seen_path, encoding="utf-8") as fh:
                return dt.date.fromisoformat(fh.read().strip())
        except Exception:
            return None

    def _touch(self, today: dt.date) -> None:
        last = self.last_seen()
        if last is None or today > last:
            with open(self.seen_path, "w", encoding="utf-8") as fh:
                fh.write(today.isoformat())

    def status(self, public_key_pem: Optional[bytes] = None, today: Optional[dt.date] = None) -> LicenseStatus:
        today = today or dt.date.today()
        st = verify(self.key(), public_key_pem, today, last_seen=self.last_seen())
        if st.state != "CLOCK_TAMPERED":
            self._touch(today)
        return st

    def install(self, key_text: str, public_key_pem: Optional[bytes] = None,
                today: Optional[dt.date] = None) -> LicenseStatus:
        """Verify first; only a valid key replaces the installed one."""
        today = today or dt.date.today()
        st = verify(key_text, public_key_pem, today, last_seen=self.last_seen())
        if st.valid:
            tmp = self.key_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(key_text.strip())
            os.replace(tmp, self.key_path)
            self._touch(today)
        return st
