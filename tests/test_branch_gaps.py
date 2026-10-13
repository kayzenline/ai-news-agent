"""补齐覆盖率报告里还没走到的分支。只断言这些分支的对外行为。"""

import json
import socket
import subprocess
import sys
import threading
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.agent import call_key, canonical_args
from app.brief import history_urls, render_markdown, validate_draft
from app.evidence import EvidenceStore, canonical_url
from app.llm.demo import demo_script
from tests.test_brief import draft, item, store


def test_call_key_ignores_non_string_fetch_url_and_bad_json():
    assert call_key("fetch_url", json.dumps({"url": 1, "max_chars": 3})).startswith("fetch_url:")
    assert call_key("fetch_url", "{") == "fetch_url:{"
    assert canonical_args("{") == "{"
    assert canonical_args(1) == 1


def test_draft_shape_and_field_limits():
    s = store()
    assert "JSON object" in validate_draft("[]", s)[1][0]
    assert "note must be a string" in " ".join(validate_draft(draft(note=1), s)[1])
    assert "items must be a list" in " ".join(validate_draft(json.dumps({"schema_version": 1, "items": {}}), s)[1])
    assert "must be an object" in " ".join(validate_draft(draft("nope"), s)[1])
    assert "too long" in " ".join(validate_draft(draft(item(title="x" * 201)), s)[1])
    assert "change_type must be" in " ".join(validate_draft(draft(item(change_type="nope")), s)[1])
    assert "event_id must be a string" in " ".join(validate_draft(draft(item(event_id=1)), s)[1])
    assert "at most 5" in " ".join(validate_draft(draft(item(evidence_ids=["ev_001"] * 6)), s)[1])


def test_english_step_warning_renders_update_delta():
    s = store()
    published = history_urls(["https://ex.com/a"])
    d, errs = validate_draft(draft(item(change_type="update", delta="Now GA")), s, published)
    assert errs == []
    md = render_markdown(d, s, "E", "2026-10-11", language="en", stopped_reason="max_steps", steps=0)
    assert "Notice" in md and "step limit" in md and "Update: Now GA" in md


def test_canonical_url_and_search_lines_without_http():
    assert canonical_url("http://[") == "http://["
    s = EvidenceStore()
    out = s.annotate_search("- [hn] title only\n- [hn] T | not-a-url | 2026")
    assert s.items == {}
    assert out.splitlines() == ["- [hn] title only", "- [hn] T | not-a-url | 2026"]


def test_demo_script_drops_items_after_failed_validation():
    sysm = "Today is 2026-10-11. write this run's structured draft to staging/x/draft.json"
    msgs = [{"role": "system", "content": sysm}, {"role": "user", "content": "go"}]
    steps = [
        ("read_file", '{"name":"A","topics":["AI"]}'),
        ("list_dir", "(empty)"),
        ("web_search", "- ev_001 [hn] Story | https://ex.com/a | "),
        ("write_file", "wrote 1 chars"),
    ]
    for i, (name, content) in enumerate(steps):
        msgs.append(
            {"role": "assistant", "content": "", "tool_calls": [{"id": str(i), "name": name, "arguments": "{}"}]}
        )
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": content})
    msgs.append({"role": "user", "content": "draft failed validation: items[1].title"})
    call = demo_script(msgs).tool_calls[0]
    body = json.loads(json.loads(call.arguments)["content"])
    assert call.name == "write_file" and body["items"] == [] and "不发布" in body["note"]


def test_missing_dotenv_is_ignored(tmp_path):
    from app.config import _load_dotenv

    _load_dotenv(str(tmp_path / "no-such.env"))


def test_check_reports_provider_setup_timeout_and_bad_arguments(monkeypatch, capsys):
    from app.llm import check
    from app.llm.base import LLMResponse, ToolCall

    monkeypatch.setattr(check.config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(check, "get_provider", lambda: (_ for _ in ()).throw(ValueError("missing key")))
    assert check.main() == 2 and "missing key" in capsys.readouterr().out
    monkeypatch.setattr(check, "get_provider", lambda: (_ for _ in ()).throw(RuntimeError("sdk")))
    assert check.main() == 2 and "无法初始化" in capsys.readouterr().out

    class APITimeoutError(Exception):
        pass

    client = SimpleNamespace(close=lambda: None)
    client.with_options = lambda **kw: client

    def timeout_chat(*args):
        raise APITimeoutError("slow")

    monkeypatch.setattr(check, "get_provider", lambda: SimpleNamespace(client=client, chat=timeout_chat))
    assert check.main() == 1 and "超时" in capsys.readouterr().out

    def opaque_chat(*args):
        raise RuntimeError("secret-body")

    monkeypatch.setattr(check, "get_provider", lambda: SimpleNamespace(client=client, chat=opaque_chat))
    assert check.main() == 1
    output = capsys.readouterr().out
    assert "网络连接或响应解析异常" in output and "secret-body" not in output

    monkeypatch.setattr(
        check,
        "get_provider",
        lambda: SimpleNamespace(
            client=client, chat=lambda *a: LLMResponse("", [ToolCall("c", "configuration_check", "{")], "tool_use")
        ),
    )
    assert check.main() == 1 and "未返回预期工具调用" in capsys.readouterr().out


def test_check_module_exits_with_main_status(monkeypatch, capsys):
    import runpy
    import warnings

    from app import config

    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("app.llm.check", run_name="__main__")
    assert exc.value.code == 2 and "fake" in capsys.readouterr().out


def test_claude_chat_and_unknown_provider(monkeypatch):
    from app import config
    from app.llm.providers import ClaudeProvider, get_provider

    created = {}

    def create(**kw):
        created["kw"] = kw
        usage = SimpleNamespace(input_tokens=2, output_tokens=3)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="ok"),
                SimpleNamespace(type="tool_use", id="t1", name="list_dir", input={"path": "."}),
            ],
            stop_reason="tool_use",
            usage=usage,
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda **kw: client))
    monkeypatch.setattr(config, "LLM_PROVIDER", "claude")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_MODEL", "claude-test")
    system, msgs = ClaudeProvider.to_api_messages(
        [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "bash", "arguments": "{"}]},
        ]
    )
    assert system == "" and msgs[0]["content"][0]["input"] == {}
    provider = get_provider()
    response = provider.chat(
        [{"role": "user", "content": "go"}],
        [{"type": "function", "function": {"name": "list_dir", "description": "d", "parameters": {"type": "object"}}}],
    )
    assert response.tool_calls[0].name == "list_dir"
    assert created["kw"]["tools"][0]["input_schema"] == {"type": "object"}

    monkeypatch.setattr(config, "LLM_PROVIDER", "nope")
    with pytest.raises(ValueError, match="unknown LLM_PROVIDER"):
        get_provider()


def test_lifespan_does_not_shut_down_a_stopped_scheduler(monkeypatch):
    from app import main

    class Stopped:
        running = False

        def shutdown(self, wait=False):
            raise AssertionError("scheduler was not running")

    monkeypatch.setattr(main.scheduler, "start", lambda: None)
    monkeypatch.setattr(main.scheduler, "scheduler", Stopped())
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "recover_interrupted", lambda: 0)
    monkeypatch.setattr(main, "mark_stuck_deliveries", lambda: 0)

    async def enter():
        async with main.lifespan(main.app):
            return "up"

    import asyncio

    assert asyncio.run(enter()) == "up"


def test_existing_email_run_without_prefs_and_delivery_retry(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import config, db, main

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    monkeypatch.setitem(config.SMTP, "enabled", False)
    seen = {}

    def submit(uid, trigger="manual"):
        seen["uid"] = uid
        return SimpleNamespace(
            run_id="r1",
            status="queued",
            error="",
            user_id=uid,
            trigger=trigger,
            brief_id=None,
            created_at=datetime(2026, 10, 13, 1, 2, 3),
        )

    monkeypatch.setattr(main, "submit_run", submit)
    with TestClient(main.app) as client:
        assert client.post("/users", data={"name": "A", "email": "dup@x.com"}).status_code == 200
        r_mismatch = client.post("/users", data={"name": "B", "email": "dup@x.com"}, follow_redirects=False)
        assert r_mismatch.status_code == 303 and "error=name_mismatch" in r_mismatch.headers.get("location", "")
        with db.SessionLocal() as s:
            rows = s.query(db.User).filter_by(email="dup@x.com").all()
            assert len(rows) == 1 and rows[0].name == "A"
            uid = rows[0].id
        response = client.post(f"/users/{uid}/run", data={"note": "no prefs"}, follow_redirects=False)
        assert response.status_code == 303 and seen["uid"] == uid
        with db.SessionLocal() as s:
            user = s.get(db.User, uid)
            brief = db.Brief(user_id=uid, date="2026-10-13", path=str(tmp_path / "missing.md"), status="ok")
            s.add(brief)
            s.flush()
            delivery = db.Delivery(brief_id=brief.id, recipient=user.email, status="pending")
            s.add(delivery)
            s.commit()
            did, bid = delivery.id, brief.id
        retry = client.post(f"/deliveries/{did}/retry", follow_redirects=False)
        assert retry.status_code == 303 and retry.headers["location"].endswith(f"/briefs/{bid}")
        with db.SessionLocal() as s:
            assert s.get(db.Delivery, did).status == "not_configured"


def test_blank_name_is_left_unchanged():
    from app.db import User
    from app.main import _update_user_prefs

    user = User(name="Keep", email="k@x.com", topics=[], keywords=[])
    _update_user_prefs(user, None, "AI", "llm", "en", "09:30", False, "Australia/Sydney")
    assert (
        user.name == "Keep"
        and user.topics == ["AI"]
        and user.email_enabled == 0
        and user.timezone == "Australia/Sydney"
    )


def test_scheduler_fallbacks(tmp_path, monkeypatch):
    from app import config, db
    from app import scheduler as sched

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
    user = db.User(name="A", email="tz@x.com", timezone="Not/AZone")
    assert sched.user_tz(user).key == config.DEFAULT_TIMEZONE

    class Never(datetime):
        def astimezone(self, tz=None):
            return datetime(1999, 1, 1, tzinfo=tz)

    wall = Never(2026, 10, 4, 2, 30, tzinfo=ZoneInfo("Australia/Sydney"))
    assert sched._first_valid(wall) == wall

    with db.SessionLocal() as s:
        s.add(db.User(name="A", email="bad-time@x.com", send_time="nope", timezone="Asia/Shanghai"))
        s.add(db.User(name="B", email="job@x.com", send_time="08:00", timezone="Asia/Shanghai"))
        s.commit()
        uid = s.query(db.User).filter_by(email="job@x.com").one().id
        bad_id = s.query(db.User).filter_by(email="bad-time@x.com").one().id
    due = {user_id for user_id, _day in sched.due_users(datetime(2026, 10, 13, 2, 0, tzinfo=ZoneInfo("UTC")))}
    assert bad_id not in due

    submitted = {}

    def submit(user_id, trigger="manual", idempotency_key=None, today=None):
        submitted["args"] = (user_id, trigger, idempotency_key, today)
        return SimpleNamespace(run_id="sched-1", status="queued")

    monkeypatch.setattr("app.service.submit_run", submit)
    sched._job(uid)
    assert submitted["args"][0] == uid and submitted["args"][1] == "scheduled"
    monkeypatch.setattr("app.service.submit_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    sched._job(uid)


def test_run_lock_missing_delivery_and_send_email(tmp_path, monkeypatch):
    from app import config, db, service

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
    lock = service._locks.setdefault(424242, threading.Lock())
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="already in progress"):
            service.run_for_user(424242)
    finally:
        lock.release()
        service._locks.pop(424242, None)

    with pytest.raises(LookupError):
        service.deliver(10**9)

    with db.SessionLocal() as s:
        user = db.User(name="A", email="mail@x.com")
        s.add(user)
        s.flush()
        brief = db.Brief(user_id=user.id, date="2026-10-13", path="x", status="ok")
        s.add(brief)
        s.flush()
        delivery = db.Delivery(brief_id=brief.id, recipient="mail@x.com", status="pending")
        s.add(delivery)
        s.commit()
        did = delivery.id
    monkeypatch.setitem(config.SMTP, "enabled", False)
    assert service.deliver(did).status == "not_configured"

    with pytest.raises(RuntimeError, match="not configured"):
        service.send_email("mail@x.com", "s", "body")

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=20):
            self.host, self.port, self.timeout = host, port, timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, password):
            self.user, self.password = user, password

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(service.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setitem(config.SMTP, "enabled", True)
    monkeypatch.setitem(config.SMTP, "host", "smtp.example")
    monkeypatch.setitem(config.SMTP, "port", 465)
    monkeypatch.setitem(config.SMTP, "user", "bot")
    monkeypatch.setitem(config.SMTP, "password", "pw")
    monkeypatch.setitem(config.SMTP, "from", "")
    service.send_email("mail@x.com", "subj", "body", message_id="<m@x>")
    service.send_email("mail@x.com", "subj", "body")
    assert sent[0]["Message-ID"] == "<m@x>" and sent[0]["From"] == "bot"
    assert "Message-ID" not in sent[1]


def test_filesystem_edge_branches(tmp_path, monkeypatch):
    from app.tools import fs_tools as T
    from app.tools.sandbox import Sandbox, SandboxError

    sb = Sandbox(tmp_path / "ws")
    assert sb.resolve(None) == sb.root
    (sb.root / "briefs").mkdir()
    text = "\n".join(f"hit {i}" for i in range(60))
    T.write_file(sb, "big.txt", text)
    with pytest.raises(NotADirectoryError):
        T.list_dir(sb, "big.txt")
    monkeypatch.setattr(T, "MAX_READ", 5)
    assert T.read_file(sb, "big.txt").endswith("[truncated]")
    with pytest.raises(NotADirectoryError):
        T.search_content(sb, "hit", "big.txt")
    truncated = T.search_content(sb, "hit", ".")
    assert truncated.count("big.txt:") == 50 and truncated.endswith("[more hits truncated]")
    locked = sb.root / "locked.txt"
    locked.write_text("hit hidden\n")
    locked.chmod(0)
    try:
        assert "hidden" not in T.search_content(sb, "hidden", ".")
    finally:
        locked.chmod(0o644)
    with pytest.raises(SandboxError, match="workspace root"):
        T.write_file(sb, ".", "no")

    with pytest.raises(SandboxError, match="needs a value"):
        T.bash(sb, "find . -name")
    assert T.bash(sb, "find . -print").startswith("[exit 0]")
    with pytest.raises(SandboxError, match="unexpected find argument"):
        T.bash(sb, "find . -name x stray")
    assert T.bash(sb, "ls -- briefs").startswith("[exit 0]")
    with pytest.raises(SandboxError, match="needs a value"):
        T.bash(sb, "head -n")
    with pytest.raises(SandboxError, match="date only accepts"):
        T.bash(sb, "date tomorrow")
    with pytest.raises(ValueError, match="empty command"):
        T.bash(sb, "   ")
    monkeypatch.setattr(T, "MAX_GLOB_MATCHES", 1)
    (sb.root / "a.txt").write_text("a")
    (sb.root / "b.txt").write_text("b")
    with pytest.raises(SandboxError, match="matches"):
        T.bash(sb, "ls *.txt")

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ls", timeout=10)

    monkeypatch.setattr(T.subprocess, "run", timed_out)
    with pytest.raises(TimeoutError, match="timed out"):
        T.bash(sb, "ls briefs")


def test_news_source_and_fetch_branches(monkeypatch):
    import httpx

    from app import config
    from app.tools import news_tools as N

    def redirect(url, **kwargs):
        return httpx.Response(
            301, headers={"location": "https://arxiv.org/api/query?x=1"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(N.httpx, "get", redirect)
    with pytest.raises(ValueError, match="too many redirects"):
        N._arxiv_get({})

    assert N.query_terms("中")[1] == ["中"]
    assert N.query_terms("模型")[1] == ["模型"]

    monkeypatch.setattr(N, "RSS_FEEDS", {"feed": "https://feed.example/rss"})
    monkeypatch.setattr(N.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert N._rss("query", 2, errors=None, notes=None) == []

    monkeypatch.setattr(config, "TAVILY_API_KEY", "k")

    def post(url, **kwargs):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "T", "url": "https://ex.com/t", "published_date": "not-a-date", "content": "c"},
                ]
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(N.httpx, "post", post)
    assert "https://ex.com/t" in N._tavily("x", 5)[0]["url"]

    unknown = N.web_search("x", sources=["nope"])
    assert "unknown source nope" in unknown and unknown.all_failed
    monkeypatch.setattr(N, "_hn", lambda query, days: (_ for _ in ()).throw(RuntimeError("boom")))
    failed = N.web_search("x", sources=["hackernews"])
    assert "RuntimeError: boom" in failed and "hackernews" in failed.sources_failed

    monkeypatch.setattr(N.socket, "getaddrinfo", lambda *a, **k: [])
    N.check_public_url("https://example.com/a")
    monkeypatch.setattr(N.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror(8, "no")))
    with pytest.raises(ValueError, match="cannot resolve"):
        N.check_public_url("https://missing.example/")
    with pytest.raises(ValueError, match="no host"):
        N.check_public_url("http://")

    def two_public(host, port, type=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
        ]

    monkeypatch.setattr(N.socket, "getaddrinfo", two_public)
    N.check_public_url("https://example.com/a")


def test_pdf_text_and_redirect_limit(monkeypatch):
    import httpx
    import pypdf

    from app.tools import news_tools as N

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class TwoPages:
        def __init__(self, buf):
            self.pages = [Page("Hello   world"), Page("second")]

    class LongPage:
        def __init__(self, buf):
            self.pages = [Page("x" * 50), Page("later")]

    class Broken:
        def __init__(self, buf):
            raise RuntimeError("bad pdf")

    monkeypatch.setattr(pypdf, "PdfReader", TwoPages)
    assert N._extract_pdf_text(b"%PDF") == "Hello world\n\nsecond"
    monkeypatch.setattr(pypdf, "PdfReader", LongPage)
    assert N._extract_pdf_text(b"%PDF", max_chars=10) == "x" * 10
    monkeypatch.setattr(pypdf, "PdfReader", Broken)
    assert N._extract_pdf_text(b"%PDF").startswith("ERROR: failed to extract text from PDF")

    monkeypatch.setattr(N, "check_public_url", lambda url: None)
    monkeypatch.setattr(N, "_extract_pdf_text", lambda raw, max_chars=4000: "from pdf")
    pdf = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-type": "application/pdf"}, stream=httpx.ByteStream(b"%PDF-1.4")
            )
        )
    )
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: pdf.stream(method, url, follow_redirects=False))
    assert N.fetch_url("https://ex.com/paper") == "from pdf"

    hops = {"n": 0}

    def always_redirect(req):
        hops["n"] += 1
        return httpx.Response(302, headers={"location": "https://ex.com/next"})

    redir = httpx.Client(transport=httpx.MockTransport(always_redirect))
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: redir.stream(method, url, follow_redirects=False))
    monkeypatch.setattr(N, "MAX_REDIRECTS", 0)
    with pytest.raises(ValueError, match="too many redirects"):
        N.fetch_url("https://ex.com/start")
    assert hops["n"] == 1


def test_registry_rejects_non_object_and_truncates(tmp_path, monkeypatch):
    from app import config
    from app.tools import fs_tools as T
    from app.tools.registry import ToolRegistry
    from app.tools.sandbox import Sandbox

    sb = Sandbox(tmp_path / "ws")
    reg = ToolRegistry(sb)
    ok, out = reg.call("read_file", "[]")
    assert not ok and "JSON object" in out
    monkeypatch.setattr(config, "TOOL_OUTPUT_LIMIT", 8)
    T.write_file(sb, "a.txt", "0123456789abcdef")
    ok, out = reg.call("read_file", '{"path":"a.txt"}')
    assert ok and "truncated" in out

    plain = ToolRegistry(sb, extra={"web_search": lambda query, **k: "- [hn] A | https://ex.com/a | "})
    ok, out = plain.call("web_search", '{"query":"x"}')
    assert ok and out.startswith("- [hn] A") and "ev_" not in out
