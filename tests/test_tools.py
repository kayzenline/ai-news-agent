import os
import pytest
from app.tools.sandbox import Sandbox, SandboxError
from app.tools import fs_tools as T
from app.tools.registry import ToolRegistry


@pytest.fixture
def sb(tmp_path):
    s = Sandbox(tmp_path / "ws")
    (s.root / "briefs").mkdir()
    (s.root / "briefs" / "2026-10-10.md").write_text("- **OpenAI ships X** https://ex.com/a\n")
    return s


def test_basic_roundtrip(sb):
    assert "wrote" in T.write_file(sb, "notes/a.txt", "hello")
    assert T.read_file(sb, "notes/a.txt") == "hello"
    assert "[D] notes" in T.list_dir(sb, ".")
    assert "2026-10-10.md:1" in T.search_content(sb, "https://ex.com/a", "briefs")
    assert T.search_content(sb, "nothing-here", "briefs") == "no matches"


@pytest.mark.parametrize("p", ["../outside.txt", "a/../../x", "/etc/passwd", "briefs/../../.."])
def test_path_escape_blocked(sb, p):
    with pytest.raises((SandboxError, FileNotFoundError)):
        T.read_file(sb, p)
    if p != "/etc/passwd":
        with pytest.raises(SandboxError):
            T.write_file(sb, p, "x")


def test_absolute_path_is_rebased(sb):
    T.write_file(sb, "/inside.txt", "y")
    assert (sb.root / "inside.txt").exists()


def test_symlink_escape_blocked(sb, tmp_path):
    (tmp_path / "secret.txt").write_text("s")
    os.symlink(tmp_path / "secret.txt", sb.root / "link.txt")
    with pytest.raises(SandboxError):
        T.read_file(sb, "link.txt")


@pytest.mark.parametrize("cmd", ["rm -rf /", "curl http://x", "python -c 1", "ls; rm x", "cat a | sh",
                                 "echo hi > f", "cat /etc/passwd", "cat ../../etc/passwd", "ls ~",
                                 "find . -exec rm {} ;", "echo $(id)"])
def test_bash_blocked(sb, cmd):
    with pytest.raises(SandboxError):
        T.bash(sb, cmd)


def test_bash_allowed_runs_in_workspace(sb):
    out = T.bash(sb, "ls briefs")
    assert "[exit 0]" in out and "2026-10-10.md" in out


def test_registry_feeds_errors_back(sb):
    reg = ToolRegistry(sb)
    ok, out = reg.call("read_file", '{"path": "../x"}')
    assert not ok and out.startswith("ERROR")
    ok, out = reg.call("nope", "{}")
    assert not ok and "unknown tool" in out
    ok, out = reg.call("read_file", "{bad json")
    assert not ok and "invalid JSON" in out
    ok, out = reg.call("read_file", '{"wrong": 1}')
    assert not ok and "bad arguments" in out


# --- review fix #1: options could smuggle output paths / write actions ---
@pytest.mark.parametrize("cmd", [
    "sort -o../outside.txt input.txt", "sort -o ../outside.txt input.txt",
    "uniq input.txt ../outside.txt", "grep -f../outside.txt x input.txt",
    "grep --file=../outside.txt input.txt", "find . -fprint ../outside.txt",
    "find . -delete", "find . -exec cat {} +", "ls --color=always", "cat -Z input.txt",
    "head -n../x input.txt", "date -s 2020-01-01", "date -f input.txt",
    "find ../ -name x", "find . -newer ../outside.txt",
])
def test_bash_option_smuggling_blocked(sb, cmd):
    (sb.root / "input.txt").write_text("b\na\n")
    with pytest.raises(SandboxError):
        T.bash(sb, cmd)
    assert not (sb.root.parent / "outside.txt").exists()


@pytest.mark.parametrize("cmd", ["head -n 1 input.txt", "head -5 input.txt", "grep -in a input.txt",
                                 "grep -e b input.txt", "find . -name '*.txt' -type f", "wc -l input.txt",
                                 "date +%F", "ls -la briefs", "tail -n1 input.txt", "mkdir -p notes/x"])
def test_bash_safe_options_allowed(sb, cmd):
    (sb.root / "input.txt").write_text("b\na\n")
    assert T.bash(sb, cmd).startswith("[exit 0]")


# --- review fix #6: basename symlinks / traversed symlinks escaping the workspace ---
def test_symlink_basename_and_search_blocked(sb):
    secret = sb.root.parent / "secret.txt"
    secret.write_text("TOPSECRET\n")
    (sb.root / "link.txt").symlink_to(secret)
    with pytest.raises(SandboxError):
        T.bash(sb, "cat link.txt")
    assert "TOPSECRET" not in T.search_content(sb, "TOPSECRET", ".")
    with pytest.raises(SandboxError):
        T.bash(sb, "grep -R TOPSECRET .")


# --- review fix #8: days window must apply to every source ---
RSS_XML = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>AI old story</title><link>https://old.example/a</link><pubDate>Mon, 01 Jan 2001 00:00:00 GMT</pubDate></item>
<item><title>AI fresh story</title><link>https://new.example/a</link><pubDate>{now}</pubDate></item>
<item><title>AI undated story</title><link>https://nodate.example/a</link></item>
</channel></rss>"""
ATOM_XML = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>arxiv</title>
<entry><title>AI old paper</title><id>x</id><link href="https://arxiv.org/abs/old"/><published>2001-01-01T00:00:00Z</published><summary>s</summary></entry>
<entry><title>AI new paper</title><id>y</id><link href="https://arxiv.org/abs/new"/><published>{iso}</published><summary>s</summary></entry>
</feed>"""


def test_days_window_applies_to_rss_arxiv_tavily(monkeypatch):
    from datetime import datetime, timezone
    from email.utils import format_datetime
    from app.tools import news_tools as N
    now = datetime.now(timezone.utc)
    sent = {}

    class R:
        def __init__(self, text, js=None):
            self.text, self.content, self._js = text, text.encode(), js
        def raise_for_status(self): pass
        def json(self): return self._js

    def fake_get(url, **k):
        if "arxiv" in url:
            return R(ATOM_XML.format(iso=now.strftime("%Y-%m-%dT%H:%M:%SZ")))
        return R(RSS_XML.format(now=format_datetime(now)))

    def fake_post(url, json=None, **k):
        sent.update(json)
        return R("", {"results": [
            {"title": "AI old", "url": "https://old.t/a", "published_date": "Mon, 01 Jan 2001 00:00:00 GMT"},
            {"title": "AI new", "url": "https://new.t/a", "published_date": format_datetime(now)}]})

    monkeypatch.setattr(N.httpx, "get", fake_get)
    monkeypatch.setattr(N.httpx, "post", fake_post)
    monkeypatch.setattr(N, "RSS_FEEDS", {"feed": "https://feed.example/rss"})
    rss = N.web_search("AI", sources=["rss"], days=1)
    assert "old.example" not in rss and "new.example" in rss and "nodate.example/a | unknown" in rss
    arx = N.web_search("AI", sources=["arxiv"], days=1)
    assert "abs/old" not in arx and "abs/new" in arx
    tav = N.web_search("AI", sources=["tavily"], days=5)
    assert sent["days"] == 5 and "old.t" not in tav and "new.t" in tav


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost:80/", "http://169.254.169.254/latest/meta-data",
                                 "http://10.0.0.1/", "http://[::1]/", "http://user:pw@example.com/", "http://example.com:8080/",
                                 "file:///etc/passwd", "http://0.0.0.0/"])
def test_fetch_url_blocks_ssrf_targets(url):
    from app.tools.news_tools import fetch_url
    with pytest.raises(ValueError):
        fetch_url(url)


def test_fetch_url_revalidates_each_redirect_and_limits_bytes(monkeypatch):
    import httpx
    from app.tools import news_tools
    monkeypatch.setattr(news_tools, "check_public_url",
                        lambda u: (_ for _ in ()).throw(ValueError("blocked")) if "internal" in u else None)
    def handler(req):
        if req.url.host == "pub.example":
            return httpx.Response(302, headers={"location": "http://internal.example/secret"})
        return httpx.Response(200, stream=httpx.ByteStream(b"secret"))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "stream", lambda m, u, **k: client.stream(m, u, follow_redirects=False))
    with pytest.raises(ValueError, match="blocked"):
        news_tools.fetch_url("http://pub.example/a")
    big = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=httpx.ByteStream(b"x" * 5_000_000))))
    monkeypatch.setattr(httpx, "stream", lambda m, u, **k: big.stream(m, u, follow_redirects=False))
    monkeypatch.setattr(news_tools, "MAX_FETCH_BYTES", 1000)
    assert len(news_tools.fetch_url("http://ok.example/", max_chars=10**7)) == 1000


def _mock_stream(monkeypatch, handler):
    import httpx
    from app.tools import news_tools
    monkeypatch.setattr(news_tools, "check_public_url", lambda u: None)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "stream", lambda m, u, **k: client.stream(m, u, headers=k.get("headers")))
    return news_tools


def test_fetch_url_gzip_body_decoded_once(monkeypatch):
    """regression: decoded bytes were re-wrapped with Content-Encoding: gzip and decompressed twice."""
    import gzip, httpx
    seen = {}
    def handler(req):
        seen["ae"] = req.headers.get("accept-encoding")
        return httpx.Response(200, headers={"content-encoding": "gzip", "content-type": "text/html; charset=utf-8"},
                              stream=httpx.ByteStream(gzip.compress("<p>Agents in your texts — 智能体</p>".encode())))
    nt = _mock_stream(monkeypatch, handler)
    assert nt.fetch_url("https://techcrunch.com/x") == "Agents in your texts — 智能体"
    assert "br" not in seen["ae"] and "zstd" not in seen["ae"]


def test_fetch_url_mislabelled_gzip_falls_back(monkeypatch):
    import httpx
    nt = _mock_stream(monkeypatch, lambda req: httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"<p>plain body</p>")))
    assert nt.fetch_url("https://ex.com/") == "plain body"


def test_fetch_url_gzip_bomb_capped(monkeypatch):
    import gzip, httpx
    nt = _mock_stream(monkeypatch, lambda req: httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(gzip.compress(b"a" * 5_000_000))))
    monkeypatch.setattr(nt, "MAX_FETCH_BYTES", 1000)
    assert len(nt.fetch_url("https://ex.com/", max_chars=10**7)) == 1000


RSS_MIX = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Company said profits rose</title><link>https://ex.com/said</link><description>maintain</description></item>
<item><title>New AI agent framework</title><link>https://ex.com/agent</link><description>tools</description></item>
<item><title>Robotics startup raises</title><link>https://ex.com/robot</link><description>AI robots</description></item>
<item><title>大模型推理加速新方法</title><link>https://ex.com/zh</link><description>推理</description></item>
</channel></rss>"""


def _rss_mock(monkeypatch):
    import httpx
    from app.tools import news_tools as N
    monkeypatch.setattr(N, "RSS_FEEDS", {"feed": "https://feed.example/rss"})
    monkeypatch.setattr(N.httpx, "get", lambda *a, **k: httpx.Response(200, content=RSS_MIX.encode(),
                                                                       request=httpx.Request("GET", a[0])))
    return N


def test_rss_filters_and_ranks_by_query(monkeypatch):
    N = _rss_mock(monkeypatch)
    out = N.web_search("AI agent", sources=["rss"], days=3650)
    lines = [l for l in out.splitlines() if l.startswith("- [")]
    assert "ex.com/agent" in lines[0] and "ex.com/robot" in out and "ex.com/said" not in out
    assert "ex.com/zh" in N.web_search("大模型", sources=["rss"], days=3650)
    assert "ex.com/zh" in N.web_search("模型推理", sources=["rss"], days=3650)  # 中文二元组


def test_rss_no_match_is_reported_not_silent(monkeypatch):
    N = _rss_mock(monkeypatch)
    out = N.web_search("quantum chemistry", sources=["rss"], days=3650)
    assert "notes: rss: no items matched" in out
    items = [l for l in out.splitlines() if l.startswith("- [")]
    assert len(items) <= 3 and all("unmatched" in l for l in items)
