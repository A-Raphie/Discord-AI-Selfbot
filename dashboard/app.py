import os
import sys
import time
import sqlite3
from pathlib import Path
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config_loader import (
    load_config, save_config, get, set_value, set_paused, is_paused,
    get_channels, get_instructions, get_decision_prompt, get_triggers
)

KB_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "knowledge.db")
LOG_FILE = "/tmp/bot.log"

app = FastAPI(title="Raphie Bot Dashboard")
app.add_middleware(SessionMiddleware, secret_key="raphie-secret-change-me")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))


def check_auth(request: Request):
    if not request.session.get("auth"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    config = load_config(force=True)
    
    kb_count = 0
    qa_count = 0
    try:
        conn = sqlite3.connect(KB_DB)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM knowledge")
        kb_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM qa_pairs")
        qa_count = c.fetchone()[0]
        conn.close()
    except Exception:
        pass
    
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
            recent_logs = "".join(lines[-50:])
    except Exception:
        recent_logs = "No logs available"
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "config": config,
        "kb_count": kb_count,
        "qa_count": qa_count,
        "paused": is_paused(),
        "recent_logs": recent_logs,
    })


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": False})


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    config = load_config()
    if password == config.get("dashboard", {}).get("password", "raphie2024"):
        request.session["auth"] = True
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": True})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.post("/toggle-pause")
async def toggle_pause(request: Request):
    current = is_paused()
    set_paused(not current)
    return RedirectResponse(url="/", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    config = load_config(force=True)
    return templates.TemplateResponse("settings.html", {"request": request, "config": config})


@app.post("/settings")
async def save_settings(
    request: Request,
    model: str = Form(...),
    cooldown: int = Form(...),
    typing_min: int = Form(...),
    typing_max: int = Form(...),
    join_chance: float = Form(...),
    max_context: int = Form(...),
    channels: str = Form(...),
):
    config = load_config(force=True)
    config["ai"]["model"] = model
    config["behavior"]["cooldown"] = cooldown
    config["behavior"]["typing_time_min"] = typing_min
    config["behavior"]["typing_time_max"] = typing_max
    config["behavior"]["join_conversation_chance"] = join_chance
    config["behavior"]["max_context"] = max_context
    config["bot"]["channels"] = [int(ch.strip()) for ch in channels.split(",") if ch.strip().isdigit()]
    save_config(config)
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/triggers", response_class=HTMLResponse)
async def triggers_page(request: Request):
    config = load_config(force=True)
    triggers = config.get("triggers", {})
    return templates.TemplateResponse("triggers.html", {
        "request": request,
        "triggers": triggers,
    })


@app.post("/triggers")
async def save_triggers(
    request: Request,
    greetings: str = Form(...),
    casual_words: str = Form(...),
    direct_words: str = Form(...),
    question_indicators: str = Form(...),
):
    config = load_config(force=True)
    config["triggers"]["greetings"] = [w.strip() for w in greetings.split(",") if w.strip()]
    config["triggers"]["casual_words"] = [w.strip() for w in casual_words.split(",") if w.strip()]
    config["triggers"]["direct_words"] = [w.strip() for w in direct_words.split(",") if w.strip()]
    config["triggers"]["question_indicators"] = [w.strip() for w in question_indicators.split(",") if w.strip()]
    save_config(config)
    return RedirectResponse(url="/triggers", status_code=303)


@app.get("/instructions", response_class=HTMLResponse)
async def instructions_page(request: Request):
    config = load_config(force=True)
    return templates.TemplateResponse("instructions.html", {
        "request": request,
        "instructions": config.get("instructions", ""),
        "decision_prompt": config.get("decision_prompt", ""),
    })


@app.post("/instructions")
async def save_instructions(
    request: Request,
    instructions: str = Form(...),
    decision_prompt: str = Form(...),
):
    config = load_config(force=True)
    config["instructions"] = instructions
    config["decision_prompt"] = decision_prompt
    save_config(config)
    return RedirectResponse(url="/instructions", status_code=303)


@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, channel: str = ""):
    config = load_config(force=True)
    facts = []
    try:
        conn = sqlite3.connect(KB_DB)
        c = conn.cursor()
        if channel:
            c.execute("SELECT id, content, category, source_user, created_at, is_permanent, channel_id FROM knowledge WHERE channel_id = ? ORDER BY created_at DESC LIMIT 100", (int(channel),))
        else:
            c.execute("SELECT id, content, category, source_user, created_at, is_permanent, channel_id FROM knowledge ORDER BY created_at DESC LIMIT 100")
        facts = c.fetchall()
        conn.close()
    except Exception:
        pass
    
    channels = config.get("bot", {}).get("channels", [])
    
    return templates.TemplateResponse("knowledge.html", {
        "request": request,
        "facts": facts,
        "channels": channels,
        "selected_channel": channel,
    })


@app.post("/knowledge/delete/{fact_id}")
async def delete_fact(request: Request, fact_id: int):
    try:
        conn = sqlite3.connect(KB_DB)
        c = conn.cursor()
        c.execute("DELETE FROM knowledge WHERE id = ?", (fact_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return RedirectResponse(url="/knowledge", status_code=303)


@app.post("/knowledge/add")
async def add_permanent_fact(
    request: Request,
    content: str = Form(...),
    category: str = Form("general"),
    channel_id: str = Form(""),
):
    try:
        conn = sqlite3.connect(KB_DB)
        c = conn.cursor()
        ch_id = int(channel_id) if channel_id.strip().isdigit() else None
        c.execute(
            "INSERT INTO knowledge (content, category, source_user, source_message, created_at, last_used, use_count, is_permanent, channel_id) VALUES (?, ?, 'dashboard', '', ?, ?, 0, 1, ?)",
            (content, category, time.time(), time.time(), ch_id)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    return RedirectResponse(url="/knowledge", status_code=303)


@app.get("/qa", response_class=HTMLResponse)
async def qa_page(request: Request, channel: str = ""):
    config = load_config(force=True)
    pairs = []
    try:
        conn = sqlite3.connect(KB_DB)
        c = conn.cursor()
        if channel:
            c.execute("SELECT id, question, answer, source_user, use_count, channel_id FROM qa_pairs WHERE channel_id = ? ORDER BY use_count DESC LIMIT 100", (int(channel),))
        else:
            c.execute("SELECT id, question, answer, source_user, use_count, channel_id FROM qa_pairs ORDER BY use_count DESC LIMIT 100")
        pairs = c.fetchall()
        conn.close()
    except Exception:
        pass
    
    channels = config.get("bot", {}).get("channels", [])
    
    return templates.TemplateResponse("qa.html", {
        "request": request,
        "pairs": pairs,
        "channels": channels,
        "selected_channel": channel,
    })


@app.post("/qa/delete/{qa_id}")
async def delete_qa(request: Request, qa_id: int):
    try:
        conn = sqlite3.connect(KB_DB)
        c = conn.cursor()
        c.execute("DELETE FROM qa_pairs WHERE id = ?", (qa_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return RedirectResponse(url="/qa", status_code=303)


@app.post("/qa/add")
async def add_qa(
    request: Request,
    question: str = Form(...),
    answer: str = Form(...),
    channel_id: str = Form(""),
):
    try:
        conn = sqlite3.connect(KB_DB)
        c = conn.cursor()
        ch_id = int(channel_id) if channel_id.strip().isdigit() else None
        c.execute(
            "INSERT INTO qa_pairs (question, answer, source_user, source_message, created_at, last_used, use_count, channel_id) VALUES (?, ?, 'dashboard', '', ?, ?, 0, ?)",
            (question, answer, time.time(), time.time(), ch_id)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    return RedirectResponse(url="/qa", status_code=303)


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
            logs = "".join(lines[-200:])
    except Exception:
        logs = "No logs available"
    
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "logs": logs,
    })


@app.get("/health")
async def health():
    return {"status": "ok", "bot": "running", "paused": is_paused()}


def start_dashboard(port=None):
    import uvicorn
    if port is None:
        port = int(os.environ.get("PORT", get("dashboard.port", 8080)))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    start_dashboard()
