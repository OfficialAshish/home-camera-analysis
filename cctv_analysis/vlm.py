"""Vision-language model (VLM) providers: event descriptions + suspicious verdicts,
and image reading used for plates/clock when OCR is not enough.

* ``transformers`` – a local Hugging Face model (default Qwen3-VL-2B-Instruct).
* ``openai``       – any OpenAI-compatible ``/v1/chat/completions`` server running a vision
                     model: Ollama, LM Studio, vLLM, llama.cpp server, a hosted API …

Wire format (openai provider): OpenAI Chat Completions with an image content part
``{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,…"}}`` – see
https://platform.openai.com/docs/api-reference/chat/create . Ollama accepts this object form
(ollama/openai/openai.go, case "image_url", which takes either a {"url"} object or a string).
The API key is read from the environment variable named by ``Config.vlm_api_key_env``; it is
never written to config files or logs.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import List

import numpy as np

log = logging.getLogger("cctv")


class VLMBase:
    name = "base"

    def __init__(self, cfg):
        self.cfg = cfg

    def ask(self, img_bgr: np.ndarray, prompt: str, max_new_tokens: int = 160) -> str:  # pragma: no cover
        raise NotImplementedError

    def describe_event(self, sheet_bgr: np.ndarray, context: str = "") -> dict:
        prompt = self.cfg.vlm_prompt + (f"\nContext from detectors: {context}" if context else "")
        return parse_verdict(self.ask(sheet_bgr, prompt, self.cfg.vlm_max_new_tokens))

    def read_plate(self, crop_bgr: np.ndarray) -> str:
        txt = self.ask(crop_bgr, "This is a vehicle number plate. Reply ONLY with the plate characters "
                                 "(letters and digits, no spaces), or NONE if unreadable.", 24)
        t = re.sub(r"[^A-Z0-9]", "", txt.upper())
        return "" if t in ("", "NONE") else t


def _resize(img_bgr: np.ndarray, max_side: int) -> np.ndarray:
    import cv2
    h, w = img_bgr.shape[:2]
    s = min(1.0, max_side / max(h, w))
    return cv2.resize(img_bgr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else img_bgr


class TransformersVLM(VLMBase):
    name = "transformers"

    def __init__(self, cfg, device: str):
        super().__init__(cfg)
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor
        self.torch = torch
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
        from . import modelcache
        cd = modelcache.hf_dir()
        self.model = AutoModelForImageTextToText.from_pretrained(
            cfg.vlm_model, dtype=dtype, device_map="auto" if cuda else None, cache_dir=cd).eval()
        if not cuda:
            self.model.to("cpu")
        self.proc = AutoProcessor.from_pretrained(cfg.vlm_model, cache_dir=cd)

    def ask(self, img_bgr, prompt, max_new_tokens=160):
        from PIL import Image
        im = Image.fromarray(_resize(img_bgr, 1280)[:, :, ::-1])
        messages = [{"role": "user", "content": [{"type": "image", "image": im},
                                                 {"type": "text", "text": prompt}]}]
        inputs = self.proc.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                               return_dict=True, return_tensors="pt").to(self.model.device)
        with self.torch.no_grad():
            out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        gen = out[:, inputs["input_ids"].shape[1]:]
        return self.proc.batch_decode(gen, skip_special_tokens=True)[0].strip()


class OpenAICompatVLM(VLMBase):
    """Any server implementing OpenAI's POST {base_url}/chat/completions with image inputs."""
    name = "openai"

    def __init__(self, cfg, session=None):
        super().__init__(cfg)
        if not (cfg.vlm_base_url or "").strip():
            raise RuntimeError("NOT_CONFIGURED: vlm_base_url is empty (e.g. http://127.0.0.1:11434/v1 for Ollama)")
        import requests
        self.http = session or requests.Session()
        self.url = cfg.vlm_base_url.rstrip("/") + "/chat/completions"
        self.key = os.environ.get(cfg.vlm_api_key_env or "", "")

    def payload(self, img_bgr: np.ndarray, prompt: str, max_new_tokens: int) -> dict:
        import cv2
        ok, buf = cv2.imencode(".jpg", _resize(img_bgr, 1280), [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            raise ValueError("could not encode image")
        b64 = base64.b64encode(buf.tobytes()).decode()
        return {"model": self.cfg.vlm_model, "max_tokens": int(max_new_tokens), "temperature": 0,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}

    def ask(self, img_bgr, prompt, max_new_tokens=160):
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        r = self.http.post(self.url, json=self.payload(img_bgr, prompt, max_new_tokens), headers=headers,
                           timeout=self.cfg.vlm_timeout)
        if r.status_code != 200:
            raise RuntimeError(f"VLM server returned HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        try:
            content = j["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"unexpected VLM response shape: {str(j)[:200]}")
        if isinstance(content, list):          # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return (content or "").strip()


def list_server_models(base_url: str, api_key: str = "", timeout: float = 10.0) -> List[str]:
    """GET {base_url}/models (OpenAI 'List models'); used by the app's 'Test connection'."""
    import requests
    h = {"Authorization": "Bearer " + api_key} if api_key else {}
    r = requests.get(base_url.rstrip("/") + "/models", headers=h, timeout=timeout)
    r.raise_for_status()
    return sorted(m.get("id", "") for m in r.json().get("data", []) if isinstance(m, dict))


def make_vlm(cfg, device: str) -> VLMBase:
    if cfg.vlm_provider == "openai":
        return OpenAICompatVLM(cfg)
    if cfg.vlm_provider == "transformers":
        return TransformersVLM(cfg, device)
    raise ValueError(f"unknown vlm_provider {cfg.vlm_provider!r}")


def parse_verdict(txt: str) -> dict:
    out: dict = {"description": txt.strip(), "suspicious": None, "category": "", "confidence": None,
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
