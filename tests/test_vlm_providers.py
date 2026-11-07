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
from cctv_analysis.vlm import ChainVLM, OpenAICompatVLM, make_vlm, probe  # noqa: E402

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
            if "What number" in text:
                content = "42"
            elif "number plate" in text:
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
    pr = probe({"provider": "openai", "model": "qwen3-vl:2b", "base_url": fake.url, "api_key_env": "CCTV_VLM_API_KEY"})
    assert pr["models"] == ["qwen3-vl:2b"] and pr["answer"] == "42" and pr["vision_ok"] is True and not pr["error"]
    _check_wire_format(fake.requests[-1], "qwen3-vl:2b")              # the test question carried a real picture


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
    from cctv_analysis.vlm import NVIDIA_BASE_URL, resolve_chat_completion_url
    assert resolve_chat_completion_url("https://integrate.api.nvidia.com/v1/chat/completions") == \
        "https://integrate.api.nvidia.com/v1/chat/completions"
    assert resolve_chat_completion_url("https://integrate.api.nvidia.com/v1") == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert resolve_chat_completion_url("https://integrate.api.nvidia.com") == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert resolve_chat_completion_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1/chat/completions"
    cfg = Config(vlm_provider="nvidia", vlm_model="meta/llama-3.2-11b-vision-instruct", vlm_base_url="")
    assert not cfg.validate() and cfg.vlm_base_url == ""                    # validate() no longer edits the config
    v = make_vlm(cfg, "cpu")
    assert isinstance(v, OpenAICompatVLM) and v.url == NVIDIA_BASE_URL + "/chat/completions"
    assert any("model name is empty" in p for p in Config(vlm_provider="openai", vlm_model="",
                                                          vlm_base_url="http://x/v1").validate())


def _server(handler):
    """A one-route fake chat server; handler(json) -> (body, status)."""
    from flask import Flask, jsonify, request
    from waitress.server import create_server
    app = Flask("fake-" + str(id(handler)))
    seen = []

    @app.post("/v1/chat/completions")
    def chat():
        j = request.get_json()
        seen.append(j)
        body, status = handler(j)
        return jsonify(body), status
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = create_server(app, host="127.0.0.1", port=port, threads=2)
    threading.Thread(target=srv.run, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}/v1", seen


def test_probe_works_without_a_model_list_and_needs_a_vision_answer():
    srv, url, seen = _server(lambda j: ({"choices": [{"message": {"content": "I see the number 42."}}]}, 200))
    try:
        pr = probe({"provider": "openai", "model": "some/vision-model", "base_url": url + "/chat/completions"})
        assert pr["models"] == [] and pr["vision_ok"] is True and seen[0]["model"] == "some/vision-model"
        assert seen[0]["messages"][0]["content"][1]["type"] == "image_url"
    finally:
        srv.close()
    srv, url, _ = _server(lambda j: ({"choices": [{"message": {"content": "Hello!"}}]}, 200))
    try:
        pr = probe({"provider": "openai", "model": "text-only", "base_url": url})
        assert pr["vision_ok"] is False and "vision model" in pr["error"]
    finally:
        srv.close()
    assert "model name is empty" in probe({"provider": "openai", "model": "", "base_url": "http://x"})["error"]


def test_reasoning_models_get_more_tokens_and_their_reasoning_is_never_the_answer():
    import numpy as np

    def answers_when_given_room(j):
        if j["max_tokens"] < 512:
            return {"choices": [{"finish_reason": "length", "message": {"content": None,
                                                                        "reasoning_content": "Let me think..."}}]}, 200
        return {"choices": [{"finish_reason": "stop", "message": {"content": "A person at the counter."}}]}, 200
    srv, url, seen = _server(answers_when_given_room)
    try:
        v = make_vlm(Config(vlm_provider="openai", vlm_model="r", vlm_base_url=url), "cpu")
        assert v.ask(np.zeros((32, 32, 3), np.uint8), "describe", 100) == "A person at the counter."
        assert [j["max_tokens"] for j in seen] == [100, 512] and all(j["temperature"] == 0 for j in seen)
    finally:
        srv.close()
    srv, url, _ = _server(lambda j: ({"choices": [{"finish_reason": "length", "message": {
        "content": "", "reasoning_content": "thinking forever"}}]}, 200))
    try:
        v = make_vlm(Config(vlm_provider="openai", vlm_model="r", vlm_base_url=url), "cpu")
        with pytest.raises(RuntimeError, match="no answer"):
            v.ask(np.zeros((32, 32, 3), np.uint8), "describe")
    finally:
        srv.close()


def test_fallback_providers_answer_when_the_first_fails(fake, tmp_path):
    """Primary server dead, a second one configured as fallback: every question is answered by the
    fallback; after 3 failures the dead one is no longer asked."""
    import numpy as np
    bad = FakeServer(fail=True)
    try:
        cfg = Config(vlm_provider="openai", vlm_model="qwen3-vl:2b", vlm_base_url=bad.url,
                     vlm_fallbacks=[{"provider": "openai", "model": "qwen3-vl:2b", "base_url": fake.url}])
        v = make_vlm(cfg, "cpu")
        assert isinstance(v, ChainVLM)
        for _ in range(5):
            assert v.read_plate(np.zeros((40, 120, 3), np.uint8)) == "MH12AB1234"
        assert len(bad.requests) == 3 and len(fake.requests) == 5 and v.last_provider == "openai:qwen3-vl:2b"
        assert any("vlm_fallbacks[0]" in p for p in Config(vlm_fallbacks=[{"provider": "x", "model": "m"}]).validate())
        inp = tmp_path / "in"
        make_cloud_clips.make(str(inp), 2)
        bad2 = FakeServer(fail=True)
        try:
            cfg2 = _cfg(tmp_path, inp, use_vlm=True, vlm_provider="openai", vlm_model="qwen3-vl:2b",
                        vlm_base_url=bad2.url, vlm_scope="all",
                        vlm_fallbacks=[{"provider": "openai", "model": "qwen3-vl:2b", "base_url": fake.url}])
            Analyzer(cfg2).run(build_reports=False)
            db = sqlite3.connect(os.path.join(cfg2.output_dir, "analysis.db"))
            vlm = [json.loads(r[0]) for r in db.execute("SELECT vlm FROM events WHERE vlm IS NOT NULL")]
            assert vlm and all(x["description"] == "A person walks past the counter." for x in vlm)
        finally:
            bad2.close()
    finally:
        bad.close()
