"""AI model providers of the app (*Settings › AI model providers*).

An ordered list of vision-language model providers – an OpenAI-compatible server (Ollama, LM
Studio, vLLM, llama.cpp, OpenAI), NVIDIA NIM / integrate.api.nvidia.com, or a built-in local
Hugging Face model – each with its own address, **model name** and API key. Every AI question
(event descriptions, plate reading, *Ask the AI* in the Forensic Studio) goes to the first
enabled provider; when it fails the next one answers (``vlm.ChainVLM``). This list is the only
place the app's AI provider is configured; analyses get it through ``config_overrides``.

API keys are kept in ``secrets.json`` (``vlm_key:<id>``), never returned to the browser and
handed to the analysis only as environment variables (``CCTV_VLM_KEY_<ID>``).
"""
from __future__ import annotations

import os
import re
from typing import Dict, List

from flask import jsonify

from .common import ApiError

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def _env_name(pid: str) -> str:
    return "CCTV_VLM_KEY_" + re.sub(r"[^A-Z0-9]", "_", pid.upper())


def stored(settings) -> List[dict]:
    """The saved providers (order = fallback order), migrating the single provider of older versions once."""
    items = settings.load().get("vlm_providers") or []
    if not items:
        last = settings.load().get("last_config") or {}
        if last.get("vlm_provider") in ("openai", "nvidia") and (last.get("vlm_base_url") or last.get("vlm_provider") == "nvidia"):
            items = [{"id": "provider-1", "label": "migrated", "provider": last["vlm_provider"],
                      "base_url": last.get("vlm_base_url", ""), "model": last.get("vlm_model", ""), "enabled": True}]
            old = settings.secret("vlm_api_key")
            if old:
                settings.set_secret("vlm_key:provider-1", old)
                settings.set_secret("vlm_api_key", "")
            settings.save({"vlm_providers": items})
    return [dict(x) for x in items if isinstance(x, dict)]


def public(settings) -> List[dict]:
    return [{**x, "key_set": bool(settings.secret("vlm_key:" + x["id"]))} for x in stored(settings)]


def specs(settings) -> List[dict]:
    """Enabled providers as ``vlm`` specs; their keys are put into the environment for the analysis."""
    out = []
    for x in stored(settings):
        if not x.get("enabled", True):
            continue
        env = _env_name(x["id"])
        key = settings.secret("vlm_key:" + x["id"])
        if key:
            os.environ[env] = key
        else:
            os.environ.pop(env, None)
        out.append({"provider": x["provider"], "model": x.get("model", ""), "base_url": x.get("base_url", ""),
                    "api_key_env": env})
    return out


def config_overrides(settings) -> Dict[str, object]:
    """Config fields for an analysis: the first enabled provider + the rest as fallbacks
    ({} when none is configured: the built-in default model is used)."""
    sp = specs(settings)
    if not sp:
        return {}
    first = sp[0]
    return {"vlm_provider": first["provider"], "vlm_model": first["model"], "vlm_base_url": first["base_url"],
            "vlm_api_key_env": first["api_key_env"], "vlm_fallbacks": sp[1:]}


def register(app, *, settings, body) -> None:
    from ..vlm import NVIDIA_BASE_URL, PROVIDERS, check_spec

    @app.get("/api/ai/providers")
    def ai_providers_get():
        return jsonify(providers=public(settings), kinds=list(PROVIDERS), nvidia_base_url=NVIDIA_BASE_URL)

    @app.put("/api/ai/providers")
    def ai_providers_put():
        d = body()
        items = d.get("providers")
        if not isinstance(items, list) or len(items) > 10:
            raise ApiError(400, "providers: a list of at most 10 providers")
        out, seen = [], set()
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise ApiError(400, f"provider {i}: object required")
            pid = str(it.get("id") or f"provider-{i}").strip().lower()
            if not ID.fullmatch(pid) or pid in seen:
                raise ApiError(400, f"provider {i}: id must be unique, letters/digits/-")
            seen.add(pid)
            rec = {"id": pid, "label": str(it.get("label") or "")[:60].strip(),
                   "provider": str(it.get("provider") or ""), "base_url": str(it.get("base_url") or "")[:300].strip(),
                   "model": str(it.get("model") or "")[:200].strip(), "enabled": bool(it.get("enabled", True))}
            if rec["base_url"] and not re.match(r"^https?://", rec["base_url"]):
                raise ApiError(400, f"provider {i}: the address must start with http:// or https://")
            problems = check_spec(rec)
            if problems and rec["enabled"]:
                raise ApiError(400, f"provider {i} ({rec['label'] or rec['model'] or pid}): " + "; ".join(problems))
            key = it.get("api_key")
            if isinstance(key, str) and key.strip():
                if len(key) > 500:
                    raise ApiError(400, f"provider {i}: API key too long")
                settings.set_secret("vlm_key:" + pid, key.strip())
            elif it.get("clear_key"):
                settings.set_secret("vlm_key:" + pid, "")
            out.append(rec)
        for old in stored(settings):                       # keys of removed providers are deleted
            if old["id"] not in seen:
                settings.set_secret("vlm_key:" + old["id"], "")
        settings.save({"vlm_providers": out})
        return jsonify(providers=public(settings))

    @app.post("/api/ai/providers/<pid>/test")
    def ai_provider_test(pid):
        from ..vlm import probe
        x = next((p for p in stored(settings) if p["id"] == pid), None)
        if x is None:
            raise ApiError(404, "no such provider – save the list first")
        env = _env_name(pid)
        key = settings.secret("vlm_key:" + pid)
        if key:
            os.environ[env] = key
        res = probe({"provider": x["provider"], "model": x.get("model", ""), "base_url": x.get("base_url", ""),
                     "api_key_env": env})
        return jsonify(res)
