# AI descriptions (vision-language models)

Status: **current** · Code: `vlm.py` · Tests: `tests/test_vlm_providers.py`

| Setting | Values |
|---|---|
| `use_vlm` | describe events with a VLM (default on in the notebook, off in the desktop app) |
| `vlm_provider` | `transformers` – a Hugging Face model on this computer; `openai` – any OpenAI-compatible server |
| `vlm_model` | HF repo id (`Qwen/Qwen3-VL-2B-Instruct`) or the server's model name (`qwen3-vl:2b`) |
| `vlm_base_url` | `openai` only, e.g. `http://127.0.0.1:11434/v1` (Ollama), `http://127.0.0.1:1234/v1` (LM Studio), `http://HOST:8000/v1` (vLLM) |
| `vlm_api_key_env` | name of the environment variable holding a key (`CCTV_VLM_API_KEY`); the app stores the key in `secrets.json` |
| `vlm_scope`, `vlm_max_events`, `vlm_prompt`, `vlm_max_new_tokens`, `vlm_timeout` | what to describe and how |
| `plate_vlm_read` | also ask the VLM to read plates that OCR could not read |

## Using a model you already run (example: Ollama)
```bash
ollama pull qwen3-vl:2b          # any vision model your server offers
python -m cctv_analysis --input … --output … use_vlm=true vlm_provider=openai \
       vlm_base_url=http://127.0.0.1:11434/v1 vlm_model=qwen3-vl:2b
```
In the app: *Settings › AI model server › Test connection* lists the server's models; then on
*Set up* choose “AI model runs in = openai”, fill in the model name and the server address.

## Wire format
`POST {base_url}/chat/completions` with `{"model", "temperature": 0, "max_tokens",
"messages": [{"role": "user", "content": [{"type": "text", …}, {"type": "image_url",
"image_url": {"url": "data:image/jpeg;base64,…"}}]}]}`; answer from `choices[0].message.content`.
This is OpenAI's Chat Completions format. Ollama's OpenAI layer accepts the `{"url": …}` object
(checked in `ollama/openai/openai.go`, `case "image_url"`). `GET {base_url}/models` is used for
*Test connection*. The test suite runs the whole pipeline against a fake server and checks every
request has this exact shape. No real Ollama/LM Studio instance was available in this
environment, so compatibility with each product rests on their documented OpenAI compatibility.

## Failure handling
`NOT_CONFIGURED`: `vlm_provider=openai` without `vlm_base_url` fails configuration validation
before anything runs. Three consecutive request failures switch descriptions off for the rest of
the run (logged as an error); the analysis itself continues. The built-in `transformers` path was
verified with `HuggingFaceTB/SmolVLM-256M-Instruct` on CPU; the default Qwen3-VL-2B needs ~9 GB
free RAM on CPU (fp32) or a GPU.
