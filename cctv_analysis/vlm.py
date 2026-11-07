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
from typing import Dict, List, Optional

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


PROVIDERS = ("transformers", "openai", "nvidia")
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"     # the 'nvidia' provider's default address


def provider_specs(cfg) -> List[dict]:
    """The configured providers in fallback order: the primary (``vlm_provider`` / ``vlm_model`` /
    ``vlm_base_url`` / ``vlm_api_key_env``) followed by ``vlm_fallbacks``."""
    first = {"provider": cfg.vlm_provider, "model": cfg.vlm_model, "base_url": cfg.vlm_base_url,
             "api_key_env": cfg.vlm_api_key_env}
    return [first] + [dict(f) for f in (getattr(cfg, "vlm_fallbacks", None) or [])]


def check_spec(spec: dict) -> List[str]:
    """Problems with one provider entry (empty list = usable)."""
    p = []
    if spec.get("provider") not in PROVIDERS:
        p.append(f"provider {spec.get('provider')!r} is not one of {list(PROVIDERS)}")
    if not str(spec.get("model") or "").strip():
        p.append("model name is empty")
    if spec.get("provider") == "openai" and not str(spec.get("base_url") or "").strip():
        p.append("NOT_CONFIGURED: vlm_base_url (server address) is required for an OpenAI-compatible server "
                 "(e.g. http://127.0.0.1:11434/v1 for Ollama)")
    return p


def resolve_chat_completion_url(url: str) -> str:
    """Given a base URL, the full endpoint (users paste NVIDIA's invoke URL), or the NVIDIA host
    without /v1: the exact POST …/chat/completions URL."""
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

    def __init__(self, cfg, session=None, spec: Optional[dict] = None):
        super().__init__(cfg)
        spec = spec or provider_specs(cfg)[0]
        raw_url = (spec.get("base_url") or "").strip() or (NVIDIA_BASE_URL if spec.get("provider") == "nvidia" else "")
        if not raw_url:
            raise RuntimeError("NOT_CONFIGURED: vlm_base_url is empty (e.g. http://127.0.0.1:11434/v1 for Ollama)")
        import requests
        self.http = session or requests.Session()
        self.url = resolve_chat_completion_url(raw_url)
        self.model = spec.get("model") or cfg.vlm_model
        self.key = os.environ.get(spec.get("api_key_env") or "", "")
        self.label = f"{spec.get('provider')}:{self.model}"

    def payload(self, img_bgr: Optional[np.ndarray], prompt: str, max_new_tokens: int) -> dict:
        content: list = [{"type": "text", "text": prompt}]
        if img_bgr is not None:
            import cv2
            ok, buf = cv2.imencode(".jpg", _resize(img_bgr, 1280), [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                raise ValueError("could not encode image")
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()}})
        # temperature 0: the same picture and question give the same answer (readings are evidence aids)
        return {"model": self.model, "max_tokens": int(max_new_tokens), "temperature": 0,
                "messages": [{"role": "user", "content": content}]}

    def _post(self, body: dict, headers: dict):
        r = self.http.post(self.url, json=body, headers=headers, timeout=self.cfg.vlm_timeout)
        if r.status_code == 400 and "temperature" in body:
            # some hosted models reject an explicit temperature; ask once more without it
            r2 = self.http.post(self.url, json={k: v for k, v in body.items() if k != "temperature"}, headers=headers,
                                timeout=self.cfg.vlm_timeout)
            if r2.status_code == 200:
                return r2
        return r

    def ask(self, img_bgr, prompt, max_new_tokens=160):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        tokens = int(max_new_tokens)
        for attempt in range(3):
            body = self.payload(img_bgr, prompt, tokens)
            r = self._post(body, headers)
            if r.status_code != 200:
                raise RuntimeError(f"VLM server returned HTTP {r.status_code}: {r.text[:200]}")
            j = r.json()
            try:
                choice = j["choices"][0]
                msg = choice.get("message") or {}
                content = msg.get("content")
                if content is None and "text" in choice:
                    content = choice["text"]
            except (KeyError, IndexError, TypeError):
                raise RuntimeError(f"unexpected VLM response shape: {str(j)[:200]}")
            if isinstance(content, list):          # some servers return content parts
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            content = (content or "").strip()
            if content:
                return content
            # reasoning models spend the budget on hidden reasoning first: more tokens, same question.
            # The reasoning text itself is never used as the answer.
            if choice.get("finish_reason") == "length" or msg.get("reasoning_content"):
                tokens = min(4096, max(512, tokens * 4))
                continue
            break
        raise RuntimeError(f"{self.label} returned no answer" +
                           (" (only reasoning, the token budget ran out)" if msg.get("reasoning_content") else ""))


class ChainVLM(VLMBase):
    """Several providers in fallback order: a question goes to the first that is working; a
    provider that fails 3 times is skipped for the rest of the run. Members are built only when
    needed, so a local fallback model is loaded only if the servers before it fail."""
    name = "chain"

    def __init__(self, cfg, device: str, specs: List[dict]):
        super().__init__(cfg)
        self.device, self.specs = device, specs
        self.members: Dict[int, VLMBase] = {}
        self.failures: Dict[int, int] = {}
        self.last_provider = ""
        self.label = " → ".join(f"{s['provider']}:{s['model']}" for s in specs)

    def _member(self, i: int) -> VLMBase:
        if i not in self.members:
            self.members[i] = _single(self.cfg, self.device, self.specs[i])
        return self.members[i]

    def ask(self, img_bgr, prompt, max_new_tokens=160):
        errors = []
        for i, sp in enumerate(self.specs):
            if self.failures.get(i, 0) >= 3:
                continue
            try:
                out = self._member(i).ask(img_bgr, prompt, max_new_tokens)
                self.last_provider = f"{sp['provider']}:{sp['model']}"
                return out
            except Exception as e:
                self.failures[i] = self.failures.get(i, 0) + 1
                errors.append(f"{sp['provider']}:{sp['model']}: {str(e)[:160]}")
                log.warning("AI provider %s failed (%d), trying the next: %s", sp["model"], self.failures[i], e)
        raise RuntimeError("every AI provider failed – " + " | ".join(errors) if errors else
                           "every AI provider is disabled after repeated failures")


def _single(cfg, device: str, spec: dict) -> VLMBase:
    problems = check_spec(spec)
    if problems:
        raise RuntimeError("; ".join(problems))
    if spec["provider"] in ("openai", "nvidia"):
        return OpenAICompatVLM(cfg, spec=spec)
    import copy
    c = copy.copy(cfg)
    c.vlm_model = spec["model"]
    return TransformersVLM(c, device)


def make_vlm(cfg, device: str) -> VLMBase:
    specs = provider_specs(cfg)
    if specs[0].get("provider") not in PROVIDERS:
        raise ValueError(f"unknown vlm_provider {specs[0].get('provider')!r}")
    if len(specs) == 1:
        return _single(cfg, device, specs[0])
    return ChainVLM(cfg, device, specs)


def probe(spec: dict, cfg=None, timeout: float = 60.0) -> dict:
    """What *Test* in Settings › AI providers reports for one provider:
    ``models`` (the server's model list, when it has one), ``answer`` to a real vision question –
    a picture with the number 42 drawn on it – and ``vision_ok`` when the answer contains 42.
    Proves that the address, the key, the model name and image input all work."""
    import cv2

    from .config import Config
    cfg = cfg or Config()
    out: Dict[str, object] = {"models": [], "answer": "", "vision_ok": False, "error": ""}
    problems = check_spec(spec)
    if problems:
        out["error"] = "; ".join(problems)
        return out
    if spec["provider"] in ("openai", "nvidia"):
        import requests
        chat = resolve_chat_completion_url(spec.get("base_url") or (NVIDIA_BASE_URL if spec["provider"] == "nvidia" else ""))
        key = os.environ.get(spec.get("api_key_env") or "", "")
        try:
            r = requests.get(chat[:-len("chat/completions")] + "models", headers={"Authorization": "Bearer " + key} if key else {}, timeout=15)
            if r.status_code == 200:
                data = r.json()
                items = data.get("data") if isinstance(data, dict) else data
                out["models"] = sorted(str(m.get("id") or m.get("name")) for m in (items or [])
                                       if isinstance(m, dict) and (m.get("id") or m.get("name")))
        except Exception:
            pass                                    # many hosted endpoints have no model list; the question decides
    img = np.full((96, 160, 3), 255, np.uint8)
    cv2.putText(img, "42", (34, 72), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (0, 0, 0), 5, cv2.LINE_AA)
    try:
        import copy
        c = copy.copy(cfg)
        c.vlm_timeout = timeout
        v = _single(c, "cpu", spec)
        answer = v.ask(img, "What number is written in this picture? Reply with the digits only.", 16)[:200]
        out["answer"], out["vision_ok"] = answer, "42" in answer
        if not out["vision_ok"]:
            out["error"] = "the model answered but did not read the test picture – is it a vision model?"
    except Exception as e:
        out["error"] = str(e)[:300]
    return out


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
