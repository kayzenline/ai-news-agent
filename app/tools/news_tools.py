"""新闻工具：web_search 聚合免费来源（HN Algolia / arXiv / RSS，可选 Tavily），fetch_url 抓正文。"""
import html
import re
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from .. import config

RSS_FEEDS = {
    "techcrunch_ai": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "venturebeat_ai": "https://venturebeat.com/category/ai/feed/",
    "mit_tech_review": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "huggingface_blog": "https://huggingface.co/blog/feed.xml",
}
UA = {"User-Agent": "ai-news-agent/0.1 (+https://github.com/kayzenline)"}
TIMEOUT = 15


def _hn(query: str, days: int) -> list[dict]:
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    r = httpx.get("https://hn.algolia.com/api/v1/search_by_date",
                  params={"query": query, "tags": "story", "numericFilters": f"created_at_i>{since}", "hitsPerPage": 8},
                  timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    return [{"source": "hackernews", "title": h.get("title"), "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
             "date": h.get("created_at", "")[:10], "points": h.get("points")} for h in r.json().get("hits", []) if h.get("title")]


def _arxiv(query: str) -> list[dict]:
    r = httpx.get("http://export.arxiv.org/api/query",
                  params={"search_query": f"all:{query}", "sortBy": "submittedDate", "sortOrder": "descending", "max_results": 5},
                  timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    feed = feedparser.parse(r.text)
    return [{"source": "arxiv", "title": " ".join(e.title.split()), "url": e.link, "date": e.get("published", "")[:10],
             "summary": " ".join(e.get("summary", "").split())[:300]} for e in feed.entries]


def _rss(query: str) -> list[dict]:
    words = [w.lower() for w in query.split() if len(w) > 1]
    out = []
    for name, url in RSS_FEEDS.items():
        try:
            r = httpx.get(url, timeout=TIMEOUT, headers=UA, follow_redirects=True)
            feed = feedparser.parse(r.content)
        except Exception:
            continue
        for e in feed.entries[:30]:
            text = (e.get("title", "") + " " + e.get("summary", "")).lower()
            if not words or any(w in text for w in words):
                out.append({"source": name, "title": e.get("title"), "url": e.get("link"),
                            "date": (e.get("published") or "")[:16]})
    return out[:10]


def _tavily(query: str) -> list[dict]:
    r = httpx.post("https://api.tavily.com/search", timeout=TIMEOUT,
                   json={"api_key": config.TAVILY_API_KEY, "query": query, "topic": "news", "days": 2, "max_results": 8})
    r.raise_for_status()
    return [{"source": "tavily", "title": x["title"], "url": x["url"], "summary": x.get("content", "")[:300]}
            for x in r.json().get("results", [])]


def web_search(query: str, sources: list[str] | None = None, days: int = 2) -> str:
    sources = sources or (["tavily"] if config.TAVILY_API_KEY else ["hackernews", "rss", "arxiv"])
    fns = {"hackernews": lambda: _hn(query, days), "arxiv": lambda: _arxiv(query), "rss": lambda: _rss(query), "tavily": lambda: _tavily(query)}
    results, errors = [], []
    for s in sources:
        if s not in fns:
            errors.append(f"unknown source {s}")
            continue
        try:
            results.extend(fns[s]())
        except Exception as e:  # 单个来源失败不影响其它来源，错误回传给模型
            errors.append(f"{s}: {type(e).__name__}: {e}")
    lines = [f"- [{x['source']}] {x['title']} | {x['url']} | {x.get('date','')}" + (f" | {x['summary']}" if x.get("summary") else "")
             for x in results]
    if errors:
        lines.append("errors: " + "; ".join(errors))
    return "\n".join(lines) or "no results"


def fetch_url(url: str, max_chars: int = 4000) -> str:
    if not url.startswith(("http://", "https://")):
        raise ValueError("only http(s) urls are allowed")
    r = httpx.get(url, timeout=TIMEOUT, headers=UA, follow_redirects=True)
    r.raise_for_status()
    text = re.sub(r"(?is)<(script|style|nav|footer|header).*?</\1>", " ", r.text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]
