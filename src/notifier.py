"""Slack Incoming Webhook を使った通知モジュール。"""

import logging
import os

import requests

from src.categories import Category
from src.classifier import ClassifiedItem

logger = logging.getLogger(__name__)


_SLACK_SECTION_MAX_LENGTH = 3000


def _build_section_blocks(header: str, items: list[ClassifiedItem]) -> list[dict]:
    blocks: list[dict] = []
    current_lines: list[str] = []
    current_len = len(header) + 1

    for item in items:
        line = "  - " + item.summary
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


def _build_blocks(version: str, items: list[ClassifiedItem]) -> list[dict]:
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
        blocks.extend(_build_section_blocks("*:warning: Breaking Changes*", breakings))

    if features:
        blocks.extend(_build_section_blocks("*:sparkles: New Features*", features))

    if improvements:
        blocks.extend(_build_section_blocks("*:arrow_up: Improvements*", improvements))

    if changes:
        blocks.extend(_build_section_blocks("*:arrows_counterclockwise: Changes*", changes))

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


def notify(version: str, items: list[ClassifiedItem]) -> None:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("SLACK_WEBHOOK_URL environment variable is not set")

    if not items:
        payload = {
            "text": "Claude Code " + version + " がリリースされました（Bugfix のみ） <https://github.com/anthropics/claude-code/releases/tag/v" + version + "|Release Notes>",
        }
    else:
        blocks = _build_blocks(version, items)
        payload = {
            "blocks": blocks,
            "text": "Claude Code " + version + " - new features and improvements detected",
        }

    response = requests.post(webhook_url, json=payload, timeout=30)
    response.raise_for_status()
    logger.info("Slack notification sent for version %s", version)


def notify_no_updates() -> None:
    """新しいリリースがない場合の Slack 通知を送信する。"""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("SLACK_WEBHOOK_URL environment variable is not set")

    payload = {
        "text": ":white_check_mark: 今日の Claude Code アップデートはありませんでした。",
    }

    response = requests.post(webhook_url, json=payload, timeout=30)
    response.raise_for_status()
    logger.info("Slack notification sent: no new releases")


def format_dry_run(version: str, items: list[ClassifiedItem]) -> str:
    if not items:
        return "[" + version + "] Release found, but no new features, improvements, or breaking changes (bugfix only)."

    features = [item for item in items if item.category == Category.FEATURE]
    improvements = [item for item in items if item.category == Category.IMPROVEMENT]
    breakings = [item for item in items if item.category == Category.BREAKING]
    changes = [item for item in items if item.category == Category.CHANGE]

    lines = ["=== Claude Code " + version + " ==="]

    if breakings:
        lines.append("\n[Breaking Changes]")
        for b in breakings:
            lines.append("  - " + b.summary)

    if features:
        lines.append("\n[New Features]")
        for f in features:
            lines.append("  - " + f.summary)

    if improvements:
        lines.append("\n[Improvements]")
        for i in improvements:
            lines.append("  - " + i.summary)

    if changes:
        lines.append("\n[Changes]")
        for c in changes:
            lines.append("  - " + c.summary)

    return "\n".join(lines)
