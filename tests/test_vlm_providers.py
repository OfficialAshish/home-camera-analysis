"""VLM providers. The OpenAI-compatible provider is exercised against a local fake server that
asserts the wire format (OpenAI Chat Completions with an image_url data URL), so descriptions
and plate reading run end to end without a real model."""
import base64
import json
import os
import socket
import sqlite3
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_cloud_clips  # noqa: E402
import make_plate_video  # noqa: E402

from cctv_analysis.config import Config  # noqa: E402
from cctv_analysis.pipeline import Analyzer  # noqa: E402
from cctv_analysis.vlm import OpenAICompatVLM, list_server_models, make_vlm  # noqa: E402

MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))


class FakeServer:
    def __init__(self, fail=False):
        from flask import Flask, jsonify, request
        from waitress.server import create_server
        self.requests, self.fail = [], fail
        app = Flask("fake-openai")

        @app.get("/v1/models")
        def models():
            return jsonify(object="list", data=[{"id": "qwen3-vl:2b", "object": "model"}])

        @app.post("/v1/chat/completions")
        def chat():
            j = request.get_json()
            self.requests.append({"json": j, "auth": request.headers.get("Authorization", "")})
            if self.fail:
                return jsonify(error={"message": "model not loaded"}), 500
            text = j["messages"][0]["content"][0]["text"]
            if "number plate" in text:
                content = "MH12AB1234"
            else:
                content = json.dumps({"description": "A person walks past the counter.", "suspicious": False,
                                      "category": "none", "confidence": 0.8, "reason": ""})
            return jsonify(id="x", object="chat.completion", choices=[{"index": 0, "message": {"role": "assistant",
                                                                                                "content": content}}])
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.srv = create_server(app, host="127.0.0.1", port=self.port, threads=2)
        threading.Thread(target=self.srv.run, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self.srv.close()


@pytest.fixture()
def fake():
    f = FakeServer()
    yield f
    f.close()


def _check_wire_format(req, model):
    j = req["json"]
    assert j["model"] == model and j["temperature"] == 0 and j["max_tokens"] > 0
    parts = j["messages"][0]["content"]
    assert parts[0]["type"] == "text" and parts[1]["type"] == "image_url"
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1])[:2] == b"\xff\xd8"          # a real JPEG


def test_openai_provider_wire_format_and_key(fake, monkeypatch):
    import numpy as np
    monkeypatch.setenv("CCTV_VLM_API_KEY", "sk-test")
    cfg = Config(vlm_provider="openai", vlm_base_url=fake.url, vlm_model="qwen3-vl:2b")
    v = make_vlm(cfg, "cpu")
    assert isinstance(v, OpenAICompatVLM)
    res = v.describe_event(np.zeros((90, 160, 3), np.uint8), "1 person")
    assert res["description"] == "A person walks past the counter." and res["suspicious"] is False
    _check_wire_format(fake.requests[-1], "qwen3-vl:2b")
    assert fake.requests[-1]["auth"] == "Bearer sk-test"
    assert list_server_models(fake.url) == ["qwen3-vl:2b"]


def test_openai_provider_not_configured_fails_loudly():
    assert any("NOT_CONFIGURED" in p for p in Config(vlm_provider="openai", vlm_base_url="").validate())
    with pytest.raises(RuntimeError, match="NOT_CONFIGURED"):
        OpenAICompatVLM(Config(vlm_provider="openai", vlm_base_url=""))
    with pytest.raises(ValueError, match="vlm_base_url"):
        Analyzer(Config(vlm_provider="openai", vlm_base_url="", output_dir="/tmp/x_nc", work_dir="/tmp/x_nc_w"))


def _cfg(tmp, inp, **kw):
    return Config(input_dir=str(inp), output_dir=str(tmp / "out"), work_dir=str(tmp / "work"), model_cache_dir=MODELS,
                  use_semantic=False, use_faces=False, use_pose=False, detector_model="yolo26n.pt",
                  detector_fallbacks=["yolo11n.pt"], det_imgsz=640, make_highlight_reel=False, save_clips=False, **kw)


def test_pipeline_uses_local_server_for_descriptions_and_plates(fake, tmp_path):
    inp = tmp_path / "in"
    make_cloud_clips.make(str(inp), 2)
    (inp / "gate").mkdir()
    make_plate_video.make(str(inp / "gate" / "ch02_20261031093000.mp4"), blur=15, scale=0.9)   # unreadable by OCR
    cfg = _cfg(tmp_path, inp, use_vlm=True, vlm_provider="openai", vlm_base_url=fake.url, vlm_model="qwen3-vl:2b",
               vlm_scope="all", use_plates=True, plate_vlm_read=True)
    Analyzer(cfg).run(build_reports=False)
    db = sqlite3.connect(os.path.join(cfg.output_dir, "analysis.db"))
    vlm = [json.loads(r[0]) for r in db.execute("SELECT vlm FROM events WHERE vlm IS NOT NULL")]
    n_events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert len(vlm) == n_events >= 3 and all(v["description"] == "A person walks past the counter." for v in vlm)
    plate = db.execute("SELECT text, method FROM plates ORDER BY confidence DESC").fetchone()
    assert plate and plate[0] == "MH12AB1234" and "vlm" in plate[1]
    kinds = ["plate" if "number plate" in r["json"]["messages"][0]["content"][0]["text"] else "event"
             for r in fake.requests]
    assert kinds.count("event") == n_events and kinds.count("plate") >= 1
    for r in fake.requests:
        _check_wire_format(r, "qwen3-vl:2b")


def test_dead_server_disables_vlm_after_three_failures(tmp_path):
    bad = FakeServer(fail=True)
    try:
        inp = tmp_path / "in"
        make_cloud_clips.make(str(inp), 5)
        cfg = _cfg(tmp_path, inp, use_vlm=True, vlm_provider="openai", vlm_base_url=bad.url, vlm_scope="all")
        stats = Analyzer(cfg).run(build_reports=False)
        assert stats["videos_ok"] == 5                                    # analysis itself still completes
        assert len(bad.requests) == 3                                     # then the breaker stops calling
    finally:
        bad.close()


def test_transformers_provider_runs_a_local_model():
    """The built-in provider with a small model (SmolVLM-256M, same as the notebook runner)."""
    import cv2

    from cctv_analysis import modelcache
    from cctv_analysis.vlm import TransformersVLM
    modelcache.init(MODELS)
    v = make_vlm(Config(vlm_model="HuggingFaceTB/SmolVLM-256M-Instruct", vlm_max_new_tokens=40), "cpu")
    assert isinstance(v, TransformersVLM)
    out = v.describe_event(cv2.imread(os.path.join(ROOT, "tests", "assets", "car_rear.png")), "1 car")
    assert isinstance(out["description"], str) and out["description"].strip()


def test_vlm_url_normalization_and_nvidia():
    from cctv_analysis.vlm import normalize_vlm_base_url, resolve_chat_completion_url
    assert normalize_vlm_base_url("https://integrate.api.nvidia.com/v1/chat/completions") == "https://integrate.api.nvidia.com/v1"
    assert normalize_vlm_base_url("https://integrate.api.nvidia.com/v1") == "https://integrate.api.nvidia.com/v1"
    assert resolve_chat_completion_url("https://integrate.api.nvidia.com/v1/chat/completions") == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert resolve_chat_completion_url("https://integrate.api.nvidia.com/v1") == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert resolve_chat_completion_url("https://integrate.api.nvidia.com") == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert resolve_chat_completion_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1/chat/completions"

    cfg = Config(vlm_provider="nvidia", vlm_base_url="https://integrate.api.nvidia.com/v1/chat/completions")
    assert not cfg.validate()
    v = make_vlm(cfg, "cpu")
    assert isinstance(v, OpenAICompatVLM)
    assert v.url == "https://integrate.api.nvidia.com/v1/chat/completions"


def test_list_server_models_fallback_when_models_404s():
    import socket, threading
    from flask import Flask, jsonify, request
    from waitress.server import create_server

    app = Flask("test-fallback")
    @app.post("/v1/chat/completions")
    def chat():
        return jsonify(choices=[{"message": {"content": "ok"}}])

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = create_server(app, host="127.0.0.1", port=port, threads=2)
    threading.Thread(target=srv.run, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        models = list_server_models(url, model="mistralai/mistral-large-4-1t-a52b")
        assert any("mistral" in m for m in models) or any("connected" in m for m in models)
    finally:
        srv.close()


def test_reasoning_content_fallback():
    import socket, threading, numpy as np
    from flask import Flask, jsonify
    from waitress.server import create_server

    app = Flask("test-reasoning")
    @app.post("/v1/chat/completions")
    def chat():
        return jsonify(choices=[{"message": {"role": "assistant", "content": None,
                                             "reasoning_content": "The image shows a person at counter."}}])

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = create_server(app, host="127.0.0.1", port=port, threads=2)
    threading.Thread(target=srv.run, daemon=True).start()
    try:
        cfg = Config(vlm_provider="openai", vlm_base_url=f"http://127.0.0.1:{port}/v1")
        v = make_vlm(cfg, "cpu")
        res = v.ask(np.zeros((64, 64, 3), np.uint8), "describe")
        assert res == "The image shows a person at counter."
    finally:
        srv.close()
