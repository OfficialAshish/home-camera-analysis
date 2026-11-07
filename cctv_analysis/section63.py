"""Section 63 certificate (Bharatiya Sakshya Adhiniyam, 2023) and the case evidence bundle.

Section 63(4) requires a certificate "specified in the Schedule" to be submitted with an
electronic record every time it is produced for admission; the Schedule has **Part A** (to be
filled by the party producing the record – here the person in charge of the DVR/NVR, or the
officer who seized it) and **Part B** (to be filled by the expert). Both parts state the source
device particulars and the hash value(s) of the record with the algorithm, with "Hash report to be
enclosed with the certificate".

The wording below follows the Schedule of the Bharatiya Sakshya (Second) Bill, 2023 as introduced
in Lok Sabha on 9 December 2023 (enacted as Act 47 of 2023), retrieved 2026-11-07 from
https://prsindia.org/files/bills_acts/bills_parliament/2023/Bhartiya_Sakshya_(Second)_Bill_2023.pdf
(SHA-256 7c0f918b01ed2cf4afb825f7e5a3c3cbbdfbbcecf62fe7d6a2244322e56a062c; quoted in
docs/legal/BSA-2023-SECTION-63.md). UNVERIFIED: the Gazette text of the Act was not reachable
(egazette.gov.in answered 503); any difference from the Bill's Schedule must be checked by the
department's legal cell before first use.

What the tool fills in: case details, the device particulars of every source (tick boxes for
the device type and Owned/Maintained/Managed/Operated), the SHA-256 algorithm, and the **hash
report** – a table of every file in the bundle (original segments, enhanced snapshots, reports)
with camera, calibrated and DVR timestamp span and SHA-256 – plus the SHA-256 of every seized
source file and a Merkle root over all of them. What it never fills in: the signatures, and the
names/addresses/places of the declarants unless the officer typed them – a declaration is made by
a person, not by software.

``build_bundle`` writes one ZIP: ``certificates/<source>/`` (**one certificate per source
device** – Part A is signed by the person in charge of that device, who can vouch for nothing
else – as PDF, HTML and certificate.json, each hash report listing only that device's records),
``originals/`` (stream-copy cuts of the source recordings, not re-encoded), ``enhanced/``
(Forensic Studio outputs with their filter logs), ``events/`` (event.json per sighting/event),
``audit/`` (case and source audit logs, acquisition records), ``hash_report.csv``, ``README.txt``
and ``checksums.sha256`` (``sha256sum -c`` format, every file).
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import io
import json
import logging
import os
import shutil
import tempfile
import zipfile
from typing import Dict, List, Optional, Sequence

from . import __version__, merkle
from .cases import DEVICE_KINDS, Case, calibrate, format_offset
from .evidence import _cut_original, sha256_file

log = logging.getLogger("cctv")
SCHEDULE_SOURCE = ("Bharatiya Sakshya (Second) Bill, 2023 (enacted as the Bharatiya Sakshya Adhiniyam, 2023, "
                   "Act 47 of 2023), Schedule [See section 63(4)(c)]")
TICKS = ("Computer / Storage Media", "DVR", "Mobile", "Flash Drive", "CD/DVD", "Server", "Cloud", "Other")
CONTROL = ("Owned", "Maintained", "Managed", "Operated")
ALGOS = ("SHA1", "SHA256", "MD5", "Other")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NVR_AS_DVR = {"NVR": "DVR"}       # the Schedule has no NVR box: an NVR is a digital video recorder


def _declarant(d: Optional[dict]) -> dict:
    d = d or {}
    return {k: str(d.get(k, "") or "")[:200] for k in ("name", "relation_name", "address", "designation", "place")}


def certificate_data(case: Case, exhibits: List[dict], part_a: Optional[dict] = None,
                     part_b: Optional[dict] = None, sources: Optional[Sequence[str]] = None,
                     when: Optional[dt.datetime] = None) -> dict:
    """Everything printed on the certificate (also stored as certificate.json in the bundle)."""
    from .ingest import load_ingest
    when = (when or dt.datetime.now(IST)).astimezone(IST)
    devices, seized = [], []
    for s in case.sources:
        if sources is not None and s.source_id not in sources:
            continue
        kind = NVR_AS_DVR.get(s.device.kind, s.device.kind)
        rec = load_ingest(case.source_dir(s.source_id)) or {}
        files = rec.get("files", [])
        devices.append({"source_id": s.source_id, "label": s.label,
                        "ticks": [t for t in TICKS if t == kind or (kind not in TICKS and t == "Other")],
                        "kind": s.device.kind, "other": s.device.kind if kind not in TICKS else "",
                        "make_model": " ".join(x for x in (s.device.make, s.device.model) if x),
                        "color": s.device.color, "serial": s.device.serial, "identifier": s.device.identifier,
                        "storage_medium": s.device.storage_medium, "operating_system": s.device.operating_system,
                        "custodian": s.device.custodian, "control": [c for c in CONTROL if c in s.device.control],
                        "clock_offset": format_offset(s.clock_offset),
                        "camera_offsets": {k: format_offset(v) for k, v in s.camera_offsets.items()},
                        "files": len(files), "bytes": rec.get("total_bytes", 0), "merkle_root": rec.get("merkle_root", ""),
                        "notes": s.device.notes})
        seized += [{"source_id": s.source_id, "file": f["rel"], "bytes": f["bytes"], "sha256": f["sha256"]} for f in files]
    root_items = merkle.file_lines({e["file"]: e["sha256"] for e in exhibits})
    return {
        "schedule": SCHEDULE_SOURCE,
        "case": {k: getattr(case, k) for k in ("case_id", "fir_number", "police_station", "under_sections", "district",
                                               "state", "investigating_officer", "incident_time")},
        "devices": devices, "exhibits": exhibits, "seized_files": seized,
        "algorithm": "SHA256",
        "exhibits_merkle_root": merkle.root(root_items),
        "seized_merkle_root": merkle.root(merkle.file_lines({f"{x['source_id']}/{x['file']}": x["sha256"]
                                                            for x in seized})),
        "part_a": _declarant(part_a), "part_b": _declarant(part_b),
        "date": when.strftime("%d/%m/%Y"), "time_ist": when.strftime("%H:%M"),
        "generated": when.isoformat(timespec="seconds"), "tool": f"Drishti-AI (home-camera-analysis {__version__})",
    }


# ------------------------------------------------------------------ HTML
def _box(on: bool) -> str:
    return "☑" if on else "☐"


def _blank(v: str, width: int = 28) -> str:
    return html.escape(v) if v else "_" * width


def _device_html(d: dict) -> str:
    E = html.escape
    ticks = " ".join(f"{_box(t in d['ticks'])} {E(t)}" for t in TICKS)
    return (f"<p class='dev'><b>{E(d['label'])}</b> ({E(d['source_id'])})<br>{ticks}<br>"
            f"Other: {_blank(d['other'], 20)}<br>"
            f"Make &amp; Model: {_blank(d['make_model'])} Color: {_blank(d['color'], 12)}<br>"
            f"Serial Number: {_blank(d['serial'])}<br>"
            f"IMEI/UIN/UID/MAC/Cloud ID: {_blank(d['identifier'])} (as applicable)<br>"
            f"Other relevant information: storage medium {_blank(d['storage_medium'], 10)}, operating system "
            f"{_blank(d['operating_system'], 10)}, custodian {_blank(d['custodian'], 16)}, DVR clock correction "
            f"{E(d['clock_offset'])}{(' (per camera: ' + E(', '.join(f'{k} {v}' for k, v in d['camera_offsets'].items())) + ')') if d['camera_offsets'] else ''}"
            f"; {d['files']} file(s), Merkle root <span class='mono'>{E(d['merkle_root'])}</span>"
            f"{(' – ' + E(d['notes'])) if d['notes'] else ''}</p>")


def _hash_line(c: dict) -> str:
    return (f"<p>I state that the HASH value/s of the electronic/digital record/s is <b>as listed in the enclosed "
            f"hash report</b> (Merkle root of all listed values: <span class='mono'>{c['exhibits_merkle_root']}</span>), "
            "obtained through the following algorithm:—<br>"
            + " ".join(f"{_box(a == 'SHA256')} {a}" for a in ALGOS)
            + " (Legally acceptable standard)<br><i>(Hash report to be enclosed with the certificate)</i></p>")


def _declaration_head(p: dict) -> str:
    return (f"<p>I, {_blank(p['name'])} (Name), Son/daughter/spouse of {_blank(p['relation_name'])} "
            f"residing/employed at {_blank(p['address'], 36)} do hereby solemnly affirm and sincerely state and submit "
            "as follows:—</p>")


def _signature(p: dict, c: dict, with_designation: bool) -> str:
    who = "(Name, designation and signature)" if with_designation else "(Name and signature)"
    name = html.escape(p["name"] + (", " + p["designation"] if with_designation and p["designation"] else ""))
    return (f"<table class='sig'><tr><td><div class='line'></div>{name or '&nbsp;'}<br>{who}</td>"
            f"<td>Date (DD/MM/YYYY): {c['date']}<br>Time (IST): {c['time_ist']} hours (In 24 hours format)<br>"
            f"Place: {_blank(p['place'], 16)}</td></tr></table>")


def certificate_html(c: dict) -> str:
    E = html.escape
    case = c["case"]
    devices = "".join(_device_html(d) for d in c["devices"])
    rows = "".join(f"<tr><td>{i}</td><td class='mono'>{E(e['file'])}</td><td>{E(e.get('camera') or '')}</td>"
                   f"<td>{E(e.get('span_calibrated') or '–')}</td><td>{E(e.get('span_dvr') or '–')}</td>"
                   f"<td class='mono'>{E(e['sha256'])}</td></tr>" for i, e in enumerate(c["exhibits"], 1))
    seized = "".join(f"<tr><td>{E(x['source_id'])}</td><td class='mono'>{E(x['file'])}</td><td>{x['bytes']:,}</td>"
                     f"<td class='mono'>{E(x['sha256'])}</td></tr>" for x in c["seized_files"])
    control_lines = "".join(
        f"<p>{E(d['label'])}: " + " ".join(f"{_box(x in d['control'])} {x}" for x in CONTROL) + " by me (select as applicable).</p>"
        for d in c["devices"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Certificate under Section 63 BSA 2023 — {E(case['case_id'])}</title>
<style>body{{font-family:'Times New Roman',serif;margin:28px;color:#000;font-size:13.5px;line-height:1.45}}
h1{{font-size:17px;text-align:center;margin:4px 0}} h2{{font-size:15px;text-align:center;margin:14px 0 2px}}
.sub{{text-align:center;font-size:12px;margin:0}} table{{border-collapse:collapse;width:100%;font-size:11.5px}}
th,td{{border:1px solid #444;padding:3px 4px;vertical-align:top;text-align:left}} th{{background:#eee}}
.mono{{font-family:'Courier New',monospace;word-break:break-all;font-size:10.5px}} .dev{{margin:6px 0 6px 12px}}
table.sig td{{border:0;width:50%}} .line{{border-bottom:1px solid #000;height:38px;width:85%}}
.case td{{width:25%}} .page{{page-break-before:always}} small{{color:#333}}
@media print{{body{{margin:12mm}}}}</style></head><body>
<p class="sub">THE SCHEDULE<br>[See section 63(4)(c)]</p>
<h1>CERTIFICATE</h1>
<table class="case"><tr><th>Case ID</th><td>{E(case['case_id'])}</td><th>FIR No.</th><td>{E(case['fir_number'])}</td></tr>
<tr><th>Police Station</th><td>{E(case['police_station'])}</td><th>Under Sections (BNS)</th><td>{E(case['under_sections'])}</td></tr>
<tr><th>District</th><td>{E(case['district'])}</td><th>State</th><td>{E(case['state'])}</td></tr>
<tr><th>Investigating Officer</th><td>{E(case['investigating_officer'])}</td><th>Incident time</th><td>{E(case['incident_time'])}</td></tr></table>

<h2>PART A</h2><p class="sub">(To be filled by the Party)</p>
{_declaration_head(c['part_a'])}
<p>I have produced electronic record/output of the digital record taken from the following device/digital record source (tick mark):—</p>
{devices}
<p>The digital device or the digital record source was under the lawful control for regularly creating, storing or processing
information for the purposes of carrying out regular activities and during this period, the computer or the communication
device was working properly and the relevant information was regularly fed into the computer during the ordinary course of
business. If the computer/digital device at any point of time was not working properly or out of operation, then it has not
affected the electronic/digital record or its accuracy. The digital device or the source of the digital record is:—</p>
{control_lines}
{_hash_line(c)}
{_signature(c['part_a'], c, False)}

<h2 class="page">PART B</h2><p class="sub">(To be filled by the Expert)</p>
{_declaration_head(c['part_b'])}
<p>The produced electronic record/output of the digital record are obtained from the following device/digital record source (tick mark):—</p>
{devices}
{_hash_line(c)}
{_signature(c['part_b'], c, True)}

<h2 class="page">HASH REPORT</h2><p class="sub">(enclosed with the certificate) · algorithm SHA-256 · {len(c['exhibits'])} record(s)</p>
<table><tr><th>#</th><th>File name</th><th>Camera</th><th>Timestamp span (calibrated)</th><th>Timestamp span (DVR clock)</th><th>SHA-256 hash digest</th></tr>{rows}</table>
<p>Merkle root (RFC 6962, SHA-256) of the records above: <span class="mono">{c['exhibits_merkle_root']}</span></p>
<h2>Seized source recordings</h2><p class="sub">acquisition hashes of every file of every source · Merkle root
<span class="mono">{c['seized_merkle_root']}</span></p>
<table><tr><th>Source</th><th>File</th><th>Bytes</th><th>SHA-256</th></tr>{seized or '<tr><td colspan=4>not ingested</td></tr>'}</table>
<p><small>Calibrated time = DVR clock + the clock correction stated for the device; the DVR clock is shown unchanged.
Original segments are stream copies (not re-encoded) and start at a keyframe, so a segment may begin a few seconds
before the span shown.
Generated {E(c['generated'])} by {E(c['tool'])}. Certificate text follows {E(c['schedule'])}. Verify every listed file with
<span class="mono">sha256sum -c checksums.sha256</span> in the bundle.</small></p>
</body></html>"""


# ------------------------------------------------------------------ PDF
def certificate_pdf(c: dict, path: str) -> None:
    """The same certificate as a printable A4 PDF (reportlab)."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["Normal"], fontName="Times-Roman", fontSize=10.5, leading=14)
    small = ParagraphStyle("s", parent=body, fontSize=8, leading=10)
    mono = ParagraphStyle("m", parent=body, fontName="Courier", fontSize=6.6, leading=8)
    center = ParagraphStyle("c", parent=body, alignment=TA_CENTER)
    h1 = ParagraphStyle("h1", parent=center, fontName="Times-Bold", fontSize=14, leading=18)
    h2 = ParagraphStyle("h2", parent=center, fontName="Times-Bold", fontSize=12, leading=16, spaceBefore=6)
    E = html.escape

    def box(on):
        return "[X]" if on else "[ &nbsp;]"

    def blank(v, n=28):
        return E(v) if v else "_" * n

    def P(t, st=body):
        return Paragraph(t, st)

    def grid(rows, widths, header=True):
        t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke if header else colors.white)]))
        return t

    def devices():
        out = []
        for d in c["devices"]:
            ticks = " &nbsp; ".join(f"{box(t in d['ticks'])} {E(t)}" for t in TICKS)
            out.append(P(f"<b>{E(d['label'])}</b> ({E(d['source_id'])})<br/>{ticks}<br/>Other: {blank(d['other'], 20)}<br/>"
                         f"Make &amp; Model: {blank(d['make_model'])} &nbsp; Color: {blank(d['color'], 12)}<br/>"
                         f"Serial Number: {blank(d['serial'])}<br/>IMEI/UIN/UID/MAC/Cloud ID: {blank(d['identifier'])} "
                         f"(as applicable)<br/>Other relevant information: storage medium {blank(d['storage_medium'], 10)}, "
                         f"operating system {blank(d['operating_system'], 10)}, custodian {blank(d['custodian'], 16)}, "
                         f"DVR clock correction {E(d['clock_offset'])}; {d['files']} file(s); Merkle root "
                         f"<font face='Courier' size='7'>{E(d['merkle_root'])}</font>"))
            out.append(Spacer(1, 3))
        return out

    def head(p):
        return P(f"I, {blank(p['name'])} (Name), Son/daughter/spouse of {blank(p['relation_name'])} residing/employed "
                 f"at {blank(p['address'], 36)} do hereby solemnly affirm and sincerely state and submit as follows:—")

    def hashes():
        return P("I state that the HASH value/s of the electronic/digital record/s is <b>as listed in the enclosed hash "
                 f"report</b> (Merkle root of all listed values: <font face='Courier' size='7'>{c['exhibits_merkle_root']}"
                 "</font>), obtained through the following algorithm:—<br/>"
                 + " &nbsp; ".join(f"{box(a == 'SHA256')} {a}" for a in ALGOS)
                 + " (Legally acceptable standard)<br/><i>(Hash report to be enclosed with the certificate)</i>")

    def sign(p, desig):
        who = "(Name, designation and signature)" if desig else "(Name and signature)"
        name = E(p["name"] + (", " + p["designation"] if desig and p["designation"] else ""))
        t = Table([[P("<br/><br/>______________________________<br/>" + (name or "") + "<br/>" + who),
                    P(f"Date (DD/MM/YYYY): {c['date']}<br/>Time (IST): {c['time_ist']} hours (In 24 hours format)<br/>"
                      f"Place: {blank(p['place'], 16)}")]], colWidths=[90 * mm, 85 * mm])
        return t

    case = c["case"]
    story = [P("THE SCHEDULE<br/>[See section 63(4)(c)]", center), P("CERTIFICATE", h1),
             grid([[P("<b>Case ID</b>"), P(E(case["case_id"])), P("<b>FIR No.</b>"), P(E(case["fir_number"]))],
                   [P("<b>Police Station</b>"), P(E(case["police_station"])), P("<b>Under Sections (BNS)</b>"),
                    P(E(case["under_sections"]))],
                   [P("<b>District</b>"), P(E(case["district"])), P("<b>State</b>"), P(E(case["state"]))],
                   [P("<b>Investigating Officer</b>"), P(E(case["investigating_officer"])), P("<b>Incident time</b>"),
                    P(E(case["incident_time"]))]], [32 * mm, 55 * mm, 35 * mm, 53 * mm], header=False),
             P("PART A", h2), P("(To be filled by the Party)", center), head(c["part_a"]),
             P("I have produced electronic record/output of the digital record taken from the following device/digital "
               "record source (tick mark):—"), *devices(),
             P("The digital device or the digital record source was under the lawful control for regularly creating, "
               "storing or processing information for the purposes of carrying out regular activities and during this "
               "period, the computer or the communication device was working properly and the relevant information was "
               "regularly fed into the computer during the ordinary course of business. If the computer/digital device "
               "at any point of time was not working properly or out of operation, then it has not affected the "
               "electronic/digital record or its accuracy. The digital device or the source of the digital record is:—"),
             *[P(E(d["label"]) + ": " + " &nbsp; ".join(f"{box(x in d['control'])} {x}" for x in CONTROL)
                 + " by me (select as applicable).") for d in c["devices"]],
             hashes(), Spacer(1, 4), sign(c["part_a"], False),
             PageBreak(), P("PART B", h2), P("(To be filled by the Expert)", center), head(c["part_b"]),
             P("The produced electronic record/output of the digital record are obtained from the following "
               "device/digital record source (tick mark):—"), *devices(), hashes(), Spacer(1, 4),
             sign(c["part_b"], True), PageBreak(),
             P("HASH REPORT", h2), P(f"(enclosed with the certificate) · algorithm SHA-256 · {len(c['exhibits'])} record(s)",
                                    center), Spacer(1, 4)]
    rows = [[P("<b>#</b>", small), P("<b>File name</b>", small), P("<b>Camera</b>", small),
             P("<b>Span (calibrated)</b>", small), P("<b>Span (DVR clock)</b>", small), P("<b>SHA-256</b>", small)]]
    rows += [[P(str(i), small), P(E(e["file"]), mono), P(E(e.get("camera") or ""), small),
              P(E(e.get("span_calibrated") or "–"), small), P(E(e.get("span_dvr") or "–"), small), P(e["sha256"], mono)]
             for i, e in enumerate(c["exhibits"], 1)]
    story += [grid(rows, [7 * mm, 48 * mm, 18 * mm, 30 * mm, 30 * mm, 47 * mm]),
              P(f"Merkle root (RFC 6962, SHA-256) of the records above: <font face='Courier' size='7'>"
                f"{c['exhibits_merkle_root']}</font>", small), Spacer(1, 6),
              P(f"<b>Seized source recordings</b> – Merkle root <font face='Courier' size='7'>{c['seized_merkle_root']}"
                "</font>", small)]
    srows = [[P("<b>Source</b>", small), P("<b>File</b>", small), P("<b>Bytes</b>", small), P("<b>SHA-256</b>", small)]]
    srows += [[P(E(x["source_id"]), small), P(E(x["file"]), mono), P(f"{x['bytes']:,}", small), P(x["sha256"], mono)]
              for x in c["seized_files"]] or [[P("not ingested", small), "", "", ""]]
    story += [grid(srows, [25 * mm, 60 * mm, 22 * mm, 73 * mm]), Spacer(1, 6),
              P("Calibrated time = DVR clock + the clock correction stated for the device. Original segments are stream "
                "copies (not re-encoded) and start at a keyframe, so a segment may begin a few seconds before the span "
                f"shown. Generated {E(c['generated'])} "
                f"by {E(c['tool'])}. Certificate text follows {E(c['schedule'])}. Verify every file with "
                "<font face='Courier'>sha256sum -c checksums.sha256</font>.", small)]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Times-Roman", 8)
        canvas.drawString(15 * mm, 10 * mm, f"Case {case['case_id']} · FIR {case['fir_number']} · Section 63 BSA certificate")
        canvas.drawRightString(195 * mm, 10 * mm, f"page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"Section 63 BSA certificate {case['case_id']}",
                            author=c["tool"], subject=f"FIR {case['fir_number']}")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


# ------------------------------------------------------------------ bundle
def _span(start: Optional[str], seconds: float) -> str:
    if not start:
        return ""
    try:
        t = dt.datetime.strptime(start[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return start
    return f"{t:%Y-%m-%d %H:%M:%S} to {t + dt.timedelta(seconds=max(0.0, seconds)):%H:%M:%S}"


README = """Evidence bundle – case {case_id} (FIR {fir})
Generated {generated} by {tool}

certificates/<source>/   one certificate under Section 63 BSA 2023 per source device
                         (Section63_Certificate.pdf / .html, certificate.json). Part A is
                         signed by the person in charge of THAT device, Part B by the expert.
                         Each certificate's hash report lists only the records of its device.
originals/<source>/      the event segments cut from the seized recordings by stream copy
                         (not re-encoded; a cut may begin a few seconds before the event)
enhanced/                Forensic Studio stills: input, output and the filter log (.json)
events/<source>/         times (DVR clock and calibrated), camera, source file and offsets
audit/                   case and source audit logs, acquisition hashes (ingest.json)
hash_report.csv          every record with camera, both time spans and SHA-256
checksums.sha256         SHA-256 of every file in this bundle

To verify: extract the ZIP, then in the extracted folder run
    sha256sum -c checksums.sha256          (Linux / macOS / Git Bash)
    Get-FileHash -Algorithm SHA256 <file>  (Windows PowerShell, compare with the list)
Every line must report OK. The SHA-256 of this ZIP is recorded in the case audit log.
"""


def build_bundle(case: Case, items: List[dict], dest_zip: str, part_a: Optional[Dict[str, dict]] = None,
                 part_b: Optional[dict] = None, enhancements: Sequence[str] = (), operator: str = "",
                 notes: str = "", progress=None) -> dict:
    """Evidence ZIP with one Section 63 certificate per source device.

    `items`: [{"source_id", "event_id"}] – the events/sightings to produce. `enhancements`: names
    of saved Forensic Studio outputs (``<case>/enhancements/<name>.png``); each belongs to the
    source its frame came from. `part_a`: {source_id: declarant} (default: the declarant saved on
    the source); `part_b`: the expert (default: the case's saved expert)."""
    import sqlite3
    if not items and not enhancements:
        raise ValueError("choose at least one event, sighting or enhanced still")
    tmp = tempfile.mkdtemp(prefix="bundle_")
    exhibits: List[dict] = []
    sources_used: List[str] = []
    total = len(items) + len(enhancements)
    try:
        for n, it in enumerate(items, 1):
            sid, eid = it.get("source_id"), it.get("event_id")
            src = case.source(sid)
            if progress:
                progress(n, total, eid)
            out = case.source_dir(sid)
            db = sqlite3.connect(f"file:{os.path.join(out, 'analysis.db')}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            try:
                e = db.execute("SELECT * FROM events WHERE event_id=?", (eid,)).fetchone()
                if e is None:
                    raise KeyError(f"no event {eid} in source {sid}")
                e = dict(e)
            finally:
                db.close()
            if sid not in sources_used:
                sources_used.append(sid)
            off = src.offset_for(e["camera"])
            d = os.path.join(tmp, "originals", sid)
            os.makedirs(d, exist_ok=True)
            video = src.recording(e["rel_path"])
            ext = os.path.splitext(video)[1] or ".mp4"
            name = f"{eid}{ext}"
            dur = float(e["end_off"] or 0) - float(e["start_off"] or 0)
            if not os.path.exists(video):
                raise FileNotFoundError(f"source recording not available: {video} (is the drive plugged in? if it "
                                        "has another drive letter now, relink the source)")
            if not _cut_original(video, e["start_off"], e["end_off"], os.path.join(d, name)):
                raise RuntimeError(f"could not cut the original segment of {eid}")
            meta = {"source_id": sid, "source_label": src.label, "event_id": eid, "camera": e["camera"],
                    "dvr_start": e["start_dt"], "dvr_end": e["end_dt"],
                    "calibrated_start": calibrate(e["start_dt"], off), "calibrated_end": calibrate(e["end_dt"], off),
                    "clock_offset": format_offset(off), "time_source": e["time_source"],
                    "source_file": e["rel_path"], "source_offsets": [e["start_off"], e["end_off"]],
                    "source_file_sha256": sha256_file(video),
                    "note": "stream copy of the recording (not re-encoded); it may begin a few seconds before the "
                            "event start (keyframe boundary)"}
            ej = os.path.join(tmp, "events", sid, f"{eid}.json")
            os.makedirs(os.path.dirname(ej), exist_ok=True)
            with open(ej, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=1)
            exhibits.append({"file": f"originals/{sid}/{name}", "source_id": sid, "camera": f"{src.label}/{e['camera']}",
                             "span_calibrated": _span(meta["calibrated_start"], dur), "span_dvr": _span(e["start_dt"], dur),
                             "kind": "original segment"})
        edir = os.path.join(case.root, "enhancements")
        for k, nm in enumerate(enhancements, len(items) + 1):
            if progress:
                progress(k, total, nm)
            base = os.path.basename(nm)
            if base.endswith(".png"):
                base = base[:-4]
            for suffix in (".png", ".json", "_input.png"):
                if not os.path.exists(os.path.join(edir, base + suffix)):
                    raise FileNotFoundError(f"enhancement {base} not found")
            with open(os.path.join(edir, base + ".json"), encoding="utf-8") as fh:
                rec = json.load(fh)
            src_info = rec.get("source", {})
            sid = src_info.get("source_id", "")
            try:
                case.source(sid)
            except KeyError:
                raise ValueError(f"enhancement {base} does not name a source of this case – it cannot be certified")
            if sid not in sources_used:
                sources_used.append(sid)
            os.makedirs(os.path.join(tmp, "enhanced"), exist_ok=True)
            for suffix in (".png", ".json", "_input.png"):
                shutil.copyfile(os.path.join(edir, base + suffix), os.path.join(tmp, "enhanced", base + suffix))
            exhibits.append({"file": f"enhanced/{base}.png", "source_id": sid, "camera": src_info.get("camera", ""),
                             "span_calibrated": src_info.get("calibrated_time", ""), "span_dvr": src_info.get("dvr_time", ""),
                             "kind": "enhanced still: " + rec.get("log", "")})
        os.makedirs(os.path.join(tmp, "audit"), exist_ok=True)
        if os.path.exists(os.path.join(case.root, "audit_log.jsonl")):
            shutil.copyfile(os.path.join(case.root, "audit_log.jsonl"), os.path.join(tmp, "audit", "case_audit_log.jsonl"))
        for sid in sources_used:
            p = os.path.join(case.source_dir(sid), "audit_log.jsonl")
            if os.path.exists(p):
                shutil.copyfile(p, os.path.join(tmp, "audit", f"{sid}_audit_log.jsonl"))
            ing = os.path.join(case.source_dir(sid), "ingest.json")
            if os.path.exists(ing):
                shutil.copyfile(ing, os.path.join(tmp, "audit", f"{sid}_ingest.json"))
        for e in exhibits:
            e["sha256"] = sha256_file(os.path.join(tmp, e["file"]))
        records = list(exhibits)
        for root_, _, fs in os.walk(os.path.join(tmp, "audit")):
            for f in sorted(fs):
                rel = os.path.relpath(os.path.join(root_, f), tmp).replace(os.sep, "/")
                records.append({"file": rel, "source_id": "", "camera": "", "span_calibrated": "", "span_dvr": "",
                                "kind": "audit record", "sha256": sha256_file(os.path.join(root_, f))})
        certificates: Dict[str, str] = {}
        expert = part_b if part_b is not None else case.expert
        for sid in sources_used:
            src = case.source(sid)
            mine = [e for e in exhibits if e["source_id"] == sid]
            decl = (part_a or {}).get(sid, src.declarant)
            cert = certificate_data(case, mine, decl, expert, [sid])
            cert["operator"], cert["notes"] = operator, notes
            cdir = os.path.join(tmp, "certificates", sid)
            os.makedirs(cdir, exist_ok=True)
            with open(os.path.join(cdir, "certificate.json"), "w", encoding="utf-8") as fh:
                json.dump(cert, fh, indent=1)
            with open(os.path.join(cdir, "Section63_Certificate.html"), "w", encoding="utf-8") as fh:
                fh.write(certificate_html(cert))
            certificate_pdf(cert, os.path.join(cdir, "Section63_Certificate.pdf"))
            certificates[sid] = cert["exhibits_merkle_root"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["file", "kind", "certificate", "camera", "span_calibrated", "span_dvr", "sha256"])
        for e in records:
            w.writerow([e["file"], e["kind"], e["source_id"], e["camera"], e["span_calibrated"], e["span_dvr"],
                        e["sha256"]])
        with open(os.path.join(tmp, "hash_report.csv"), "w", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())
        with open(os.path.join(tmp, "README.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(README.format(case_id=case.case_id, fir=case.fir_number or "–",
                                   generated=dt.datetime.now(IST).isoformat(timespec="seconds"),
                                   tool=f"Drishti-AI (home-camera-analysis {__version__})"))
        sums: Dict[str, str] = {}
        for root_, _, fs in os.walk(tmp):
            for f in fs:
                p = os.path.join(root_, f)
                sums[os.path.relpath(p, tmp).replace(os.sep, "/")] = sha256_file(p)
        with open(os.path.join(tmp, "checksums.sha256"), "w", encoding="utf-8", newline="\n") as fh:
            for name in sorted(sums):
                fh.write(f"{sums[name]}  {name}\n")
        os.makedirs(os.path.dirname(os.path.abspath(dest_zip)), exist_ok=True)
        part = dest_zip + ".part"
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            for name in sorted(sums) + ["checksums.sha256"]:
                z.write(os.path.join(tmp, name), name)
        os.replace(part, dest_zip)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    zsha = sha256_file(dest_zip)
    case.audit.append("bundle_exported", case_id=case.case_id, zip=os.path.basename(dest_zip), zip_sha256=zsha,
                      exhibits=len(exhibits), certificates=certificates, operator=operator,
                      items=[f"{i['source_id']}/{i['event_id']}" for i in items], enhancements=list(enhancements))
    return {"zip": dest_zip, "zip_sha256": zsha, "exhibits": len(exhibits), "files": len(sums) + 1,
            "certificates": certificates}


def verify_bundle(zip_path: str) -> dict:
    """Re-hash every file of a bundle against checksums.sha256 (what `sha256sum -c` does) and
    check every certificate's hash report and Merkle root against the files."""
    import hashlib
    import re
    with zipfile.ZipFile(zip_path) as z:
        lines = z.read("checksums.sha256").decode().splitlines()
        listed = {}
        for ln in lines:
            h, name = ln.split("  ", 1)
            listed[name] = h
        bad = [n for n, h in listed.items() if n in z.namelist() and hashlib.sha256(z.read(n)).hexdigest() != h]
        missing = [n for n in listed if n not in z.namelist()]
        extra = [n for n in z.namelist() if n not in listed and n != "checksums.sha256"]
        certs = {n.split("/")[1]: json.loads(z.read(n)) for n in z.namelist()
                 if re.fullmatch(r"certificates/[^/]+/certificate\.json", n)}
    per = {}
    for sid, cert in certs.items():
        items = merkle.file_lines({e["file"]: e["sha256"] for e in cert["exhibits"]})
        per[sid] = {"merkle_ok": merkle.root(items) == cert["exhibits_merkle_root"],
                    "exhibits_match_checksums": all(listed.get(e["file"]) == e["sha256"] for e in cert["exhibits"])}
    ok = bool(certs) and not bad and not missing and not extra and \
        all(v["merkle_ok"] and v["exhibits_match_checksums"] for v in per.values())
    return {"ok": ok, "modified": bad, "missing": missing, "unlisted": extra, "certificates": per, "files": len(listed)}


__all__ = ["DEVICE_KINDS", "build_bundle", "certificate_data", "certificate_html", "certificate_pdf", "verify_bundle"]
