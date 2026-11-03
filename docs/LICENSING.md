# Selling licences (vendor guide)

Status: **current** · Code: `cctv_analysis/licensing.py`, `tools/license_tool.py` · Tests: `tests/test_licensing.py`

## One-time setup (on your own computer)
```bash
python tools/license_tool.py init
```
Creates your **private signing key** at `~/.cctv-license/signing_key.pem` (owner-only permissions)
and writes the matching public key to `cctv_analysis/license_public_key.pem`. Commit the public key
and rebuild the app. Back the private key up offline. Anyone holding it can issue licences;
losing it means you can't issue keys that existing installs accept. Never commit it or email it.

## Issuing a licence
```bash
python tools/license_tool.py issue --customer "Ravi Stores" --months 6 --out ravi.lic
python tools/license_tool.py issue --customer "City Mall" --days 30 --start 2026-12-01 --out mall.lic
python tools/license_tool.py issue --customer "Shop 2" --months 12 --machine 3F2A91C04B7E6D18A0C2   # one PC only
python tools/license_tool.py verify ravi.lic
```
The customer pastes the text (or the file's content) in **Settings › Licence › Activate**. The
machine ID for `--machine` is shown on the same screen. Renewal = issue a new key with a new expiry.

## What the app enforces
| State | Meaning | Effect |
|---|---|---|
| LICENSED | valid signature, not expired, right machine | full use; badge shows days left |
| TRIAL | no licence installed | each job analyses at most 5 videos |
| EXPIRED | past the expiry date | trial limits; message asks to renew |
| INVALID | edited/forged key, other vendor's key, other machine | trial limits; key not installed |
| CLOCK_TAMPERED | system date earlier than a date already seen (−2 days tolerance) or before issue | trial limits until the clock is fixed |
| NOT_CONFIGURED | the build has no public key | trial limits (vendor setup missing) |

Verification is offline (no server). The licence gates the **packaged desktop app**. The Python
package/CLI/notebook are not gated (decision D9): the source is public, so a technical user can
always remove a check. The licence is a commercial control for non-technical customers, not copy
protection.
