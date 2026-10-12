import json
from app.brief import history_urls, render_markdown, validate_draft
from app.evidence import EvidenceStore


def store():
    s = EvidenceStore()
    s.add("web_search", "hn", "A launch", "https://ex.com/a?utm_source=x", "2026-10-11", "a")
    s.add("web_search", "rss", "B", "https://ex.com/b", "", "b")
    return s


def item(**k):
    base = {"title": "A ships tools", "topic": "AI Agent", "summary": "Short.", "why_it_matters": "You follow agents.",
            "evidence_ids": ["ev_001"]}
    base.update(k)
    return base


def draft(*items, **top):
    return json.dumps({"schema_version": 1, "items": list(items), **top})


def test_valid_draft_and_render():
    d, errs = validate_draft(draft(item(), item(title="B", evidence_ids=["ev_002"])), store())
    assert errs == []
    md = render_markdown(d, store(), "Edison", "2026-10-11")
    assert "## AI Agent" in md and "[A launch](https://ex.com/a?utm_source=x)" in md
    assert "日期未知" in md and "推荐理由" in md


def test_empty_draft_is_valid_no_news():
    d, errs = validate_draft(draft(note="来源正常，但都已报道过"), store())
    assert errs == [] and "今日没有新的相关内容" in render_markdown(d, store(), "E", "2026-10-11")


def test_rejects_bad_json_and_schema():
    assert "not valid JSON" in validate_draft("{oops", store())[1][0]
    errs = validate_draft(json.dumps({"schema_version": 2, "items": [item(extra=1, summary="")], "x": 1}), store())[1]
    joined = " ".join(errs)
    assert "schema_version" in joined and "unknown top-level" in joined and "unknown fields" in joined and "summary" in joined


def test_rejects_unknown_or_missing_evidence_and_inline_urls():
    s = store()
    assert "unknown evidence ids" in " ".join(validate_draft(draft(item(evidence_ids=["ev_999"])), s)[1])
    assert "non-empty list" in " ".join(validate_draft(draft(item(evidence_ids=[])), s)[1])
    assert "must not contain URLs" in " ".join(validate_draft(draft(item(summary="see https://evil.com")), s)[1])


def test_rejects_too_many_duplicates_and_published():
    s = EvidenceStore()
    for i in range(11):
        s.add("web_search", "hn", f"t{i}", f"https://ex.com/{i}")
    errs = validate_draft(draft(*[item(evidence_ids=[f"ev_{i+1:03d}"]) for i in range(11)]), s)[1]
    assert "too many items" in errs[0]
    errs = validate_draft(draft(item(), item(title="again")), store())[1]
    assert "same source" in " ".join(errs)
    pub = history_urls(["- [A](https://EX.com/a#x)"])
    assert "already published" in " ".join(validate_draft(draft(item()), store(), pub)[1])
    ok = validate_draft(draft(item(change_type="update", delta="Now GA")), store(), pub)[1]
    assert ok == []
    assert "needs a non-empty delta" in " ".join(validate_draft(draft(item(change_type="update")), store())[1])
    assert "must be removed" in " ".join(validate_draft(draft(item(change_type="duplicate")), store())[1])


def test_render_escapes_model_text():
    d, _ = validate_draft(draft(item(title="<img src=x onerror=1> [x](javascript:alert(1))")), store())
    md = render_markdown(d, store(), "<b>E</b>", "2026-10-11")
    from app.main import render_markdown as to_html
    html = to_html(md)
    assert "<img" not in html and "<b>" not in html and "&lt;b&gt;E" in html
    assert 'href="javascript' not in html
