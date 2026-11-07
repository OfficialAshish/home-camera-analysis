"""Vision-language model (VLM) providers: event descriptions + suspicious verdicts,
and image reading used for plates/clock when OCR is not enough.

* ``transformers`` – a local Hugging Face model (default Qwen3-VL-2B-Instruct).
* ``openai``       – any OpenAI-compatible ``/v1/chat/completions`` server running a vision
                     model: Ollama, LM Studio, vLLM, llama.cpp server, hosted APIs (OpenAI,
                     NVIDIA NIM / integrate.api.nvidia.com) …
* ``nvidia``       – convenience alias for NVIDIA NIM / integrate.api.nvidia.com endpoints.

Wire format (openai / nvidia provider): OpenAI Chat Completions with an image content part
``{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,…"}}`` – see
https://platform.openai.com/docs/api-reference/chat/create . Ollama, vLLM, and NVIDIA NIM
all accept this object form.
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


def normalize_vlm_base_url(url: str) -> str:
    """Strip trailing slashes and /chat/completions suffix if user passed full invocation URL."""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/chat/completions"):
        u = u[:-len("/chat/completions")].rstrip("/")
    return u


def resolve_chat_completion_url(url: str) -> str:
    """Given a base URL, full endpoint, or host, return the exact POST /chat/completions URL."""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    if "api.nvidia.com" in u and not u.endswith("/v1"):
        return f"{u}/v1/chat/completions"
    return f"{u}/chat/completions"


class OpenAICompatVLM(VLMBase):
    """Any server implementing OpenAI's POST {base_url}/chat/completions with image inputs
    (Ollama, LM Studio, vLLM, llama.cpp server, NVIDIA NIM / integrate.api.nvidia.com, OpenAI …)."""
    name = "openai"

    def __init__(self, cfg, session=None):
        super().__init__(cfg)
        raw_url = (cfg.vlm_base_url or "").strip()
        if not raw_url and cfg.vlm_provider == "nvidia":
            raw_url = "https://integrate.api.nvidia.com/v1"
        if not raw_url:
            raise RuntimeError("NOT_CONFIGURED: vlm_base_url is empty (e.g. http://127.0.0.1:11434/v1 for Ollama)")
        import requests
        self.http = session or requests.Session()
        self.url = resolve_chat_completion_url(raw_url)
        self.key = os.environ.get(cfg.vlm_api_key_env or "", "")

    def payload(self, img_bgr: np.ndarray, prompt: str, max_new_tokens: int) -> dict:
        import cv2
        ok, buf = cv2.imencode(".jpg", _resize(img_bgr, 1280), [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            raise ValueError("could not encode image")
        b64 = base64.b64encode(buf.tobytes()).decode()
        # For hosted/reasoning models (e.g. NVIDIA NIM / Mistral Large reasoning), reasoning tokens consume token budget.
        # Ensure at least 512 tokens so reasoning does not exhaust max_tokens before producing content.
        is_cloud_or_reasoning = "api.nvidia.com" in self.url or any(k in (self.cfg.vlm_model or "").lower() for k in ("mistral", "reasoning", "r1"))
        tokens = max(int(max_new_tokens), 512) if is_cloud_or_reasoning else int(max_new_tokens)
        temp = 0.2 if is_cloud_or_reasoning else 0
        return {"model": self.cfg.vlm_model, "max_tokens": tokens, "temperature": temp,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}

    def ask(self, img_bgr, prompt, max_new_tokens=160):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        body = self.payload(img_bgr, prompt, max_new_tokens)
        r = self.http.post(self.url, json=body, headers=headers, timeout=self.cfg.vlm_timeout)
        if r.status_code == 400:
            # Fallback if server rejects temperature or specific parameter
            fallback_body = {
                "model": self.cfg.vlm_model,
                "max_tokens": max(int(max_new_tokens), 256),
                "messages": body["messages"],
            }
            r_fb = self.http.post(self.url, json=fallback_body, headers=headers, timeout=self.cfg.vlm_timeout)
            if r_fb.status_code == 200:
                r = r_fb
        if r.status_code != 200:
            raise RuntimeError(f"VLM server returned HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        try:
            choice = j["choices"][0]
            msg = choice.get("message", {})
            content = msg.get("content")
            # Fallback for reasoning models where answer is in reasoning_content or finish_reason is length
            if not content and msg.get("reasoning_content"):
                content = msg["reasoning_content"]
            if content is None and "text" in choice:
                content = choice["text"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"unexpected VLM response shape: {str(j)[:200]}")
        if isinstance(content, list):          # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return (content or "").strip()


def list_server_models(base_url: str, api_key: str = "", timeout: float = 10.0, model: str = "") -> List[str]:
    """Probe {base_url}/models (OpenAI 'List models') or fallback to /chat/completions; used by 'Test connection'."""
    import requests
    base = normalize_vlm_base_url(base_url)
    h = {"Accept": "application/json"}
    if api_key:
        h["Authorization"] = "Bearer " + api_key

    candidates = []
    if base:
        candidates.append(f"{base}/models")
        if not base.endswith("/v1") and "/v1/" not in base:
            candidates.append(f"{base}/v1/models")
        if "/v1" in base:
            candidates.append(base.rsplit("/v1", 1)[0] + "/models")

    last_err = None
    for cand in candidates:
        try:
            r = requests.get(cand, headers=h, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                items = data.get("data") if isinstance(data, dict) else (data if isinstance(data, list) else [])
                if isinstance(items, list) and items:
                    ids = [m.get("id") or m.get("name") if isinstance(m, dict) else str(m) for m in items]
                    return sorted(filter(None, ids))
        except Exception as e:
            last_err = e

    # Fallback: if /models endpoint is not exposed (common on hosted NVIDIA NIM invoke URLs / custom proxies),
    # probe the /chat/completions endpoint directly to verify connectivity and auth.
    chat_url = resolve_chat_completion_url(base_url)
    probe_model = model.strip() or "mistralai/mistral-large-4-1t-a52b"
    test_payload = {
        "model": probe_model,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
        "max_tokens": 1,
    }
    try:
        r = requests.post(chat_url, headers={"Content-Type": "application/json", **h}, json=test_payload, timeout=timeout)
        if r.status_code == 200:
            return [probe_model, "(connected via /chat/completions)"]
        if r.status_code in (401, 403):
            raise RuntimeError(f"Authentication failed (HTTP {r.status_code}): {r.text[:200]}")
        if r.status_code in (400, 422):
            msg = r.text[:200]
            if "model" in msg.lower():
                return [f"(server connected, specify valid model: {probe_model})"]
            return [f"(server connected via chat/completions, status {r.status_code})"]
        r.raise_for_status()
    except Exception as e:
        if last_err and not isinstance(e, RuntimeError):
            raise last_err
        raise e
    return []


def make_vlm(cfg, device: str) -> VLMBase:
    if cfg.vlm_provider in ("openai", "nvidia"):
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
