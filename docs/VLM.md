# AI model providers (vision-language models)

Status: **current** · Code: `vlm.py`, `app/ai_providers.py` · Tests: `tests/test_vlm_providers.py`,
`tests/test_app_api.py::test_ai_providers_keep_keys_secret_and_are_tested_with_a_real_picture`

A vision-language model (VLM) is used for three things: event descriptions (`use_vlm`), reading
plates the plate reader could not read (`plate_vlm_read`), and *Ask the AI* in the Forensic
Studio. The answer is always advisory text for the examiner; a VLM never changes a picture.

## Providers and fallback

| Type | What it is | Address | Model name |
|---|---|---|---|
| `openai` | any OpenAI-compatible server: Ollama, LM Studio, vLLM, llama.cpp server, OpenAI | required, e.g. `http://127.0.0.1:11434/v1` | the server's name, e.g. `qwen2.5vl:7b` |
| `nvidia` | NVIDIA NIM / `integrate.api.nvidia.com` | may stay empty (= `https://integrate.api.nvidia.com/v1`); a pasted invoke URL ending in `/chat/completions` is used as is | e.g. `meta/llama-3.2-90b-vision-instruct` |
| `transformers` | a Hugging Face model run on this computer | – | repo id, e.g. `Qwen/Qwen3-VL-2B-Instruct` |

**Desktop app:** *Settings › AI model providers* is the only place the provider is configured: an
ordered list, each entry with type, address, **model name** and API key, *use* on/off, ↑ to
reorder, and *Test*. Questions go to the first enabled provider; when it fails the next one
answers (`vlm.ChainVLM`); a provider that failed 3 times is skipped for the rest of the run.
Members are created only when reached, so a local fallback model is loaded only when the servers
before it fail. With no provider in the list the built-in `Qwen/Qwen3-VL-2B-Instruct` runs on this
PC (slow without a GPU). *Test* first reads the server's model list (`GET …/models`, shown when
the server has one) and then asks a real vision question – a picture with the number 42 – and
reports *Working* only when the answer contains 42: it proves the address, the key, the model
name and image input together.

**Library / CLI / notebook:** the first provider is `vlm_provider`, `vlm_model`, `vlm_base_url`,
`vlm_api_key_env`; more are `vlm_fallbacks` – a list of `{"provider", "model", "base_url",
"api_key_env"}`. `Config.validate()` checks every entry (it never edits the configuration).

```bash
ollama pull qwen2.5vl:7b
python -m cctv_analysis --input … --output … use_vlm=true vlm_provider=openai \
       vlm_base_url=http://127.0.0.1:11434/v1 vlm_model=qwen2.5vl:7b
```

## API keys
The app keeps each provider's key in `secrets.json` (owner-only permissions) under
`vlm_key:<id>`; it is never returned to the browser (`key_set` says whether one is saved), and it
reaches an analysis only as the environment variable `CCTV_VLM_KEY_<ID>`, whose *name* is in the
configuration. Removing a provider deletes its key. The single key of older versions
(`vlm_api_key`) is moved to the first provider once, when the list is first read.

## Wire format
`POST {address}/chat/completions` with `{"model", "temperature": 0, "max_tokens", "messages":
[{"role": "user", "content": [{"type": "text", …}, {"type": "image_url", "image_url": {"url":
"data:image/jpeg;base64,…"}}]}]}`; the answer is `choices[0].message.content`. Sources:
OpenAI Chat Completions (https://platform.openai.com/docs/api-reference/chat/create); Ollama's
OpenAI layer accepts the `{"url": …}` object (`ollama/openai/openai.go`, case `"image_url"`);
NVIDIA's API reference for its vision models documents the same content parts – "Images are
passed with type=image_url; set image_url.url to an image URL or a base64 data URI" – and the
invoke URL `https://integrate.api.nvidia.com/v1/chat/completions`
(https://docs.api.nvidia.com/nim/reference/meta-llama-3_2-11b-vision-instruct-infer, fetched
2026-11-07). `GET https://integrate.api.nvidia.com/v1/models` answered without a key on that date
(84 models).

Temperature is always 0 (the same picture and question give the same answer). A server that
rejects an explicit temperature with HTTP 400 is asked once more without it. Reasoning models
that return no `content` (the budget went into hidden reasoning, `finish_reason: "length"`, or
only `reasoning_content`) are asked again with a larger budget (up to 4096 tokens); the reasoning
text itself is never used as the answer.

What was not verified here: no NVIDIA key and no real Ollama / LM Studio instance were available
in this environment. The test suite runs the whole pipeline, the fallback chain and *Test*
against fake servers that check every request has this exact shape.

## Failure handling
`NOT_CONFIGURED`: an `openai` entry without an address, or any entry without a model name,
fails validation before anything runs (the app refuses to save such an enabled entry). During
an analysis three consecutive failures of the whole chain switch descriptions off for the rest of
the run (logged as an error); the analysis itself continues. The built-in `transformers` path was
verified with `HuggingFaceTB/SmolVLM-256M-Instruct` on CPU; the default Qwen3-VL-2B needs ~9 GB
free RAM on CPU (fp32) or a GPU.
