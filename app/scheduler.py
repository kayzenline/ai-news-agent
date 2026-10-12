"""APScheduler：每个用户一个 cron（按用户 IANA 时区的 send_time），外加补跑扫描。

业务日期按用户时区计算；定时任务以 (用户, 本地日期) 为幂等键，同日最多一个逻辑 run。
误触发宽限只覆盖调度器知道的错过执行；进程停机期间错过的由 catch_up() 在窗口内补一次。
DST 跳过的本地时刻由补跑扫描在当天恢复后执行；重复时刻靠幂等键只执行一次。
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler

from . import config
from .db import Run, SessionLocal, User

log = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")


def user_tz(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or config.DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        log.error("invalid timezone %r for user=%s; using %s", user.timezone, user.id, config.DEFAULT_TIMEZONE)
        return ZoneInfo(config.DEFAULT_TIMEZONE)


def business_date(user: User, now: datetime | None = None) -> str:
    now = now or datetime.now(ZoneInfo("UTC"))
    return now.astimezone(user_tz(user)).date().isoformat()


def sched_key(user_id: int, day: str) -> str:
    return f"sched:{user_id}:{day}"


def _job(user_id: int):
    from .service import submit_run
    try:
        with SessionLocal() as s:
            u = s.get(User, user_id)
            day = business_date(u)
        r = submit_run(user_id, trigger="scheduled", idempotency_key=sched_key(user_id, day), today=day)
        log.info("scheduled run user=%s run=%s status=%s", user_id, r.run_id, r.status)
    except Exception:
        log.exception("scheduled run submit failed user=%s", user_id)


def schedule_user(user: User):
    hh, mm = (user.send_time or "08:00").split(":")
    scheduler.add_job(_job, "cron", hour=int(hh), minute=int(mm), args=[user.id], id=f"user_{user.id}",
                      timezone=user_tz(user), replace_existing=True, misfire_grace_time=3600,
                      coalesce=True, max_instances=1)


def _first_valid(wall: datetime) -> datetime:
    """DST 跳过的本地时刻（如悉尼 02:30 不存在）顺延到当天恢复后的首个有效时刻（03:00）。"""
    utc = ZoneInfo("UTC")
    t = wall
    for _ in range(180):
        if t.astimezone(utc).astimezone(wall.tzinfo).replace(tzinfo=None) == t.replace(tzinfo=None):
            return t
        t = (t.replace(tzinfo=None) + timedelta(minutes=1)).replace(tzinfo=wall.tzinfo)
    return wall


def due_users(now: datetime | None = None) -> list[tuple[int, str]]:
    """今天（用户本地日期）已过开始时间、仍在补跑窗口内、且没有当日定时 run 的用户。"""
    now = now or datetime.now(ZoneInfo("UTC"))
    out = []
    with SessionLocal() as s:
        for u in s.query(User).all():
            tz = user_tz(u)
            local = now.astimezone(tz)
            try:
                hh, mm = map(int, (u.send_time or "08:00").split(":"))
                due = datetime(local.year, local.month, local.day, hh, mm, tzinfo=tz)
            except ValueError:
                continue
            due = _first_valid(due)
            if not (due <= local <= due + timedelta(hours=config.CATCHUP_HOURS)):
                continue
            day = local.date().isoformat()
            if s.query(Run).filter_by(idempotency_key=sched_key(u.id, day)).first() is None:
                out.append((u.id, day))
    return out


def catch_up(now: datetime | None = None) -> list[str]:
    from .service import submit_run
    started = []
    for uid, day in due_users(now):
        r = submit_run(uid, trigger="scheduled", idempotency_key=sched_key(uid, day), today=day)
        log.info("catch-up run user=%s day=%s run=%s", uid, day, r.run_id)
        started.append(r.run_id)
    return started


def start():
    with SessionLocal() as s:
        for u in s.query(User).all():
            try:
                schedule_user(u)
            except ValueError:  # 坏的存量配置只跳过该用户，不阻止整个服务启动
                log.error("invalid send_time %r for user=%s; job skipped", u.send_time, u.id)
    scheduler.add_job(catch_up, "interval", minutes=10, id="catch_up", replace_existing=True,
                      next_run_time=datetime.now(ZoneInfo("UTC")))
    if not scheduler.running:
        scheduler.start()
