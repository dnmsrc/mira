"""Minimal OpenAI-compatible chat server for Qwen3-4B-Instruct-2507 (local, no key).

Runs on the CUDA torch that already ships with the AniSora bundle, so no extra
runtime is needed:

  models\\anisora\\wan\\runtime\\python.exe tools\\llm\\serve_qwen.py --port 8081

Exposes:
  POST /v1/chat/completions   (stream=true -> SSE deltas, compatible with app/providers.py)
  GET  /v1/models
  GET  /health
"""
import argparse
import json
import os
import threading
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = ROOT / "models" / "llm" / "Qwen3-4B-Instruct-2507"


def main() -> None:
    import torch
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, StreamingResponse
    from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()

    t0 = time.time()
    tok = AutoTokenizer.from_pretrained(a.model, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        a.model, torch_dtype=torch.bfloat16, device_map="cuda", trust_remote_code=True
    ).eval()
    print(f"[qwen] loaded in {time.time()-t0:.1f}s on {next(model.parameters()).device}", flush=True)

    app = FastAPI()

    def build_prompt(messages):
        try:
            return tok.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
            )
        except TypeError:
            return tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

    @app.get("/health")
    def health():
        return {"ok": True, "model": a.model, "device": str(next(model.parameters()).device)}

    @app.get("/v1/models")
    def models():
        return {"object": "list", "data": [{"id": "qwen3-4b-instruct", "object": "model"}]}

    @app.post("/v1/chat/completions")
    def chat(payload: dict):
        messages = payload.get("messages") or []
        max_new = int(payload.get("max_tokens") or 256)
        temperature = float(payload.get("temperature") or 0.85)
        top_p = float(payload.get("top_p") or 0.9)
        stream = bool(payload.get("stream"))
        prompt = build_prompt(messages)
        inputs = tok(prompt, return_tensors="pt").to(model.device)

        gen_kwargs = dict(
            **inputs,
            max_new_tokens=max_new,
            do_sample=temperature > 0,
            temperature=max(temperature, 1e-5),
            top_p=top_p,
            repetition_penalty=1.08,
            pad_token_id=tok.eos_token_id,
        )

        if not stream:
            with torch.inference_mode():
                out = model.generate(**gen_kwargs)
            text = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
            return JSONResponse({
                "id": f"cmpl-{int(time.time()*1000)}", "object": "chat.completion", "model": "qwen3-4b-instruct",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
            })

        streamer = TextIteratorStreamer(tok, skip_prompt=True, skip_special_tokens=True)
        gen_kwargs["streamer"] = streamer
        thread = threading.Thread(target=lambda: model.generate(**gen_kwargs), daemon=True)
        thread.start()

        def sse():
            first = True
            for piece in streamer:
                if not piece:
                    continue
                chunk = {
                    "id": "cmpl-stream", "object": "chat.completion.chunk", "model": "qwen3-4b-instruct",
                    "choices": [{"index": 0, "delta": ({"role": "assistant", "content": piece} if first else {"content": piece}),
                                 "finish_reason": None}],
                }
                first = False
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    print(f"[qwen] serving on http://{a.host}:{a.port}/v1", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
