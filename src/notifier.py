"""Slack Incoming Webhook を使った通知モジュール。

日本語チャンネルと英語チャンネルの両方に通知できる。
各チャンネルは環境変数で Webhook URL を指定し、言語に応じて
要約（summary / summary_en）と固定文言を出し分ける。
"""

import logging
import os
from dataclasses import dataclass

import requests

from src.categories import Category
from src.classifier import ClassifiedItem

logger = logging.getLogger(__name__)


_SLACK_SECTION_MAX_LENGTH = 3000


@dataclass(frozen=True)
class _Channel:
    """通知先チャンネルの設定。"""

    lang: str  # 言語コード（"ja" / "en"）。要約フィールドと文言の選択に使用
    webhook_env: str  # Webhook URL を格納する環境変数名
    required: bool  # 必須かどうか（未設定時にエラーにするか）


# 通知先チャンネル一覧。日本語は必須、英語は任意（未設定ならスキップ）。
_CHANNELS: list[_Channel] = [
    _Channel(lang="ja", webhook_env="SLACK_WEBHOOK_URL", required=True),
    _Channel(lang="en", webhook_env="SLACK_WEBHOOK_URL_EN", required=False),
]


# Bugfix のみのリリース時の通知文言（言語別）。
_BUGFIX_ONLY_TEXT = {
    "ja": "Claude Code {v} がリリースされました（Bugfix のみ） <{url}|Release Notes>",
    "en": "Claude Code {v} has been released (bugfix only). <{url}|Release Notes>",
}

# 新規リリースがない場合の通知文言（言語別）。
_NO_UPDATES_TEXT = {
    "ja": ":white_check_mark: 今日の Claude Code アップデートはありませんでした。",
    "en": ":white_check_mark: No Claude Code updates today.",
}


def _item_summary(item: ClassifiedItem, lang: str) -> str:
    """指定言語の要約を返す。英語要約が無い場合は日本語要約にフォールバックする。"""
    if lang == "en":
        return item.summary_en or item.summary
    return item.summary


def _build_section_blocks(header: str, items: list[ClassifiedItem], lang: str) -> list[dict]:
    blocks: list[dict] = []
    current_lines: list[str] = []
    current_len = len(header) + 1

    for item in items:
        line = "  - " + _item_summary(item, lang)
        line_len = len(line) + 1
        if current_len + line_len > _SLACK_SECTION_MAX_LENGTH and current_lines:
            text = header + "\n" + "\n".join(current_lines)
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
            current_lines = []
            current_len = len(header) + 1
        current_lines.append(line)
        current_len += line_len

    if current_lines:
        text = header + "\n" + "\n".join(current_lines)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    return blocks


def _build_blocks(version: str, items: list[ClassifiedItem], lang: str) -> list[dict]:
    features = [item for item in items if item.category == Category.FEATURE]
    improvements = [item for item in items if item.category == Category.IMPROVEMENT]
    breakings = [item for item in items if item.category == Category.BREAKING]
    changes = [item for item in items if item.category == Category.CHANGE]

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Claude Code " + version + " - Release Radar",
            },
        },
    ]

    if breakings:
        blocks.extend(_build_section_blocks("*:warning: Breaking Changes*", breakings, lang))

    if features:
        blocks.extend(_build_section_blocks("*:sparkles: New Features*", features, lang))

    if improvements:
        blocks.extend(_build_section_blocks("*:arrow_up: Improvements*", improvements, lang))

    if changes:
        blocks.extend(_build_section_blocks("*:arrows_counterclockwise: Changes*", changes, lang))

    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": "<https://github.com/anthropics/claude-code/releases/tag/v" + version + "|View full release notes>",
            }
        ],
    })

    return blocks


def _post(webhook_url: str, payload: dict) -> None:
    """Slack Webhook に payload を POST する。"""
    response = requests.post(webhook_url, json=payload, timeout=30)
    response.raise_for_status()


def _resolve_channel_url(channel: _Channel) -> str | None:
    """チャンネルの Webhook URL を解決する。

    必須チャンネルで未設定の場合は例外を送出し、
    任意チャンネルで未設定の場合は None を返す（呼び出し側でスキップ）。
    """
    webhook_url = os.environ.get(channel.webhook_env)
    if webhook_url:
        return webhook_url
    if channel.required:
        raise RuntimeError(f"{channel.webhook_env} environment variable is not set")
    logger.info("%s not set, skipping %s channel", channel.webhook_env, channel.lang)
    return None


def notify(version: str, items: list[ClassifiedItem]) -> None:
    """設定済みの全チャンネル（日本語・英語）にリリース通知を送信する。"""
    release_url = "https://github.com/anthropics/claude-code/releases/tag/v" + version

    for channel in _CHANNELS:
        webhook_url = _resolve_channel_url(channel)
        if not webhook_url:
            continue

        if not items:
            payload = {
                "text": _BUGFIX_ONLY_TEXT[channel.lang].format(v=version, url=release_url),
            }
        else:
            blocks = _build_blocks(version, items, channel.lang)
            payload = {
                "blocks": blocks,
                "text": "Claude Code " + version + " - new features and improvements detected",
            }

        _post(webhook_url, payload)
        logger.info("Slack notification sent for version %s (%s)", version, channel.lang)


def notify_no_updates() -> None:
    """新しいリリースがない場合の通知を全チャンネルに送信する。"""
    for channel in _CHANNELS:
        webhook_url = _resolve_channel_url(channel)
        if not webhook_url:
            continue

        payload = {"text": _NO_UPDATES_TEXT[channel.lang]}
        _post(webhook_url, payload)
        logger.info("Slack notification sent: no new releases (%s)", channel.lang)


def format_dry_run(version: str, items: list[ClassifiedItem]) -> str:
    if not items:
        return "[" + version + "] Release found, but no new features, improvements, or breaking changes (bugfix only)."

    features = [item for item in items if item.category == Category.FEATURE]
    improvements = [item for item in items if item.category == Category.IMPROVEMENT]
    breakings = [item for item in items if item.category == Category.BREAKING]
    changes = [item for item in items if item.category == Category.CHANGE]

    lines = ["=== Claude Code " + version + " ==="]

    def _append(title: str, group: list[ClassifiedItem]) -> None:
        if not group:
            return
        lines.append("\n[" + title + "]")
        for item in group:
            lines.append("  - " + item.summary)
            # 英語チャンネル向け要約も確認できるよう併記する
            lines.append("    (en) " + (item.summary_en or item.summary))

    _append("Breaking Changes", breakings)
    _append("New Features", features)
    _append("Improvements", improvements)
    _append("Changes", changes)

    return "\n".join(lines)
