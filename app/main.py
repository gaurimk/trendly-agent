from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent import run_turn, session_store
from app.tools import _get_escalations_for_testing

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Trendly Support Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    trace: list


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    session = session_store.get_or_create(req.session_id)
    try:
        result = run_turn(session, req.message)
    except RuntimeError as e:
        # e.g. missing GROQ_API_KEY - surface clearly instead of a bare 500
        raise HTTPException(status_code=500, detail=str(e))
    return ChatResponse(session_id=session.session_id, reply=result["reply"], trace=result["trace"])


@app.get("/debug/escalations")
def debug_escalations():
    """Exposes the in-memory human-handoff queue - useful in the demo to show
    escalation tickets actually being created. Not authenticated because this
    is a take-home demo; a real deployment would put this behind agent auth."""
    return _get_escalations_for_testing()


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(STATIC_DIR / "index.html"))
