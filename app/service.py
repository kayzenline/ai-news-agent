"""业务层：同步用户画像到工作区文件 -> 跑 Agent -> 记录简报索引与 trace -> 可选邮件推送。"""
import json
import smtplib
import threading
from datetime import date
from email.mime.text import MIMEText

from . import config
from .agent import Agent, dump_trace
from .db import Brief, SessionLocal, User
from .llm.providers import get_provider
from .tools.registry import ToolRegistry
from .tools.sandbox import Sandbox

_locks: dict[int, threading.Lock] = {}


def workspace(user_id: int) -> Sandbox:
    return Sandbox(config.DATA_DIR / "workspaces" / f"user_{user_id}")


def sync_profile(user: User) -> Sandbox:
    sb = workspace(user.id)
    (sb.root / "briefs").mkdir(exist_ok=True)
    (sb.root / "profile.json").write_text(json.dumps({
        "name": user.name, "topics": user.topics, "keywords": user.keywords, "language": user.language,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return sb


def run_for_user(user_id: int, llm=None, today: str | None = None) -> Brief:
    lock = _locks.setdefault(user_id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("a run is already in progress for this user")
    try:
        with SessionLocal() as s:
            user = s.get(User, user_id)
            sb = sync_profile(user)
            today = today or date.today().isoformat()
            agent = Agent(llm or get_provider(), ToolRegistry(sb))
            res = agent.run(f"Please prepare today's AI news brief for user {user.name}.", today=today)
            path = sb.root / "briefs" / f"{today}.md"
            ok = path.exists() and res.stopped_reason == "model_finished"
            b = Brief(user_id=user.id, date=today, path=str(path), status="ok" if ok else "failed",
                      stopped_reason=res.stopped_reason, steps=res.steps, tokens_in=res.usage["input"],
                      tokens_out=res.usage["output"], trace_json=dump_trace(res.trace))
            s.add(b)
            s.commit()
            if ok and user.email_enabled:
                try:
                    send_email(user.email, f"AI 新闻简报 {today}", path.read_text(encoding="utf-8"))
                except Exception as e:  # 推送失败不影响简报落盘
                    b.stopped_reason += f" (email failed: {e})"
                    s.commit()
            return b
    finally:
        lock.release()


def send_email(to: str, subject: str, body: str):
    c = config.SMTP
    if not c["enabled"]:
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, c["from"] or c["user"], to
    with smtplib.SMTP_SSL(c["host"], c["port"], timeout=20) as srv:
        srv.login(c["user"], c["password"])
        srv.send_message(msg)
