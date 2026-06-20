"""Stateless utility helpers: duration parsing, time formatting, text helpers."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_duration_str(value: str) -> int:
    if not value:
        return 0

    normalized = value.lower().strip()
    if normalized in {"ban", "perm", "permanent", "infinity"}:
        return -1
    if normalized in {"warn", "warning", "0"}:
        return 0

    try:
        if int(normalized) == -1:
            return -1
    except ValueError:
        pass

    total = 0
    for amount, unit in re.findall(r"(\d+)\s*([wdhm]?)", normalized):
        magnitude = int(amount)
        if unit == "w":
            total += magnitude * 10080
        elif unit == "d":
            total += magnitude * 1440
        elif unit == "h":
            total += magnitude * 60
        else:
            total += magnitude
    return total if total > 0 else 0


def truncate_text(value: Optional[str], limit: int) -> str:
    if not value:
        return ""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def extract_snowflake_id(raw_value: str) -> Optional[int]:
    match = re.search(r"(\d{15,22})", str(raw_value or ""))
    if match:
        return int(match.group(1))
    return int(raw_value) if str(raw_value).isdigit() else None


def format_duration(minutes: int) -> str:
    if minutes == -1:
        return "Ban"
    if minutes == 0:
        return "Warning"
    if minutes < 60:
        return f"{minutes} mins"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


def create_progress_bar(percent: float, length: int = 10) -> str:
    bounded = max(0.0, min(1.0, percent))
    filled = int(length * bounded)
    return "█" * filled + "░" * (length - filled)
