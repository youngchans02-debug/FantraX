#!/usr/bin/env python3
"""Weekly report calculations: round results, luck, power ranking, POTW, preview."""

from __future__ import print_function

import csv
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data_layout import (
    current_path,
    first_existing,
    migrate_legacy_snapshots,
    players_stats_lookup_paths,
    read_json,
    roster_lookup_paths,
)


ROOT = Path(__file__).resolve().parent

PERIOD_7_VALIDATION = (
    ("kms361657", 127.91, "Kangte", 97.72),
    ("eunho", 199.68, "soundwave134", 141.89),
    ("DJSoccerKing", 164.07, "Parapara123", 145.97),
    ("moonland", 178.37, "moonbape", 171.41),
    ("SYnaldo", 161.03, "yeseong", 146.38),
    ("Young FC", 196.21, "hwoojo", 191.03),
    ("KurtZuna", 154.18, "GloryMang", 128.62),
)

POS_LABELS = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_config(path=None):
    config_path = Path(path) if path else ROOT / "config.json"
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


def normalize_player_id(raw):
    if raw is None:
        return ""
    return str(raw).strip().strip("*")


def parse_float(value, default=0.0):
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return default


def actual_win(score, opp_score):
    if score > opp_score:
        return 1.0
    if score < opp_score:
        return 0.0
    return 0.5


def expected_win(score, other_scores):
    if not other_scores:
        return 0.0
    wins = 0.0
    for other in other_scores:
        wins += actual_win(score, other)
    return wins / float(len(other_scores))


def period_has_started(payload):
    """True if any team recorded a game or a non-zero score.

    This is a started-period check, not a completed-period check.
    A genuine 0-point lineup (gamesPlayed 0 and score 0 for one team)
    does not mark the whole period as unstarted.
    """
    for matchup in payload.get("matchups") or []:
        for side in ("home", "away"):
            team = matchup.get(side) or {}
            gp = parse_float(team.get("gamesPlayed"), 0.0)
            score = parse_float(team.get("score"), 0.0)
            if gp > 0 or score != 0:
                return True
    return False


def parse_fantrax_datetime(value):
    """Parse Fantrax timestamps such as 2026-10-23T14:59:59.0-0400."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    text = str(value).strip()
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-])(\d{2}):?(\d{2})$",
        text,
    )
    if match:
        body, fraction, sign, hours, minutes = match.groups()
        if fraction and fraction != ".0":
            body = body + fraction
        offset_minutes = int(hours) * 60 + int(minutes)
        if sign == "-":
            offset_minutes = -offset_minutes
        naive = datetime.fromisoformat(body)
        return naive.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))

    if text.endswith("Z"):
        return datetime.fromisoformat(text[:-1]).replace(tzinfo=timezone.utc)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def utcnow():
    return datetime.now(timezone.utc)


def normalize_now(now):
    if now is None:
        return utcnow()
    parsed = parse_fantrax_datetime(now)
    if parsed is None:
        raise ValueError("Could not parse now={0!r}".format(now))
    return parsed


def scoring_period_window(league_info, period):
    meta = scoring_period_meta(league_info, period) if league_info else {}
    return (
        parse_fantrax_datetime(meta.get("startDate")),
        parse_fantrax_datetime(meta.get("endDate")),
    )


def period_status(period, league_info, payload=None, now=None):
    """Classify a period as upcoming / in_progress / completed using endDate.

    Fantrax getLeagueInfo.scoringPeriods only expose number/startDate/endDate.
    getMatchupScores has no complete/final/status field. gamesPlayed is not
    used here because DNP or an empty lineup can leave a completed week at 0.
    """
    now = normalize_now(now)
    start, end = scoring_period_window(league_info, period)
    started = period_has_started(payload) if payload else False

    if end is not None and now > end:
        state = "completed"
    elif start is not None and now < start:
        state = "upcoming"
    elif start is not None and end is not None and start <= now <= end:
        state = "in_progress"
    else:
        state = "unknown"

    return {
        "period": int(period),
        "state": state,
        "start": start,
        "end": end,
        "now": now,
        "has_started_scoring": started,
        "completed": state == "completed",
    }


def period_is_completed(period, league_info, now=None):
    """True only after Fantrax scoringPeriods.endDate has passed."""
    return period_status(period, league_info, now=now)["completed"]


def completed_periods(league_info, now=None):
    periods = []
    for item in (league_info or {}).get("scoringPeriods") or []:
        number = item.get("number")
        if number is None:
            continue
        number = int(number)
        if period_is_completed(number, league_info, now=now):
            periods.append(number)
    return periods


def latest_completed_period(league_info, now=None, payloads=None):
    """Latest period whose Fantrax scoring window has ended.

    payloads is accepted for call-site convenience but is not used: score /
    gamesPlayed activity cannot prove a gameweek is finished.
    """
    del payloads
    done = completed_periods(league_info, now=now)
    return max(done) if done else None


def extract_sides(payload):
    rows = []
    for matchup in payload.get("matchups") or []:
        home = matchup.get("home") or {}
        away = matchup.get("away") or {}
        if not home.get("teamId") or not away.get("teamId"):
            continue
        rows.append(
            {
                "home": {
                    "teamId": home["teamId"],
                    "teamName": home.get("teamName") or home.get("name") or home["teamId"],
                    "score": parse_float(home.get("score")),
                    "gamesPlayed": parse_float(home.get("gamesPlayed")),
                },
                "away": {
                    "teamId": away["teamId"],
                    "teamName": away.get("teamName") or away.get("name") or away["teamId"],
                    "score": parse_float(away.get("score")),
                    "gamesPlayed": parse_float(away.get("gamesPlayed")),
                },
            }
        )
    return rows


def team_scores_from_payload(payload):
    scores = {}
    for row in extract_sides(payload):
        for side in ("home", "away"):
            team = row[side]
            scores[team["teamId"]] = team
    return scores


def load_period_payloads(data_dir):
    data_dir = Path(data_dir)
    payloads = {}

    slim_path = data_dir / "matchup_scores_slim.json"
    all_path = data_dir / "all_matchup_scores.json"
    if slim_path.exists():
        raw = load_json(slim_path)
        for key, payload in raw.items():
            payloads[int(key)] = payload
    elif all_path.exists():
        raw = load_json(all_path)
        for key, payload in raw.items():
            payloads[int(key)] = payload

    matchups_dir = data_dir / "matchups"
    if matchups_dir.exists():
        for path in matchups_dir.glob("period_*.json"):
            match = re.search(r"period_(\d+)\.json$", path.name)
            if not match:
                continue
            period = int(match.group(1))
            payloads[period] = load_json(path)

    historical_root = data_dir / "historical"
    if historical_root.exists():
        for path in historical_root.glob("period_*/matchups.json"):
            match = re.search(r"period_(\d+)", path.parent.name)
            if not match:
                continue
            payloads[int(match.group(1))] = load_json(path)

    return dict(sorted(payloads.items()))


def started_periods(payloads):
    return [period for period, payload in sorted(payloads.items()) if period_has_started(payload)]


def latest_started_period(payloads):
    started = started_periods(payloads)
    return started[-1] if started else None


def build_team_directory(league_info=None, standings=None, rosters=None):
    teams = {}

    def upsert(team_id, team_name=None, short_name=None):
        if not team_id:
            return
        entry = teams.setdefault(
            team_id,
            {"teamId": team_id, "teamName": team_id, "shortNames": []},
        )
        if team_name:
            entry["teamName"] = team_name
        if short_name and short_name not in entry["shortNames"]:
            entry["shortNames"].append(short_name)

    if league_info:
        for team_id, info in (league_info.get("teamInfo") or {}).items():
            upsert(team_id, info.get("name"))
        for period in league_info.get("matchups") or []:
            for matchup in period.get("matchupList") or []:
                for side in ("home", "away"):
                    team = matchup.get(side) or {}
                    upsert(team.get("id"), team.get("name"), team.get("shortName"))

    if isinstance(standings, list):
        for row in standings:
            upsert(row.get("teamId"), row.get("teamName"))

    if rosters and isinstance(rosters.get("rosters"), dict):
        for team_id, team in rosters["rosters"].items():
            upsert(team_id, team.get("teamName"))

    return teams


def display_name(teams, team_id, fallback=None):
    entry = teams.get(team_id) or {}
    return entry.get("teamName") or fallback or team_id


def short_name_index(teams):
    index = {}
    for team_id, entry in teams.items():
        index[entry.get("teamName", "").lower()] = team_id
        for short in entry.get("shortNames") or []:
            index[short.lower()] = team_id
    return index


def scoring_period_meta(league_info, period):
    for item in (league_info or {}).get("scoringPeriods") or []:
        if int(item.get("number") or 0) == int(period):
            return item
    return {}


def schedule_for_period(league_info, period):
    for item in (league_info or {}).get("matchups") or []:
        if int(item.get("period") or 0) == int(period):
            return item.get("matchupList") or []
    return []


def compute_round_results(payload, teams=None):
    teams = teams or {}
    matchups = []
    team_rows = []

    for row in extract_sides(payload):
        home = row["home"]
        away = row["away"]
        margin = abs(home["score"] - away["score"])
        if home["score"] > away["score"]:
            winner_id = home["teamId"]
            result = "home"
        elif away["score"] > home["score"]:
            winner_id = away["teamId"]
            result = "away"
        else:
            winner_id = None
            result = "tie"

        matchup = {
            "home": dict(home),
            "away": dict(away),
            "margin": margin,
            "result": result,
            "winnerId": winner_id,
        }
        matchups.append(matchup)
        team_rows.extend((home, away))

    if not team_rows:
        return {
            "matchups": [],
            "highest": [],
            "lowest": [],
            "largest_margin": [],
            "closest": [],
            "started": False,
        }

    high = max(team["score"] for team in team_rows)
    low = min(team["score"] for team in team_rows)
    largest = max(m["margin"] for m in matchups)
    closest = min(m["margin"] for m in matchups)

    def labeled(team):
        return {
            "teamId": team["teamId"],
            "teamName": display_name(teams, team["teamId"], team["teamName"]),
            "score": team["score"],
            "gamesPlayed": team["gamesPlayed"],
        }

    return {
        "matchups": matchups,
        "highest": [labeled(t) for t in team_rows if t["score"] == high],
        "lowest": [labeled(t) for t in team_rows if t["score"] == low],
        "largest_margin": [m for m in matchups if m["margin"] == largest],
        "closest": [m for m in matchups if m["margin"] == closest],
        "started": period_has_started(payload),
    }


def compute_luck(payloads, upto_period, teams=None):
    teams = teams or {}
    started = [p for p in started_periods(payloads) if p <= upto_period]
    by_team = {}

    for period in started:
        scores = team_scores_from_payload(payloads[period])
        all_scores = {tid: row["score"] for tid, row in scores.items()}
        opponents = {}
        for row in extract_sides(payloads[period]):
            opponents[row["home"]["teamId"]] = row["away"]
            opponents[row["away"]["teamId"]] = row["home"]

        for team_id, row in scores.items():
            others = [score for tid, score in all_scores.items() if tid != team_id]
            exp = expected_win(row["score"], others)
            opp = opponents.get(team_id)
            act = actual_win(row["score"], opp["score"]) if opp else 0.0
            luck = act - exp
            entry = by_team.setdefault(
                team_id,
                {
                    "teamId": team_id,
                    "teamName": display_name(teams, team_id, row["teamName"]),
                    "rounds": [],
                    "season_luck": 0.0,
                    "season_actual": 0.0,
                    "season_expected": 0.0,
                },
            )
            entry["rounds"].append(
                {
                    "period": period,
                    "score": row["score"],
                    "opponentId": opp["teamId"] if opp else None,
                    "opponentName": display_name(teams, opp["teamId"], opp["teamName"]) if opp else None,
                    "opponentScore": opp["score"] if opp else None,
                    "actual_win": act,
                    "expected_win": exp,
                    "luck": luck,
                }
            )
            entry["season_luck"] += luck
            entry["season_actual"] += act
            entry["season_expected"] += exp

    current = []
    season = []
    for entry in by_team.values():
        current_round = next((r for r in entry["rounds"] if r["period"] == upto_period), None)
        season.append(entry)
        if current_round:
            current.append(
                {
                    "teamId": entry["teamId"],
                    "teamName": entry["teamName"],
                    "score": current_round["score"],
                    "opponentId": current_round["opponentId"],
                    "opponentName": current_round["opponentName"],
                    "opponentScore": current_round["opponentScore"],
                    "actual_win": current_round["actual_win"],
                    "expected_win": current_round["expected_win"],
                    "round_luck": current_round["luck"],
                    "season_luck": entry["season_luck"],
                }
            )

    current.sort(key=lambda x: (x["round_luck"], x["score"]))
    season.sort(key=lambda x: x["season_luck"])

    unlucky = current[0] if current else None
    lucky = current[-1] if current else None
    return {
        "period": upto_period,
        "rounds_used": started,
        "current": sorted(current, key=lambda x: x["round_luck"]),
        "season": season,
        "unlucky_king": unlucky,
        "lucky_king": lucky,
    }


def _minmax(values):
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return [50.0] * len(values)
    return [((v - lo) / (hi - lo)) * 100.0 for v in values]


def compute_power_ranking(payloads, upto_period, config=None, teams=None, standings=None):
    teams = teams or {}
    cfg = (config or {}).get("power_ranking") or {}
    w_season = float(cfg.get("weight_season_avg", 0.5))
    w_recent = float(cfg.get("weight_recent_avg", 0.3))
    w_exp = float(cfg.get("weight_expected_win", 0.2))
    recent_n = int(cfg.get("recent_rounds", 3))
    normalize = bool(cfg.get("normalize_components", True))
    weight_sum = w_season + w_recent + w_exp
    if weight_sum <= 0:
        raise ValueError("Power ranking weights must sum to a positive number.")
    w_season, w_recent, w_exp = w_season / weight_sum, w_recent / weight_sum, w_exp / weight_sum

    started = [p for p in started_periods(payloads) if p <= upto_period]
    if not started:
        return {"period": upto_period, "rankings": [], "previous": [], "weights": cfg}

    history = defaultdict(list)
    for period in started:
        scores = team_scores_from_payload(payloads[period])
        all_scores = {tid: row["score"] for tid, row in scores.items()}
        for team_id, row in scores.items():
            others = [score for tid, score in all_scores.items() if tid != team_id]
            history[team_id].append(
                {
                    "period": period,
                    "score": row["score"],
                    "expected_win": expected_win(row["score"], others),
                    "teamName": row["teamName"],
                }
            )

    standings_map = {}
    if isinstance(standings, list):
        for row in standings:
            standings_map[row.get("teamId")] = row

    raw = []
    for team_id, rounds in history.items():
        season_scores = [r["score"] for r in rounds]
        recent_scores = [r["score"] for r in rounds[-recent_n:]]
        exp_rates = [r["expected_win"] for r in rounds]
        raw.append(
            {
                "teamId": team_id,
                "teamName": display_name(teams, team_id, rounds[-1]["teamName"]),
                "season_avg": sum(season_scores) / float(len(season_scores)),
                "recent_avg": sum(recent_scores) / float(len(recent_scores)),
                "expected_win_pct": sum(exp_rates) / float(len(exp_rates)),
                "rounds": len(rounds),
                "standings_rank": (standings_map.get(team_id) or {}).get("rank"),
                "record": (standings_map.get(team_id) or {}).get("points"),
            }
        )

    if normalize:
        season_n = _minmax([r["season_avg"] for r in raw])
        recent_n_vals = _minmax([r["recent_avg"] for r in raw])
        exp_n = _minmax([r["expected_win_pct"] for r in raw])
        for row, s, r, e in zip(raw, season_n, recent_n_vals, exp_n):
            row["component_season"] = s
            row["component_recent"] = r
            row["component_expected_win"] = e
            row["power_score"] = w_season * s + w_recent * r + w_exp * e
    else:
        for row in raw:
            row["component_season"] = row["season_avg"]
            row["component_recent"] = row["recent_avg"]
            row["component_expected_win"] = row["expected_win_pct"] * 100.0
            row["power_score"] = (
                w_season * row["component_season"]
                + w_recent * row["component_recent"]
                + w_exp * row["component_expected_win"]
            )

    raw.sort(key=lambda x: (-x["power_score"], -x["season_avg"], x["teamName"]))
    for idx, row in enumerate(raw, start=1):
        row["rank"] = idx

    previous = []
    prev_period = started[-2] if len(started) >= 2 else None
    if prev_period is not None:
        previous = compute_power_ranking(
            payloads,
            prev_period,
            config=config,
            teams=teams,
            standings=None,
        )["rankings"]
        prev_rank = {row["teamId"]: row["rank"] for row in previous}
        for row in raw:
            old = prev_rank.get(row["teamId"])
            row["prev_rank"] = old
            row["rank_change"] = (old - row["rank"]) if old is not None else None
    else:
        for row in raw:
            row["prev_rank"] = None
            row["rank_change"] = None

    return {
        "period": upto_period,
        "rounds_used": started,
        "weights": {
            "season_avg": w_season,
            "recent_avg": w_recent,
            "expected_win": w_exp,
            "recent_rounds": recent_n,
            "normalize_components": normalize,
        },
        "rankings": raw,
        "previous": previous,
    }


def load_players_json(path):
    path = Path(path)
    if not path.exists():
        return []
    data = load_json(path)
    rows = data.get("players") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    players = []
    for raw in rows:
        players.append(
            {
                "id": normalize_player_id(raw.get("id") or raw.get("ID")),
                "player": (raw.get("player") or raw.get("name") or "").strip(),
                "epl_team": (raw.get("epl_team") or raw.get("Team") or "").strip(),
                "position": (raw.get("position") or "").strip().upper()[:1],
                "status": (raw.get("status") or "").strip(),
                "opponent": (raw.get("opponent") or "").strip(),
                "fpts": parse_float(raw.get("fpts") or raw.get("FPts")),
                "fp_g": parse_float(raw.get("fp_g") or raw.get("FP/G")),
                "icons": raw.get("icons") or [],
                "eligible_positions": raw.get("eligible_positions") or raw.get("eligiblePositions") or [],
                "stats": raw.get("stats") or {},
                "raw": raw,
            }
        )
    return [p for p in players if p["id"]]


def load_player_records(json_path, csv_path):
    json_players = load_players_json(json_path)
    if json_players:
        return json_players, "json:{0}".format(Path(json_path).name)
    csv_players = load_players_csv(csv_path)
    if csv_players:
        return csv_players, "csv:{0}".format(Path(csv_path).name)
    return [], None


def load_players_csv(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for raw in reader:
            player_id = normalize_player_id(raw.get("ID") or raw.get("Player ID") or raw.get("id"))
            rows.append(
                {
                    "id": player_id,
                    "player": (raw.get("Player") or "").strip(),
                    "epl_team": (raw.get("Team") or "").strip(),
                    "position": (raw.get("Position") or raw.get("Pos") or "").strip().upper()[:1],
                    "status": (raw.get("Status") or "").strip(),
                    "opponent": (raw.get("Opponent") or "").strip(),
                    "fpts": parse_float(raw.get("FPts")),
                    "fp_g": parse_float(raw.get("FP/G")),
                    "raw": raw,
                }
            )
        return rows


def compute_potw(players, teams=None, rosters=None, source=None):
    teams = teams or {}
    if not players:
        return {
            "available": False,
            "reason": "기간별 선수 FPts가 없습니다. Players API 또는 input/players_gw.csv 가 필요합니다.",
        }

    scored = [p for p in players if p["fpts"] != 0 or p["player"]]
    if not scored:
        return {"available": False, "reason": "기간별 선수 FPts가 비어 있습니다."}

    owner_by_id = {}
    if rosters and isinstance(rosters.get("rosters"), dict):
        for team_id, team in rosters["rosters"].items():
            for item in team.get("rosterItems") or []:
                owner_by_id[normalize_player_id(item.get("id"))] = {
                    "teamId": team_id,
                    "teamName": display_name(teams, team_id, team.get("teamName")),
                    "status": item.get("status"),
                }
    if rosters and isinstance(rosters.get("teams"), list):
        for team in rosters["teams"]:
            team_id = team.get("teamId")
            for item in team.get("players") or []:
                owner_by_id[normalize_player_id(item.get("playerId"))] = {
                    "teamId": team_id,
                    "teamName": display_name(teams, team_id, team.get("teamName")),
                    "status": item.get("fantasyStatus"),
                }

    name_index = short_name_index(teams)

    def decorate(player):
        owner = owner_by_id.get(player["id"])
        status = player.get("status") or ""
        status_team_id = name_index.get(re.sub(r"<[^>]+>", "", status).strip().lower())
        if not owner and status_team_id:
            owner = {
                "teamId": status_team_id,
                "teamName": display_name(teams, status_team_id),
                "status": None,
            }
        return {
            "id": player["id"],
            "player": player["player"],
            "epl_team": player["epl_team"],
            "position": player["position"],
            "position_label": POS_LABELS.get(player["position"], player["position"] or "?"),
            "fpts": player["fpts"],
            "fp_g": player["fp_g"],
            "csv_status": status,
            "fantasy_team": (owner or {}).get("teamName"),
            "fantasy_team_id": (owner or {}).get("teamId"),
        }

    decorated = [decorate(p) for p in scored]
    by_pos = {}
    for pos in ("G", "D", "M", "F"):
        group = [p for p in decorated if p["position"] == pos]
        group.sort(key=lambda x: (-x["fpts"], x["player"]))
        by_pos[pos] = group[0] if group else None

    overall = max(decorated, key=lambda x: (x["fpts"], x["player"]))
    return {
        "available": True,
        "by_position": by_pos,
        "overall": overall,
        "source": source or "players_gw.csv",
    }


def historical_score_sigma(payloads, upto_period):
    values = []
    for period in started_periods(payloads):
        if period > upto_period:
            continue
        for team in team_scores_from_payload(payloads[period]).values():
            values.append(team["score"])
    if len(values) < 2:
        return 25.0
    mean = sum(values) / float(len(values))
    var = sum((v - mean) ** 2 for v in values) / float(len(values) - 1)
    return max(math.sqrt(var), 8.0)


def compute_next_preview(league_info, rosters, season_players, payloads, period, config=None, teams=None):
    teams = teams or {}
    next_period = period + 1
    schedule = schedule_for_period(league_info, next_period)
    if not schedule:
        return {"available": False, "period": next_period, "reason": "다음 period 스케줄이 league_info에 없습니다."}

    players_by_id = {p["id"]: p for p in (season_players or []) if p.get("id")}
    roster_map = (rosters or {}).get("rosters") or {}
    projected = {}
    missing = {}

    for team_id, team in roster_map.items():
        total = 0.0
        counted = 0
        unmatched = 0
        used = []
        for item in team.get("rosterItems") or []:
            if (item.get("status") or "").upper() != "ACTIVE":
                continue
            pid = normalize_player_id(item.get("id"))
            player = players_by_id.get(pid)
            if not player:
                unmatched += 1
                continue
            total += player["fp_g"]
            counted += 1
            used.append(
                {
                    "id": pid,
                    "player": player["player"],
                    "position": player["position"],
                    "fp_g": player["fp_g"],
                }
            )
        projected[team_id] = {
            "teamId": team_id,
            "teamName": display_name(teams, team_id, team.get("teamName")),
            "expected_score": total,
            "active_used": counted,
            "active_unmatched": unmatched,
            "players": used,
        }
        if unmatched:
            missing[team_id] = unmatched

    cfg = (config or {}).get("preview") or {}
    sigma = cfg.get("score_sigma")
    sigma = float(sigma) if sigma else historical_score_sigma(payloads, period)

    matchups = []
    for item in schedule:
        home = item.get("home") or {}
        away = item.get("away") or {}
        home_id = home.get("id")
        away_id = away.get("id")
        home_proj = projected.get(home_id) or {
            "teamId": home_id,
            "teamName": home.get("name"),
            "expected_score": None,
            "active_used": 0,
            "active_unmatched": 0,
        }
        away_proj = projected.get(away_id) or {
            "teamId": away_id,
            "teamName": away.get("name"),
            "expected_score": None,
            "active_used": 0,
            "active_unmatched": 0,
        }
        h_exp = home_proj.get("expected_score")
        a_exp = away_proj.get("expected_score")
        home_win_p = None
        if h_exp is not None and a_exp is not None:
            home_win_p = 1.0 / (1.0 + math.exp(-(h_exp - a_exp) / sigma))
        matchups.append(
            {
                "home": home_proj,
                "away": away_proj,
                "home_win_prob": home_win_p,
                "away_win_prob": (1.0 - home_win_p) if home_win_p is not None else None,
                "expected_margin": (h_exp - a_exp) if h_exp is not None and a_exp is not None else None,
            }
        )

    watch = None
    scored = [m for m in matchups if m["home_win_prob"] is not None]
    if scored:
        watch = min(scored, key=lambda m: abs(m["home_win_prob"] - 0.5))

    return {
        "available": True,
        "period": next_period,
        "has_projections": bool(season_players),
        "sigma": sigma,
        "matchups": matchups,
        "watch": watch,
        "unmatched_active": missing,
        "source": "players_season + ACTIVE rosters" if season_players else "league_info schedule only",
    }


def validate_period_7(payload, tolerance=0.011):
    rows = extract_sides(payload)
    remaining = list(PERIOD_7_VALIDATION)
    unmatched = []
    for row in rows:
        pair = {row["home"]["teamName"]: row["home"]["score"], row["away"]["teamName"]: row["away"]["score"]}
        found = None
        for expected in remaining:
            a_name, a_score, b_name, b_score = expected
            if a_name in pair and b_name in pair:
                if abs(pair[a_name] - a_score) <= tolerance and abs(pair[b_name] - b_score) <= tolerance:
                    found = expected
                    break
        if found:
            remaining.remove(found)
        else:
            unmatched.append(pair)
    return {
        "ok": not remaining and not unmatched,
        "missing": remaining,
        "unmatched": unmatched,
        "matchup_count": len(rows),
    }


def load_report_inputs(config=None, data_dir=None):
    config = config or load_config()
    paths = config.get("paths") or {}
    data_dir = Path(data_dir or paths.get("data_dir") or "fantrax_data")
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir

    def optional_json(name):
        path = data_dir / name
        return load_json(path) if path.exists() else None

    migrate_legacy_snapshots(data_dir)
    league_info = read_json(current_path(data_dir, "league_info.json")) or optional_json("league_info.json") or {}
    current_rosters = read_json(current_path(data_dir, "team_rosters.json")) or optional_json("team_rosters.json") or {}
    standings = read_json(current_path(data_dir, "standings.json")) or optional_json("standings.json") or []
    payloads = load_period_payloads(data_dir)
    teams = build_team_directory(league_info, standings, current_rosters)

    season_csv = ROOT / paths.get("players_season_csv", "input/players_season.csv")
    gw_csv = ROOT / paths.get("players_gw_csv", "input/players_gw.csv")
    draft_csv = ROOT / paths.get("draft_csv", "input/draft.csv")
    season_json = first_existing(current_path(data_dir, "players_season.json"), data_dir / "players_season.json")
    season_players, season_source = load_player_records(season_json, season_csv) if season_json else ([], None)

    period_players = {}
    period_sources = {}
    seen_periods = set()
    for path in sorted(data_dir.glob("historical/period_*/players_stats.json")):
        match = re.search(r"period_(\d+)", path.parent.name)
        if not match:
            continue
        number = int(match.group(1))
        period_players[number] = load_players_json(path)
        period_sources[number] = "historical/period_{0}/players_stats.json".format(number)
        seen_periods.add(number)
    for path in sorted(data_dir.glob("players_period_*.json")):
        match = re.search(r"players_period_(\d+)\.json$", path.name)
        if not match:
            continue
        number = int(match.group(1))
        if number in seen_periods:
            continue
        period_players[number] = load_players_json(path)
        period_sources[number] = "json:{0}".format(path.name)

    return {
        "config": config,
        "data_dir": data_dir,
        "league_info": league_info,
        "rosters": None,
        "current_rosters": current_rosters,
        "standings": standings,
        "payloads": payloads,
        "teams": teams,
        "season_players": season_players,
        "season_source": season_source,
        "gw_players": load_players_csv(gw_csv),
        "period_players": period_players,
        "period_sources": period_sources,
        "draft_csv_exists": draft_csv.exists(),
        "paths": {
            "season_csv": season_csv,
            "gw_csv": gw_csv,
            "draft_csv": draft_csv,
            "season_json": season_json or data_dir / "players_season.json",
        },
    }


def build_weekly_model(inputs, period=None, now=None):
    payloads = inputs["payloads"]
    if not payloads:
        raise RuntimeError("No matchup payloads found. Run fantrax_report_fetcher.py first.")

    league_info = inputs["league_info"]
    completed = latest_completed_period(league_info, now=now)
    requested = period
    period_note = None

    if period is None:
        if completed is None:
            raise RuntimeError(
                "No scoring period has passed its Fantrax endDate yet. "
                "Pass --period N to generate a report for an in-progress round."
            )
        result_period = completed
    else:
        result_period = period
        if period not in payloads:
            raise RuntimeError("No matchup JSON for period {0}.".format(period))
        status = period_status(period, league_info, payloads.get(period), now=now)
        if status["completed"]:
            result_period = period
        elif status["state"] == "upcoming" or not period_has_started(payloads[period]):
            if completed is None:
                raise RuntimeError("Requested period {0} has not started and no completed period exists.".format(period))
            result_period = completed
            period_note = (
                "Requested period {0} has not started. "
                "Report uses latest completed period {1}."
            ).format(period, result_period)
        elif status["state"] == "in_progress":
            end_label = status["end"].isoformat() if status["end"] else "unknown"
            period_note = (
                "Requested period {0} is still in progress "
                "(Fantrax scoring window ends {1})."
            ).format(period, end_label)

    if result_period not in payloads:
        raise RuntimeError("No matchup JSON for period {0}.".format(result_period))

    teams = inputs["teams"]
    config = inputs["config"]
    payload = payloads[result_period]
    validation = validate_period_7(payload) if result_period == 7 else None
    result_status = period_status(result_period, league_info, payload, now=now)

    gw_players = inputs["period_players"].get(result_period) or []
    gw_source = inputs["period_sources"].get(result_period)
    if not gw_players:
        stats_path = first_existing(*players_stats_lookup_paths(inputs["data_dir"], result_period))
        if stats_path:
            gw_players = load_players_json(stats_path)
            gw_source = stats_path.name
    if not gw_players:
        gw_players = inputs["gw_players"]
        gw_source = "csv:players_gw.csv" if gw_players else None

    roster_path = first_existing(*roster_lookup_paths(inputs["data_dir"], result_period))
    historical_roster = load_json(roster_path) if roster_path else None

    return {
        "league_name": league_info.get("leagueName") or config.get("league_name") or "Fantrax League",
        "requested_period": requested,
        "period": result_period,
        "period_note": period_note,
        "period_meta": scoring_period_meta(league_info, result_period),
        "period_status": result_status,
        "started_periods": started_periods(payloads),
        "completed_periods": completed_periods(league_info, now=now),
        "latest_completed_period": completed,
        "round_results": compute_round_results(payload, teams),
        "luck": compute_luck(payloads, result_period, teams),
        "power_ranking": compute_power_ranking(
            payloads,
            result_period,
            config=config,
            teams=teams,
            standings=inputs["standings"],
        ),
        "potw": compute_potw(gw_players, teams=teams, rosters=historical_roster, source=gw_source),
        "preview": None,
        "historical_roster": historical_roster,
        "validation": validation,
        "player_sources": {
            "season": inputs.get("season_source"),
            "potw": gw_source,
        },
        "csv": {
            "season": bool(inputs["season_players"]),
            "gw": bool(gw_players),
            "draft": inputs["draft_csv_exists"],
        },
    }
