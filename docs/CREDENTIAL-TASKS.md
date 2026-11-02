# Credential tasks

Status: **current**. Only work that can't be done without a credential the developer
doesn't hold. Each test is committed and runs with `python -m pytest -m release tests/test_release.py`.

## 1. Licence signing key (vendor)
1. **Credential:** the vendor's Ed25519 licence signing key. Only the owner should hold it, so it
   must be created on the owner's own computer.
2. **Command:** `python tools/license_tool.py init && git add cctv_analysis/license_public_key.pem && git commit -m "Licence public key"`
3. **Expected output:** `private signing key: <home>/.cctv-license/signing_key.pem  (keep secret, back it up offline)`
   and `public key: …/cctv_analysis/license_public_key.pem  (commit this; it ships with the app)`.
4. **Test that will then pass:** `tests/test_release.py::test_licence_public_key_is_configured`.

## 2. Windows code-signing certificate
1. **Credential:** an Authenticode code-signing certificate (`.pfx` + password), from a CA such as
   DigiCert, Sectigo or SSL.com, or an Azure Trusted Signing account. Without it, Windows SmartScreen
   warns customers about an unknown publisher.
2. **Command (after `packaging\build_windows.ps1`):**
   `signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f cert.pfx /p <password> dist\CCTVAnalysis\CCTVAnalysis.exe`
3. **Expected output:** `Successfully signed: dist\CCTVAnalysis\CCTVAnalysis.exe`
4. **Test that will then pass (on Windows):** `tests/test_release.py::test_windows_exe_is_signed`.
