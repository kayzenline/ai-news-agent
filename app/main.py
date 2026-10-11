"""FastAPI + Jinja2 前端：身份、订阅偏好、历史简报、Agent trace、立即运行。"""
import json
import re
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import markdown
import nh3
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, scheduler
from .db import Brief, SessionLocal, User, init_db
from .service import run_for_user

logging.basicConfig(level=logging.INFO)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# 简报 Markdown 来自模型/新闻源/用户昵称，均不可信：转 HTML 后用严格白名单清理再 |safe 渲染
SAFE_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "ul", "ol", "li", "strong", "em", "b", "i",
             "code", "pre", "blockquote", "a", "table", "thead", "tbody", "tr", "th", "td"}


def render_markdown(md: str) -> str:
    html = markdown.markdown(md)
    return nh3.clean(html, tags=SAFE_TAGS, attributes={"a": {"href", "title"}},
                     url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer nofollow")


@asynccontextmanager
async def lifespan(app):
    init_db()
    scheduler.start()
    yield
    if scheduler.scheduler.running:
        scheduler.scheduler.shutdown(wait=False)


app = FastAPI(title="AI News Agent", lifespan=lifespan)


def _split(s: str) -> list[str]:
    return [x.strip() for x in s.replace("，", ",").split(",") if x.strip()]


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with SessionLocal() as s:
        users = s.query(User).order_by(User.id).all()
    return templates.TemplateResponse(request, "index.html", {"users": users, "provider": config.LLM_PROVIDER})


@app.post("/users")
def create_user(name: str = Form(...), email: str = Form(...)):
    with SessionLocal() as s:
        u = s.query(User).filter_by(email=email).first()
        if not u:
            u = User(name=name, email=email, topics=["大模型", "AI Agent"], keywords=["LLM", "agent"])
            s.add(u)
            s.commit()
            scheduler.schedule_user(u)
        return RedirectResponse(f"/users/{u.id}", status_code=303)


@app.get("/users/{uid}", response_class=HTMLResponse)
def user_page(request: Request, uid: int):
    with SessionLocal() as s:
        u = s.get(User, uid) or _404()
        briefs = s.query(Brief).filter_by(user_id=uid).order_by(Brief.id.desc()).all()
    job = scheduler.scheduler.get_job(f"user_{uid}")
    return templates.TemplateResponse(request, "user.html", {"u": u, "briefs": briefs,
                                      "next_run": job.next_run_time if job else None, "msg": request.query_params.get("msg")})


@app.post("/users/{uid}/prefs")
def save_prefs(uid: int, name: str = Form(...), topics: str = Form(""), keywords: str = Form(""),
               language: str = Form("zh"), send_time: str = Form("08:00"), email_enabled: str = Form(None)):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", send_time or ""):
        raise HTTPException(422, "send_time must be HH:MM (00:00-23:59)")
    with SessionLocal() as s:
        u = s.get(User, uid) or _404()
        u.name, u.topics, u.keywords, u.language = name, _split(topics), _split(keywords), language
        u.send_time, u.email_enabled = send_time, 1 if email_enabled else 0
        s.commit()
        scheduler.schedule_user(u)
    return RedirectResponse(f"/users/{uid}?msg=saved", status_code=303)


@app.post("/users/{uid}/run")
def run_now(uid: int):
    def _bg():
        try:
            run_for_user(uid)
        except Exception:
            logging.exception("run now failed")
    threading.Thread(target=_bg, daemon=True).start()
    return RedirectResponse(f"/users/{uid}?msg=started", status_code=303)


@app.get("/briefs/{bid}", response_class=HTMLResponse)
def brief_page(request: Request, bid: int):
    with SessionLocal() as s:
        b = s.get(Brief, bid) or _404()
    p = Path(b.path)
    md = p.read_text(encoding="utf-8") if p.exists() else "_（简报文件不存在，见 trace）_"
    return templates.TemplateResponse(request, "brief.html", {"b": b, "html": render_markdown(md),
                                      "trace": json.loads(b.trace_json or "[]")})


def _404():
    raise HTTPException(404)
