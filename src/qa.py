"""Gemini API を使って Slack Q&A ボットの回答を生成するモジュール。

RAG で取得した関連 CHANGELOG 項目と Web 検索結果をコンテキストとして
Gemini Flash に渡し、Slack mrkdwn 形式で回答を生成する。
"""

import logging
import os

from google.genai import types
from google.genai.errors import ClientError, ServerError

from src.retriever import ChangelogItem, _get_client

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.5-flash"
_FALLBACK_MODEL = "gemini-2.5-flash"


QA_SYSTEM_PROMPT = """\
あなたは CLI ツール「Claude Code」のリリース・機能に関する質問に答える
Slack アシスタントです。

## 回答の方針

- 提供された「関連するリリース履歴」と「Web 情報」のみに基づいて回答してください
- 憶測や不確かな情報は避け、ソースに根拠がない場合は「公式情報では確認できません」と明記してください
- 機能や変更点を説明する際は、**どのバージョンで追加/変更されたか**を必ず明示してください
- 回答は日本語で、簡潔かつ具体的に書いてください
- 箇条書きを積極的に活用してください

## 出力形式

Slack の mrkdwn 形式で整形してください:
- 太字: `*太字*`（アスタリスク1個）
- イタリック: `_イタリック_`
- インラインコード: `` `code` ``
- コードブロック: ``` ``` ```
- リンク: `<URL|表示テキスト>`
- 箇条書き: `• 項目` または `- 項目`

Markdown の `**太字**` や `[text](url)` は使わないでください（Slack では正しく表示されません）。

回答本文のみを出力してください。余計な前置きや後書きは不要です。
"""


def _format_changelog_context(items: list[ChangelogItem]) -> str:
    """関連 CHANGELOG 項目を Gemini へのコンテキスト用テキストにまとめる。"""
    if not items:
        return "（関連するリリース項目は見つかりませんでした）"
    lines = []
    for item in items:
        lines.append(f"- [v{item.version}] {item.text}")
    return "\n".join(lines)


def _build_prompt(
    question: str, changelog_items: list[ChangelogItem], web_context: str
) -> str:
    """Gemini に渡す最終プロンプトを組み立てる。"""
    changelog_text = _format_changelog_context(changelog_items)
    web_text = web_context if web_context else "（Web 情報は取得できませんでした）"

    return f"""\
# ユーザーからの質問

{question}

# 関連するリリース履歴（CHANGELOG から RAG 検索で抽出）

{changelog_text}

# Web 情報（公式ドキュメント・検索結果）

{web_text}

# 指示

上記の情報のみに基づいて、ユーザーの質問に Slack mrkdwn 形式で日本語で回答してください。
"""


def _call_model(model_name: str, prompt: str) -> str:
    """指定モデルで generate_content を呼び出してテキストを返す。"""
    client = _get_client()
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(system_instruction=QA_SYSTEM_PROMPT),
    )
    return (response.text or "").strip()


def answer_question(
    question: str, changelog_items: list[ChangelogItem], web_context: str
) -> str:
    """質問に対する回答テキストを生成して返す。"""
    if not question.strip():
        return "質問が空です。質問内容を入力してください。"

    model_name = os.environ.get("GEMINI_MODEL") or _DEFAULT_MODEL
    prompt = _build_prompt(question, changelog_items, web_context)

    logger.info("Generating answer with model %s", model_name)

    try:
        return _call_model(model_name, prompt)
    except ServerError as e:
        if e.code == 503 and model_name != _FALLBACK_MODEL:
            logger.warning(
                "Model %s returned 503, falling back to %s", model_name, _FALLBACK_MODEL
            )
            return _call_model(_FALLBACK_MODEL, prompt)
        raise
    except ClientError as e:
        if e.code == 429:
            logger.warning("Gemini API rate limit exceeded: %s", e.message)
        raise
