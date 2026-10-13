from app.evidence import EvidenceStore, canonical_url
from app.tools.registry import ToolRegistry
from app.tools.sandbox import Sandbox


def test_search_results_get_sequential_ids(tmp_path):
    store = EvidenceStore()
    reg = ToolRegistry(Sandbox(tmp_path), evidence=store,
                       extra={"web_search": lambda query, **k: "- [hn] A | https://ex.com/a | 2026-10-11\n- [rss] B | https://ex.com/b | unknown | sum\nerrors: arxiv: down",
                              "fetch_url": lambda url, **k: "page text"})
    ok, out = reg.call("web_search", '{"query":"x"}')
    assert ok and out.splitlines()[0].startswith("- ev_001 [hn] A") and out.splitlines()[1].startswith("- ev_002 [rss]")
    assert "errors: arxiv" in out
    assert store.get("ev_001").url == "https://ex.com/a" and store.get("ev_001").published_at == "2026-10-11"
    assert store.get("ev_002").excerpt == "sum"
    ok, out = reg.call("fetch_url", '{"url":"https://ex.com/a"}')
    assert out.startswith("evidence_id: ev_003") and store.get("ev_003").tool == "fetch_url"


def test_failed_tool_call_assigns_no_evidence(tmp_path):
    store = EvidenceStore()
    def boom(**k): raise RuntimeError("x")
    reg = ToolRegistry(Sandbox(tmp_path), evidence=store, extra={"fetch_url": boom})
    ok, _ = reg.call("fetch_url", '{"url":"https://ex.com"}')
    assert not ok and store.items == {}


def test_unmatched_search_lines_get_no_evidence_id():
    store = EvidenceStore()
    out = store.annotate_search(
        "- [techcrunch_ai] Meta model | https://ex.com/m | 2026-10-12 | unmatched: recent item, does not contain the query terms\n"
        "- [hn] Real hit | https://ex.com/real | 2026-10-12")
    assert store.items.keys() == {"ev_001"}
    assert "ev_" not in out.splitlines()[0] and out.splitlines()[1].startswith("- ev_001 [hn]")


def test_canonical_url():
    assert canonical_url("HTTPS://Ex.COM:443/Path?utm_source=x&id=7#frag") == "https://ex.com/Path?id=7"
    assert canonical_url("http://ex.com:8080/a") == "http://ex.com:8080/a"


def test_fetch_inherits_date_and_source_from_search():
    from app.brief import render_markdown
    s = EvidenceStore()
    s.annotate_search("- [techcrunch_ai] Agents in texts | https://techcrunch.com/2026/10/10/agents/ | 2026-10-10 09:00")
    s.annotate_fetch("https://TechCrunch.com/2026/10/10/agents#top", "body")
    e = s.get("ev_002")
    assert e.published_at == "2026-10-10 09:00" and e.source == "techcrunch_ai" and e.title == "Agents in texts"
    md = render_markdown({"items": [{"title": "t", "topic": "AI", "summary": "s", "why_it_matters": "w",
                                     "evidence_ids": ["ev_002"]}]}, s, "E", "2026-10-11")
    assert "2026-10-10 09:00" in md and "日期未知" not in md
    s.annotate_fetch("https://other.com/x", "body")
    assert s.get("ev_003").published_at == "unknown"
