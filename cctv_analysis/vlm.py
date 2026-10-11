"""Vision-language model (default Qwen3-VL-2B-Instruct) that writes a short
natural-language description of each event's contact sheet, gives its own
suspicious/not verdict, and can read the DVR's burnt-in clock (OSD)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Optional

import numpy as np

log = logging.getLogger("cctv")


class VLM:
    def __init__(self, cfg, device: str):
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor
        self.torch = torch
        self.cfg = cfg
        cuda = device.startswith("cuda")
        if cuda:
            if torch.cuda.is_bf16_supported():
                dtype = torch.bfloat16
            else:
                # T4 & older: fp16 can overflow in Qwen-VL; small models fit comfortably in fp32
                small = any(t in cfg.vlm_model for t in ("256M", "500M", "0.5B", "1B", "2B"))
                dtype = torch.float32 if small else torch.float16
        else:
            dtype = torch.float32
        self.model = AutoModelForImageTextToText.from_pretrained(
            cfg.vlm_model, dtype=dtype, device_map="auto" if cuda else None).eval()
        if not cuda:
            self.model.to("cpu")
        self.proc = AutoProcessor.from_pretrained(cfg.vlm_model)

    def ask(self, img_bgr: np.ndarray, prompt: str, max_new_tokens: int = 160, max_side: int = 1280) -> str:
        from PIL import Image
        h, w = img_bgr.shape[:2]
        s = min(1.0, max_side / max(h, w))
        im = Image.fromarray(img_bgr[:, :, ::-1])
        if s < 1:
            im = im.resize((int(w * s), int(h * s)))
        messages = [{"role": "user", "content": [{"type": "image", "image": im},
                                                 {"type": "text", "text": prompt}]}]
        inputs = self.proc.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                               return_dict=True, return_tensors="pt")
        inputs = inputs.to(self.model.device)
        with self.torch.no_grad():
            out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        gen = out[:, inputs["input_ids"].shape[1]:]
        return self.proc.batch_decode(gen, skip_special_tokens=True)[0].strip()

    def describe_event(self, sheet_bgr: np.ndarray, context: str = "") -> dict:
        prompt = self.cfg.vlm_prompt + (f"\nContext from detectors: {context}" if context else "")
        txt = self.ask(sheet_bgr, prompt, self.cfg.vlm_max_new_tokens)
        return parse_verdict(txt)

    def read_clock(self, frame_bgr: np.ndarray) -> Optional[dt.datetime]:
        txt = self.ask(frame_bgr, "Read the date and time printed on this CCTV frame. Reply ONLY in the "
                                  "format YYYY-MM-DD HH:MM:SS, or NONE if there is no timestamp.", 40)
        return parse_clock(txt)


def parse_verdict(txt: str) -> dict:
    out = {"description": txt.strip(), "suspicious": None, "category": "", "confidence": None,
           "reason": "", "raw": txt}
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        raw = m.group(0)
        for cand in (raw, raw.replace("'", '"'), re.sub(r",\s*}", "}", raw)):
            try:
                j = json.loads(cand)
                out.update({k: j.get(k, out[k]) for k in ("description", "suspicious", "category",
                                                          "confidence", "reason")})
                break
            except Exception:
                continue
    if isinstance(out["suspicious"], str):
        out["suspicious"] = out["suspicious"].strip().lower() in ("true", "yes", "1")
    if out["suspicious"] is None:
        low = txt.lower()
        out["suspicious"] = bool(re.search(r"\b(suspicious|theft|steal|stealing|fight|weapon|conceal)", low)) \
            and not re.search(r"not suspicious|nothing suspicious|no suspicious", low)
    try:
        out["confidence"] = float(out["confidence"]) if out["confidence"] is not None else None
    except Exception:
        out["confidence"] = None
    return out


def parse_clock(txt: str) -> Optional[dt.datetime]:
    m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})[ T]+(\d{1,2}):(\d{2}):(\d{2})", txt)
    if m:
        try:
            return dt.datetime(*map(int, m.groups()))
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})[ T]+(\d{1,2}):(\d{2}):(\d{2})", txt)
    if m:
        d, mo, y, H, M, S = map(int, m.groups())
        try:
            return dt.datetime(y, mo, d, H, M, S)
        except ValueError:
            try:
                return dt.datetime(y, d, mo, H, M, S)   # month-first DVRs
            except ValueError:
                pass
    return None
