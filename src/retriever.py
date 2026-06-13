"""CHANGELOG.md を項目単位でエンベディング化し、類似項目を検索する RAG モジュール。

起動時に CHANGELOG を取得して項目を抽出し、未キャッシュの項目のみ Gemini
Embedding API で埋め込みを生成する。キャッシュは data/embeddings.json に保存。
質問時はクエリを埋め込み、コサイン類似度で top-K の項目を返す。
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field

import numpy as np
from google import genai
from google.genai import types

from src.github_client import fetch_changelog, parse_changelog

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-001"
CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "embeddings.json"
)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Gemini クライアントを取得する（シングルトン）。"""
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable is not set")
        _client = genai.Client(api_key=api_key)
    return _client


@dataclass
class ChangelogItem:
    """CHANGELOG の1項目（1箇条書き）とその埋め込み。"""

    version: str
    text: str
    hash: str = ""
    embedding: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "text": self.text,
            "hash": self.hash,
            "embedding": self.embedding,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChangelogItem":
        return cls(
            version=data["version"],
            text=data["text"],
            hash=data.get("hash", ""),
            embedding=data.get("embedding", []),
        )


def _compute_hash(text: str) -> str:
    """項目テキストのハッシュを計算（キャッシュキー用）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_items(version: str, body: str) -> list[ChangelogItem]:
    """バージョン本文から箇条書き項目を抽出する。

    ``- `` で始まる行を項目とみなす。継続行（インデントされた行）は
    直前の項目にマージする。
    """
    items: list[ChangelogItem] = []
    current: list[str] = []

    for line in body.splitlines():
        stripped = line.rstrip()
        if stripped.startswith("- "):
            # 直前の項目を確定
            if current:
                text = " ".join(current).strip()
                if text:
                    items.append(
                        ChangelogItem(
                            version=version, text=text, hash=_compute_hash(text)
                        )
                    )
            current = [stripped[2:].strip()]
        elif stripped and current and (line.startswith("  ") or line.startswith("\t")):
            # 継続行（インデント）
            current.append(stripped)
        # それ以外の行（空行やヘッダー）は無視

    # 最後の項目
    if current:
        text = " ".join(current).strip()
        if text:
            items.append(
                ChangelogItem(version=version, text=text, hash=_compute_hash(text))
            )

    return items


def _load_cache() -> dict[str, ChangelogItem]:
    """キャッシュファイルを hash をキーとした dict として読み込む。"""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        if data.get("model") != EMBEDDING_MODEL:
            logger.info(
                "Cache model mismatch (%s != %s), rebuilding",
                data.get("model"),
                EMBEDDING_MODEL,
            )
            return {}
        cache: dict[str, ChangelogItem] = {}
        for raw in data.get("items", []):
            item = ChangelogItem.from_dict(raw)
            if item.hash:
                cache[item.hash] = item
        logger.info("Loaded %d cached embeddings", len(cache))
        return cache
    except (json.JSONDecodeError, OSError, KeyError) as e:
        logger.warning("Failed to load embedding cache: %s", e)
        return {}


def _save_cache(items: list[ChangelogItem]) -> None:
    """項目リストをキャッシュファイルに保存する。"""
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    data = {
        "model": EMBEDDING_MODEL,
        "items": [item.to_dict() for item in items],
    }
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f)
    logger.info("Saved %d embeddings to cache", len(items))


def _embed_text(text: str, task_type: str) -> list[float]:
    """1つのテキストを埋め込みベクトルに変換する。"""
    client = _get_client()
    response = client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=text,
        config=types.EmbedContentConfig(task_type=task_type),
    )
    return list(response.embeddings[0].values)


def build_or_update_index() -> list[ChangelogItem]:
    """CHANGELOG を取得・パースし、項目ごとの埋め込みを生成・キャッシュする。

    既存キャッシュに存在する項目はそのまま使い、新規項目のみ API を呼ぶ。
    """
    content = fetch_changelog()
    if not content:
        logger.warning("CHANGELOG is empty, cannot build index")
        return []

    sections = parse_changelog(content)
    cache = _load_cache()

    items: list[ChangelogItem] = []
    new_count = 0
    for version, body in sections.items():
        for item in _extract_items(version, body):
            if item.hash in cache:
                # キャッシュから流用
                items.append(cache[item.hash])
            else:
                try:
                    item.embedding = _embed_text(item.text, "RETRIEVAL_DOCUMENT")
                    items.append(item)
                    new_count += 1
                except Exception:
                    logger.error(
                        "Failed to embed item in %s, skipping: %s",
                        version,
                        item.text[:80],
                        exc_info=True,
                    )

    logger.info(
        "Index built: %d total items (%d newly embedded)", len(items), new_count
    )

    if new_count > 0:
        _save_cache(items)

    return items


def retrieve(
    question: str, items: list[ChangelogItem], top_k: int = 10
) -> list[ChangelogItem]:
    """質問に最も関連する項目を top_k 件返す（コサイン類似度）。"""
    if not items:
        return []

    query_vec = np.array(_embed_text(question, "RETRIEVAL_QUERY"), dtype=np.float32)
    doc_matrix = np.array([item.embedding for item in items], dtype=np.float32)

    # コサイン類似度
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
    doc_norms = doc_matrix / (
        np.linalg.norm(doc_matrix, axis=1, keepdims=True) + 1e-10
    )
    scores = doc_norms @ query_norm

    top_indices = np.argsort(-scores)[:top_k]
    return [items[i] for i in top_indices]
