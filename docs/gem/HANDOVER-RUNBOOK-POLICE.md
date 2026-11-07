# Drishti-AI — handover runbook for police units

Status: **current** · For the district/unit IT nodal officer and the investigating officers who use
the workstation. Every step names what you should see, so that a wrong result is noticed at once.

## 1. Installation (once per computer)
1. **Hardware:** Windows 10/11 64-bit, 8 GB RAM or more, a second disk or a large partition for
   case data (playback copies are as large as the originals).
2. **Packaged build:** copy the `CCTVAnalysis` folder to `C:\Program Files\Drishti-AI\` (or any
   folder) and create a desktop shortcut to `CCTVAnalysis.exe`.
   **Source install:** copy the repository folder and double-click `run_app.bat`; the first start
   offers to install the packages into `.venv\` (needs internet once, about 2 GB). Unattended:
   `run_app.bat --install --self-test` in a Command Prompt.
3. **Check:** in a Command Prompt in that folder run `CCTVAnalysis.exe --self-test` (or
   `run_app.bat --self-test`). Expected: a line ending with `'certificate_pdf': True,
   'forensic_filters': True, 'appearance_vector': True}` and exit code 0 (`echo %ERRORLEVEL%`
   prints `0`).
4. **Models for offline use:** start the app, *Settings › Models*, download every model you will
   use (at least the object detector of your preset; *Scene search (SigLIP-2)* for describe-it
   search). On computers without internet copy the `models` folder from a prepared computer into
   `%APPDATA%\CCTVAnalysis\models\`.
5. **Licence:** *Settings › Licence* shows this computer's ID; send it to the seller, paste the key
   you receive, *Activate*. Expected: the badge shows *Licensed · N days left*.
6. **Cases folder:** *Cases › Change…* → a folder on the large disk, e.g. `D:\Drishti-Cases`.
7. **Operator name:** *Settings › Operator* → the officer's name and rank (it is written into
   every audit entry and export).

## 2. Seizure and acquisition
1. Never open the seized DVR's disk in Windows Explorer for anything but copying. Export or copy
   the recordings to a pen drive or disk as the seizure procedure prescribes; one folder per device.
2. Write down the DVR's displayed time and the true time (a phone clock synchronised to network
   time) at the moment of seizure: the difference is the **clock correction**. DVR 11 minutes fast →
   `-00:11:00`.
3. In Drishti-AI: *Quick investigation* → drop the folders (or *Add folder…*) → enter each clock
   correction → FIR number, police station, officer → *Run investigation*.
4. The first stage computes the **SHA-256 of every seized file** (the acquisition hash) and stores
   it in the case; nothing is ever written into the seized folders. Note the *Acquisition Merkle
   root* of each source (*Cases* screen) in the case diary.

## 3. Investigation
* **Timeline & search:** every event of every DVR on one corrected clock. *Describe it* ("man in a
  red helmet on a motorbike"), *Clothing colours*, *Number plate*. Open an event to play it, follow
  a person (*Find this person on all cameras*), a face (*Follow face*) or a vehicle.
* **Journey:** untick wrong matches, *Rebuild journey*, *Make highlight reel* (each frame shows the
  source, camera, corrected time and the DVR's own time).
* **Correcting a clock later:** *Cases › source › Clock correction › Save*. Takes effect at once,
  no re-analysis; the change is in the audit log.
* **Forensic Studio:** from an event, *Open in Forensic Studio at this moment*. Add filters (CLAHE
  for dark footage, Wiener/Richardson–Lucy for motion blur, Perspective for a plate seen at an
  angle, frame stacking for noise), *Save enhanced still*. Each still keeps its input, its filter
  log and hashes, and is checked to reproduce bit for bit.

## 4. Producing evidence (Section 63 BSA 2023)
1. Add the events and stills you will produce (*Add to court bundle*).
2. *Court bundle*: fill **Part A for each device** (the person in charge of that DVR: shop owner,
   RWA secretary, traffic-control operator) and **Part B** (the expert). *Preview certificate*
   shows each device's certificate as it will print.
3. *Create court bundle*. Note the **ZIP SHA-256** shown in the case diary / seizure memo.
4. Print `certificates/<source>/Section63_Certificate.pdf` from the ZIP for each device; the
   declarants check every particular and sign. The software never signs or fills in a person's
   particulars.
5. Before producing the bundle: *Exports › Verify* must say *intact*. Anyone can re-check the
   extracted bundle with `sha256sum -c checksums.sha256` (Linux) or `Get-FileHash` (Windows) –
   see `README.txt` inside the ZIP.

## 5. Backup and retention
* Back up the whole cases folder (each case is self-contained: `case.json`, audit log, analysis,
  enhancements, exports) to the unit's backup disk after each working day.
* Keep the seized media as the seizure procedure prescribes; Drishti-AI's copies do not replace
  them. A playback copy is a lossless remux for viewing; the original remains the evidence.
* Delete cases only as the retention rules allow; deleting a folder is not logged by the app.

## 6. Troubleshooting
| Symptom | Cause and fix |
|---|---|
| Source card says *FOLDER NOT FOUND* | The drive is unplugged or has another letter. Plug it in, or *Relink…* to the new location: the app re-checks every file's SHA-256 against the acquisition hash and refuses a folder that differs |
| *Describe it* says no source was analysed with scene search | Tick *Describe-it search* and run the case again (finished videos are skipped; only new work is done) |
| *Describe it* says the scene-search model is not installed | *Settings › Models › Scene search (SigLIP-2) › Download* (or copy the models folder) |
| Video does not play in the event view | The browser cannot play that format and no lossless copy could be made; use *Open in Forensic Studio* to see the frames, or open the original in VLC |
| Analysis very slow | Use the *Fast* preset; untick faces/plates/describe-it search if not needed; close other programs (above 85 % memory the app slows itself down to stay stable) |
| *Trial* badge | No valid licence: at most 5 videos per run. Install the key (section 1.5) |
| Anything else | Note the time, copy the last lines of the black console window and of *Progress › Log*, and contact support `[[SELLER: contact]]`. Never send footage outside the office. |
