"""APScheduler：每个用户一个 cron job（按其 send_time），修改偏好后重新注册。"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .db import SessionLocal, User

log = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def _job(user_id: int):
    from .service import run_for_user
    try:
        b = run_for_user(user_id)
        log.info("scheduled brief user=%s status=%s", user_id, b.status)
    except Exception:
        log.exception("scheduled run failed user=%s", user_id)


def schedule_user(user: User):
    hh, mm = (user.send_time or "08:00").split(":")
    scheduler.add_job(_job, "cron", hour=int(hh), minute=int(mm), args=[user.id], id=f"user_{user.id}",
                      replace_existing=True, misfire_grace_time=3600, coalesce=True, max_instances=1)


def start():
    with SessionLocal() as s:
        for u in s.query(User).all():
            schedule_user(u)
    if not scheduler.running:
        scheduler.start()
