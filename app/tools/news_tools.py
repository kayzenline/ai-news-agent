"""新闻工具：web_search 聚合免费来源（HN Algolia / arXiv / RSS，可选 Tavily），fetch_url 抓正文。"""
import html
import ipaddress
import re
import socket
import zlib
from urllib.parse import urljoin, urlsplit
from email.utils import parsedate_to_datetime
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


def _entry_dt(e) -> datetime | None:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def _recent(dt: datetime | None, days: int) -> bool:
    """回溯窗口过滤：发布日期早于 now-days 的丢弃；日期未知的保留，并在结果里标 date=unknown 交给模型判断。"""
    return dt is None or dt >= datetime.now(timezone.utc) - timedelta(days=days)


def _fmt(dt: datetime | None, width: int = 10) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")[:width] if dt else "unknown"


def _hn(query: str, days: int) -> list[dict]:
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    r = httpx.get("https://hn.algolia.com/api/v1/search_by_date",
                  params={"query": query, "tags": "story", "numericFilters": f"created_at_i>{since}", "hitsPerPage": 8},
                  timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    return [{"source": "hackernews", "title": h.get("title"), "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
             "date": h.get("created_at", "")[:10], "points": h.get("points")} for h in r.json().get("hits", []) if h.get("title")]


def _arxiv(query: str, days: int) -> list[dict]:
    r = httpx.get("http://export.arxiv.org/api/query",
                  params={"search_query": f"all:{query}", "sortBy": "submittedDate", "sortOrder": "descending", "max_results": 5},
                  timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    feed = feedparser.parse(r.text)
    return [{"source": "arxiv", "title": " ".join(e.title.split()), "url": e.link, "date": _fmt(_entry_dt(e)),
             "summary": " ".join(e.get("summary", "").split())[:300]}
            for e in feed.entries if _recent(_entry_dt(e), days)]


STOPWORDS = {"the", "and", "for", "with", "news", "latest", "of", "in", "on", "to", "a", "an"}
CJK_RE = re.compile(r"[\u3400-\u9fff]+")


def query_terms(query: str) -> tuple[list[re.Pattern], list[str]]:
    """英文词按词边界匹配（避免 "ai" 命中 "said"）；中文连续串用本身及二元组做子串匹配。"""
    ascii_terms = [w for w in re.findall(r"[a-z0-9][a-z0-9+.\-]*", query.lower()) if w not in STOPWORDS]
    pats = [re.compile(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])") for w in ascii_terms]
    cjk = []
    for run in CJK_RE.findall(query):
        cjk.append(run)
        if len(run) > 2:
            cjk += [run[i:i + 2] for i in range(len(run) - 1)]
    return pats, list(dict.fromkeys(cjk))


def _score(title: str, summary: str, pats, cjk) -> int:
    t, sm = title.lower(), summary.lower()
    score = 0
    for p in pats:
        score += 2 * bool(p.search(t)) + bool(p.search(sm))
    for w in cjk:
        score += 2 * (w in title) + (w in summary)
    return score


def _rss(query: str, days: int, errors: list | None = None, notes: list | None = None) -> list[dict]:
    """按查询词给 RSS 条目打分排序；没有任何命中时如实说明，只附少量最新条目并标 unmatched。"""
    pats, cjk = query_terms(query)
    scored, recent = [], []
    for name, url in RSS_FEEDS.items():
        try:
            r = httpx.get(url, timeout=TIMEOUT, headers=UA, follow_redirects=True)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:  # 单个 feed 失败不再静默：记录到 errors 回传给模型
            if errors is not None:
                errors.append(f"rss/{name}: {type(e).__name__}: {e}")
            continue
        for e in feed.entries[:30]:
            dt = _entry_dt(e)
            if not _recent(dt, days):
                continue
            item = {"source": name, "title": e.get("title"), "url": e.get("link"), "date": _fmt(dt, 16), "_dt": dt}
            sc = _score(e.get("title", ""), e.get("summary", ""), pats, cjk) if (pats or cjk) else 1
            (scored if sc > 0 else recent).append((sc, item))
    key = lambda x: (x[0], x[1]["_dt"] or datetime.min.replace(tzinfo=timezone.utc))
    if scored:
        out = [i for _, i in sorted(scored, key=key, reverse=True)[:10]]
    else:
        out = [dict(i, summary="unmatched: recent item, does not contain the query terms")
               for _, i in sorted(recent, key=key, reverse=True)[:3]]
        if notes is not None:
            notes.append(f"rss: no items matched {query!r}; showing {len(out)} most recent unfiltered items flagged 'unmatched' (try English keywords)")
    for i in out:
        i.pop("_dt", None)
    return out


def _tavily(query: str, days: int) -> list[dict]:
    r = httpx.post("https://api.tavily.com/search", timeout=TIMEOUT,
                   json={"api_key": config.TAVILY_API_KEY, "query": query, "topic": "news", "days": days, "max_results": 8})
    r.raise_for_status()
    out = []
    for x in r.json().get("results", []):
        try:
            dt = parsedate_to_datetime(x["published_date"]) if x.get("published_date") else None
        except (TypeError, ValueError):
            dt = None
        if _recent(dt, days):
            out.append({"source": "tavily", "title": x["title"], "url": x["url"], "date": _fmt(dt),
                        "summary": x.get("content", "")[:300]})
    return out


def web_search(query: str, sources: list[str] | None = None, days: int = 2) -> str:
    sources = sources or (["tavily"] if config.TAVILY_API_KEY else ["hackernews", "rss", "arxiv"])
    fns = {"hackernews": lambda: _hn(query, days), "arxiv": lambda: _arxiv(query, days), "rss": lambda: _rss(query, days),
           "tavily": lambda: _tavily(query, days)}
    results, errors, notes = [], [], []
    fns["rss"] = lambda: _rss(query, days, errors, notes)
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
    if notes:
        lines.append("notes: " + "; ".join(notes))
    if errors:
        lines.append("errors: " + "; ".join(errors))
    return "\n".join(lines) or "no results"


MAX_FETCH_BYTES = 2_000_000
MAX_REDIRECTS = 5
ALLOWED_PORTS = {80, 443}


def check_public_url(url: str) -> None:
    """SSRF 防护：只允许 http(s)、默认端口、无凭据，且 DNS 解析出的每个地址都必须是公网地址（IPv4/IPv6）。
    局限：检查与连接之间仍有 DNS rebinding 窗口，共享部署前需用网络出口规则补强。"""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("only http(s) urls are allowed")
    if parts.username or parts.password:
        raise ValueError("urls with credentials are not allowed")
    host = parts.hostname
    if not host:
        raise ValueError("url has no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise ValueError(f"port {port} not allowed")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve {host}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError(f"blocked non-public address {ip} for host {host}")


FETCH_HEADERS = {**UA, "Accept-Encoding": "gzip, deflate", "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"}


def _decode_body(raw: bytes, encoding: str) -> bytes:
    """按 Content-Encoding 只解压一次，解压后同样限制在 MAX_FETCH_BYTES（防压缩炸弹）。
    截断的压缩流尽量解出已有部分；编码错标（声明 gzip 实为明文）或未知编码时退回原始字节。"""
    enc = encoding.lower().strip()
    if enc in ("gzip", "x-gzip", "deflate"):
        for wbits in ((31,) if "gzip" in enc else (15, -15)):
            try:
                d = zlib.decompressobj(wbits)
                return d.decompress(raw, MAX_FETCH_BYTES)
            except zlib.error:
                continue
    return raw[:MAX_FETCH_BYTES]


def _get_limited(url: str) -> str:
    """不自动跟随重定向：每一跳都重新做公网校验；流式读取并限制压缩前/解压后字节数。

    只声明 gzip/deflate，避免依赖 br/zstd 解码器；读原始字节后自行解压一次并返回文本。
    旧实现把已解压的字节配原响应头重建 Response，会按 Content-Encoding 再解压一次，导致 "incorrect header check"。
    """
    for _ in range(MAX_REDIRECTS + 1):
        check_public_url(url)
        with httpx.stream("GET", url, timeout=TIMEOUT, headers=FETCH_HEADERS, follow_redirects=False) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers.get("location", ""))
                continue
            r.raise_for_status()
            raw = bytearray()
            for chunk in r.iter_raw():  # 原始（可能压缩的）字节，同样受上限约束
                raw += chunk
                if len(raw) > MAX_FETCH_BYTES:
                    break
            body = _decode_body(bytes(raw), r.headers.get("content-encoding", ""))
            return body.decode(r.charset_encoding or "utf-8", errors="replace")
    raise ValueError(f"too many redirects (>{MAX_REDIRECTS})")


def fetch_url(url: str, max_chars: int = 4000) -> str:
    text = re.sub(r"(?is)<(script|style|nav|footer|header).*?</\1>", " ", _get_limited(url))
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]
