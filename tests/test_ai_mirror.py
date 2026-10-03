"""Если реестр Ollama недоступен, модель качается с Hugging Face и чат работает под её именем."""

import threading

import uvicorn
from fastapi import FastAPI, Request

from dailytimer.ai import HF_MIRRORS, OllamaBackend


def fake_ollama(registry_ok: bool):
    app = FastAPI()
    state = {"installed": set(), "pulls": [], "chat_model": None}

    @app.get("/api/tags")
    def tags():
        return {"models": [{"name": n} for n in state["installed"]]}

    @app.post("/api/pull")
    async def pull(request: Request):
        name = (await request.json())["model"]
        state["pulls"].append(name)
        if name.startswith("hf.co/") or registry_ok:
            state["installed"].add(name if name.startswith("hf.co/") else f"{name}")
            return {"status": "success"}
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "pull model manifest: EOF"}, status_code=500)

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await request.json()
        state["chat_model"] = body["model"]
        if body["model"] not in state["installed"]:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "model not found"}, status_code=404)
        return {"message": {"content": "привет"}}

    return app, state


def serve(app):
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        pass
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}"


def test_falls_back_to_huggingface(monkeypatch):
    monkeypatch.setattr(OllamaBackend, "_last_failed_pull", None)
    app, state = fake_ollama(registry_ok=False)
    server, url = serve(app)
    try:
        backend = OllamaBackend(url, "qwen2.5:3b")
        assert backend.ensure_model() is True
        assert state["pulls"] == ["qwen2.5:3b", HF_MIRRORS["qwen2.5:3b"]]
        fresh = OllamaBackend(url, "qwen2.5:3b")  # новый экземпляр, как в приложении
        assert fresh.chat("s", "u", 0, False) == "привет"
        assert state["chat_model"] == HF_MIRRORS["qwen2.5:3b"]
    finally:
        server.should_exit = True


def test_registry_ok_uses_official_name(monkeypatch):
    monkeypatch.setattr(OllamaBackend, "_last_failed_pull", None)
    app, state = fake_ollama(registry_ok=True)
    server, url = serve(app)
    try:
        assert OllamaBackend(url, "qwen2.5:3b").ensure_model() is True
        assert state["pulls"] == ["qwen2.5:3b"]
        assert OllamaBackend(url, "qwen2.5:3b").chat("s", "u", 0, False) == "привет"
        assert state["chat_model"] == "qwen2.5:3b"
    finally:
        server.should_exit = True
