"""业务层：同步用户画像到工作区文件 -> 跑 Agent -> 记录简报索引与 trace -> 可选邮件推送。"""
import json
import smtplib
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
            run_id = f"{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}"
            # 模型只能写本轮 staging；画像、briefs/ 历史、runs/ 发布目录对模型只读，只有 Service 发布
            staging = f"staging/{run_id}"
            rel = f"{staging}/brief.md"
            (sb.root / staging).mkdir(parents=True, exist_ok=True)
            tools = ToolRegistry(Sandbox(sb.root, writable=[staging]))
            agent = Agent(llm or get_provider(), tools)
            started = time.time()
            res = agent.run(f"Please prepare today's AI news brief for user {user.name}.", today=today, output=rel)
            ok = res.stopped_reason == "model_finished" and _wrote_this_run(res.trace, rel, sb.root / rel, started)
            path = sb.root / "runs" / today / f"{run_id}.md"
            if ok:
                path = publish(sb, sb.root / rel, today, run_id)
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


def publish(sb: Sandbox, src, today: str, run_id: str):
    """追加式发布：runs/<date>/<run_id>.md 为正文，briefs/<date>/<run_id>.md 为去重历史；从不覆盖旧记录。"""
    body = src.read_text(encoding="utf-8")
    out = sb.root / "runs" / today / f"{run_id}.md"
    hist = sb.root / "briefs" / today / f"{run_id}.md"
    for dst in (out, hist):
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(dst)  # 同一文件系统内原子改名
    return out


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
