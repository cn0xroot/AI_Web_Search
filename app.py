import asyncio
import contextvars
import json
import os
import re
from typing import List, Optional
from urllib.parse import quote_plus

import feedparser
import httpx
from deep_translator import GoogleTranslator
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="WEB Search System")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Per-request context vars (async-safe, no global mutation) ─────────────────
_google_key: contextvars.ContextVar[str] = contextvars.ContextVar("google_key", default="")
_google_cx:  contextvars.ContextVar[str] = contextvars.ContextVar("google_cx",  default="")


class SearchRequest(BaseModel):
    keyword: str
    languages: List[str] = ["en", "fr", "ko"]
    platforms: List[str] = ["duckduckgo", "youtube", "arxiv", "reddit", "researchgate", "defcon", "blackhat"]
    api_key: Optional[str] = None               # Anthropic API key
    anthropic_base_url: Optional[str] = None    # Custom base URL (proxy / mirror)
    anthropic_model: Optional[str] = None       # Custom model ID
    google_api_key: Optional[str] = None
    google_cx: Optional[str] = None
    query_override: Optional[str] = None        # Use this query directly instead of English translation


LANG_NAMES = {
    "en": "English", "fr": "French", "ko": "Korean",
    "ja": "Japanese", "de": "German", "es": "Spanish",
    "zh-CN": "Chinese", "ru": "Russian", "ar": "Arabic",
}


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/status")
async def status():
    return {
        "ai_summary": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "google_api": bool(os.environ.get("GOOGLE_API_KEY") and os.environ.get("GOOGLE_CX")),
    }


class TestGoogleRequest(BaseModel):
    google_api_key: Optional[str] = None
    google_cx: Optional[str] = None


@app.post("/api/test-google")
async def test_google(req: TestGoogleRequest):
    """Verify Google Custom Search API credentials and return diagnostic info."""
    key = req.google_api_key or os.environ.get("GOOGLE_API_KEY", "")
    cx  = req.google_cx  or os.environ.get("GOOGLE_CX",  "")
    if not key:
        return {"ok": False, "error": "API Key 未填写"}
    if not cx:
        return {"ok": False, "error": "Search Engine ID (cx) 未填写"}
    try:
        result = await _google_cse("test query", key, cx)
        return {
            "ok": True,
            "result_count": len(result.get("results", [])),
            "key_prefix": key[:8] + "...",
            "cx": cx,
        }
    except GoogleCSEError as e:
        return {"ok": False, "error": str(e), "key_prefix": key[:8] + "...", "cx": cx}
    except Exception as e:
        return {"ok": False, "error": f"网络错误：{e}"}


# ── Google Custom Search ───────────────────────────────────────────────────────

class GoogleCSEError(Exception):
    """Raised when Google CSE returns an API-level error."""
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


async def _google_cse(query: str, api_key: str, cx: str, platform: str = "Google") -> dict:
    """Call Google Custom Search JSON API. Raises GoogleCSEError on API errors."""
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": api_key, "cx": cx, "q": query, "num": 10}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)

    # Parse Google's structured error before raise_for_status
    if resp.status_code != 200:
        try:
            err_body = resp.json().get("error", {})
            msg = err_body.get("message", resp.text[:200])
            code = err_body.get("code", resp.status_code)
        except Exception:
            msg = resp.text[:200]
            code = resp.status_code
        raise GoogleCSEError(f"Google API [{code}]: {msg}", code=code)

    data = resp.json()
    results = []
    for item in data.get("items", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", "").replace("\n", " ")[:300],
            "domain": _extract_domain(item.get("link", "")),
        })
    return {
        "platform": platform,
        "results": results,
        "search_url": f"https://www.google.com/search?q={quote_plus(query)}",
    }


def _duckduckgo_sync(query: str, platform: str = "Google") -> dict:
    try:
        from ddgs import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=8):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", "")[:300],
                    "domain": _extract_domain(r.get("href", "")),
                })
        return {
            "platform": platform,
            "results": results,
            "search_url": f"https://www.google.com/search?q={quote_plus(query)}",
        }
    except Exception as e:
        return {
            "platform": platform, "results": [], "error": str(e),
            "search_url": f"https://www.google.com/search?q={quote_plus(query)}",
        }


def _get_google_creds() -> tuple[str, str]:
    key = _google_key.get() or os.environ.get("GOOGLE_API_KEY", "")
    cx  = _google_cx.get()  or os.environ.get("GOOGLE_CX",  "")
    return key, cx


async def _google_cse_with_ddg_fallback(
    query: str, platform: str, *, filter_fn=None
) -> dict:
    """
    - With Google credentials → use Google CSE only (no DDG fallback).
    - Without credentials     → use DuckDuckGo.
    """
    key, cx = _get_google_creds()

    if key and cx:
        # Google API configured: use it exclusively
        try:
            result = await _google_cse(query, key, cx, platform=platform)
            if filter_fn:
                result["results"] = filter_fn(result.get("results", []))
            return result
        except GoogleCSEError as e:
            return {
                "platform": platform, "results": [], "error": str(e),
                "search_url": f"https://www.google.com/search?q={quote_plus(query)}",
            }
        except Exception as e:
            return {
                "platform": platform, "results": [], "error": f"请求异常：{e}",
                "search_url": f"https://www.google.com/search?q={quote_plus(query)}",
            }

    # No Google credentials: DuckDuckGo
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _duckduckgo_sync, query, platform)
    if filter_fn:
        result["results"] = filter_fn(result.get("results", []))
    return result


async def search_google(query: str) -> dict:
    return await _google_cse_with_ddg_fallback(query, "Google")


def _enrich_youtube(items: list) -> list:
    """Keep only watch URLs and attach thumbnail."""
    out = []
    for r in items:
        url = r.get("url", "")
        if "youtube.com/watch" not in url and "youtu.be/" not in url:
            continue
        vid = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url) or \
              re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", url)
        if vid:
            r["video_id"] = vid.group(1)
            r["thumbnail"] = f"https://img.youtube.com/vi/{vid.group(1)}/mqdefault.jpg"
        out.append(r)
    return out


async def search_youtube(query: str) -> dict:
    result = await _google_cse_with_ddg_fallback(
        f"{query} site:youtube.com", "YouTube", filter_fn=_enrich_youtube
    )
    result["search_url"] = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    # If still no results after both CSE and DDG, show link-only card
    if not result.get("results"):
        result["link_only"] = True
    return result


async def search_arxiv(query: str) -> dict:
    # ti: title  abs: abstract — more precise than all:
    search_field = f"ti:{quote_plus(query)}+OR+abs:{quote_plus(query)}"
    url = (
        f"https://export.arxiv.org/api/query"
        f"?search_query={search_field}&max_results=8&sortBy=relevance"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
        feed = feedparser.parse(resp.text)
        results = []
        for entry in feed.entries[:8]:
            authors = ", ".join(a.name for a in entry.get("authors", [])[:3])
            results.append({
                "title": entry.get("title", "").replace("\n", " ").strip(),
                "url": entry.get("link", ""),
                "snippet": entry.get("summary", "")[:280].replace("\n", " ").strip() + "…",
                "authors": authors,
                "date": entry.get("published", "")[:10],
                "domain": "arxiv.org",
            })
        return {
            "platform": "arXiv",
            "results": results,
            "search_url": f"https://arxiv.org/search/?searchtype=all&query={quote_plus(query)}",
        }
    except Exception as e:
        return {
            "platform": "arXiv", "results": [], "error": str(e),
            "search_url": f"https://arxiv.org/search/?searchtype=all&query={quote_plus(query)}",
        }


async def search_reddit(query: str) -> dict:
    url = f"https://www.reddit.com/search.json?q={quote_plus(query)}&limit=15&sort=relevance&t=all&type=link"
    headers = {"User-Agent": "WEB-Search-Tool/1.0 (research)"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        data = resp.json()
        results = []
        children = data.get("data", {}).get("children", [])
        # Filter: score > 1, deduplicate by title, keep top 8
        seen = set()
        for post in children:
            d = post.get("data", {})
            title = d.get("title", "")
            if not title or title in seen:
                continue
            if d.get("score", 0) < 2:
                continue
            seen.add(title)
            snippet = d.get("selftext", "")[:200].strip()
            if snippet:
                snippet += "…"
            results.append({
                "title": title,
                "url": f"https://reddit.com{d.get('permalink', '')}",
                "snippet": snippet,
                "subreddit": d.get("subreddit_name_prefixed", ""),
                "score": d.get("score", 0),
                "comments": d.get("num_comments", 0),
                "domain": "reddit.com",
            })
            if len(results) >= 8:
                break
        return {
            "platform": "Reddit",
            "results": results,
            "search_url": f"https://www.reddit.com/search/?q={quote_plus(query)}",
        }
    except Exception as e:
        return {
            "platform": "Reddit", "results": [], "error": str(e),
            "search_url": f"https://www.reddit.com/search/?q={quote_plus(query)}",
        }


async def search_researchgate(query: str) -> dict:
    # Limit to publication pages only for precision
    result = await _google_cse_with_ddg_fallback(
        f"site:researchgate.net/publication {query}", "ResearchGate",
        filter_fn=lambda items: [r for r in items if "researchgate.net" in r.get("url", "")]
    )
    result["search_url"] = f"https://www.researchgate.net/search?q={quote_plus(query)}"
    return result


async def search_defcon(query: str) -> dict:
    # Restrict to defcon.org domain
    return await _google_cse_with_ddg_fallback(f"site:defcon.org {query}", "DEF CON")


async def search_blackhat(query: str) -> dict:
    # Restrict to blackhat.com domain, focus on briefings and tools
    return await _google_cse_with_ddg_fallback(f"site:blackhat.com {query}", "Black Hat")


async def search_ccc(query: str) -> dict:
    # Prioritise media.ccc.de (talk archive) then ccc.de
    return await _google_cse_with_ddg_fallback(
        f"site:media.ccc.de {query} OR site:ccc.de {query}", "CCC"
    )


PLATFORM_SEARCHERS = {
    "duckduckgo":   search_google,
    "youtube":      search_youtube,
    "arxiv":        search_arxiv,
    "reddit":       search_reddit,
    "researchgate": search_researchgate,
    "defcon":       search_defcon,
    "blackhat":     search_blackhat,
    "ccc":          search_ccc,
}


# ── Result Translation ────────────────────────────────────────────────────────

def _translate_batch_sync(texts: list) -> list:
    out = []
    try:
        translator = GoogleTranslator(source="auto", target="zh-CN")
        for text in texts:
            if not text or not text.strip():
                out.append("")
                continue
            try:
                out.append(translator.translate(text[:500]) or "")
            except Exception:
                out.append("")
    except Exception:
        out = [""] * len(texts)
    return out


async def translate_results(results: list) -> list:
    if not results:
        return results
    titles   = [r.get("title", "")   for r in results]
    snippets = [r.get("snippet", "") for r in results]
    loop = asyncio.get_event_loop()
    titles_zh, snippets_zh = await asyncio.gather(
        loop.run_in_executor(None, _translate_batch_sync, titles),
        loop.run_in_executor(None, _translate_batch_sync, snippets),
    )
    for i, r in enumerate(results):
        r["title_zh"]   = titles_zh[i]   if i < len(titles_zh)   else ""
        r["snippet_zh"] = snippets_zh[i] if i < len(snippets_zh) else ""
    return results


# ── AI Summary ─────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


async def get_ai_summary(
    keyword: str,
    all_results: list,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> str:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return ""
    model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    try:
        import anthropic

        context = f"搜索关键词：{keyword}\n\n各平台搜索结果：\n"
        for p in all_results:
            if p.get("results"):
                context += f"\n【{p['platform']}】\n"
                for r in p["results"][:3]:
                    context += f"- {r.get('title', '')}：{r.get('snippet', '')[:120]}\n"

        # Build client — support custom base_url for proxy / mirror services
        client_kwargs: dict = {"api_key": key}
        if base_url:
            # Anthropic SDK accepts base_url to override the default endpoint
            client_kwargs["base_url"] = base_url.rstrip("/")

        client = anthropic.Anthropic(**client_kwargs)
        msg = client.messages.create(
            model=model,
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": (
                    f'请基于以下搜索结果，用中文写一段简洁的综合分析（4-6句话），'
                    f'总结关于"{keyword}"的主要发现、关键信息和研究现状。'
                    f'请直接给出分析内容，不要有"根据搜索结果"等前缀：\n\n{context}'
                ),
            }],
        )
        return msg.content[0].text
    except Exception as e:
        return ""


# ── Streaming search endpoint ──────────────────────────────────────────────────

def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/search-stream")
async def search_stream(req: SearchRequest):
    # Set Google credentials in context vars (async-safe per-request isolation)
    _google_key.set(req.google_api_key or "")
    _google_cx.set(req.google_cx or "")

    async def event_generator():
        # 1. Translate keyword into requested languages
        translations = [{"language": "原文", "text": req.keyword}]
        for lang in req.languages:
            try:
                text = GoogleTranslator(source="auto", target=lang).translate(req.keyword)
                translations.append({"language": LANG_NAMES.get(lang, lang), "text": text})
            except Exception:
                pass

        yield _sse({"type": "translations", "data": translations})

        # 2. Determine the actual search query
        if req.query_override:
            search_query = req.query_override
        else:
            # Default: use English translation
            search_query = next(
                (t["text"] for t in translations if t["language"] == "English"),
                req.keyword,
            )

        yield _sse({"type": "query", "data": search_query})

        # 3. Concurrent platform searches — yield each as it completes
        queue: asyncio.Queue = asyncio.Queue()

        async def run_one(platform: str, query: str):
            searcher = PLATFORM_SEARCHERS.get(platform)
            if searcher:
                try:
                    result = await searcher(query)
                except Exception as e:
                    result = {"platform": platform, "results": [], "error": str(e)}
            else:
                result = {"platform": platform, "results": [], "error": "Unknown platform"}
            if result.get("results"):
                result["results"] = await translate_results(result["results"])
            await queue.put(result)

        tasks = [asyncio.create_task(run_one(p, search_query)) for p in req.platforms]

        all_results = []
        for _ in req.platforms:
            result = await queue.get()
            all_results.append(result)
            yield _sse({"type": "platform", "data": result})

        for t in tasks:
            if not t.done():
                t.cancel()

        # 4. AI Summary
        summary = await get_ai_summary(
            req.keyword, all_results,
            api_key=req.api_key or "",
            base_url=req.anthropic_base_url or "",
            model=req.anthropic_model or "",
        )
        if summary:
            yield _sse({"type": "summary", "data": summary})

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""
