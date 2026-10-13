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
from .agent import Agent, dump_trace
from .brief import history_urls, render_markdown, validate_draft
from .evidence import EvidenceStore
from .db import Brief, Delivery, Run, SessionLocal, User
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
             error="" if status != "failed" and b.stopped_reason != "max_steps" else b.stopped_reason)
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
            rel = f"{staging}/draft.json"
            (sb.root / staging).mkdir(parents=True, exist_ok=True)
            store = EvidenceStore()  # 本轮证据：ID 由工具层分配，模型只能引用
            tools = ToolRegistry(Sandbox(sb.root, writable=[staging]), evidence=store)
            agent = Agent(llm or get_provider(), tools)
            started = time.time()
            draft = sb.root / rel
            # 已发布历史的规范 URL 由 Service 读取，用于强制去重（不依赖模型自觉）
            published = history_urls(f.read_text(encoding="utf-8", errors="ignore")
                                     for f in (sb.root / "briefs").rglob("*.md"))
            checked: dict = {}

            def validator(r):
                if not _wrote_this_run(r.trace, rel, draft, started):
                    return [f"you have not written a non-empty draft to {rel} in this run"]
                d, errs = validate_draft(draft.read_text(encoding="utf-8"), store, published)
                checked["draft"] = d
                return errs

            res = agent.run(f"Please prepare today's AI news brief for user {user.name}.", today=today, output=rel,
                            validator=validator)
            draft_valid = (checked.get("draft") is not None
                           and _wrote_this_run(res.trace, rel, draft, started)
                           and not getattr(res, "validation_errors", None))
            ok = res.stopped_reason in ("model_finished", "max_steps") and draft_valid
            path = sb.root / "runs" / today / f"{run_id}.md"
            status = "failed"
            if ok:
                d = checked["draft"]
                items = d.get("items") or []
                if items:
                    # Service 渲染正文：链接只来自证据库，模型文本转义
                    md = sb.root / staging / "brief.md"
                    md.write_text(render_markdown(d, store, user.name, today, user.language,
                                                  stopped_reason=res.stopped_reason, steps=res.steps), encoding="utf-8")
                    path = publish(sb, md, today, run_id)
                    for suffix, body in ((".draft.json", json.dumps(d, ensure_ascii=False, indent=2)),
                                         (".evidence.json", store.to_json())):
                        (path.parent / f"{run_id}{suffix}").write_text(body, encoding="utf-8")
                    status = "ok"
                else:
                    # 0 条项目：禁止在零成功检索证据的前提下判定 no_news（未检索或全源失败不能算无新闻）
                    if not store.has_successful_search():
                        status = "failed"
                        res.stopped_reason = "insufficient_search"
                    else:
                        md = sb.root / staging / "brief.md"
                        md.write_text(render_markdown(d, store, user.name, today, user.language,
                                                      stopped_reason=res.stopped_reason, steps=res.steps), encoding="utf-8")
                        path = publish(sb, md, today, run_id)
                        for suffix, body in ((".draft.json", json.dumps(d, ensure_ascii=False, indent=2)),
                                             (".evidence.json", store.to_json())):
                            (path.parent / f"{run_id}{suffix}").write_text(body, encoding="utf-8")
                        status = "no_news"
            b = Brief(user_id=user.id, date=today, path=str(path), status=status,
                      stopped_reason=res.stopped_reason, steps=res.steps, tokens_in=res.usage["input"],
                      tokens_out=res.usage["output"], trace_json=dump_trace(res.trace), run_id=run_id)
            s.add(b)
            s.flush()
            d = None
            if status == "ok" and user.email_enabled:
                # 简报与待发送记录在同一事务提交；提交后才投递
                d = Delivery(brief_id=b.id, channel="email", recipient=user.email,
                             status="pending" if config.SMTP["enabled"] else "not_configured",
                             message_id=f"<brief-{b.id}-{run_id}@ai-news-agent>")
                if not config.SMTP["enabled"]:
                    d.last_error = "SMTP_ENABLED=0: email is on for this user but the server has no SMTP configured"
                s.add(d)
            s.commit()
            if d is not None and d.status == "pending":
                deliver(d.id)
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


def deliver(delivery_id: int) -> Delivery:
    """发送（或重发）一条投递记录对应的原简报正文。SMTP 接受才记 sent；不承诺 exactly-once。"""
    with SessionLocal() as s:
        d = s.get(Delivery, delivery_id)
        if d is None:
            raise LookupError(delivery_id)
        if d.status == "sent":
            return d
        if not config.SMTP["enabled"]:
            d.status, d.last_error = "not_configured", "SMTP_ENABLED=0"
            s.commit()
            return d

        # 原子条件更新领取任务：仅允许从 pending / failed / unknown 抢占为 sending
        # 防止并发重试或与当前生成线程并发重复发信
        claimed = s.query(Delivery).filter(
            Delivery.id == delivery_id,
            Delivery.status.in_(["pending", "failed", "unknown"])
        ).update(
            {Delivery.status: "sending", Delivery.attempts: Delivery.attempts + 1},
            synchronize_session=False
        )
        s.commit()
        if not claimed:
            return s.get(Delivery, delivery_id)

        s.refresh(d)
        b = s.get(Brief, d.brief_id)
        try:
            from pathlib import Path
            send_email(d.recipient, f"AI 新闻简报 {b.date}", Path(b.path).read_text(encoding="utf-8"),
                       message_id=d.message_id)
            d.status, d.last_error = "sent", ""
        except Exception as e:
            d.status, d.last_error = "failed", f"{type(e).__name__}: {e}"[:500]
        s.commit()
        return d


def mark_stuck_deliveries() -> int:
    """重启时仍是 sending 的投递：SMTP 可能已接受，结果未知，标 unknown，由用户决定是否重发。"""
    with SessionLocal() as s:
        rows = s.query(Delivery).filter_by(status="sending").all()
        for d in rows:
            d.status, d.last_error = "unknown", "process stopped while sending; may or may not have been delivered"
        s.commit()
        return len(rows)


def send_email(to: str, subject: str, body: str, message_id: str = ""):
    c = config.SMTP
    if not c["enabled"]:
        raise RuntimeError("SMTP is not configured")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, c["from"] or c["user"], to
    if message_id:
        msg["Message-ID"] = message_id
    with smtplib.SMTP_SSL(c["host"], c["port"], timeout=20) as srv:
        srv.login(c["user"], c["password"])
        srv.send_message(msg)
