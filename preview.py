#!/usr/bin/env python3
"""Next-round preview from CURRENT Fantrax data only.

Never reads historical/period_N rosters or historical season stats.
"""

from __future__ import print_function

from analytics import (
    build_team_directory,
    compute_next_preview,
    latest_completed_period,
    load_players_json,
    period_status,
)
from data_layout import (
    age_hours,
    current_path,
    preview_path,
    utc_now_iso,
    write_json,
)


def _season_players(current_bundle):
    out_dir = current_bundle.get("out_dir")
    if out_dir is not None:
        path = current_path(out_dir, "players_season.json")
        rows = load_players_json(path)
        if rows:
            return rows
    raw = current_bundle.get("season_players") or {}
    return _records_from_payload(raw)


def _records_from_payload(raw):
    from analytics import normalize_player_id, parse_float

    rows = raw.get("players") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    players = []
    for item in rows:
        player_id = normalize_player_id(item.get("id") or item.get("ID"))
        if not player_id:
            continue
        players.append(
            {
                "id": player_id,
                "player": (item.get("player") or item.get("name") or "").strip(),
                "epl_team": (item.get("epl_team") or item.get("Team") or "").strip(),
                "position": (item.get("position") or "").strip().upper()[:1],
                "fpts": parse_float(item.get("fpts") or item.get("FPts")),
                "fp_g": parse_float(item.get("fp_g") or item.get("FP/G")),
            }
        )
    return players


def _warn_if_stale(current_bundle, config):
    max_age = ((config or {}).get("preview") or {}).get("max_cache_age_hours")
    if max_age is None:
        max_age = 12
    warnings = []
    metadata = current_bundle.get("metadata") or {}
    sources = metadata.get("sources") or {}
    for key, label in (("players", "players_season.json"), ("rosters", "team_rosters.json")):
        fetched = (sources.get(key) or {}).get("fetchedAt") or current_bundle.get("fetched_at")
        hours = age_hours(fetched)
        cached = (sources.get(key) or {}).get("cached") or current_bundle.get("used_cache")
        if cached and hours is not None and hours > float(max_age):
            msg = "WARNING: {0} is {1:.1f} hours old. Preview may be using stale data.".format(label, hours)
            print(msg)
            warnings.append(msg)
    return warnings


def build_next_round_preview(next_period, current_bundle, payloads=None, report_period=None, now=None):
    """Project GW N+1 from a current-data bundle. Writes previews/period_N_preview.json."""
    config = current_bundle.get("config") or {}
    out_dir = current_bundle.get("out_dir")
    league_info = current_bundle.get("league_info") or {}
    rosters = current_bundle.get("rosters") or {}
    standings = current_bundle.get("standings") or []
    teams = build_team_directory(league_info, standings, rosters)
    season_players = _season_players(current_bundle)
    report_period = int(report_period if report_period is not None else int(next_period) - 1)
    next_period = int(next_period)

    stale_warnings = _warn_if_stale(current_bundle, config)
    raw = compute_next_preview(
        league_info,
        rosters,
        season_players,
        payloads or {},
        report_period,
        config=config,
        teams=teams,
    )

    metadata = current_bundle.get("metadata") or {}
    sources_meta = metadata.get("sources") or {}
    fetched_at = current_bundle.get("fetched_at") or metadata.get("fetchedAt")
    roster_fetched = (sources_meta.get("rosters") or {}).get("fetchedAt") or fetched_at
    stats_fetched = (sources_meta.get("players") or {}).get("fetchedAt") or fetched_at
    standings_fetched = (sources_meta.get("standings") or {}).get("fetchedAt") or fetched_at
    matchups_fetched = (sources_meta.get("next_matchups") or {}).get("fetchedAt") or fetched_at

    current_fantrax_period = infer_bundle_period(current_bundle)
    completed = latest_completed_period(league_info, now=now)
    next_status = period_status(next_period, league_info, now=now)
    lineup_not_locked = next_status.get("state") in ("upcoming", "unknown") or next_status.get("state") == "in_progress"

    used_cache = bool(current_bundle.get("used_cache")) or not bool(current_bundle.get("fresh"))
    if current_bundle.get("fresh"):
        used_cache = False
    warning = current_bundle.get("warning")
    if stale_warnings:
        extra = " ".join(stale_warnings)
        warning = "{0} {1}".format(warning, extra).strip() if warning else extra

    flat_matchups = []
    for row in raw.get("matchups") or []:
        home = row.get("home") or {}
        away = row.get("away") or {}
        flat_matchups.append(
            {
                "teamA": home.get("teamName"),
                "teamB": away.get("teamName"),
                "teamAId": home.get("teamId"),
                "teamBId": away.get("teamId"),
                "teamAProjectedScore": home.get("expected_score"),
                "teamBProjectedScore": away.get("expected_score"),
                "teamAWinProbability": None if row.get("home_win_prob") is None else row.get("home_win_prob") * 100.0,
                "teamBWinProbability": None if row.get("away_win_prob") is None else row.get("away_win_prob") * 100.0,
                "home": home,
                "away": away,
                "home_win_prob": row.get("home_win_prob"),
                "away_win_prob": row.get("away_win_prob"),
                "expected_margin": row.get("expected_margin"),
            }
        )

    package = {
        "previewPeriod": next_period,
        "generatedAt": utc_now_iso(),
        "sourceDataFetchedAt": fetched_at,
        "dataFetchedAt": fetched_at,
        "lineupNotLocked": bool(lineup_not_locked),
        "rosterSource": "current getTeamRosters",
        "projectionType": "current-active-roster",
        "currentFantraxPeriod": current_fantrax_period,
        "latestCompletedPeriod": completed,
        "playerStatsThroughPeriod": report_period,
        "rosterFetchedAt": roster_fetched,
        "playerStatsFetchedAt": stats_fetched,
        "freshDataUsed": bool(current_bundle.get("fresh")),
        "usedCachedData": used_cache,
        "warning": warning,
        "sources": current_bundle.get("paths") or {
            "rosters": "current/team_rosters.json",
            "players": "current/players_season.json",
            "standings": "current/standings.json",
        },
        "sourceMetadata": {
            "rostersFetchedAt": roster_fetched,
            "playerStatsFetchedAt": stats_fetched,
            "standingsFetchedAt": standings_fetched,
            "matchupsFetchedAt": matchups_fetched,
        },
        "available": raw.get("available"),
        "has_projections": raw.get("has_projections"),
        "sigma": raw.get("sigma"),
        "reason": raw.get("reason"),
        "watch": raw.get("watch"),
        "unmatched_active": raw.get("unmatched_active"),
        "source": "current roster + current season FP/G + next matchup",
        "matchups": flat_matchups,
        "period": next_period,
    }

    if out_dir is not None:
        path = preview_path(out_dir, next_period)
        write_json(path, package)
        package["path"] = str(path)
        print("Wrote {0}".format(path))
    return package


def infer_bundle_period(current_bundle):
    rosters = current_bundle.get("rosters") or {}
    if rosters.get("period") is not None:
        return int(rosters["period"])
    meta = current_bundle.get("metadata") or {}
    if meta.get("currentFantraxPeriod") is not None:
        return int(meta["currentFantraxPeriod"])
    next_matchups = current_bundle.get("next_matchups") or {}
    if next_matchups.get("period") is not None:
        return int(next_matchups["period"])
    return None
