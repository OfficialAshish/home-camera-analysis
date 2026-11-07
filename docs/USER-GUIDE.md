# User guide (desktop app)

Status: **current** · Part A is for investigating officers (Drishti-AI cases: several seized DVRs,
one corrected timeline, court bundle). Part B is for shop owners, mall and security staff analysing
one recorder. No technical knowledge needed. IT set-up: `docs/gem/HANDOVER-RUNBOOK-POLICE.md`.

## Start
Double-click **CCTVAnalysis.exe** (or **run_app.bat** in a source install). A black window opens
(keep it open) and your browser shows the app. If the browser does not open, copy the link shown in
the black window into the browser.

# Part A – investigating officers

## A1. Quick investigation (the first screen)
1. **Drop the seized folders** on the page – one folder per DVR/NVR/pen drive – or click *Choose
   folder…* (the app's folder browser) or *Windows folder dialog…*. Each folder becomes a row.
   Nothing is ever written into these folders. If the app cannot find where a dropped folder is,
   it asks you to paste its path (in Windows Explorer: click the address bar, copy).
2. **Clock correction** per folder: if the DVR showed 21:25 when the true time was 21:14, type
   `-00:11:00` (DVR 11 minutes fast). `+00:05:00` = DVR 5 minutes slow. Leave `0` if unknown; you can
   change it later without analysing again.
3. Optional: FIR number, police station, investigating officer, a case ID (blank = automatic).
4. **Balanced** suits most laptops; **Fast** for a lot of footage. Keep *Describe-it search* on if
   you want to search with words ("man in a red helmet"); untick *Faces* or *Number plates* if not
   needed (each adds time).
5. **Run investigation.** The *Progress* page shows *Source 2 of 4 (Traffic North)*, the stage (first
   the SHA-256 of every seized file, then lossless playback copies, then the analysis) and the time
   left. *Stop* finishes the current step; the next run continues where it stopped.
6. When it says *done*, click **Open the case timeline**.

## A2. Timeline & search
* The lanes show every event of every DVR on **one corrected clock** (calibrated time = DVR time +
  correction). Each card shows the calibrated time in bold and the DVR's own time under it.
* Click an event: the recording plays at the event; the people, plates and snapshots are listed.
  * **Find this person on all cameras** – people with the same clothing and build on every DVR.
  * **Follow face P0003** – the same face on every DVR (found by face similarity; the numbers differ
    between DVRs).
  * **Find this vehicle** – the plate on every DVR, also with one misread character.
  * **Open in Forensic Studio at this moment**, **Add to court bundle**.
* **Describe it**: type what you look for, e.g. *man in a red helmet on a motorbike*. The most
  similar pictures from all DVRs come first. **Clothing colours**: choose top and bottom colours.
  **Number plate**: type a plate.
* Similarity ranks pictures for you to look at; it is not proof. Always look at the footage.

## A3. Journey
Every sighting of the person, face or vehicle, in calibrated time order. Untick the wrong ones,
click **Rebuild journey**: the route shows each camera stop with arrival, departure and the travel
time to the next. With map positions of the cameras (*Cases › source › Map position*) the route is
also drawn on a plan (no internet map is used). **Make highlight reel** makes one video of the
ticked sightings; every frame shows source, camera, corrected time and the DVR's own time.
Sightings that share the same seconds of a recording (several people in one event, one person
seen as several tracks) are joined into one piece, so no second of footage appears twice; the
reel's index lists which sightings each piece shows.

## A4. Forensic Studio
The frame is decoded from the original recording at the time shown (change the time and *Load
frame*). Add filters: *CLAHE* (dark or backlit faces), *Levels*, *Denoise*, *UnsharpMask* /
*HighPass* (edges), *WienerDeblur* / *RichardsonLucy* (motion or focus blur: set length and angle of
the blur), *Perspective* (click the four corners of a plate or sign on the original), *Crop*,
*Upscale*. *Frames to stack* combines 3–9 frames to reduce noise. The preview follows every change.
**Save enhanced still**: the still is kept with its input, the filter log and SHA-256, and the app
checks that re-running the filters gives exactly the same picture. Filters show detail that is in
the recording; they cannot add detail that is not.

## A5. Court bundle (Section 63, BSA 2023)
1. Records come from *Add to court bundle* (events) and *Journey › Add ticked*; tick the enhanced
   stills to include.
2. **Part A – one per device**: the person in charge of that DVR (shop owner, RWA secretary, traffic
   control operator): name, relation, address, place. **Part B**: the expert, with designation.
   What you type is kept for the next export (and logged).
3. *Preview certificate* shows each device's certificate as it will print.
4. **Create court bundle** → download the ZIP. Write the **ZIP SHA-256** in the case diary.
5. Print `certificates/<device>/Section63_Certificate.pdf` for each device and have Part A and Part B
   signed. The app never signs and never fills in a person's details you did not type.
6. *Exports › Verify* re-checks every file of a bundle; it must say *intact*.

## A6. Cases screen
All cases; open one to edit its details, each source's label, clock correction (and per-camera
corrections such as `ch03=+00:02:00`), device particulars for the certificate (type, make, model,
serial, storage, custodian, Owned/Maintained/Managed/Operated), Part A, map position. *Analyse all
sources* runs again (only new work is done). *Show audit log* lists every change with who and when,
and says whether the log is intact. A source whose drive now has another letter shows *FOLDER NOT
FOUND*: plug the drive in or *Relink…* to the new place – the app accepts it only if every file
matches its acquisition SHA-256.

# Part B – one recorder (shops, malls, security staff)

## 2. Set up
Click **Folder analysis**.
1. **Folder with CCTV recordings** → *Browse…* → open the folder your DVR/NVR exports to → *Use this folder*.
   Sub-folders are included.
2. **Save results to** → choose or create an empty folder (not inside the recordings folder).
3. **Speed**: *Balanced* is right for most computers. *Fast* for lots of footage, *Accurate* for far cameras.
4. Tick what you need. Items marked *slow* add a lot of time; leave them off unless you need them:
   * *Recognise repeat faces* – tells you when a new, never-seen person appears.
   * *Read vehicle number plates* – reads plates of cars/bikes; add plates to watch for in *Plate watchlist*.
   * *Enhance hard plates* – tries harder on plates of fast vehicles (much slower).
   * *Write an AI description* – a sentence about what happens in each event (slow without a graphics card).
   * *Opening/closing time* – movement outside these hours scores higher.
5. **Check computer** shows whether the computer is suitable (memory, graphics card, disk space).
6. **Scan recordings** counts the videos and hours of footage. Files whose name has no time are listed:
   their time is read from the clock printed on the video.
7. **Measure speed** (optional, about a minute) measures this computer's real speed. **Estimate time**
   then shows how long the analysis should take. It's an estimate: real time depends on how much
   movement the footage contains.
8. **Start analysis.**

## 3. While it runs
The *Progress* page shows the current video, the percentage, the time left and a log. *Stop* ends
after the current step. Finished videos are kept and the rest continues the next time you start.
You can close the browser; the analysis continues while the black window is open.

## 4. Results
* **Results**: filter by date, camera and level (HIGH / REVIEW first), search words (*running*,
  *bag*), a person number or a plate. Click an event for snapshots, the clip, the reasons, plates
  and people. Set a review status (*Suspicious*, *False alarm*, …) and notes.
* **Highlights**: one video per day with every movement in time order, in
  `highlights/<year>/<month>/` of the results folder. Click a line under the video to jump to it.
* **Number plates**: type a plate (or part of one). *Tolerance 1* also finds plates read with one wrong character.
* **Faces**: one card per person. Yellow = first seen in the latest run.
* **Fix recording times**: for folders analysed with an older version whose times look wrong.

## 5. Evidence for police or insurance
On an event click **Add to evidence**, then go to **Export evidence**, enter a case number and your
name, and click **Create package**. The ZIP contains the original recording piece (not re-encoded),
pictures, a printable report and fingerprints (SHA-256) of every file. Tick *blur faces* when the
package goes to someone who must not see bystanders.

## 6. Licence
*Settings › Licence* shows the status. Paste the key you received and click *Activate*. Without a
licence the app works in trial mode (5 videos per run).

## Good to know
* Scores and AI text help you find what to watch. They are not proof. Always check the original video.
* Inform people that CCTV is in use and follow local privacy law when you keep or share face data.
