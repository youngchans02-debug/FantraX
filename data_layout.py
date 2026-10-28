#!/usr/bin/env python3
"""Historical vs current vs preview file locations.

Past stays past. Current stays current.
"""

from __future__ import print_function

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def historical_dir(data_dir, period):
    return Path(data_dir) / "historical" / "period_{0}".format(int(period))


def historical_path(data_dir, period, name):
    return historical_dir(data_dir, period) / name


def current_dir(data_dir):
    return Path(data_dir) / "current"


def current_path(data_dir, name):
    return current_dir(data_dir) / name


def preview_path(data_dir, period):
    return Path(data_dir) / "previews" / "period_{0}_preview.json".format(int(period))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path):
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _copy_if_needed(src, dest):
    src = Path(src)
    dest = Path(dest)
    if not src.exists() or dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dest))
    return True


def migrate_legacy_snapshots(data_dir):
    """Copy legacy layout into historical/current without deleting originals."""
    data_dir = Path(data_dir)
    copied = []

    matchups_dir = data_dir / "matchups"
    if matchups_dir.exists():
        for path in matchups_dir.glob("period_*.json"):
            try:
                period = int(path.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            dest = historical_path(data_dir, period, "matchups.json")
            if _copy_if_needed(path, dest):
                copied.append(str(dest))

    rosters_dir = data_dir / "rosters"
    if rosters_dir.exists():
        for path in rosters_dir.glob("period_*_rosters.json"):
            try:
                period = int(path.name.split("_")[1])
            except (IndexError, ValueError):
                continue
            dest = historical_path(data_dir, period, "rosters.json")
            if _copy_if_needed(path, dest):
                copied.append(str(dest))

    players_dir = data_dir / "players"
    if players_dir.exists():
        for path in players_dir.glob("period_*_players.json"):
            try:
                period = int(path.name.split("_")[1])
            except (IndexError, ValueError):
                continue
            dest = historical_path(data_dir, period, "players.json")
            if _copy_if_needed(path, dest):
                copied.append(str(dest))

    analysis_dir = data_dir / "manager_analysis"
    if analysis_dir.exists():
        for path in analysis_dir.glob("period_*.json"):
            name = path.name
            if name.endswith("_summary.json"):
                try:
                    period = int(name.replace("period_", "").replace("_summary.json", ""))
                except ValueError:
                    continue
                dest = historical_path(data_dir, period, "summary.json")
            elif name.startswith("period_") and name.endswith(".json") and "_summary" not in name:
                try:
                    period = int(name.replace("period_", "").replace(".json", ""))
                except ValueError:
                    continue
                dest = historical_path(data_dir, period, "manager_analysis.json")
            else:
                continue
            if _copy_if_needed(path, dest):
                copied.append(str(dest))

    for path in data_dir.glob("players_period_*.json"):
        try:
            period = int(path.stem.replace("players_period_", ""))
        except ValueError:
            continue
        dest = historical_path(data_dir, period, "players_stats.json")
        if _copy_if_needed(path, dest):
            copied.append(str(dest))

    for name in ("league_info.json", "team_rosters.json", "standings.json", "players_season.json", "player_ids.json"):
        src = data_dir / name
        dest = current_path(data_dir, name)
        if _copy_if_needed(src, dest):
            copied.append(str(dest))

    return copied


def first_existing(*paths):
    for path in paths:
        if path is None:
            continue
        path = Path(path)
        if path.exists():
            return path
    return None


def roster_lookup_paths(data_dir, period):
    return [
        historical_path(data_dir, period, "rosters.json"),
        Path(data_dir) / "rosters" / "period_{0}_rosters.json".format(int(period)),
    ]


def players_stats_lookup_paths(data_dir, period):
    return [
        historical_path(data_dir, period, "players_stats.json"),
        Path(data_dir) / "players_period_{0}.json".format(int(period)),
    ]


def matchup_lookup_paths(data_dir, period):
    return [
        historical_path(data_dir, period, "matchups.json"),
        Path(data_dir) / "matchups" / "period_{0}.json".format(int(period)),
    ]


def parse_iso_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_hours(fetched_at, now=None):
    parsed = parse_iso_datetime(fetched_at)
    if parsed is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - parsed).total_seconds() / 3600.0
