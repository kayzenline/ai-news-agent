"""APScheduler：每个用户一个 cron job（按其 send_time），修改偏好后重新注册。"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .db import SessionLocal, User

log = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def _job(user_id: int):
    from datetime import date
    from .service import submit_run
    try:
        r = submit_run(user_id, trigger="scheduled", idempotency_key=f"sched:{user_id}:{date.today().isoformat()}")
        log.info("scheduled run user=%s run=%s status=%s", user_id, r.run_id, r.status)
    except Exception:
        log.exception("scheduled run submit failed user=%s", user_id)


def schedule_user(user: User):
    hh, mm = (user.send_time or "08:00").split(":")
    scheduler.add_job(_job, "cron", hour=int(hh), minute=int(mm), args=[user.id], id=f"user_{user.id}",
                      replace_existing=True, misfire_grace_time=3600, coalesce=True, max_instances=1)


def start():
    with SessionLocal() as s:
        for u in s.query(User).all():
            try:
                schedule_user(u)
            except ValueError:  # 坏的存量配置只跳过该用户，不阻止整个服务启动
                log.error("invalid send_time %r for user=%s; job skipped", u.send_time, u.id)
    if not scheduler.running:
        scheduler.start()
