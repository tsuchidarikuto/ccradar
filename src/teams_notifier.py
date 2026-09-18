"""Microsoft Teams への通知モジュール。

Teams の Workflows（Power Automate）で作った webhook URL に Adaptive Card を POST する。
Slack と違い `{"text": ...}` や MessageCard は 202 が返るだけでチャネルには出ない。
Adaptive Card 形式のみ使うこと。

TEAMS_WEBHOOK_URL が未設定なら何もしない。Slack 通知の邪魔をしないよう、
送信に失敗してもログに残すだけで例外は投げない。
"""

import logging
import os

import requests

from src.categories import Category
from src.classifier import ClassifiedItem

logger = logging.getLogger(__name__)


_RELEASE_URL_PREFIX = "https://github.com/anthropics/claude-code/releases/tag/v"


def _build_lines(version: str, items: list[ClassifiedItem]) -> list[str]:
    """カードに並べる行を作る。空文字列はセクションの区切りを表す。"""
    sections = [
        ("Breaking Changes", [i for i in items if i.category == Category.BREAKING]),
        ("New Features", [i for i in items if i.category == Category.FEATURE]),
        ("Improvements", [i for i in items if i.category == Category.IMPROVEMENT]),
        ("Changes", [i for i in items if i.category == Category.CHANGE]),
    ]

    lines = ["**Claude Code " + version + " - Release Radar**"]

    for header, section_items in sections:
        if not section_items:
            continue
        lines.append("")
        lines.append("**" + header + "**")
        lines.extend("- " + i.summary for i in section_items)

    lines.append("")
    lines.append(
        "[View full release notes](" + _RELEASE_URL_PREFIX + version + ")"
    )

    return lines


def _build_payload(lines: list[str]) -> dict:
    """1 行を 1 つの TextBlock にする。空行は次ブロックの区切りに変換する。

    TextBlock 内の改行は行送りにならないので、複数行を 1 つのブロックに詰めない。
    """
    body: list[dict] = []
    gap = False
    for line in lines:
        if not line:
            gap = True
            continue
        block: dict = {"type": "TextBlock", "text": line, "wrap": True}
        if gap and body:
            block["separator"] = True
            block["spacing"] = "medium"
        gap = False
        body.append(block)

    if body:
        body[0]["size"] = "Large"

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    # 1.5 は Teams で描画が崩れる報告があるため 1.4 のまま
                    "version": "1.4",
                    # 既定幅は狭く 1 文ごとに折り返すため全幅にする
                    "msteams": {"width": "Full"},
                    "body": body,
                },
            }
        ],
    }


def _post(lines: list[str]) -> bool:
    """Adaptive Card を Teams に送信する。POST が通ったら True。

    Workflows webhook はペイロードの形によらず 202 を返す。202 はリクエストを
    受け取ったという意味でしかなく、チャネルに投稿されたことは保証しない。
    形式を変えたときはチャネルを目視するか、make.powerautomate.com の
    実行履歴で投稿アクションの入力を確認すること。
    """
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not webhook_url:
        return False

    try:
        response = requests.post(webhook_url, json=_build_payload(lines), timeout=30)
        response.raise_for_status()
    except Exception:
        logger.error("Failed to send Teams notification", exc_info=True)
        return False
    return True


def notify(version: str, items: list[ClassifiedItem]) -> None:
    if not items:
        lines = [
            "Claude Code " + version + " がリリースされました（Bugfix のみ）",
            "[Release Notes](" + _RELEASE_URL_PREFIX + version + ")",
        ]
    else:
        lines = _build_lines(version, items)

    if _post(lines):
        logger.info("Teams webhook accepted payload for version %s", version)


def notify_no_updates() -> None:
    """新しいリリースがない場合の Teams 通知を送信する。"""
    if _post(["今日の Claude Code アップデートはありませんでした。"]):
        logger.info("Teams webhook accepted payload: no new releases")
