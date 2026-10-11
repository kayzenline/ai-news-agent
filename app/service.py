"""业务层：同步用户画像到工作区文件 -> 跑 Agent -> 记录简报索引与 trace -> 可选邮件推送。"""
import json
import smtplib
import shutil
import threading
import time
import uuid
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
            # 每次运行独立、不可变的输出文件；成功后才同步到 briefs/{today}.md 供去重
            run_id = f"{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}"
            rel = f"runs/{today}/{run_id}.md"
            path = sb.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            started = time.time()
            res = agent.run(f"Please prepare today's AI news brief for user {user.name}.", today=today, output=rel)
            ok = res.stopped_reason == "model_finished" and _wrote_this_run(res.trace, rel, path, started)
            if ok:
                shutil.copyfile(path, sb.root / "briefs" / f"{today}.md")
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


def _wrote_this_run(trace, rel: str, path, started: float) -> bool:
    """本次运行必须成功调用 write_file 写入自己的输出文件，且文件非空、在本次开始后修改。"""
    wrote = any(t.get("type") == "tool" and t.get("name") == "write_file" and t.get("ok")
                and json.loads(t.get("arguments") or "{}").get("path", "").lstrip("/").lstrip("./") == rel
                for t in trace)
    return (wrote and path.is_file() and path.stat().st_mtime >= started - 1
            and path.read_text(encoding="utf-8").strip() != "")


def send_email(to: str, subject: str, body: str):
    c = config.SMTP
    if not c["enabled"]:
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, c["from"] or c["user"], to
    with smtplib.SMTP_SSL(c["host"], c["port"], timeout=20) as srv:
        srv.login(c["user"], c["password"])
        srv.send_message(msg)
