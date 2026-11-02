"""What the app's setup form shows. Keys are Config field names (one name, one meaning);
the form is rendered from this list and the values go straight into Config."""

GROUPS = ["Analysis features", "Number plates", "Highlights & outputs", "AI text descriptions", "Shop / site rules",
          "Advanced"]

FIELDS = [
    # key, label, group, type, help, cost hint
    ("use_faces", "Detect faces", "Analysis features", "bool",
     "Find faces of people and save a crop of each.", "small"),
    ("face_identities", "Recognise repeat faces (unique faces)", "Analysis features", "bool",
     "Group faces into persons P0001, P0002… across days; NEW faces are highlighted.", "medium"),
    ("use_pose", "Posture cues (hands up, crouching)", "Analysis features", "bool",
     "Adds a body-pose model.", "medium"),
    ("use_semantic", "Scene tags & free-text search", "Analysis features", "bool",
     "Describe keyframes with SigLIP-2 so you can search e.g. 'man in a helmet'.", "medium"),
    ("detect_tamper", "Camera tamper detection", "Analysis features", "bool",
     "Lens covered, blackout, blur, camera moved.", "none"),
    ("require_person_for_event", "Only keep events with people/vehicles/animals", "Analysis features", "bool",
     "Drops motion with nothing detected (trees, shadows).", "none"),
    ("motion_sensitivity", "Motion sensitivity", "Analysis features", "range:0.1:0.9:0.05",
     "Higher finds smaller movements but more false alarms.", "none"),
    ("use_plates", "Read vehicle number plates", "Number plates", "bool",
     "Reads plates of cars, bikes, buses and trucks.", "medium"),
    ("plate_enhance", "Enhance hard plates (fast vehicles)", "Number plates", "bool",
     "Motion-deblurs and fuses several frames of hard plates; saves the enhanced image. Much slower.", "high"),
    ("plate_vlm_read", "Ask the AI model when a plate is unreadable", "Number plates", "bool",
     "Uses the AI description model (below) as a last resort.", "high"),
    ("plate_watchlist", "Plate watchlist (comma separated)", "Number plates", "list",
     "Alert when one of these plates is seen (1 character tolerance).", "none"),
    ("make_highlight_reel", "Daily highlight videos", "Highlights & outputs", "bool",
     "One video per day with every movement, in time order, in highlights/YYYY/MM-Mon/.", "small"),
    ("reel_scope", "Highlight content", "Highlights & outputs", "choice:all,people,flagged",
     "all movements, only events with people, or only suspicious ones.", "none"),
    ("reel_overlay", "Print clock/camera text on highlight videos", "Highlights & outputs", "bool",
     "Off = original picture only.", "none"),
    ("save_clips", "Save a short clip per event", "Highlights & outputs", "bool", "", "small"),
    ("weekly_reports", "Weekly Excel reports", "Highlights & outputs", "bool", "", "none"),
    ("use_vlm", "Write an AI description of events", "AI text descriptions", "bool",
     "A vision-language model describes what happens. Slow without a GPU.", "high"),
    ("vlm_scope", "Describe which events", "AI text descriptions", "choice:flagged,people,all", "", "none"),
    ("vlm_provider", "AI model runs in", "AI text descriptions", "choice:transformers,openai",
     "transformers = built-in model on this PC; openai = your local server (Ollama, LM Studio, vLLM…).", "none"),
    ("vlm_model", "Model name", "AI text descriptions", "text",
     "e.g. Qwen/Qwen3-VL-2B-Instruct (built-in) or qwen3-vl:2b (Ollama).", "none"),
    ("vlm_base_url", "Server address (openai)", "AI text descriptions", "text",
     "e.g. http://127.0.0.1:11434/v1 (Ollama) or http://127.0.0.1:1234/v1 (LM Studio).", "none"),
    ("open_hour", "Opening time (blank = 24 h)", "Shop / site rules", "text", "e.g. 09:00", "none"),
    ("close_hour", "Closing time", "Shop / site rules", "text", "e.g. 21:30", "none"),
    ("authorized_only_cameras", "Authorised-only cameras (comma separated)", "Shop / site rules", "list",
     "Anyone not in known faces on these cameras raises an alert.", "none"),
    ("known_faces_dir", "Known faces folder (staff/owner)", "Shop / site rules", "folder",
     "Folder/<Name>/*.jpg", "none"),
    ("face_watchlist_dir", "Watchlist faces folder", "Shop / site rules", "folder",
     "Folder/<Name>/*.jpg of people to alert on.", "none"),
    ("folder_keywords", "Only folders matching (comma separated)", "Advanced", "list",
     "e.g. 6-oct, 7-oct, ch01. Blank = all folders.", "none"),
    ("timezone", "Time zone", "Advanced", "text", "", "none"),
    ("reprocess", "Analyse again videos already done", "Advanced", "bool", "", "none"),
    ("device", "Processor", "Advanced", "choice:auto,cpu,cuda", "auto uses the GPU when available.", "none"),
    ("max_runtime_minutes", "Stop after (minutes, 0 = no limit)", "Advanced", "number", "", "none"),
]

PRESET_LABELS = {"fast": "Fast (lighter models)", "balanced": "Balanced (recommended)",
                 "accurate": "Accurate (far cameras, small faces – slow)"}

# Desktop defaults differ from the Colab notebook: no GPU is assumed, so the slow AI
# description model is off until the user enables it.
APP_DEFAULTS = {"use_vlm": False, "use_semantic": False, "use_plates": False, "plate_enhance": False}


def describe():
    from ..config import Config
    d = Config()
    out = []
    for key, label, group, typ, help_, cost in FIELDS:
        default = APP_DEFAULTS.get(key, getattr(d, key))
        out.append({"key": key, "label": label, "group": group, "type": typ, "help": help_, "cost": cost,
                    "default": default})
    return {"groups": GROUPS, "fields": out, "presets": PRESET_LABELS}
