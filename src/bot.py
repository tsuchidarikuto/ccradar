"""Slack Q&A ボットのエントリーポイント（Slack Bolt + Socket Mode）。

リリース通知スレッド内のメンションを受け取り、RAG（CHANGELOG 項目の
エンベディング検索）と Web 情報収集の結果をもとに Gemini で回答を生成し、
同じスレッドに返信する。

起動:
    uv run python -m src.bot
"""

import logging
import os
import re

from dotenv import load_dotenv

load_dotenv()

from slack_bolt import App  # noqa: E402
from slack_bolt.adapter.socket_mode import SocketModeHandler  # noqa: E402

from src.qa import answer_question  # noqa: E402
from src.retriever import ChangelogItem, build_or_update_index, retrieve  # noqa: E402
from src.searcher import fetch_web_context  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# メンション形式 <@UXXXXX> を除去するための正規表現
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

# 起動時に構築される RAG インデックス
_index: list[ChangelogItem] = []


def _extract_question(text: str) -> str:
    """メンションテキストからボット ID を除去して質問文のみ取り出す。"""
    cleaned = _MENTION_RE.sub("", text).strip()
    return cleaned


def _handle_mention(event: dict, say, client) -> None:
    """app_mention イベントのハンドラ本体。"""
    text = event.get("text", "")
    channel = event.get("channel", "")
    # スレッド内メンションなら thread_ts がある。ルートメッセージ自体の
    # メンションでも ts をそのまま返信先にするため ts をフォールバック
    thread_ts = event.get("thread_ts") or event.get("ts")

    question = _extract_question(text)
    logger.info("Received question in channel=%s thread=%s: %s", channel, thread_ts, question)

    if not question:
        say(
            text="質問内容を入力してください（例: `@bot resume 機能はどのバージョンで追加されましたか？`）",
            thread_ts=thread_ts,
        )
        return

    # 「考え中」メッセージで即時フィードバック
    try:
        thinking = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":hourglass_flowing_sand: 公式ソースを確認しています...",
        )
        thinking_ts = thinking.get("ts")
    except Exception:
        logger.warning("Failed to post thinking message", exc_info=True)
        thinking_ts = None

    try:
        # 1. RAG で関連 CHANGELOG 項目を検索
        related_items = retrieve(question, _index, top_k=10)
        logger.info("RAG retrieved %d related items", len(related_items))

        # 2. Web 情報収集
        web_context = fetch_web_context(question)
        logger.info("Fetched web context (%d chars)", len(web_context))

        # 3. Gemini で回答生成
        answer = answer_question(question, related_items, web_context)
    except Exception:
        logger.error("Failed to generate answer", exc_info=True)
        answer = ":warning: 回答の生成に失敗しました。時間を置いて再度お試しください。"

    # 「考え中」メッセージを回答で更新（失敗時は新規投稿）
    if thinking_ts:
        try:
            client.chat_update(channel=channel, ts=thinking_ts, text=answer)
            return
        except Exception:
            logger.warning("Failed to update thinking message", exc_info=True)

    say(text=answer, thread_ts=thread_ts)


def _build_app() -> App:
    """Slack Bolt App を生成し、イベントハンドラを登録する。"""
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("SLACK_BOT_TOKEN environment variable is not set")

    app = App(token=bot_token)

    @app.event("app_mention")
    def on_mention(event, say, client):  # pragma: no cover - Slack 実行時のみ
        _handle_mention(event, say, client)

    return app


def main() -> None:
    """Bot を起動する。"""
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise RuntimeError("SLACK_APP_TOKEN environment variable is not set")

    logger.info("Building RAG index from CHANGELOG...")
    global _index
    _index = build_or_update_index()
    logger.info("Index ready: %d items", len(_index))

    app = _build_app()
    handler = SocketModeHandler(app, app_token)
    logger.info("Starting Slack bot in Socket Mode...")
    handler.start()


if __name__ == "__main__":
    main()
