# MM-Bridge

MM-Bridge is a FastAPI proxy for OpenAI- and Anthropic-compatible model endpoints. It supports both a text-only reasoning model paired with a separate multimodal analyzer and a multimodal main model that can receive media directly.

In Mode 1, requests without media go directly to the final text-model server. When the current request or tool-result event contains an image, audio clip, or video, the bridge asks the multimodal analyzer to extract evidence, normalizes and validates that evidence, and injects it into the final-model request. In Mode 2, analyzer enrichment is skipped and requests are forwarded to a multimodal main model.

The bridge is designed for setups such as:

- **Final reasoning model:** a text-only model served by vLLM
- **Multimodal analyzer:** a model served by vLLM or another compatible server
- **Clients:** OpenWebUI, Cline, Claude Code, or any compatible API client

> The existing `LLAMA_*` environment-variable names are kept for compatibility. `LLAMA_ROOT_URL` may point to a vision-capable vLLM server; llama.cpp is not required.

## Documentation

- [Environment configuration](docs/ENVIRONMENT.md)
- [Multimodal module internals](modules/multimodal/README.md)

## What the bridge does

Mode 1 enrichment flow:

```text
Client
  │
  ▼
MM-Bridge
  ├─ No media ───────────────────────────────► Final text model
  │
  └─ Media present
       ├─ Send media + exact user request ───► Multimodal analyzer
       ├─ Receive text, visual, and audio evidence
       ├─ Normalize and validate the evidence
       ├─ Inject the evidence into the original request
       └─────────────────────────────────────► Final text model
```

Mode 2 skips the analyzer branch and forwards the request to the configured multimodal main model after model-ID rewriting.

The bridge preserves the incoming API protocol and endpoint. It does not translate OpenAI requests into Anthropic requests or vice versa.

```text
/v1/chat/completions → vision /v1/chat/completions → text /v1/chat/completions
/v1/messages         → vision /v1/messages         → text /v1/messages
/v1/responses        → vision /v1/responses        → text /v1/responses
```


## Main features

- Direct pass-through for requests without media
- Mode 1 analyzer enrichment and Mode 2 direct multimodal forwarding
- Question-aware visual analysis for requests with media
- Verbatim OCR, speech, and lyrics blocks separated from visual and audio analysis
- Question-aware analysis cache
- Analyzer-source response headers (`analyzer`, `cache`, or `fail_open`)
- OpenAI- and Anthropic-compatible request handling
- Optional media replacement for text-only final models
- SSE streaming with optional heartbeat messages
- Client-disconnect cleanup for both streaming and non-streaming requests
- Automatic model discovery through `/v1/models`

## Multimodal evidence blocks

The analyzer returns one `media` array without a top-level summary or candidate answer.

```text
text_blocks
└─ Verbatim OCR, speech, and lyrics with spatial or temporal locations

visual_blocks
└─ Image/video UI states, document structure, tables, charts, diagrams, and spatial relationships

audio_blocks
└─ Audio/video sound events and supported musical analysis
```

A `media` array item always represents one attached file. A composite image
with several panels, subfigures, charts, or regions remains one media item and
uses several `visual_blocks`. Supported item types are exactly `image`,
`video`, and `audio`; there is no generic `media` type.

For an exact transcription request, the final text model is instructed to use `text_blocks[].text` rather than replacing it with a translation, correction, summary, or inferred meaning. `visual_blocks[].basis` distinguishes directly observed evidence from inferred conclusions.

The user's exact request guides inspection depth without allowing relevant text, UI state, document structure, chart relationships, or audio events to be omitted.

> The bridge normalizes recoverable analyzer shape drift against the incoming
> attachment metadata before validation. Malformed, truncated, or ambiguous
> multi-attachment output is still rejected, and validation cannot guarantee
> perfect OCR.



## MultiModal Bench Eval (MMMU)
- DeepSeek v4 Flash(0731) + Ornith-1.5-9B-MixedInt4-AutoRound
- DeepSeek v4 Flash(0731) : https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731
- Ornith-1.5-9B-MixedInt4-AutoRound : https://huggingface.co/Pilcothink/Ornith-1.5-9B-MixedInt4-AutoRound

### MMMU Results

| Result | Correct / Evaluated | Accuracy |
|---|---:|---:|
| Raw `lmms-eval` result | 692 / 900 | **76.89%** |
| Completed-sample accuracy | 679 / 848 | **80.07%** |

### Recorded Token Usage

| Metric | Usage |
|---|---:|
| Input tokens | **822,868** |
| DeepSeek Output tokens | **1,741,570** |
| Total tokens | **2,564,438** |

The evaluation consumed approximately **2.56 million tokens** in total.

A significant portion of the output-token usage came from samples that reached the generation limit. The 51 affected samples alone consumed **835,584 output tokens**, accounting for approximately **48% of all output tokens used during the evaluation**.



## Requirements

- Python 3.10 or newer
- A text-model server compatible with the endpoints used by your client
- A multimodal analyzer server for Mode 1, compatible with the endpoints used by your client
- The Python packages listed in `requirements.txt`

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/gpdev-Pilcothink/Pilco-mmbridge.git
cd Pilco-mmbridge
```

### 2. Create and activate a virtual environment

#### Linux or macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
```

#### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

#### Windows Command Prompt

```bat
py -m venv .venv
.venv\Scripts\activate.bat
```

### 3. Install dependencies from `requirements.txt`

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4. Create the local `.env` file

#### Linux or macOS

```bash
cp .env.example .env
```

#### Windows PowerShell

```powershell
Copy-Item .env.example .env
```

Generate a new bridge token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copy the generated value into `MM_BRIDGE_TOKEN` in your local `.env` file.

### 5. Configure the two upstream servers

Use root URLs without a trailing `/v1`:

```env
VLLM_ROOT_URL=http://127.0.0.1:8000
LLAMA_ROOT_URL=http://127.0.0.1:8001
```

- `VLLM_ROOT_URL` is the final text-model server.
- `LLAMA_ROOT_URL` is the vision-analyzer server.
- Either server may be vLLM as long as it provides the required compatible API.

For text-only final models, use:

```env
MM_VLLM_MEDIA_POLICY=replace
```

For complex OCR, game screens, terminal screenshots, and multi-image requests, this project recommends at least:

```env
MM_ANALYZER_MAX_TOKENS=16384
```

The configured model context length and server limits must support the selected value.

See [Environment configuration](docs/ENVIRONMENT.md) for every available variable.


## Basic checks

### Health check

```bash
curl http://127.0.0.1:18000/health
```

The public health response reports bridge state without exposing either upstream URL or the configured analyzer endpoint list.

### Model list

```bash
curl \
  -H "Authorization: Bearer YOUR_LOCAL_BRIDGE_TOKEN" \
  http://127.0.0.1:18000/v1/models
```

### Text request

```bash
curl http://127.0.0.1:18000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_LOCAL_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-mm-bridge",
    "messages": [
      {"role": "user", "content": "Reply with one short sentence."}
    ],
    "stream": false
  }'
```

## Repository layout

```text
.
├─ main.py
├─ requirements.txt
├─ .env.example
├─ .gitignore
├─ README.md
├─ docs/
│  └─ ENVIRONMENT.md
└─ modules/
   ├─ __init__.py
   └─ multimodal/
      ├─ __init__.py
      ├─ README.md
      ├─ analysis_contract.py
      ├─ app.py
      ├─ cache.py
      ├─ config.py
      ├─ context.py
      ├─ lifecycle.py
      ├─ media.py
      ├─ prompting.py
      ├─ security.py
      └─ upstream.py
```

## Important limitations

- The final text model does not receive visual tokens directly.
- Visual information omitted by the analyzer cannot be reconstructed by the text model.
- OCR accuracy depends on image quality, text size, compression, and the vision model.
- Evidence validation checks the supported schema and truncation signals, but it does not prove that the analyzer observed every real detail correctly.
- Small HUD values and dense screenshots may require higher-resolution inputs or future crop/retry logic.


