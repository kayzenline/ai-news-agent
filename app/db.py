"""SQLite + SQLAlchemy：users / briefs 两张表，启动时 create_all（轻量 migration）。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    topics: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    language: Mapped[str] = mapped_column(String(10), default="zh")
    send_time: Mapped[str] = mapped_column(String(5), default="08:00")  # HH:MM，用户时区内的“每日开始生成时间”
    timezone: Mapped[str] = mapped_column(
        String(64), default=lambda: config.DEFAULT_TIMEZONE
    )  # IANA，如 Australia/Sydney
    email_enabled: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Brief(Base):
    __tablename__ = "briefs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    date: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20))  # ok | failed
    stopped_reason: Mapped[str] = mapped_column(String(30), default="")
    steps: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    trace_json: Mapped[str] = mapped_column(Text, default="[]")
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Delivery(Base):
    """投递与生成解耦：一份简报每个渠道/收件人一条记录，失败只重发原正文，不重跑模型。"""

    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("brief_id", "channel", "recipient"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brief_id: Mapped[int] = mapped_column(ForeignKey("briefs.id"), index=True)
    channel: Mapped[str] = mapped_column(String(20), default="email")
    recipient: Mapped[str] = mapped_column(String(200))
    # disabled | not_configured | pending | sending | sent | failed | unknown
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    message_id: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Run(Base):
    """请求受理即入库：queued -> running -> succeeded | no_news | failed | interrupted。失败与中断也可见。"""

    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(20))  # manual | scheduled
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    error: Mapped[str] = mapped_column(Text, default="")
    brief_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


_engine = None
SessionLocal = sessionmaker(expire_on_commit=False)


def init_db(url: str | None = None):
    global _engine
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(url or f"sqlite:///{config.DATA_DIR / 'app.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    SessionLocal.configure(bind=_engine)
    return _engine


def _add_missing_columns(engine):
    """create_all 不会给已有表加列：这里只做“补缺失的可空列”这一种最小迁移，不改类型、不删列。"""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    ddl = col.type.compile(engine.dialect)
                    default = col.default.arg if col.default is not None and col.default.is_scalar else None
                    extra = f" DEFAULT {default!r}" if default is not None else ""
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}{extra}"))
