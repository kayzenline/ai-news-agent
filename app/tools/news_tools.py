"""新闻工具：web_search 聚合免费来源（HN Algolia / arXiv / RSS，可选 Tavily），fetch_url 抓正文。"""

import html
import ipaddress
import re
import socket
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

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
    r = httpx.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"query": query, "tags": "story", "numericFilters": f"created_at_i>{since}", "hitsPerPage": 8},
        timeout=TIMEOUT,
        headers=UA,
    )
    r.raise_for_status()
    return [
        {
            "source": "hackernews",
            "title": h.get("title"),
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            "date": h.get("created_at", "")[:10],
            "points": h.get("points"),
        }
        for h in r.json().get("hits", [])
        if h.get("title")
    ]


ARXIV_API = "https://export.arxiv.org/api/query"


def _arxiv_get(params: dict):
    """HTTPS；手动跟随重定向，最多 3 跳，且只允许跳到 https 的 arxiv.org 域名。"""
    url = ARXIV_API
    for _ in range(4):
        r = httpx.get(url, params=params, timeout=TIMEOUT, headers=UA, follow_redirects=False)
        if not getattr(r, "is_redirect", False):
            r.raise_for_status()
            return r
        url = urljoin(url, r.headers.get("location", ""))
        host = (urlsplit(url).hostname or "").lower()
        if urlsplit(url).scheme != "https" or not (host == "arxiv.org" or host.endswith(".arxiv.org")):
            raise ValueError(f"arxiv redirected to an unexpected location: {url}")
        params = None  # location 已带完整 query
    raise ValueError("arxiv: too many redirects")


def _arxiv(query: str, days: int) -> list[dict]:
    r = _arxiv_get(
        {"search_query": f"all:{query}", "sortBy": "submittedDate", "sortOrder": "descending", "max_results": 5}
    )
    feed = feedparser.parse(r.text)
    pats, cjk = query_terms(query)
    out = []
    for e in feed.entries:
        if not _recent(_entry_dt(e), days):
            continue
        title = " ".join((e.title or "").split())
        summary = " ".join(e.get("summary", "").split())
        if (pats or cjk) and _score(title, summary, pats, cjk) <= 0:
            continue
        out.append(
            {"source": "arxiv", "title": title, "url": e.link, "date": _fmt(_entry_dt(e)), "summary": summary[:300]}
        )
    return out


STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "news",
    "latest",
    "of",
    "in",
    "on",
    "to",
    "a",
    "an",
    "top",
    "new",
    "first",
    "best",
    "how",
    "what",
    "why",
    "your",
    "from",
    "that",
    "this",
    "are",
    "you",
    "our",
    "its",
    "into",
    "over",
    "after",
    "about",
    "more",
    "most",
    "will",
    "just",
    "than",
    "then",
    "also",
    "been",
    "have",
    "has",
    "was",
    "were",
    "not",
    "but",
    "can",
    "all",
    "who",
    "their",
    "they",
    "said",
    "says",
    "update",
    "updates",
    "here",
}
CJK_RE = re.compile(r"[\u3400-\u9fff]+")


def query_terms(query: str) -> tuple[list[re.Pattern], list[str]]:
    """英文词按词边界匹配（避免 "ai" 命中 "said"）；中文连续串用本身及二元组做子串匹配。"""
    ascii_terms = [
        w for w in re.findall(r"[a-z0-9][a-z0-9+.\-]*", query.lower()) if w not in STOPWORDS and not w.isdigit()
    ]
    pats = [re.compile(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])") for w in ascii_terms]
    cjk = []
    for run in CJK_RE.findall(query):
        cjk.append(run)
        if len(run) > 2:
            cjk += [run[i : i + 2] for i in range(len(run) - 1)]
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
        except httpx.HTTPStatusError as e:
            if errors is not None:
                errors.append(f"rss/{name}: HTTP {e.response.status_code}")
            continue
        except Exception as e:  # 单个 feed 失败不再静默：记录到 errors 回传给模型
            if errors is not None:
                errors.append(f"rss/{name}: {type(e).__name__}: {e}".splitlines()[0][:200])
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
        out = [
            dict(i, summary="unmatched: recent item, does not contain the query terms")
            for _, i in sorted(recent, key=key, reverse=True)[:3]
        ]
        if notes is not None:
            notes.append(
                f"rss: no items matched {query!r}; showing {len(out)} most recent unfiltered items flagged 'unmatched' (try English keywords)"
            )
    for i in out:
        i.pop("_dt", None)
    return out


def _tavily(query: str, days: int) -> list[dict]:
    r = httpx.post(
        "https://api.tavily.com/search",
        timeout=TIMEOUT,
        json={"api_key": config.TAVILY_API_KEY, "query": query, "topic": "news", "days": days, "max_results": 8},
    )
    r.raise_for_status()
    out = []
    for x in r.json().get("results", []):
        try:
            dt = parsedate_to_datetime(x["published_date"]) if x.get("published_date") else None
        except (TypeError, ValueError):
            dt = None
        if _recent(dt, days):
            out.append(
                {
                    "source": "tavily",
                    "title": x["title"],
                    "url": x["url"],
                    "date": _fmt(dt),
                    "summary": x.get("content", "")[:300],
                }
            )
    return out


class SearchResult(str):
    """带来源健康账本的搜索文本返回对象（继承 str，完全兼容字符串接口）。"""

    def __new__(
        cls,
        text: str,
        requested: list[str] | None = None,
        succeeded: list[str] | None = None,
        failed: dict[str, str] | None = None,
        results_count: int = 0,
    ):
        obj = super().__new__(cls, text)
        obj.sources_requested = requested or []
        obj.sources_succeeded = succeeded or []
        obj.sources_failed = failed or {}
        obj.results_count = results_count
        obj.all_failed = len(obj.sources_succeeded) == 0 and len(obj.sources_failed) > 0
        return obj


def web_search(query: str, sources: list[str] | None = None, days: int = 2) -> SearchResult:
    sources = sources or (["tavily"] if config.TAVILY_API_KEY else ["hackernews", "rss", "arxiv"])
    fns = {
        "hackernews": lambda: _hn(query, days),
        "arxiv": lambda: _arxiv(query, days),
        "rss": lambda: _rss(query, days),
        "tavily": lambda: _tavily(query, days),
    }
    results, errors, notes = [], [], []
    succeeded, failed = [], {}
    fns["rss"] = lambda: _rss(query, days, errors, notes)
    for s in sources:
        if s not in fns:
            err = f"unknown source {s}"
            errors.append(err)
            failed[s] = err
            continue
        if s == "tavily" and not config.TAVILY_API_KEY:
            notes.append("tavily: not configured on this server, skipped (use hackernews/rss/arxiv)")
            continue
        rss_err_idx = len(errors)
        try:
            r = fns[s]()
            results.extend(r)
            if s == "rss":
                # RSS 内部循环多个 feed；如果所有 feed 均报错且未产生条目，视为该源失败
                if len(errors) > rss_err_idx and not any(x.get("source") in RSS_FEEDS for x in r):
                    failed[s] = "; ".join(errors[rss_err_idx:])
                else:
                    succeeded.append(s)
            else:
                succeeded.append(s)
        except httpx.HTTPStatusError as e:  # 简短说明，不回显响应正文
            code = e.response.status_code
            hint = " (check TAVILY_API_KEY)" if s == "tavily" and code in (401, 403) else ""
            msg = f"HTTP {code}{hint}"
            errors.append(f"{s}: {msg}")
            failed[s] = msg
        except Exception as e:  # 单个来源失败不影响其它来源，错误回传给模型
            msg = f"{type(e).__name__}: {e}"
            errors.append(f"{s}: {msg}")
            failed[s] = msg
    lines = [
        f"- [{x['source']}] {x['title']} | {x['url']} | {x.get('date', '')}"
        + (f" | {x['summary']}" if x.get("summary") else "")
        for x in results
    ]
    if notes:
        lines.append("notes: " + "; ".join(notes))
    if errors:
        lines.append("errors: " + "; ".join(errors))
    text = "\n".join(lines) or "no results"
    return SearchResult(text, requested=list(sources), succeeded=succeeded, failed=failed, results_count=len(results))


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
        for wbits in (31,) if "gzip" in enc else (15, -15):
            try:
                d = zlib.decompressobj(wbits)
                return d.decompress(raw, MAX_FETCH_BYTES)
            except zlib.error:
                continue
    return raw[:MAX_FETCH_BYTES]


def _extract_pdf_text(raw: bytes, max_chars: int = 4000) -> str:
    """从二进制 PDF 字节流中提取文本，限制解析前 10 页，防止超大文档或恶意构造挂起。"""
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        pages_text = []
        chars_collected = 0
        for page in reader.pages[:10]:
            t = page.extract_text() or ""
            t = t.strip()
            if t:
                pages_text.append(t)
                chars_collected += len(t)
                if chars_collected >= max_chars:
                    break
        combined = "\n\n".join(pages_text)
        cleaned = re.sub(r"[ \t]+", " ", combined).strip()
        if not cleaned:
            return "PDF document contains no extractable text (e.g. scanned images without OCR layer)."
        return cleaned[:max_chars]
    except Exception as e:
        return f"ERROR: failed to extract text from PDF: {e}"


def _get_limited(url: str, max_chars: int = 4000) -> tuple[bool, str]:
    """不自动跟随重定向：每一跳都重新做公网校验；流式读取并限制压缩前/解压后字节数。
    返回 (is_pdf, text)。
    """
    for _ in range(MAX_REDIRECTS + 1):
        check_public_url(url)
        with httpx.stream("GET", url, timeout=TIMEOUT, headers=FETCH_HEADERS, follow_redirects=False) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers.get("location", ""))
                continue
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").lower()
            is_pdf = urlsplit(url).path.lower().endswith(".pdf") or "application/pdf" in ctype
            raw = bytearray()
            for chunk in r.iter_raw():  # 原始（可能压缩的）字节，同样受上限约束
                raw += chunk
                if len(raw) > MAX_FETCH_BYTES:
                    break
            body = _decode_body(bytes(raw), r.headers.get("content-encoding", ""))
            if is_pdf:
                return True, _extract_pdf_text(body, max_chars=max_chars)
            return False, body.decode(r.charset_encoding or "utf-8", errors="replace")
    raise ValueError(f"too many redirects (>{MAX_REDIRECTS})")


def fetch_url(url: str, max_chars: int = 4000) -> str:
    is_pdf, text = _get_limited(url, max_chars=max_chars)
    if is_pdf:
        return text[:max_chars]
    text = re.sub(r"(?is)<(script|style|nav|footer|header).*?</\1>", " ", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]
