"""业务层：同步用户画像到工作区文件 -> 跑 Agent -> 记录简报索引与 trace -> 可选邮件推送。"""
import json
import smtplib
import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from email.mime.text import MIMEText

from . import config
from .agent import Agent, count_items, dump_trace, validate_brief
from .db import Brief, Run, SessionLocal, User
from .llm.providers import get_provider
from .tools.registry import ToolRegistry
from .tools.sandbox import Sandbox

_locks: dict[int, threading.Lock] = {}
log = logging.getLogger(__name__)
ACTIVE = ("queued", "running")
_executor: ThreadPoolExecutor | None = None
_futures: list[Future] = []
_submit_lock = threading.Lock()


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_RUNS, thread_name_prefix="run")
    return _executor


def new_run_id() -> str:
    return f"{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}"


def submit_run(user_id: int, trigger: str = "manual", idempotency_key: str | None = None,
               llm=None, today: str | None = None) -> Run:
    """统一受理：先持久化 run，再交给有界执行池。同用户已有活动 run 或幂等键已存在时返回已有 run。"""
    with _submit_lock, SessionLocal() as s:
        if s.get(User, user_id) is None:
            raise LookupError(f"user {user_id} not found")
        if idempotency_key:
            old = s.query(Run).filter_by(idempotency_key=idempotency_key).first()
            if old:
                return old
        active = s.query(Run).filter(Run.user_id == user_id, Run.status.in_(ACTIVE)).first()
        if active:
            return active
        run = Run(run_id=new_run_id(), user_id=user_id, trigger=trigger, idempotency_key=idempotency_key)
        if s.query(Run).filter_by(status="queued").count() >= config.MAX_QUEUED_RUNS:
            run.status, run.error, run.finished_at = "failed", "capacity: run queue is full, try later", datetime.now()
            s.add(run)
            s.commit()
            return run
        s.add(run)
        s.commit()
    _futures.append(_pool().submit(_execute, run.id, llm, today))
    return run


def _set(run_pk: int, **kw):
    with SessionLocal() as s:
        r = s.get(Run, run_pk)
        for k, v in kw.items():
            setattr(r, k, v)
        s.commit()


def _execute(run_pk: int, llm=None, today=None):
    with SessionLocal() as s:
        r = s.get(Run, run_pk)
        user_id, run_id = r.user_id, r.run_id
    _set(run_pk, status="running", started_at=datetime.now())
    try:
        b = run_for_user(user_id, llm=llm, today=today, run_id=run_id)
        status = {"ok": "succeeded", "no_news": "no_news"}.get(b.status, "failed")
        _set(run_pk, status=status, brief_id=b.id, finished_at=datetime.now(),
             error="" if status != "failed" else b.stopped_reason)
    except Exception as e:  # 初始化/锁/DB 等任何异常都落到 run 终态，不只写日志
        log.exception("run %s failed", run_id)
        _set(run_pk, status="failed", error=f"{type(e).__name__}: {e}"[:500], finished_at=datetime.now())


def wait_all(timeout: float = 30):
    """测试与优雅退出用：等待已提交的 run 结束。"""
    while _futures:
        _futures.pop(0).result(timeout=timeout)


def recover_interrupted() -> int:
    """进程重启时：上次遗留的 queued/running 标为 interrupted（先可见、可重试，不做续跑）。"""
    with SessionLocal() as s:
        rows = s.query(Run).filter(Run.status.in_(ACTIVE)).all()
        for r in rows:
            r.status, r.finished_at = "interrupted", datetime.now()
            r.error = r.error or "process restarted before the run finished"
        s.commit()
        return len(rows)


def workspace(user_id: int) -> Sandbox:
    return Sandbox(config.DATA_DIR / "workspaces" / f"user_{user_id}")


def sync_profile(user: User) -> Sandbox:
    sb = workspace(user.id)
    (sb.root / "briefs").mkdir(exist_ok=True)
    (sb.root / "profile.json").write_text(json.dumps({
        "name": user.name, "topics": user.topics, "keywords": user.keywords, "language": user.language,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return sb


def run_for_user(user_id: int, llm=None, today: str | None = None, run_id: str | None = None) -> Brief:
    lock = _locks.setdefault(user_id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("a run is already in progress for this user")
    try:
        with SessionLocal() as s:
            user = s.get(User, user_id)
            sb = sync_profile(user)
            from .scheduler import business_date
            today = today or business_date(user)  # 业务日期按用户时区，而不是宿主机时区
            run_id = run_id or new_run_id()
            # 模型只能写本轮 staging；画像、briefs/ 历史、runs/ 发布目录对模型只读，只有 Service 发布
            staging = f"staging/{run_id}"
            rel = f"{staging}/brief.md"
            (sb.root / staging).mkdir(parents=True, exist_ok=True)
            tools = ToolRegistry(Sandbox(sb.root, writable=[staging]))
            agent = Agent(llm or get_provider(), tools)
            started = time.time()
            draft = sb.root / rel

            def validator(r):
                if not _wrote_this_run(r.trace, rel, draft, started):
                    return [f"you have not written a non-empty brief to {rel} in this run"]
                return validate_brief(draft.read_text(encoding="utf-8"), r.evidence_urls)

            res = agent.run(f"Please prepare today's AI news brief for user {user.name}.", today=today, output=rel,
                            validator=validator)
            ok = res.stopped_reason == "model_finished" and _wrote_this_run(res.trace, rel, draft, started)
            path = sb.root / "runs" / today / f"{run_id}.md"
            status = "failed"
            if ok:
                path = publish(sb, draft, today, run_id)
                # 0 条合格条目 => no_news（检索正常但没有值得发的内容），不凑数、不发邮件
                status = "ok" if count_items(path.read_text(encoding="utf-8")) else "no_news"
            b = Brief(user_id=user.id, date=today, path=str(path), status=status,
                      stopped_reason=res.stopped_reason, steps=res.steps, tokens_in=res.usage["input"],
                      tokens_out=res.usage["output"], trace_json=dump_trace(res.trace), run_id=run_id)
            s.add(b)
            s.commit()
            if status == "ok" and user.email_enabled:
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
