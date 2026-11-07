# Section 63, Bharatiya Sakshya Adhiniyam, 2023 — text the certificate follows

Status: **current** · For: developers, legal reviewers · Used by `cctv_analysis/section63.py`

## Source
* Document: *The Bharatiya Sakshya (Second) Bill, 2023*, Bill No. 289 of 2023, as introduced in
  Lok Sabha on 9 December 2023 (enacted as the Bharatiya Sakshya Adhiniyam, 2023, Act 47 of 2023).
* Retrieved 2026-11-07 from
  `https://prsindia.org/files/bills_acts/bills_parliament/2023/Bhartiya_Sakshya_(Second)_Bill_2023.pdf`
  (79 pages; SHA-256 `7c0f918b01ed2cf4afb825f7e5a3c3cbbdfbbcecf62fe7d6a2244322e56a062c`).
* **UNVERIFIED:** the Gazette copy of the Act could not be retrieved (egazette.gov.in answered
  HTTP 503; indiacode.nic.in timed out). Before the certificate is first used in a case, the
  department's legal cell must compare the text below with the Gazette text of Act 47 of 2023.
  If anything differs, update `section63.py` and this file. The certificate's wording is all in
  one place: `certificate_html` / `certificate_pdf`.

## Section 63(4) (quoted)
> (4) In any proceeding where it is desired to give a statement in evidence by virtue of this
> section, a certificate doing any of the following things shall be submitted along with the
> electronic record at each instance where it is being submitted for admission, namely:—
> (a) identifying the electronic record containing the statement and describing the manner in
> which it was produced;
> (b) giving such particulars of any device involved in the production of that electronic record
> as may be appropriate for the purpose of showing that the electronic record was produced by a
> computer or a communication device referred to in clauses (a) to (e) of sub-section (3);
> (c) dealing with any of the matters to which the conditions mentioned in sub-section (2) relate,
> and purporting to be signed by a person in charge of the computer or communication device or the
> management of the relevant activities (whichever is appropriate) and an expert shall be evidence
> of any matter stated in the certificate; and for the purposes of this sub-section it shall be
> sufficient for a matter to be stated to the best of the knowledge and belief of the person
> stating it in the certificate specified in the Schedule.

## The Schedule [See section 63(4)(c)] — CERTIFICATE (quoted)

**PART A (To be filled by the Party)**

> I, ______ (Name), Son/daughter/spouse of ______ residing/employed at ______ do hereby solemnly
> affirm and sincerely state and submit as follows:—
> I have produced electronic record/output of the digital record taken from the following
> device/digital record source (tick mark):—
> Computer / Storage Media ☐ DVR ☐ Mobile ☐ Flash Drive ☐ CD/DVD ☐ Server ☐ Cloud ☐ Other ☐
> Other: ______
> Make & Model: ______ Color: ______
> Serial Number: ______
> IMEI/UIN/UID/MAC/Cloud ID ______ (as applicable)
> and any other relevant information, if any, about the device/digital record ____ (specify).
> The digital device or the digital record source was under the lawful control for regularly
> creating, storing or processing information for the purposes of carrying out regular activities
> and during this period, the computer or the communication device was working properly and the
> relevant information was regularly fed into the computer during the ordinary course of business.
> If the computer/digital device at any point of time was not working properly or out of
> operation, then it has not affected the electronic/digital record or its accuracy. The digital
> device or the source of the digital record is:—
> Owned ☐ Maintained ☐ Managed ☐ Operated ☐ by me (select as applicable).
> I state that the HASH value/s of the electronic/digital record/s is ______, obtained through the
> following algorithm:— SHA1: SHA256: MD5: Other ______ (Legally acceptable standard)
> (Hash report to be enclosed with the certificate)
> (Name and signature)
> Date (DD/MM/YYYY): ___ Time (IST): ___ hours (In 24 hours format) Place: ___

**PART B (To be filled by the Expert)**

> I, ______ (Name), Son/daughter/spouse of ______ residing/employed at ______ do hereby solemnly
> affirm and sincerely state and submit as follows:—
> The produced electronic record/output of the digital record are obtained from the following
> device/digital record source (tick mark):— [the same device boxes and particulars as Part A]
> I state that the HASH value/s of the electronic/digital record/s is ______, obtained through the
> following algorithm:— SHA1: SHA256: MD5: Other ______ (Legally acceptable standard)
> (Hash report to be enclosed with the certificate)
> (Name, designation and signature)
> Date (DD/MM/YYYY): ___ Time (IST): ___ hours (In 24 hours format) Place: ___

## How the tool fills it
| Schedule item | Filled from | Left for the declarant |
|---|---|---|
| Name, relation, address, place (Part A) | typed once per source (*Court bundle › Part A*), audited | yes, if not typed |
| Name, relation, address, designation, place (Part B) | typed once per case, audited | yes, if not typed |
| Device type ticks | each source's *device kind* (an NVR is ticked as **DVR**: the Schedule has no NVR box; "Other" plus the text for anything else) | — |
| Make & Model, Colour, Serial, IMEI/UIN/UID/MAC/Cloud ID | the source's device particulars | blanks print as lines |
| Other relevant information | storage medium, operating system, custodian, clock correction, file count, Merkle root | — |
| Owned / Maintained / Managed / Operated | the source's *control* selection | — |
| Hash value(s) | "as listed in the enclosed hash report" + the Merkle root of the listed values | — |
| Algorithm | **SHA256** ticked | — |
| Hash report | enclosed table: file name, camera, timestamp span (calibrated and DVR clock), SHA-256; plus the acquisition SHA-256 of every seized file | — |
| Signature | never filled | **always** |
| Date, Time (IST, 24 h), Place | date/time of generation; place if typed | place if not typed |

**One certificate per device.** Section 63(4)(c) asks for a certificate signed by "a person in
charge of the computer or communication device or the management of the relevant activities" and
an expert. A shop owner is in charge of his DVR and of nothing else, so a bundle with recordings of
three DVRs carries three certificates (`certificates/<source>/`), each listing one device, the
records produced from it and its acquisition hashes; Part B is the same expert on each.

The tool does not affirm anything on anyone's behalf: the declarations are made by the people who
sign Part A and Part B, who must check every particular before signing.
