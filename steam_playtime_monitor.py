#!/usr/bin/env python3
"""Monitor Steam recent playtime growth from the public profile XML.

The public XML only exposes recent playtime in 0.1 hour units. This script
converts that value to minutes, so the effective granularity is 6 minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


DEFAULT_PROFILE_XML_URL = "https://steamcommunity.com/profiles/76561198839776064/?xml=1"
DEFAULT_BARK_BASE_URL = os.environ.get("BARK_BASE_URL", "")
DEFAULT_STATE_FILE = Path(__file__).resolve().parent / "data" / "steam_recent_playtime_state.json"
DEFAULT_TIMEOUT = 15.0
USER_AGENT = "steam-playtime-monitor/1.0"
DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
DISPLAY_TIME_FORMAT = "%Y-%m-%d %H:%M"


@dataclass
class GameSnapshot:
    app_id: str
    name: str
    recent_hours: str
    recent_minutes: int
    total_hours_on_record: str | None


def env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "读取 Steam 公开 XML，比较最近两周游戏时长；若有增长则通过 Bark 推送通知。"
        )
    )
    parser.add_argument(
        "--profile-xml-url",
        default=env_or_default("STEAM_PROFILE_XML_URL", DEFAULT_PROFILE_XML_URL),
        help="Steam 公开 XML 地址，可由环境变量 STEAM_PROFILE_XML_URL 覆盖。",
    )
    parser.add_argument(
        "--bark-base-url",
        default=DEFAULT_BARK_BASE_URL,
        help="Bark 基础地址，格式示例: https://api.day.app/<device_key>；建议用环境变量 BARK_BASE_URL 注入。",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="本地快照文件路径。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="HTTP 超时时间，单位秒。",
    )
    parser.add_argument(
        "--print-current",
        action="store_true",
        help="打印当前解析到的游戏分钟数。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要推送的内容，不真正发送 Bark 请求。",
    )
    return parser.parse_args()


def fetch_profile_xml(profile_xml_url: str, timeout: float) -> str:
    request = urllib.request.Request(
        profile_xml_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = response.status
        payload = response.read().decode("utf-8", errors="replace")
    if status != 200:
        raise RuntimeError(f"拉取 Steam XML 失败，HTTP {status}")
    return payload


def child_text(element: ET.Element, tag_name: str) -> str:
    child = element.find(tag_name)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def hours_to_minutes(hours_text: str) -> int:
    try:
        hours_value = Decimal(hours_text.strip())
    except InvalidOperation as exc:
        raise ValueError(f"无法解析时长字段: {hours_text!r}") from exc
    minutes_value = (hours_value * Decimal("60")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(minutes_value)


def parse_app_id(game_link: str) -> str:
    match = re.search(r"/app/(\d+)", game_link)
    return match.group(1) if match else ""


def build_current_snapshot(xml_text: str, profile_xml_url: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)
    steam_id64 = child_text(root, "steamID64")
    steam_name = child_text(root, "steamID")
    games: dict[str, dict[str, Any]] = {}

    for game_element in root.findall("./mostPlayedGames/mostPlayedGame"):
        game_name = child_text(game_element, "gameName")
        recent_hours = child_text(game_element, "hoursPlayed")
        if not game_name or not recent_hours:
            continue

        app_id = parse_app_id(child_text(game_element, "gameLink")) or game_name
        game_snapshot = GameSnapshot(
            app_id=app_id,
            name=game_name,
            recent_hours=recent_hours,
            recent_minutes=hours_to_minutes(recent_hours),
            total_hours_on_record=child_text(game_element, "hoursOnRecord") or None,
        )
        games[app_id] = asdict(game_snapshot)

    return {
        "steam_id64": steam_id64,
        "steam_name": steam_name,
        "profile_xml_url": profile_xml_url,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "games": games,
    }


def load_previous_snapshot(state_file: Path) -> dict[str, Any] | None:
    if not state_file.exists():
        return None
    with state_file.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_snapshot(state_file: Path, snapshot: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with state_file.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)


def format_minutes(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}小时")
    if remainder or not parts:
        parts.append(f"{remainder}分钟")
    return "".join(parts)


def parse_snapshot_datetime(snapshot: dict[str, Any] | None) -> datetime | None:
    if not snapshot:
        return None
    fetched_at = str(snapshot.get("fetched_at_utc", "")).strip()
    if not fetched_at:
        return None
    try:
        return datetime.fromisoformat(fetched_at)
    except ValueError:
        return None


def format_hours_delta(minutes: int) -> str:
    hours_value = (Decimal(minutes) / Decimal("60")).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    hours_text = format(hours_value.normalize(), "f")
    if "." in hours_text:
        hours_text = hours_text.rstrip("0").rstrip(".")
    return f"{hours_text}小时"


def format_capture_range(
    previous_snapshot: dict[str, Any] | None, current_snapshot: dict[str, Any]
) -> str:
    previous_dt = parse_snapshot_datetime(previous_snapshot)
    current_dt = parse_snapshot_datetime(current_snapshot)
    if previous_dt is None or current_dt is None:
        return "上次抓取时间-现在"

    previous_text = previous_dt.astimezone(DISPLAY_TIMEZONE).strftime(DISPLAY_TIME_FORMAT)
    current_text = current_dt.astimezone(DISPLAY_TIMEZONE).strftime(DISPLAY_TIME_FORMAT)
    return f"{previous_text}-{current_text}"


def detect_growth(
    previous_snapshot: dict[str, Any] | None, current_snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    if previous_snapshot is None:
        return []

    previous_steam_id64 = previous_snapshot.get("steam_id64")
    current_steam_id64 = current_snapshot.get("steam_id64")
    if previous_steam_id64 and current_steam_id64 and previous_steam_id64 != current_steam_id64:
        raise RuntimeError("state-file 中保存的账号与当前拉取的 Steam 账号不一致")

    previous_games = previous_snapshot.get("games", {})
    growth_items: list[dict[str, Any]] = []
    for app_id, current_game in current_snapshot.get("games", {}).items():
        previous_game = previous_games.get(app_id, {})
        previous_minutes = int(previous_game.get("recent_minutes", 0))
        current_minutes = int(current_game.get("recent_minutes", 0))
        delta_minutes = current_minutes - previous_minutes
        if delta_minutes <= 0:
            continue

        growth_items.append(
            {
                "app_id": app_id,
                "name": current_game.get("name", app_id),
                "previous_minutes": previous_minutes,
                "current_minutes": current_minutes,
                "delta_minutes": delta_minutes,
            }
        )

    growth_items.sort(key=lambda item: (-item["delta_minutes"], item["name"].lower()))
    return growth_items


def build_push_message(
    previous_snapshot: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
    growth_items: list[dict[str, Any]],
) -> tuple[str, str]:
    profile_name = current_snapshot.get("steam_name") or current_snapshot.get("steam_id64") or "Steam 账号"
    capture_range = format_capture_range(previous_snapshot, current_snapshot)
    title = f"{profile_name} 游戏时长增长"
    lines = [
        f"{profile_name} {item['name']} {capture_range} 游戏时长增加 {format_hours_delta(item['delta_minutes'])}"
        for item in growth_items
    ]
    body = "\n".join(lines)
    return title, body


def send_bark_notification(
    bark_base_url: str, title: str, body: str, timeout: float
) -> tuple[int, str]:
    bark_base_url = bark_base_url.rstrip("/")
    push_url = (
        f"{bark_base_url}/"
        f"{urllib.parse.quote(title, safe='')}/"
        f"{urllib.parse.quote(body, safe='')}"
    )
    request = urllib.request.Request(
        push_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = response.status
        payload = response.read().decode("utf-8", errors="replace")
    if status < 200 or status >= 300:
        raise RuntimeError(f"Bark 推送失败，HTTP {status}: {payload}")
    return status, payload


def print_current_snapshot(current_snapshot: dict[str, Any]) -> None:
    profile_name = current_snapshot.get("steam_name") or current_snapshot.get("steam_id64") or "Steam 账号"
    print(f"当前账号: {profile_name}")
    games = sorted(
        current_snapshot.get("games", {}).values(),
        key=lambda item: (-int(item["recent_minutes"]), item["name"].lower()),
    )
    for game in games:
        total_hours = game.get("total_hours_on_record")
        total_part = f", 总时长 {total_hours} 小时" if total_hours else ""
        print(
            f"- {game['name']}: 近2周 {game['recent_minutes']} 分钟 "
            f"({game['recent_hours']} 小时){total_part}"
        )


def main() -> int:
    args = parse_args()
    current_snapshot = build_current_snapshot(
        fetch_profile_xml(args.profile_xml_url, args.timeout),
        args.profile_xml_url,
    )

    if args.print_current:
        print_current_snapshot(current_snapshot)

    previous_snapshot = load_previous_snapshot(args.state_file)
    growth_items = detect_growth(previous_snapshot, current_snapshot)

    if previous_snapshot is None:
        save_snapshot(args.state_file, current_snapshot)
        print(f"首次运行，已建立基线: {args.state_file}")
        return 0

    if not growth_items:
        save_snapshot(args.state_file, current_snapshot)
        print("最近游戏时长没有增长，本次不推送。")
        return 0

    title, body = build_push_message(previous_snapshot, current_snapshot, growth_items)
    print("检测到增长:")
    print(body)

    if args.dry_run:
        print("dry-run 已开启，跳过 Bark 推送。")
        return 0

    if not args.bark_base_url:
        raise RuntimeError("缺少 Bark 地址，请通过 --bark-base-url 或环境变量 BARK_BASE_URL 提供")

    status, payload = send_bark_notification(
        args.bark_base_url,
        title,
        body,
        args.timeout,
    )
    save_snapshot(args.state_file, current_snapshot)
    print(f"Bark 推送成功，HTTP {status}")
    if payload:
        print(payload)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
