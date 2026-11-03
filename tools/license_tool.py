"""Vendor tool: create your signing key once, then issue time-limited licences offline.

  python tools/license_tool.py init                       # once; keeps the PRIVATE key outside the repo
  python tools/license_tool.py issue --customer "Ravi Stores" --months 6 --out ravi.lic
  python tools/license_tool.py issue --customer "Mall Security" --days 30 --machine 3F2A...   # bind to one PC
  python tools/license_tool.py verify ravi.lic
  python tools/license_tool.py machine-id                 # what a customer reads in Settings › Licence

The private key lives in --key-dir (default ~/.cctv-license/signing_key.pem). Never commit it
and never send it to anyone: whoever has it can issue licences. The public key is written to
cctv_analysis/license_public_key.pem and ships with the app.
"""
import argparse
import datetime as dt
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis import licensing as L  # noqa: E402

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".cctv-license")


def add_months(d: dt.date, months: int) -> dt.date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    import calendar
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def cmd_init(a):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    os.makedirs(a.key_dir, exist_ok=True)
    priv_path = os.path.join(a.key_dir, "signing_key.pem")
    if os.path.exists(priv_path) and not a.force:
        sys.exit(f"{priv_path} already exists (use --force to replace it – old licences then stop working)")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    with open(a.public_out, "wb") as fh:
        fh.write(pub)
    print(f"private signing key: {priv_path}  (keep secret, back it up offline)")
    print(f"public key:          {a.public_out}  (commit this; it ships with the app)")


def cmd_issue(a):
    priv_path = os.path.join(a.key_dir, "signing_key.pem")
    if not os.path.exists(priv_path):
        sys.exit(f"no signing key at {priv_path} – run: python tools/license_tool.py init")
    issued = dt.date.fromisoformat(a.start) if a.start else dt.date.today()
    if a.days:
        expires = issued + dt.timedelta(days=a.days)
    else:
        expires = add_months(issued, a.months)
    payload = {"v": 1, "id": uuid.uuid4().hex[:12], "customer": a.customer, "issued": issued.isoformat(),
               "expires": expires.isoformat(), "plan": a.plan, "features": a.features or []}
    if a.machine:
        payload["machine"] = a.machine.strip().upper()
    with open(priv_path, "rb") as fh:
        key = L.sign(payload, fh.read())
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(key + "\n")
        print(f"licence for {a.customer} valid {issued} → {expires} written to {a.out}")
    else:
        print(key)


def cmd_verify(a):
    text = open(a.file, encoding="utf-8").read() if os.path.exists(a.file) else a.file
    pub = open(a.public_key, "rb").read() if a.public_key else None
    st = L.verify(text, pub)
    print(st.state, "-", st.reason)
    sys.exit(0 if st.valid else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--key-dir", default=DEFAULT_DIR)
    p.add_argument("--public-out", default=L.PUBLIC_KEY_PATH)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("issue")
    p.add_argument("--customer", required=True)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--months", type=int, default=6)
    g.add_argument("--days", type=int)
    p.add_argument("--start", help="YYYY-MM-DD (default today)")
    p.add_argument("--plan", default="standard")
    p.add_argument("--features", nargs="*")
    p.add_argument("--machine", help="bind to one computer (id shown in the app)")
    p.add_argument("--key-dir", default=DEFAULT_DIR)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_issue)
    p = sub.add_parser("verify")
    p.add_argument("file", help="licence file or key text")
    p.add_argument("--public-key")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("machine-id")
    p.set_defaults(fn=lambda a: print(L.machine_id()))
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
