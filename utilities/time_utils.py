# -*- coding: utf-8 -*-
"""Shared timezone helpers for timestamps shown or emitted by GradeLogy."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def now_beijing():
    """Return the current instant represented in China Standard Time."""
    return datetime.now(BEIJING_TZ)


def beijing_now_text(timespec="seconds"):
    """Return an ISO-8601 timestamp with an explicit +08:00 offset."""
    return now_beijing().isoformat(sep=" ", timespec=timespec)


def beijing_wall_now():
    """Return Beijing wall time without tzinfo for legacy local-time database fields."""
    return now_beijing().replace(tzinfo=None)


def format_beijing_time(value, source="UTC", fmt="%Y-%m-%d %H:%M:%S"):
    """Format a timestamp in Beijing time; source specifies how to interpret naive values."""
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return str(value)
    if parsed.tzinfo is None:
        if source == "UTC":
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            if source == "legacy":
                import config
                source = config.get("LEGACY_TIMEZONE", "Asia/Shanghai")
            try:
                source_zone = ZoneInfo(source)
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                source_zone = BEIJING_TZ
            parsed = parsed.replace(tzinfo=source_zone)
    return parsed.astimezone(BEIJING_TZ).strftime(fmt)
