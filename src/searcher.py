"""Web 情報収集モジュール。

Claude Code 公式ドキュメント（GitHub README）を直接取得し、
さらに DuckDuckGo でニュース・関連情報を検索して上位ページのテキストを集める。
Q&A ボットの回答生成時のコンテキストとして使う。
"""

import logging
import re
from typing import Optional

import requests
from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)

_OFFICIAL_README_URL = (
    "https://raw.githubusercontent.com/anthropics/claude-code/main/README.md"
)
_OFFICIAL_DOCS_MAX_CHARS = 10_000
_WEB_PAGE_MAX_CHARS = 5_000
_WEB_SEARCH_RESULTS = 3
_REQUEST_TIMEOUT = 15

# HTML タグ除去用の正規表現
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    """HTML をプレーンテキストに変換する（簡易実装）。"""
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _fetch_url(url: str, max_chars: int) -> Optional[str]:
    """URL を取得し、テキストに変換して max_chars まで切り詰めて返す。"""
    try:
        response = requests.get(
            url,
            timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": "ccradar-qa-bot/1.0"},
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None

    content_type = response.headers.get("Content-Type", "")
    if "html" in content_type.lower():
        text = _html_to_text(response.text)
    else:
        text = response.text

    if len(text) > max_chars:
        text = text[:max_chars] + "..."
    return text


def _get_official_docs() -> str:
    """Claude Code 公式 GitHub の README を取得する。"""
    text = _fetch_url(_OFFICIAL_README_URL, _OFFICIAL_DOCS_MAX_CHARS)
    if text:
        logger.info("Fetched official README (%d chars)", len(text))
        return text
    return ""


def _search_web(question: str) -> list[tuple[str, str, str]]:
    """DuckDuckGo で検索し、上位結果のページを取得する。

    Returns:
        [(title, url, body_text), ...] のリスト。
    """
    query = f"Claude Code {question}"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=_WEB_SEARCH_RESULTS))
    except Exception as e:
        logger.warning("DuckDuckGo search failed: %s", e)
        return []

    logger.info("DuckDuckGo returned %d result(s) for: %s", len(results), query)

    fetched: list[tuple[str, str, str]] = []
    for result in results:
        url = result.get("href") or result.get("url") or ""
        title = result.get("title", "")
        if not url:
            continue
        body = _fetch_url(url, _WEB_PAGE_MAX_CHARS)
        if body:
            fetched.append((title, url, body))
    return fetched


def fetch_web_context(question: str) -> str:
    """公式ドキュメント + Web 検索結果を1つのテキストにまとめて返す。"""
    sections: list[str] = []

    official = _get_official_docs()
    if official:
        sections.append("=== Claude Code 公式 README ===\n" + official)

    web_results = _search_web(question)
    for i, (title, url, body) in enumerate(web_results, start=1):
        sections.append(
            f"=== Web検索結果 {i}: {title} ({url}) ===\n{body}"
        )

    if not sections:
        return ""

    return "\n\n".join(sections)
