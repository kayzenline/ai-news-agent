import json

from fastapi.testclient import TestClient


def test_pages_and_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    from app import main, service
    from app.tools import news_tools

    monkeypatch.setattr(news_tools, "web_search", lambda query, **k: "- [hn] A | https://ex.com/a | ")
    with TestClient(main.app) as c:
        assert c.get("/").status_code == 200
        r = c.post("/users", data={"name": "Edison", "email": "e@x.com"})
        assert r.status_code == 200 and "订阅偏好" in r.text
        c.post(
            "/users/1/prefs", data={"name": "Edison", "topics": "大模型,Agent", "keywords": "LLM", "send_time": "02:00"}
        )
        b = service.run_for_user(1)
        assert b.status == "ok"
        page = c.get(f"/briefs/{b.id}")
        assert "ex.com/a" in page.text and "write_file" in page.text


def test_nickname_xss_is_sanitized(tmp_path, monkeypatch):
    """review fix #2: nickname reaches the brief heading; raw HTML must not survive rendering."""
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    from app import main, service
    from app.tools import news_tools

    monkeypatch.setattr(news_tools, "web_search", lambda query, **k: "- [hn] A | https://ex.com/a | ")
    evil = "<img src=x onerror=alert(1)>"
    with TestClient(main.app) as c:
        c.post("/users", data={"name": evil, "email": "e@x.com"})
        uid = max(u.id for u in service.SessionLocal().query(service.User).all())
        b = service.run_for_user(uid)
        page = c.get(f"/briefs/{b.id}")
        assert page.status_code == 200
        card = page.text.split('<div class="card">', 1)[1].split("</div>", 1)[0]
        assert "<img" not in card and "&lt;img" in card  # 只作为转义后的文本出现
        assert "ex.com/a" in card


def test_render_markdown_allowlist():
    from app.main import render_markdown

    out = render_markdown("# hi <script>alert(1)</script>\n\n[x](javascript:alert(1)) [ok](https://ex.com)")
    assert "<script" not in out and "javascript:" not in out and 'href="https://ex.com"' in out


def _setup(tmp_path, monkeypatch):
    from app import config, db, service

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
    with db.SessionLocal() as s:
        u = db.User(name="Edison", email="e@x.com", email_enabled=1)
        s.add(u)
        s.commit()
        uid = u.id
    sent = []
    monkeypatch.setitem(config.SMTP, "enabled", True)
    monkeypatch.setattr(service, "send_email", lambda *a, **k: sent.append(a))
    from app.tools import news_tools

    monkeypatch.setattr(
        news_tools, "web_search", lambda query, **k: "- [hn] A | https://ex.com/a | \n- [hn] B | https://ex.com/b | "
    )
    return service, uid, sent


def D(*ids, title="Story"):
    """合法 draft.json：每个 evidence id 一条。"""
    return json.dumps(
        {
            "schema_version": 1,
            "items": [
                {
                    "title": f"{title} {i}",
                    "topic": "AI",
                    "summary": "Short summary.",
                    "why_it_matters": "You follow AI.",
                    "evidence_ids": [i],
                }
                for i in ids
            ],
        }
    )


def _writer(text):
    import json
    import re

    from app.llm.base import LLMResponse, ToolCall

    def model(msgs):
        n = sum(1 for m in msgs if m["role"] == "tool")
        if n == 0:
            return LLMResponse("", [ToolCall("s", "web_search", json.dumps({"query": "ai"}))], "tool_use")
        if n == 2:
            return LLMResponse("done", [], "end")
        out = re.search(r"draft to (\S+)", msgs[0]["content"]).group(1)
        return LLMResponse("", [ToolCall("w", "write_file", json.dumps({"path": out, "content": text}))], "tool_use")

    return model


def test_stale_same_day_file_does_not_mark_run_ok(tmp_path, monkeypatch):
    """review fix #3: second run that writes nothing must fail and not re-send email."""
    from app.llm.base import LLMResponse
    from app.llm.providers import FakeProvider

    service, uid, sent = _setup(tmp_path, monkeypatch)
    b1 = service.run_for_user(uid, llm=FakeProvider(_writer(D("ev_001"))), today="2026-10-11")
    b2 = service.run_for_user(uid, llm=FakeProvider([LLMResponse("nothing", [], "end")]), today="2026-10-11")
    assert b1.status == "ok" and b2.status == "failed"
    assert len(sent) == 1


def test_same_day_runs_keep_their_own_body(tmp_path, monkeypatch):
    """review fix #4 + design review P1-2: history is append-only, A -> B -> A still finds A."""
    from pathlib import Path

    from app.llm.providers import FakeProvider
    from app.tools import fs_tools

    service, uid, _ = _setup(tmp_path, monkeypatch)
    b1 = service.run_for_user(uid, llm=FakeProvider(_writer(D("ev_001"))), today="2026-10-11")
    b2 = service.run_for_user(uid, llm=FakeProvider(_writer(D("ev_002"))), today="2026-10-11")
    assert b1.path != b2.path
    assert "https://ex.com/a" in Path(b1.path).read_text() and "ex.com/b" not in Path(b1.path).read_text()
    assert "https://ex.com/b" in Path(b2.path).read_text()
    sb = service.workspace(uid)
    assert "ex.com/a" in fs_tools.search_content(sb, "https://ex.com/a", "briefs")
    assert "ex.com/b" in fs_tools.search_content(sb, "https://ex.com/b", "briefs")
    # 第三次又想发 A：Service 用完整历史强制去重，草稿被打回
    b3 = service.run_for_user(uid, llm=FakeProvider(_writer(D("ev_001"))), today="2026-10-11")
    assert b3.status == "failed" and b3.stopped_reason == "validation_failed"
    assert "already published" in b3.trace_json


def test_model_cannot_rewrite_history_or_profile(tmp_path, monkeypatch):
    """design review P1-2: the agent may only write its own staging dir."""
    import json
    from pathlib import Path

    from app.llm.base import LLMResponse, ToolCall
    from app.llm.providers import FakeProvider

    service, uid, _ = _setup(tmp_path, monkeypatch)
    b1 = service.run_for_user(uid, llm=FakeProvider(_writer(D("ev_001"))), today="2026-10-11")
    hist = next((tmp_path / "workspaces" / f"user_{uid}" / "briefs").rglob("*.md"))
    before_run, before_hist = Path(b1.path).read_text(), hist.read_text()
    rel_old = str(Path(b1.path).relative_to(tmp_path / "workspaces" / f"user_{uid}"))
    seen = []

    def evil(msgs):
        if msgs[-1]["role"] == "tool":
            seen.append(msgs[-1]["content"])
        if len(seen) == 4:
            return LLMResponse("done", [], "end")
        targets = ["profile.json", str(hist.relative_to(hist.parents[2])), rel_old]
        if len(seen) < 3:
            return LLMResponse(
                "",
                [ToolCall(f"e{len(seen)}", "write_file", json.dumps({"path": targets[len(seen)], "content": "pwned"}))],
                "tool_use",
            )
        return LLMResponse("", [ToolCall("m", "bash", json.dumps({"command": "mkdir -p briefs/x"}))], "tool_use")

    service.run_for_user(uid, llm=FakeProvider(evil), today="2026-10-12")
    assert all(o.startswith("ERROR") and "read-only" in o for o in seen)
    assert Path(b1.path).read_text() == before_run and hist.read_text() == before_hist
    assert "pwned" not in (hist.parents[2] / "profile.json").read_text()


def test_invalid_send_time_rejected_and_startup_survives(tmp_path, monkeypatch):
    """review fix #5: 25:00 must be rejected before commit; a bad stored value must not crash startup."""
    from app import config, db, main, scheduler

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with TestClient(main.app) as c:
        c.post("/users", data={"name": "A", "email": "a@x.com"})
        r = c.post("/users/1/prefs", data={"name": "A", "send_time": "25:00"})
        assert r.status_code == 422
        with db.SessionLocal() as s:
            assert s.get(db.User, 1).send_time != "25:00"
            s.get(db.User, 1).send_time = "99:99"  # 模拟历史坏数据
            s.commit()
    scheduler.start()  # 不应抛异常


def test_fabricated_link_is_fed_back_then_rejected(tmp_path, monkeypatch):
    """design review P1-1: links must come from this run's tool evidence; errors go back to the model."""
    from app.llm.providers import FakeProvider

    service, uid, sent = _setup(tmp_path, monkeypatch)
    b = service.run_for_user(uid, llm=FakeProvider(_always_writer(D("ev_999"))), today="2026-10-11")
    assert b.status == "failed" and b.stopped_reason == "validation_failed"
    trace = json.loads(b.trace_json)
    assert sum(1 for t in trace if t["type"] == "validation") == 3  # 1 次 + 2 次修订
    assert sent == []


def _always_writer(text):
    import re

    from app.llm.base import LLMResponse, ToolCall

    def model(msgs):
        if msgs[-1]["role"] == "tool":
            return LLMResponse("done", [], "end")
        out = re.search(r"draft to (\S+)", msgs[0]["content"]).group(1)
        return LLMResponse("", [ToolCall("w", "write_file", json.dumps({"path": out, "content": text}))], "tool_use")

    return model


def test_zero_items_is_no_news_and_not_emailed(tmp_path, monkeypatch):
    from app.llm.providers import FakeProvider

    service, uid, sent = _setup(tmp_path, monkeypatch)
    b = service.run_for_user(uid, llm=FakeProvider(_writer(D())), today="2026-10-11")
    assert b.status == "no_news" and sent == []


def test_run_is_recorded_before_execution_and_dedups(tmp_path, monkeypatch):
    """design review P1-3: a run row exists from acceptance; same user can't start two; failures get a terminal state."""
    from app.db import Run, SessionLocal
    from app.llm.providers import FakeProvider

    service, uid, _ = _setup(tmp_path, monkeypatch)
    r1 = service.submit_run(uid, llm=FakeProvider(_writer(D("ev_001"))), today="2026-10-11")
    r2 = service.submit_run(uid, llm=FakeProvider(_writer(D())), today="2026-10-11")
    service.wait_all()
    with SessionLocal() as s:
        runs = s.query(Run).all()
    assert r1.run_id == r2.run_id or len(runs) == 2  # 第二次要么并入活动 run，要么在第一次结束后才受理
    assert runs[0].status == "succeeded" and runs[0].brief_id


def test_run_init_failure_is_visible(tmp_path, monkeypatch):
    from app.db import Run, SessionLocal

    service, uid, _ = _setup(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("no API key")

    monkeypatch.setattr(service, "get_provider", boom)
    service.submit_run(uid)
    service.wait_all()
    with SessionLocal() as s:
        r = s.query(Run).one()
    assert r.status == "failed" and "no API key" in r.error


def test_idempotency_key_and_interrupted_recovery(tmp_path, monkeypatch):
    from app.db import Run, SessionLocal

    service, uid, _ = _setup(tmp_path, monkeypatch)
    with SessionLocal() as s:
        s.add(Run(run_id="old-1", user_id=uid, trigger="manual", status="running"))
        s.commit()
    assert service.recover_interrupted() == 1
    monkeypatch.setattr(service, "get_provider", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    a = service.submit_run(uid, trigger="scheduled", idempotency_key="sched:1:2026-10-11")
    service.wait_all()
    b = service.submit_run(uid, trigger="scheduled", idempotency_key="sched:1:2026-10-11")
    assert a.run_id == b.run_id
    with SessionLocal() as s:
        assert s.query(Run).filter_by(run_id="old-1").one().status == "interrupted"


def test_queue_capacity_is_explicit(tmp_path, monkeypatch):
    from app import config

    service, uid, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MAX_QUEUED_RUNS", 0)
    r = service.submit_run(uid)
    assert r.status == "failed" and r.error.startswith("capacity")


def test_run_now_endpoint_and_status(tmp_path, monkeypatch):
    from app import config, main, service

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    from app.tools import news_tools

    monkeypatch.setattr(news_tools, "web_search", lambda query, **k: "- [hn] A | https://ex.com/a | ")
    with TestClient(main.app) as c:
        c.post("/users", data={"name": "E", "email": "e@x.com"})
        r = c.post("/users/1/run", follow_redirects=False)
        run_id = r.headers["location"].split("run=")[1]
        service.wait_all()
        st = c.get(f"/runs/{run_id}").json()
        assert st["status"] == "succeeded" and st["brief_id"]
        assert c.post("/users/999/run").status_code == 404


def test_activity_snapshot_tracks_completion_and_only_current_user(tmp_path, monkeypatch):
    """页面轮询能看到新运行和终态，且快照仅包含该用户的记录。"""
    from app import config, main
    from app.db import Brief, Run, SessionLocal, User

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with TestClient(main.app) as c:
        c.post("/users", data={"name": "E", "email": "e@x.com"})
        with SessionLocal() as s:
            other = User(name="Other", email="other@x.com")
            s.add(other)
            s.flush()
            s.add(Run(run_id="other-run", user_id=other.id, trigger="manual", status="failed", error="private"))
            s.add(Run(run_id="watched", user_id=1, trigger="scheduled", status="running"))
            s.add(Brief(user_id=other.id, date="2026-10-13", path="private-path", status="ok"))
            s.commit()
        response = c.get("/users/1/activity")
        assert response.headers["cache-control"] == "no-store"
        assert [r["run_id"] for r in response.json()["runs"]] == ["watched"]
        assert response.json()["runs"][0]["status"] == "running"
        assert response.json()["briefs"] == []
        with SessionLocal() as s:
            brief = Brief(
                user_id=1,
                date="2026-10-13",
                path="local-path",
                status="ok",
                stopped_reason="model_finished",
                steps=3,
                tokens_in=20,
                tokens_out=10,
            )
            s.add(brief)
            s.flush()
            run = s.query(Run).filter_by(run_id="watched").one()
            run.status, run.brief_id = "succeeded", brief.id
            s.commit()
            bid = brief.id
        finished = c.get("/users/1/activity").json()
        assert finished["runs"][0]["status"] == "succeeded"
        assert finished["runs"][0]["brief_id"] == bid
        assert finished["briefs"][0]["id"] == bid
        assert finished["briefs"][0]["tokens_in"] == 20
        assert "path" not in finished["briefs"][0] and "trace_json" not in finished["briefs"][0]
        assert c.get("/users/999/activity").status_code == 404


def test_run_now_supports_json_without_navigation(tmp_path, monkeypatch):
    """JS 提交使用 JSON，普通表单继续跳转；异常结果也能直接显示。"""
    from app import config, main, service

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "get_provider", lambda: (_ for _ in ()).throw(RuntimeError("test failure")))
    with TestClient(main.app) as c:
        c.post("/users", data={"name": "E", "email": "e@x.com"})
        response = c.post("/users/1/run", headers={"Accept": "application/json"})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert "location" not in response.headers
        run_id = response.json()["run_id"]
        service.wait_all()
        run = c.get("/users/1/activity").json()["runs"][0]
        assert run["run_id"] == run_id and run["status"] == "failed"
        assert "test failure" in run["error"]
        page = c.get("/users/1")
        assert 'id="run-status"' in page.text and 'id="generate-form"' in page.text
        assert c.get("/static/user_status.js").status_code == 200
        assert c.post("/users/999/run", headers={"Accept": "application/json"}).status_code == 404


def test_init_db_adds_missing_columns_to_old_db(tmp_path, monkeypatch):
    import sqlite3

    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    con = sqlite3.connect(tmp_path / "app.db")
    con.executescript(
        "CREATE TABLE briefs (id INTEGER PRIMARY KEY, user_id INTEGER, date VARCHAR(10), path VARCHAR(300),"
        " status VARCHAR(20)); CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(80), email VARCHAR(200));"
    )
    con.close()
    db.init_db()
    cols = {r[1] for r in sqlite3.connect(tmp_path / "app.db").execute("PRAGMA table_info(briefs)")}
    assert "run_id" in cols


def test_business_date_and_catch_up_use_user_timezone(tmp_path, monkeypatch):
    """design review P1-4: date follows the user's IANA zone; missed runs are caught up once inside the window."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app import config, scheduler
    from app.db import SessionLocal, User

    service, uid, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "CATCHUP_HOURS", 6)
    with SessionLocal() as s:
        u = s.get(User, uid)
        u.timezone, u.send_time = "Australia/Sydney", "08:00"
        s.commit()
        u = s.get(User, uid)
    utc = ZoneInfo("UTC")
    # 2026-10-12 22:30 UTC = 2026-10-13 09:30 悉尼（UTC+11）
    now = datetime(2026, 10, 12, 22, 30, tzinfo=utc)
    assert scheduler.business_date(u, now) == "2026-10-13"
    assert scheduler.due_users(datetime(2026, 10, 12, 20, 0, tzinfo=utc)) == []  # 07:00 本地，未到
    assert scheduler.due_users(now) == [(uid, "2026-10-13")]
    assert scheduler.due_users(datetime(2026, 10, 13, 5, 0, tzinfo=utc)) == []  # 16:00 本地，超出 6h 补跑窗口
    monkeypatch.setattr(service, "get_provider", lambda: (_ for _ in ()).throw(RuntimeError("no key")))
    assert len(scheduler.catch_up(now)) == 1
    service.wait_all()
    assert scheduler.due_users(now) == []  # 同日只一个逻辑 run（失败也不会被扫描反复重跑）


def test_dst_gap_start_time_still_due(tmp_path, monkeypatch):
    """Sydney 2026-10-04 02:30 doesn't exist (clocks jump 02:00 -> 03:00); the catch-up scan still fires that day."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app import config, scheduler
    from app.db import SessionLocal, User

    service, uid, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "CATCHUP_HOURS", 6)
    with SessionLocal() as s:
        u = s.get(User, uid)
        u.timezone, u.send_time = "Australia/Sydney", "02:30"
        s.commit()
    assert scheduler.due_users(datetime(2026, 10, 3, 15, 50, tzinfo=ZoneInfo("UTC"))) == []  # 01:50 AEST
    now = datetime(2026, 10, 3, 16, 10, tzinfo=ZoneInfo("UTC"))  # 03:10 AEDT
    assert scheduler.due_users(now) == [(uid, "2026-10-04")]


def test_invalid_timezone_rejected(tmp_path, monkeypatch):
    from app import config, main

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with TestClient(main.app) as c:
        c.post("/users", data={"name": "A", "email": "a@x.com"})
        assert c.post("/users/1/prefs", data={"name": "A", "timezone": "Mars/Base"}).status_code == 422
        assert c.post("/users/1/prefs", data={"name": "A", "timezone": "Australia/Sydney"}).status_code == 200


def test_delivery_is_separate_from_generation(tmp_path, monkeypatch):
    """design review P1-5: email outcome has its own record; retry resends the same body without rerunning the model."""
    from pathlib import Path

    from app.db import Delivery, SessionLocal
    from app.llm.providers import FakeProvider

    service, uid, sent = _setup(tmp_path, monkeypatch)
    calls = []

    def flaky(to, subject, body, **k):
        calls.append(body)
        if len(calls) == 1:
            raise OSError("smtp down")

    monkeypatch.setattr(service, "send_email", flaky)
    b = service.run_for_user(uid, llm=FakeProvider(_writer(D("ev_001"))), today="2026-10-11")
    assert b.status == "ok" and b.stopped_reason == "model_finished"  # 不再把邮件错误拼进生成结果
    with SessionLocal() as s:
        d = s.query(Delivery).one()
    assert d.status == "failed" and "smtp down" in d.last_error
    d = service.deliver(d.id)
    assert d.status == "sent" and d.attempts == 2 and calls[0] == calls[1] == Path(b.path).read_text()
    service.deliver(d.id)
    assert len(calls) == 2  # sent 后不再重复发送


def test_email_on_but_smtp_off_is_visible(tmp_path, monkeypatch):
    from app import config
    from app.db import Delivery, SessionLocal
    from app.llm.providers import FakeProvider

    service, uid, sent = _setup(tmp_path, monkeypatch)
    monkeypatch.setitem(config.SMTP, "enabled", False)
    service.run_for_user(uid, llm=FakeProvider(_writer(D("ev_001"))), today="2026-10-11")
    with SessionLocal() as s:
        assert s.query(Delivery).one().status == "not_configured"
    assert sent == []


def test_stuck_sending_marked_unknown(tmp_path, monkeypatch):
    from app.db import Brief, Delivery, SessionLocal

    service, uid, _ = _setup(tmp_path, monkeypatch)
    with SessionLocal() as s:
        b = Brief(user_id=uid, date="2026-10-11", path="x", status="ok")
        s.add(b)
        s.flush()
        s.add(Delivery(brief_id=b.id, recipient="e@x.com", status="sending"))
        s.commit()
    assert service.mark_stuck_deliveries() == 1
    with SessionLocal() as s:
        assert s.query(Delivery).one().status == "unknown"


def test_service_renders_brief_and_keeps_draft_and_evidence(tmp_path, monkeypatch):
    """structured contract: model writes draft.json only; Service renders Markdown and stores draft + evidence."""
    from pathlib import Path

    from app.llm.providers import FakeProvider

    service, uid, _ = _setup(tmp_path, monkeypatch)
    b = service.run_for_user(uid, llm=FakeProvider(_writer(D("ev_002", "ev_001"))), today="2026-10-11")
    assert b.status == "ok"
    md = Path(b.path).read_text()
    assert md.startswith("# Edison 的 AI 新闻简报 · 2026-10-11") and "[B](https://ex.com/b)" in md and "推荐理由" in md
    ev = json.loads(Path(b.path).with_suffix(".evidence.json").read_text())
    assert [e["evidence_id"] for e in ev] == ["ev_001", "ev_002"] and ev[0]["url"] == "https://ex.com/a"
    assert json.loads(Path(b.path).with_suffix(".draft.json").read_text())["items"][0]["evidence_ids"] == ["ev_002"]


def test_markdown_draft_is_rejected(tmp_path, monkeypatch):
    from app.llm.providers import FakeProvider

    service, uid, _ = _setup(tmp_path, monkeypatch)
    b = service.run_for_user(uid, llm=FakeProvider(_always_writer("# brief\n- https://ex.com/a")), today="2026-10-11")
    assert b.status == "failed" and "not valid JSON" in b.trace_json


def test_revision_loop_fixes_draft(tmp_path, monkeypatch):
    import re

    from app.llm.base import LLMResponse, ToolCall
    from app.llm.providers import FakeProvider

    service, uid, _ = _setup(tmp_path, monkeypatch)
    state = {"writes": 0}

    def model(msgs):
        last = msgs[-1]
        out = re.search(r"draft to (\S+)", msgs[0]["content"]).group(1)
        if not any(m["role"] == "tool" for m in msgs):
            return LLMResponse("", [ToolCall("s", "web_search", json.dumps({"query": "ai"}))], "tool_use")
        if last["role"] == "tool" and state["writes"]:
            return LLMResponse("done", [], "end")
        state["writes"] += 1
        content = D("ev_404") if state["writes"] == 1 else D("ev_001")
        return LLMResponse(
            "",
            [ToolCall(f"w{state['writes']}", "write_file", json.dumps({"path": out, "content": content}))],
            "tool_use",
        )

    b = service.run_for_user(uid, llm=FakeProvider(model), today="2026-10-11")
    assert b.status == "ok" and state["writes"] == 2
    assert "unknown evidence ids" in b.trace_json


def test_zero_search_empty_draft_is_marked_failed(tmp_path, monkeypatch):
    """Zero searches before writing an empty draft must be marked failed, not no_news."""
    from app.llm.providers import FakeProvider

    service, uid, sent = _setup(tmp_path, monkeypatch)
    b = service.run_for_user(uid, llm=FakeProvider(_always_writer(D())), today="2026-10-11")
    assert b.status == "failed" and b.stopped_reason == "insufficient_search"
    assert sent == []


def test_all_sources_failed_returns_false_and_cannot_be_no_news(tmp_path, monkeypatch):
    """When all search sources fail, tool returns ok=False and empty draft cannot be no_news."""
    from app.llm.providers import FakeProvider
    from app.tools import news_tools

    service, uid, sent = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(news_tools, "web_search", lambda query, **k: "errors: hn: HTTP 500; rss: timeout; arxiv: down")
    b = service.run_for_user(uid, llm=FakeProvider(_writer(D())), today="2026-10-11")
    assert b.status == "failed" and b.stopped_reason == "insufficient_search"
    assert "ERROR: all search sources failed" in b.trace_json
    assert sent == []


def test_successful_search_with_no_hits_allows_valid_no_news(tmp_path, monkeypatch):
    """When search succeeds without errors but returns no items, empty draft is legitimately no_news."""
    from app.llm.providers import FakeProvider
    from app.tools import news_tools

    service, uid, sent = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(news_tools, "web_search", lambda query, **k: "no results")
    b = service.run_for_user(uid, llm=FakeProvider(_writer(D())), today="2026-10-11")
    assert b.status == "no_news" and sent == []


def test_concurrent_deliver_claims_atomically(tmp_path, monkeypatch):
    """Concurrent delivery claims must be atomic: only one thread sends email, preventing duplicates."""
    import threading
    import time

    from app.db import Brief, Delivery, SessionLocal

    service, uid, _ = _setup(tmp_path, monkeypatch)
    with SessionLocal() as s:
        b = Brief(user_id=uid, date="2026-10-11", path=str(tmp_path / "b.md"), status="ok")
        s.add(b)
        s.flush()
        d = Delivery(brief_id=b.id, recipient="e@x.com", status="pending")
        s.add(d)
        s.commit()
        did = d.id
    (tmp_path / "b.md").write_text("news body")

    sent_calls = []

    def slow_send(to, subject, body, **k):
        time.sleep(0.05)
        sent_calls.append(body)

    monkeypatch.setattr(service, "send_email", slow_send)

    t1 = threading.Thread(target=lambda: service.deliver(did))
    t2 = threading.Thread(target=lambda: service.deliver(did))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(sent_calls) == 1
    with SessionLocal() as s:
        res_d = s.get(Delivery, did)
        assert res_d.status == "sent" and res_d.attempts == 1


def test_run_with_form_data_auto_saves_preferences(tmp_path, monkeypatch):
    """When /users/{uid}/run is submitted with form data, preferences are automatically updated first."""
    from fastapi.testclient import TestClient

    from app import config, db, main, service

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    db.init_db()
    with db.SessionLocal() as s:
        u = db.User(name="Eddie", email="eddie@example.com", topics=["金融"], keywords=["股票"])
        s.add(u)
        s.commit()
        uid = u.id

    with TestClient(main.app) as c:
        # Submit run with updated topics and keywords form
        r = c.post(
            f"/users/{uid}/run",
            data={"name": "Eddie", "topics": "LLM, AI", "keywords": "数据中心", "language": "zh"},
            follow_redirects=False,
        )
        assert r.status_code == 303

    service.wait_all()

    with db.SessionLocal() as s:
        updated_u = s.get(db.User, uid)
        assert updated_u.topics == ["LLM", "AI"]
        assert updated_u.keywords == ["数据中心"]


def test_max_steps_with_valid_draft_publishes_with_warning_annotation(tmp_path, monkeypatch):
    """If agent reaches max_steps but wrote a valid draft, service publishes it with a warning annotation."""
    import re
    from pathlib import Path

    from app import config
    from app.llm.base import LLMResponse, ToolCall
    from app.llm.providers import FakeProvider

    service, uid, sent = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MAX_STEPS", 2)

    def step2_model(msgs):
        n = sum(1 for m in msgs if m["role"] == "tool")
        if n == 0:
            return LLMResponse("", [ToolCall("s", "web_search", json.dumps({"query": "ai"}))], "tool_use")
        # Step 2: write draft and do not yield an empty tool_call turn (exhausts max_steps=2)
        out = re.search(r"draft to (\S+)", msgs[0]["content"]).group(1)
        return LLMResponse(
            "", [ToolCall("w", "write_file", json.dumps({"path": out, "content": D("ev_001")}))], "tool_use"
        )

    b = service.run_for_user(uid, llm=FakeProvider(step2_model), today="2026-10-11")
    assert b.status == "ok"
    assert b.stopped_reason == "max_steps"
    assert b.steps == 2
    body = Path(b.path).read_text(encoding="utf-8")
    assert "运行提示" in body
    assert "最大步数上限" in body
    assert "AGENT_MAX_STEPS" in body


def test_max_steps_without_valid_draft_still_fails(tmp_path, monkeypatch):
    """If agent reaches max_steps without writing a valid draft, status remains failed."""
    from app import config
    from app.llm.base import LLMResponse, ToolCall
    from app.llm.providers import FakeProvider

    service, uid, sent = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MAX_STEPS", 2)

    def never_write_model(msgs):
        return LLMResponse("", [ToolCall("s", "web_search", json.dumps({"query": "ai"}))], "tool_use")

    b = service.run_for_user(uid, llm=FakeProvider(never_write_model), today="2026-10-11")
    assert b.status == "failed"
    assert b.stopped_reason == "max_steps"


def test_fetch_url_extracts_pdf_text(tmp_path, monkeypatch):
    import io

    from pypdf import PdfWriter

    from app.tools import news_tools
    from app.tools.registry import ToolRegistry
    from app.tools.sandbox import Sandbox

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    # Blank/scanned PDF returns friendly fallback
    assert "no extractable text" in news_tools._extract_pdf_text(pdf_bytes)

    # PDF fetch returns extracted text
    monkeypatch.setattr(
        news_tools, "_get_limited", lambda url, **k: (True, "Deep learning breakthrough details in PDF")
    )
    reg = ToolRegistry(Sandbox(tmp_path))
    ok, out = reg.call("fetch_url", {"url": "https://example.com/paper.pdf"})
    assert ok
    assert "Deep learning breakthrough" in out


def test_blank_name_or_email_is_rejected(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import config, db, main

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
    with TestClient(main.app) as c:
        assert c.post("/users", data={"name": "   ", "email": "a@x.com"}).status_code == 422
        assert c.post("/users", data={"name": "A", "email": "  "}).status_code == 422


def test_login_with_mismatched_nickname_is_rejected(tmp_path, monkeypatch):
    """If an email already exists, submitting with a different nickname is rejected with error notice."""
    from fastapi.testclient import TestClient

    from app import config, db, main

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
    with TestClient(main.app) as c:
        # Create user Alice
        r1 = c.post("/users", data={"name": "Alice", "email": "alice@example.com"}, follow_redirects=False)
        assert r1.status_code == 303
        uid = int(r1.headers["location"].split("/")[-1])

        # Attempt to login using the same email but different nickname Bob
        r2 = c.post("/users", data={"name": "Bob", "email": "alice@example.com"}, follow_redirects=False)
        assert r2.status_code == 303
        assert "error=name_mismatch" in r2.headers["location"]

        # Ensure index page renders the error message
        page = c.get(r2.headers["location"])
        assert "输入的昵称与已绑定的昵称不符" in page.text

        # Verify DB still has Alice and was not modified
        with db.SessionLocal() as s:
            u = s.get(db.User, uid)
            assert u.name == "Alice"

        # Attempt to login using the correct nickname Alice
        r3 = c.post("/users", data={"name": "Alice", "email": "alice@example.com"}, follow_redirects=False)
        assert r3.status_code == 303
        assert r3.headers["location"] == f"/users/{uid}"
