"""Proves that the tests guarding security / correctness rules really guard them: each mutation
removes one rule from the code, runs the tests that claim to cover it, and requires them to
FAIL. The source file is always restored.

  python tools/mutation_check.py            # all mutations
  python tools/mutation_check.py --fast     # skip the ones that need full pipeline runs
"""
import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (name, file, original text, mutated text, pytest selection, slow)
MUTATIONS = [
    ("API token required", "cctv_analysis/app/server.py",
     "ok = hmac.compare_digest(hdr, token) or (media and hmac.compare_digest(ck, token))", "ok = True",
     "tests/test_app_api.py::test_api_requires_the_token", False),
    ("cookie only for media", "cctv_analysis/app/server.py",
     'media = request.method == "GET" and (path == "/api/file" or path.startswith("/api/exports/"))', "media = True",
     "tests/test_app_api.py::test_cookie_alone_cannot_call_the_api", False),
    ("Host header check", "cctv_analysis/app/server.py",
     "if not allow_lan and request.host not in allowed_hosts:", "if False:",
     "tests/test_app_api.py::test_foreign_host_header_is_rejected", False),
    ("rate limit", "cctv_analysis/app/server.py", "            if len(q) >= n:\n                return False",
     "            if False:\n                return False", "tests/test_app_api.py::test_rate_limit", False),
    ("bad config rejected", "cctv_analysis/app/server.py", "        problems = cfg.validate()\n        if problems:",
     "        problems = cfg.validate()\n        if False:", "tests/test_app_api.py::test_rejects_bad_config", False),
    ("licence expiry", "cctv_analysis/licensing.py", "    if left < 0:", "    if False:",
     "tests/test_licensing.py::test_expiry_is_enforced", False),
    ("licence signature", "cctv_analysis/licensing.py", "        pub.verify(sig, body)\n", "        pass\n",
     "tests/test_licensing.py::test_edited_key_is_rejected", False),
    ("licence clock rollback", "cctv_analysis/licensing.py",
     "    if last_seen and today < last_seen - dt.timedelta(days=CLOCK_SKEW_DAYS):", "    if False:",
     "tests/test_licensing.py::test_clock_rollback_detected", False),
    ("licence machine binding", "cctv_analysis/licensing.py",
     '    if p.get("machine") and p["machine"] != (this_machine or machine_id()):', "    if False:",
     "tests/test_licensing.py::test_machine_binding", False),
    ("trial limit", "cctv_analysis/licensing.py", "    if not status.valid:\n        cfg.max_videos",
     "    if False:\n        cfg.max_videos", "tests/test_guards_unit.py::test_trial_limit_applies_only_without_a_valid_licence",
     False),
    ("one analysis at a time", "cctv_analysis/app/jobs.py", "            if exclusive:", "            if False:",
     "tests/test_guards_unit.py::test_one_analysis_at_a_time_and_idempotent_start", False),
    ("audit chain", "cctv_analysis/audit.py", 'if e.get("prev") != prev or _digest(e) != e.get("hash"):', "if False:",
     "tests/test_guards_unit.py::test_audit_chain_detects_edit_delete_and_reorder", False),
    ("metadata rejected when it contradicts folder date", "cctv_analysis/timeline.py",
     "                    if hint and d.date() != hint:", "                    if False:",
     "tests/test_timeline.py::test_cloud_export_times_come_from_the_dvr_clock", False),
    ("OSD reading must match folder date", "cctv_analysis/timeline.py",
     "    return hint is None or abs((d.date() - hint).days) <= 1", "    return True",
     "tests/test_timeline.py::test_osd_rejects_reading_that_contradicts_folder_date", False),
    ("file start from its first frames", "cctv_analysis/timeline.py",
     "    if len(early) >= 2 and close(early[0][1], early[1][1]):", "    if False:",
     "tests/test_timeline.py::test_stitched_file_is_flagged_discontinuous", False),
    ("plate never 'corrected' twice", "cctv_analysis/plates.py", "    if best is None or best[1] > 1:",
     "    if best is None or best[1] > 2:", "tests/test_plates.py::test_indian_format_fit", False),
    ("exact-frame seek", "cctv_analysis/video_io.py", 'cmd = [FFMPEG, "-nostdin", "-v", "error", "-ss", f"{max(0.0, t):.3f}"',
     'cmd = [FFMPEG, "-nostdin", "-v", "error", "-ss", f"{max(0.0, t) + 0.4:.3f}"',
     "tests/test_plates.py::test_read_frame_at_returns_the_exact_frame", False),
    ("path confinement", "cctv_analysis/app/server.py", "    if os.path.commonpath([base_r, full]) != base_r:",
     "    if False:", "tests/test_app_api.py::test_file_serving_cannot_escape_the_output_folder", True),
    ("per-event DVR clock", "cctv_analysis/timeline.py", "    if not cfg.osd_verify_events or", "    if True or not cfg.osd_verify_events or",
     "tests/test_pipeline_e2e.py::test_stitched_file_event_uses_its_own_clock", True),
    ("clean reel picture", "cctv_analysis/reels.py",
     "ok = (_encode_overlay if cfg.reel_overlay else _encode_clean)(e, part, cfg, dur)",
     "ok = _encode_overlay(e, part, cfg, dur)", "tests/test_pipeline_e2e.py::test_reel_picture_is_unmodified", True),
    ("reel chronological order", "cctv_analysis/reels.py",
     '            return (0, r["start_dt"], [], r["camera"] or "", r["start_off"])',
     '            return (0, r["rel_path"] or "", [], r["camera"] or "", r["start_off"])',
     "tests/test_pipeline_e2e.py::test_reel_is_chronological_and_foldered", True),
    ("VLM circuit breaker", "cctv_analysis/pipeline.py", "        if self._vlm_errors >= 3 and not self._vlm_failed:",
     "        if self._vlm_errors >= 999 and not self._vlm_failed:",
     "tests/test_vlm_providers.py::test_dead_server_disables_vlm_after_three_failures", True),
    ("evidence tamper detection", "cctv_analysis/evidence.py",
     'bad = [n for n, h in manifest["files"].items() if hashlib.sha256(z.read(n)).hexdigest() != h]', "bad = []",
     "tests/test_app_api.py::test_evidence_export_and_verification", True),
]


def run(selection: str) -> int:
    env = dict(os.environ)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", selection],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=1800).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("-k", default="", help="only mutations whose name contains this")
    a = ap.parse_args()
    results = []
    for name, rel, orig, mut, sel, slow in MUTATIONS:
        if (a.fast and slow) or (a.k and a.k not in name):
            continue
        path = os.path.join(ROOT, rel)
        src = open(path, encoding="utf-8").read()
        if src.count(orig) != 1:
            results.append((name, "SETUP-ERROR", f"original text found {src.count(orig)}x in {rel}"))
            continue
        base = run(sel)                       # the test must pass on the real code first
        t0 = time.time()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src.replace(orig, mut))
            rc = run(sel)
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src)
        verdict = "KILLED" if (base == 0 and rc != 0) else ("BASE-FAILS" if base != 0 else "SURVIVED")
        results.append((name, verdict, f"{sel} ({time.time() - t0:.0f}s)"))
        print(f"{verdict:9s} {name:50s} {sel}", flush=True)
    bad = [r for r in results if r[1] != "KILLED"]
    print(f"\n{len(results) - len(bad)}/{len(results)} mutations killed")
    for r in bad:
        print("  NOT KILLED:", r)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
