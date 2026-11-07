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
     'media = request.method == "GET" and (path == "/api/file" or path.startswith("/api/exports/") or\n'
     '                                                 case_api.is_media(path))', "media = True",
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
     "    groups.sort(key=lambda g: (-len(g), g[0][0]))", "    groups.sort(key=lambda g: (-len(g), -g[0][0]))",
     "tests/test_timeline.py::test_stitched_file_is_flagged_discontinuous", False),
    ("plate never 'corrected' twice", "cctv_analysis/plates.py", "    if best is None or best[1] > 1:",
     "    if best is None or best[1] > 2:", "tests/test_plates.py::test_indian_format_fit", False),
    ("exact-frame seek", "cctv_analysis/video_io.py",
     '        pre, post = seek_args(src, t)\n        cmd = [FFMPEG, "-nostdin", "-v", "error", *pre, *_input_args(src), '
     '"-i", src, *post,\n               "-map", "0:v:0", "-an", "-sn", "-frames:v", "1"',
     '        pre, post = seek_args(src, t + 0.4)\n        cmd = [FFMPEG, "-nostdin", "-v", "error", *pre, '
     '*_input_args(src), "-i", src, *post,\n               "-map", "0:v:0", "-an", "-sn", "-frames:v", "1"',
     "tests/test_plates.py::test_read_frame_at_returns_the_exact_frame", False),
    ("case-run progress is the overall percent", "cctv_analysis/app/jobs.py",
     '        if p.get("sources"):', "        if False:",
     "tests/test_guards_unit.py::test_case_run_progress_is_the_overall_percent_not_one_source_s", False),
    ("raw-stream copy cut starts before the event", "cctv_analysis/evidence.py",
     "        start = max(0.0, start - 3.0)", "        start = max(0.0, start)",
     "tests/test_multi_folder_ingest.py::test_a_copy_cut_of_a_raw_stream_contains_the_event_start", False),
    ("memory governor shrinks batches above 85 %", "cctv_analysis/resources.py",
     "        if used < self.high:\n            return nominal", "        if True:\n            return nominal",
     "tests/test_resources.py::test_batches_shrink_above_85_percent_and_never_below_one", False),
    ("one Section 63 certificate per device", "cctv_analysis/section63.py",
     '            mine = [e for e in exhibits if e["source_id"] == sid]', "            mine = list(exhibits)",
     "tests/test_section63_bsa.py::test_each_device_gets_its_own_certificate_with_its_own_declarant", False),
    ("bundle verification checks the certificate root", "cctv_analysis/section63.py",
     '        per[sid] = {"merkle_ok": merkle.root(items) == cert["exhibits_merkle_root"],',
     '        per[sid] = {"merkle_ok": True,',
     "tests/test_section63_bsa.py::test_checksums_verify_with_sha256sum_and_tampering_is_detected", False),
    ("system folder dialog only where it can open", "cctv_analysis/app/server.py",
     "        if not native_dialog():\n            raise ApiError(501", "        if False:\n            raise ApiError(501",
     "tests/test_case_api.py::test_the_system_folder_dialog_is_offered_only_where_it_can_open", False),
    ("path confinement", "cctv_analysis/app/common.py", "    if os.path.commonpath([base_r, full]) != base_r:",
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
    ("case media routes only for the cookie", "cctv_analysis/app/case_api.py", "    return bool(MEDIA.match(path))",
     '    return path.startswith("/api/cases")',
     "tests/test_case_api.py::test_case_routes_need_the_token_and_the_cookie_loads_media_only", True),
    ("quick investigation validates before creating", "cctv_analysis/app/case_api.py",
     '        if len({os.path.realpath(s["path"]) for s in specs}) != len(specs):', "        if False:",
     "tests/test_case_api.py::test_quick_investigation_checks_every_folder_before_creating_anything", True),
    ("face identities are per source", "cctv_analysis/journey.py",
     "    out: Dict[str, Dict[str, float]] = {sid: {value: 1.0}}",
     '    return {s["source_id"]: {value: 1.0} for s in srcs}',
     "tests/test_case_sightings.py::test_a_face_is_followed_into_the_other_source_by_similarity", True),
    ("pictures follow a moved case folder", "cctv_analysis/storage.py",
     "    if r is None:\n        moved = resolve(out_dir, p)", "    if False:\n        moved = resolve(out_dir, p)",
     "tests/test_multi_folder_ingest.py::test_a_moved_case_and_a_new_drive_letter_keep_working", True),
    ("relink checks acquisition hashes", "cctv_analysis/cases.py",
     '            elif full_hash and sha256_file(p) != f["sha256"]:', "            elif False:",
     "tests/test_multi_folder_ingest.py::test_a_moved_case_and_a_new_drive_letter_keep_working", True),
    ("trial limit over the whole case run", "cctv_analysis/investigation.py",
     "            if max_videos_total and budget <= 0:", "            if False:",
     "tests/test_multi_folder_ingest.py::test_the_trial_limit_counts_videos_of_the_whole_run", True),
    ("describe-it search added to finished events", "cctv_analysis/investigation.py",
     "                if semantic_on and not cancel.is_set():", "                if False:",
     "tests/test_case_api.py::test_describe_it_search_can_be_turned_on_later_without_analysing_again", True),
    ("saved still checked against a fresh decode", "cctv_analysis/app/case_api.py",
     '        saved["reproducible"] = EN.sha256_bytes(EN.png_bytes(run_chain(img2, chain)[0])) == saved["output_sha256"] \\\n'
     '            and (not rec["chain"] or EN.reproduce(edir, saved))',
     '        saved["reproducible"] = True',
     "tests/test_case_api.py::test_a_saved_still_is_reproduced_from_the_recording_or_flagged", True),
    ("evidence tamper detection", "cctv_analysis/evidence.py",
     'bad = [n for n, h in manifest["files"].items() if hashlib.sha256(z.read(n)).hexdigest() != h]', "bad = []",
     "tests/test_app_api.py::test_evidence_export_and_verification", True),
    # ---- added in the round of 2026-11-07
    ("an uncertain plate read is not reported", "cctv_analysis/plates.py",
     "    return bool(text) and confidence >= min_confidence and (valid_format or len(text) >= 6)",
     "    return bool(text) and (valid_format or len(text) >= 6)",
     "tests/test_plates.py::test_vote_and_watchlist", False),
    ("the analysis uses plate_min_confidence", "cctv_analysis/pipeline.py",
     'ok = is_reportable(res["text"], res["confidence"], res["valid_format"], cfg.plate_min_confidence)',
     'ok = is_reportable(res["text"], res["confidence"], res["valid_format"])',
     "tests/test_plates.py::test_an_uncertain_read_is_not_the_plate_but_its_guess_and_position_are_kept", True),
    ("the examiner's plate reading counts", "cctv_analysis/reviews.py",
     '    if rd is not None:\n        out["text"] = rd["text"]', '    if False:\n        out["text"] = rd["text"]',
     "tests/test_app_api.py::test_an_examiner_types_a_plate_and_that_reading_counts", True),
    ("an examined plate is not 'not read'", "cctv_analysis/app/results.py",
     '        eff = [p for p in eff if not p["text"] and p["reading"] is None]',
     '        eff = [p for p in eff if not p["text"]]',
     "tests/test_app_api.py::test_an_examiner_types_a_plate_and_that_reading_counts", True),
    ("a reel shows every second once", "cctv_analysis/journey.py",
     '        if last is not None and s <= last["end"] + gap:', "        if False:",
     "tests/test_case_sightings.py::test_reel_segments_cover_every_sighting_once", False),
    ("super-resolution is bit-exact", "cctv_analysis/enhance.py",
     "    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)\n    gx = cv2.Sobel(g, cv2.CV_16S, 1, 0, ksize=3).astype(np.float32)\n"
     "    gy = cv2.Sobel(g, cv2.CV_16S, 0, 1, ksize=3).astype(np.float32)\n"
     "    m = np.minimum(np.sqrt(gx * gx + gy * gy) / 4.0, 255.0).round().astype(np.uint8)\n"
     "    return cv2.GaussianBlur(m, (0, 0), 1.0).astype(np.float32) / 255.0",
     "    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0\n"
     "    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)\n    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)\n"
     "    return cv2.GaussianBlur(cv2.magnitude(gx, gy), (0, 0), 1.0)",
     "tests/test_forensic_enhancement.py::test_the_alignment_path_is_bit_exact_whatever_the_memory_alignment", False),
    ("dropped folders are looked for next to recent ones", "cctv_analysis/app/case_api.py",
     '    roots = [os.path.dirname(p.rstrip("\\\\/")) for p in recent if p] + [p for p in recent if p]',
     "    roots = [p for p in recent if p]",
     "tests/test_case_api.py::test_a_dropped_sibling_of_a_folder_used_before_is_found", False),
    ("detect-only never loads the face recogniser", "cctv_analysis/faces.py",
     "        self._rec = None\n", '        self._rec = cv2.FaceRecognizerSF.create(fetch("sface"), "")\n',
     "tests/test_face_modes.py::test_detect_keeps_face_pictures_but_compares_nothing", True),
    ("only 'unique' groups faces", "cctv_analysis/investigation.py",
     '    return {"use_faces": mode != "off", "face_identities": mode == "unique",',
     '    return {"use_faces": mode != "off", "face_identities": mode != "off",',
     "tests/test_face_modes.py::test_reference_finds_only_the_reference_person", True),
    ("the face mode overrides the run's options", "cctv_analysis/app/case_api.py",
     '        req["options"] = {**req["options"], **face_options(mode, refs)}', "        pass",
     "tests/test_case_api.py::test_the_face_mode_decides_the_face_settings_of_a_run", True),
    ("the timeline filters by score", "cctv_analysis/journey.py", "            if min_score > 0:", "            if False:",
     "tests/test_case_api.py::test_the_timeline_filters_by_score_and_kind", True),
    ("AI providers fall back in order", "cctv_analysis/vlm.py",
     "                self.failures[i] = self.failures.get(i, 0) + 1\n", "                raise\n",
     "tests/test_vlm_providers.py::test_fallback_providers_answer_when_the_first_fails", True),
]


def run(selection: str) -> int:
    env = dict(os.environ)
    # never write bytecode of a mutated file: a same-size mutation restored within the same second
    # would leave a .pyc that still matches the restored source (mtime in whole seconds + size),
    # and every later run would silently execute the mutated code
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", selection],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=1800).returncode


def _drop_bytecode(path: str) -> None:
    """Remove cached bytecode of `path` (any interpreter tag), so the restored source is compiled again."""
    d = os.path.join(os.path.dirname(path), "__pycache__")
    stem = os.path.splitext(os.path.basename(path))[0] + "."
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.startswith(stem) and f.endswith(".pyc"):
                os.remove(os.path.join(d, f))


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
        _drop_bytecode(path)                  # a stale .pyc from an interrupted run must not stand in for the source
        base = run(sel)                       # the test must pass on the real code first
        t0 = time.time()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src.replace(orig, mut))
            rc = run(sel)
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src)
            _drop_bytecode(path)
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
