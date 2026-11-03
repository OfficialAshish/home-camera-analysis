# User guide (desktop app)

Status: **current** · For shop owners, mall and security staff. No technical knowledge needed.

## 1. Start
Double-click **CCTVAnalysis.exe**. A black window opens (keep it open) and your browser shows the app.
If the browser does not open, copy the link shown in the black window into the browser.

## 2. Set up
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
