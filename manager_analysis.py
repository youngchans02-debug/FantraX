#!/usr/bin/env python3
"""GW lineup snapshots and manager-decision calculations.

Cursor writes source data + numbers. ChatGPT later picks which stories to tell.
Does not change Round Results / POTW / Power Ranking / Luck / Preview.
"""

from __future__ import print_function

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from analytics import (
    ROOT,
    display_name,
    extract_sides,
    load_config,
    load_json,
    load_players_json,
    normalize_player_id,
    parse_fantrax_datetime,
    parse_float,
    period_status,
    team_scores_from_payload,
)
from data_layout import (
    first_existing,
    historical_path,
    roster_lookup_paths,
    players_stats_lookup_paths,
)


POSITIONS = ("G", "D", "M", "F")
POS_LABELS = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}

INJURY_STATUS_ALIASES = {
    "INJ": "Injured",
    "INJURED": "Injured",
    "O": "Out",
    "OUT": "Out",
    "D": "Doubtful",
    "DOUBT": "Doubtful",
    "DOUBTFUL": "Doubtful",
    "Q": "Doubtful",
    "QUESTIONABLE": "Doubtful",
    "SUSP": "Suspended",
    "SUSPENDED": "Suspended",
    "IR": "Injured",
    "NA": "Out",
    "DNP": "Out",
    "N/A": "Out",
}

INJURY_TOOLTIP_RE = re.compile(
    r"\b(injur(?:y|ed)|hamstring|ankle|knee|groin|calf|thigh|"
    r"ruled out|doubtful|suspended|not in (?:the )?squad|"
    r"withdrew|illness|out for)\b",
    re.I,
)

NOTABLE_EVENT_CODES = {
    "G": "Goals",
    "AT": "Assists",
    "YC": "Yellow Cards",
    "RC": "Red Cards",
    "OG": "Own Goals",
    "PKM": "Penalty Kicks Missed",
    "PKS": "Penalty Kick Saves",
    "ErG": "Errors Led to Goal",
    "ErS": "Errors Led to Shot",
}


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def round2(value):
    return round(parse_float(value), 2)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def split_positions(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        tokens = value
    else:
        tokens = re.split(r"[,/|; ]+", str(value))
    out = []
    for token in tokens:
        token = str(token or "").strip().upper()[:1]
        if token in POSITIONS and token not in out:
            out.append(token)
    return out


def display_player_name(raw_name, fallback=""):
    name = (raw_name or "").strip()
    if "," in name:
        last, first = name.split(",", 1)
        name = "{0} {1}".format(first.strip(), last.strip()).strip()
    return name or fallback or ""


def load_lineup_rules(league_info=None, config=None):
    config = config or {}
    cfg = dict(config.get("lineup_rules") or {})
    roster_info = (league_info or {}).get("rosterInfo") or {}
    constraints = roster_info.get("positionConstraints") or {}

    positions = {}
    cfg_positions = cfg.get("positions") or {}
    for pos in POSITIONS:
        row = dict(cfg_positions.get(pos) or {})
        api = constraints.get(pos) or {}
        if api.get("maxActive") is not None:
            row["max"] = int(api["maxActive"])
        if api.get("minActive") is not None:
            row["min"] = int(api["minActive"])
        row.setdefault("min", 1 if pos == "G" else 0)
        row.setdefault("max", {"G": 1, "D": 5, "M": 5, "F": 3}[pos])
        positions[pos] = {"min": int(row["min"]), "max": int(row["max"])}

    total = int(
        cfg.get("totalStarters")
        or roster_info.get("maxTotalActivePlayers")
        or 11
    )
    return {
        "source": cfg.get("source") or "league_info.rosterInfo.positionConstraints",
        "totalStarters": total,
        "totalRoster": int(cfg.get("totalRoster") or roster_info.get("maxTotalPlayers") or 16),
        "maxReserve": int(cfg.get("maxReserve") or roster_info.get("maxTotalReservePlayers") or 5),
        "positions": positions,
        "notes": cfg.get("notes") or "",
    }


def legal_formations(rules):
    total = int(rules["totalStarters"])
    pos = rules["positions"]
    formations = []
    g_lo, g_hi = pos["G"]["min"], pos["G"]["max"]
    d_lo, d_hi = pos["D"]["min"], pos["D"]["max"]
    m_lo, m_hi = pos["M"]["min"], pos["M"]["max"]
    f_lo, f_hi = pos["F"]["min"], pos["F"]["max"]
    for g in range(g_lo, g_hi + 1):
        for d in range(d_lo, d_hi + 1):
            for m in range(m_lo, m_hi + 1):
                for f in range(f_lo, f_hi + 1):
                    if g + d + m + f == total:
                        formations.append({"G": g, "D": d, "M": m, "F": f})
    return formations


def _eligible(player):
    elig = list(player.get("eligiblePositions") or [])
    primary = (player.get("primaryPosition") or player.get("position") or "").upper()[:1]
    if primary in POSITIONS and primary not in elig:
        elig.append(primary)
    return [p for p in elig if p in POSITIONS]


def can_assign(players, formation):
    slots = []
    for pos in POSITIONS:
        slots.extend([pos] * int(formation.get(pos) or 0))
    if len(players) != len(slots):
        return False
    used = [False] * len(players)

    def dfs(slot_idx):
        if slot_idx == len(slots):
            return True
        need = slots[slot_idx]
        for idx, player in enumerate(players):
            if used[idx]:
                continue
            if need not in _eligible(player):
                continue
            used[idx] = True
            if dfs(slot_idx + 1):
                return True
            used[idx] = False
        return False

    return dfs(0)


def is_legal_xi(players, rules):
    if len(players) != int(rules["totalStarters"]):
        return False
    for formation in legal_formations(rules):
        if can_assign(players, formation):
            return True
    return False


def _all_single_position(players):
    for player in players:
        elig = _eligible(player)
        if len(elig) != 1:
            return False
    return True


def assign_best(players, formation):
    need = {pos: int(formation.get(pos) or 0) for pos in POSITIONS}
    if sum(need.values()) == 0:
        return []

    if _all_single_position(players):
        chosen = []
        by_pos = {pos: [] for pos in POSITIONS}
        for player in players:
            elig = _eligible(player)
            if elig:
                by_pos[elig[0]].append(player)
        for pos in POSITIONS:
            group = sorted(by_pos[pos], key=lambda p: (-p["gwPoints"], p.get("playerName") or ""))
            if len(group) < need[pos]:
                return None
            chosen.extend(group[: need[pos]])
        return chosen

    ordered = sorted(players, key=lambda p: (-p["gwPoints"], p.get("playerName") or ""))
    best = {"score": None, "players": None}

    def rec(idx, chosen, remaining, score):
        leftover = sum(remaining.values())
        if leftover == 0:
            if best["score"] is None or score > best["score"]:
                best["score"] = score
                best["players"] = list(chosen)
            return
        if idx >= len(ordered):
            return
        remaining_points = sum(p["gwPoints"] for p in ordered[idx:])
        if best["score"] is not None and score + remaining_points <= best["score"]:
            return
        player = ordered[idx]
        rec(idx + 1, chosen, remaining, score)
        for pos in _eligible(player):
            if remaining.get(pos, 0) <= 0:
                continue
            remaining[pos] -= 1
            chosen.append(player)
            rec(idx + 1, chosen, remaining, score + player["gwPoints"])
            chosen.pop()
            remaining[pos] += 1

    rec(0, [], need, 0.0)
    return best["players"]


def best_legal_xi(players, rules):
    best = None
    for formation in legal_formations(rules):
        picked = assign_best(players, formation)
        if not picked:
            continue
        score = sum(p["gwPoints"] for p in picked)
        if best is None or score > best["score"]:
            best = {
                "score": score,
                "formation": dict(formation),
                "players": picked,
            }
    return best


def guess_player_status(player):
    raw = (player.get("playerStatus") or player.get("injuryStatus") or "").strip()
    if raw:
        mapped = INJURY_STATUS_ALIASES.get(raw.upper())
        if mapped:
            return mapped, raw
        if raw.upper() not in ("T", "WW", "ACTIVE", "RESERVE") and raw:
            return raw, raw

    for icon in player.get("icons") or []:
        tip = str(icon.get("tooltip") or "")
        if INJURY_TOOLTIP_RE.search(tip):
            return "Injured/Out (news tooltip)", tip[:180]
    return None, raw


def player_brief(player, extra=None):
    row = {
        "playerId": player.get("playerId"),
        "name": player.get("playerName"),
        "position": player.get("primaryPosition") or player.get("position"),
        "eligiblePositions": list(player.get("eligiblePositions") or []),
        "fantasyStatus": player.get("fantasyStatus"),
        "points": round2(player.get("gwPoints")),
        "eplTeam": player.get("eplTeam"),
        "playerStatus": player.get("normalizedStatus"),
    }
    if extra:
        row.update(extra)
    return row


def load_player_ids(data_dir):
    from data_layout import current_path

    path = first_existing(current_path(data_dir, "player_ids.json"), Path(data_dir) / "player_ids.json")
    if path is None:
        return {}
    raw = load_json(path)
    if isinstance(raw, dict) and isinstance(raw.get("playerIds"), dict):
        raw = raw["playerIds"]
    if not isinstance(raw, dict):
        return {}
    return {normalize_player_id(key): value for key, value in raw.items()}


def lookup_name_and_meta(player_id, player_ids, stats_by_id, league_info):
    stats = stats_by_id.get(player_id) or {}
    ids = player_ids.get(player_id) or {}
    info = ((league_info or {}).get("playerInfo") or {}).get(player_id) or {}

    name = display_player_name(stats.get("player")) or display_player_name(ids.get("name"), player_id)
    epl = (stats.get("epl_team") or ids.get("team") or ids.get("teamShortName") or "").strip()
    primary = (
        (stats.get("position") or "")[:1].upper()
        or str(ids.get("position") or "")[:1].upper()
        or str(info.get("eligiblePos") or "")[:1].upper()
    )
    eligible = split_positions(stats.get("eligible_positions") or stats.get("eligiblePositions"))
    if not eligible:
        eligible = split_positions(info.get("eligiblePos") or ids.get("position") or primary)
    return {
        "playerName": name,
        "eplTeam": epl,
        "primaryPosition": primary if primary in POSITIONS else (eligible[0] if eligible else ""),
        "eligiblePositions": eligible or ([primary] if primary in POSITIONS else []),
        "stats": stats,
        "ids": ids,
        "info": info,
    }


def enrich_roster_payload(period, roster_payload, league_info, player_ids, stats_by_id=None, snapshot_time=None):
    stats_by_id = stats_by_id or {}
    snapshot_time = snapshot_time or utc_now_iso()
    teams = []
    raw_rosters = (roster_payload or {}).get("rosters") or {}
    for team_id, team in raw_rosters.items():
        players = []
        for item in team.get("rosterItems") or []:
            player_id = normalize_player_id(item.get("id"))
            if not player_id:
                continue
            meta = lookup_name_and_meta(player_id, player_ids, stats_by_id, league_info)
            slot = (item.get("position") or meta["primaryPosition"] or "").upper()[:1]
            status = (item.get("status") or "").upper()
            if status not in ("ACTIVE", "RESERVE"):
                status = status or "UNKNOWN"
            players.append(
                {
                    "playerId": player_id,
                    "playerName": meta["playerName"],
                    "eplTeam": meta["eplTeam"],
                    "primaryPosition": meta["primaryPosition"] or slot,
                    "eligiblePositions": meta["eligiblePositions"] or ([slot] if slot in POSITIONS else []),
                    "slotPosition": slot,
                    "fantasyStatus": status,
                }
            )
        teams.append(
            {
                "period": int(period),
                "snapshot_time": snapshot_time,
                "teamId": team_id,
                "teamName": team.get("teamName") or team_id,
                "players": players,
            }
        )
    teams.sort(key=lambda row: row["teamName"] or "")
    return {
        "period": int(period),
        "snapshot_time": snapshot_time,
        "source": "getTeamRosters?period={0}".format(period),
        "api_period": roster_payload.get("period"),
        "team_count": len(teams),
        "player_count": sum(len(team["players"]) for team in teams),
        "teams": teams,
    }


def roster_snapshot_path(data_dir, period):
    existing = first_existing(*roster_lookup_paths(data_dir, period))
    return existing or historical_path(data_dir, period, "rosters.json")


def roster_write_path(data_dir, period):
    return historical_path(data_dir, period, "rosters.json")


def should_overwrite_roster(path, period, league_info, config=None, now=None):
    if not Path(path).exists():
        return True
    freeze_when = ((config or {}).get("manager_analysis") or {}).get("freeze_roster_when") or [
        "in_progress",
        "completed",
    ]
    status = period_status(period, league_info, now=now)
    return status.get("state") not in freeze_when


def save_period_roster_snapshot(data_dir, period, roster_payload, league_info=None, player_ids=None, config=None, now=None):
    path = roster_write_path(data_dir, period)
    existing = first_existing(*roster_lookup_paths(data_dir, period))
    freeze_path = existing or path
    if freeze_path.exists() and not should_overwrite_roster(freeze_path, period, league_info, config=config, now=now):
        return {"action": "kept_existing", "path": str(freeze_path)}

    if player_ids is None:
        player_ids = load_player_ids(data_dir)
    else:
        player_ids = {
            normalize_player_id(key): value
            for key, value in (player_ids or {}).items()
            if isinstance(value, dict)
        }

    snapshot = enrich_roster_payload(period, roster_payload, league_info, player_ids)
    write_json(path, snapshot)
    return {"action": "wrote", "path": str(path), "player_count": snapshot["player_count"]}


def load_roster_snapshot(data_dir, period):
    path = roster_snapshot_path(data_dir, period)
    if not path.exists():
        return None
    return load_json(path)


def load_period_stats(data_dir, period):
    path = first_existing(*players_stats_lookup_paths(data_dir, period))
    rows = load_players_json(path) if path else []
    by_id = {}
    for row in rows:
        player_id = normalize_player_id(row.get("id"))
        if player_id:
            by_id[player_id] = row
    return by_id, path


def classify_zero_reason(player):
    status = player.get("normalizedStatus")
    if status in ("Injured", "Out", "Doubtful", "Suspended"):
        return status
    opponent = player.get("opponent") or ""
    if opponent and not re.search(r"\b\d+\b", opponent):
        return "No scoreline in opponent cell"
    if parse_float(player.get("gwPoints")) == 0:
        return "Zero GW FPts"
    return None


def build_period_players(data_dir, period, roster_snapshot, league_info=None, config=None):
    stats_by_id, stats_path = load_period_stats(data_dir, period)
    player_ids = load_player_ids(data_dir)
    snapshot_time = utc_now_iso()
    players = []

    for team in roster_snapshot.get("teams") or []:
        for raw in team.get("players") or []:
            player_id = normalize_player_id(raw.get("playerId"))
            stats = stats_by_id.get(player_id) or {}
            meta = lookup_name_and_meta(player_id, player_ids, stats_by_id, league_info)
            icons = stats.get("icons") or stats.get("raw", {}).get("icons") or []
            gw_points = parse_float(stats.get("fpts"))
            row = {
                "playerId": player_id,
                "playerName": raw.get("playerName") or meta["playerName"],
                "fantasyTeamId": team.get("teamId"),
                "fantasyTeam": team.get("teamName"),
                "fantasyStatus": raw.get("fantasyStatus"),
                "position": raw.get("slotPosition") or raw.get("primaryPosition") or meta["primaryPosition"],
                "primaryPosition": raw.get("primaryPosition") or meta["primaryPosition"],
                "eligiblePositions": raw.get("eligiblePositions") or meta["eligiblePositions"],
                "eplTeam": raw.get("eplTeam") or meta["eplTeam"],
                "gwPoints": gw_points,
                "opponent": stats.get("opponent") or "",
                "icons": icons,
                "stats": stats.get("raw", {}).get("stats") or stats.get("stats") or {},
                "scoringBreakdownAvailable": False,
            }
            guessed, raw_status = guess_player_status(row)
            row["playerStatus"] = raw_status or None
            row["normalizedStatus"] = guessed
            players.append(row)

    payload = {
        "period": int(period),
        "snapshot_time": snapshot_time,
        "source": "roster snapshot + {0}".format(stats_path.name if stats_path else "missing players_period JSON"),
        "count": len(players),
        "active_count": sum(1 for p in players if p["fantasyStatus"] == "ACTIVE"),
        "reserve_count": sum(1 for p in players if p["fantasyStatus"] == "RESERVE"),
        "zero_kept": sum(1 for p in players if p["gwPoints"] == 0),
        "unmatched_stats": [p["playerId"] for p in players if p["playerId"] not in stats_by_id],
        "players": players,
    }
    out_path = historical_path(data_dir, period, "players.json")
    write_json(out_path, payload)
    return payload, out_path


def extract_team_event_hints(matchup_payload):
    hints = {}
    for matchup in matchup_payload.get("matchups") or []:
        for side in ("home", "away"):
            team = matchup.get(side) or {}
            team_id = team.get("teamId")
            if not team_id:
                continue
            events = []
            for cat in matchup.get("categories") or []:
                code = cat.get("shortName")
                if code not in NOTABLE_EVENT_CODES:
                    continue
                cell = cat.get(side) or {}
                value = parse_float(cell.get("value") or cell.get("display"), 0.0)
                points = parse_float(cell.get("points"), 0.0)
                if value == 0 and points == 0:
                    continue
                events.append(
                    {
                        "code": code,
                        "name": cat.get("name") or NOTABLE_EVENT_CODES[code],
                        "value": value,
                        "points": round2(points),
                        "group": cat.get("group"),
                    }
                )
            hints[team_id] = events
    return hints


def best_single_swap(active, reserve, rules):
    best = None
    for incoming in reserve:
        for outgoing in active:
            candidate = [p for p in active if p["playerId"] != outgoing["playerId"]] + [incoming]
            if not is_legal_xi(candidate, rules):
                continue
            gain = incoming["gwPoints"] - outgoing["gwPoints"]
            row = {
                "playerIn": player_brief(incoming),
                "playerOut": player_brief(outgoing),
                "gain": round2(gain),
            }
            if best is None or gain > best["_gain"]:
                best = dict(row)
                best["_gain"] = gain
    if best:
        best.pop("_gain", None)
    return best


def manager_call_candidates(active, reserve, rules):
    rows = []
    for starter in active:
        best_alt = None
        for bench in reserve:
            swapped = [p for p in active if p["playerId"] != starter["playerId"]] + [bench]
            if not is_legal_xi(swapped, rules):
                continue
            if best_alt is None or bench["gwPoints"] > best_alt["gwPoints"]:
                best_alt = bench
        if best_alt is None:
            continue
        rows.append(
            {
                "activePlayer": player_brief(starter),
                "bestCompatibleBench": player_brief(best_alt),
                "managerGain": round2(starter["gwPoints"] - best_alt["gwPoints"]),
            }
        )
    rows.sort(key=lambda row: (-row["managerGain"], row["activePlayer"]["name"] or ""))
    return rows


def injury_tax_candidates(active, reserve, rules):
    rows = []
    for starter in active:
        if parse_float(starter.get("gwPoints")) != 0:
            continue
        status = starter.get("normalizedStatus")
        injury_like = status in ("Injured", "Out", "Doubtful", "Suspended") or (
            status or ""
        ).startswith("Injured/Out")
        best_rep = None
        for bench in reserve:
            swapped = [p for p in active if p["playerId"] != starter["playerId"]] + [bench]
            if not is_legal_xi(swapped, rules):
                continue
            if best_rep is None or bench["gwPoints"] > best_rep["gwPoints"]:
                best_rep = bench
        rows.append(
            {
                "player": player_brief(starter),
                "fantasyStatus": "ACTIVE",
                "gwPoints": 0.0,
                "playerStatus": status or "unknown",
                "injuryLike": bool(injury_like),
                "bestBenchReplacement": player_brief(best_rep) if best_rep else None,
                "replacementPoints": round2(best_rep["gwPoints"]) if best_rep else 0.0,
                "zeroReason": classify_zero_reason(starter),
            }
        )
    rows.sort(key=lambda row: (-row["replacementPoints"], row["player"]["name"] or ""))
    return rows


def analyze_team(team_players, fantrax_score, opponent, rules, event_hints, tolerance):
    active = [p for p in team_players if p.get("fantasyStatus") == "ACTIVE"]
    reserve = [p for p in team_players if p.get("fantasyStatus") == "RESERVE"]
    calculated = sum(p["gwPoints"] for p in active)
    difference = calculated - parse_float(fantrax_score)
    validation = {
        "fantraxScore": round2(fantrax_score),
        "calculatedScore": round2(calculated),
        "difference": round2(difference),
        "valid": abs(difference) <= tolerance,
        "tolerance": tolerance,
    }

    actual_xi = [player_brief(p) for p in sorted(active, key=lambda x: (-x["gwPoints"], x.get("playerName") or ""))]
    best = best_legal_xi(team_players, rules)
    best_score = best["score"] if best else 0.0
    lineup_loss = (best_score - calculated) if best else 0.0
    if best and best_score > 0:
        efficiency = (calculated / best_score) * 100.0
    elif best and best_score == 0 and calculated == 0:
        efficiency = 100.0
    else:
        efficiency = None

    highest_bench = None
    if reserve:
        top = max(reserve, key=lambda p: (p["gwPoints"], p.get("playerName") or ""))
        highest_bench = player_brief(top)

    swap = best_single_swap(active, reserve, rules)
    calls = manager_call_candidates(active, reserve, rules)
    injuries = injury_tax_candidates(active, reserve, rules)

    opp_score = parse_float((opponent or {}).get("score"))
    actual_result = "win" if calculated > opp_score else "loss" if calculated < opp_score else "tie"
    # What-if uses Fantrax official score so flip math matches the published result.
    official = parse_float(fantrax_score)
    official_result = "win" if official > opp_score else "loss" if official < opp_score else "tie"

    single_swap_flip = False
    adjusted = official
    if swap and official_result != "win":
        adjusted = official + swap["gain"]
        single_swap_flip = adjusted > opp_score

    best_xi_flip = False
    if best and official_result != "win":
        best_xi_flip = best_score > opp_score

    return {
        "teamId": team_players[0]["fantasyTeamId"] if team_players else None,
        "teamName": team_players[0]["fantasyTeam"] if team_players else None,
        "actualScore": round2(official),
        "bestPossibleScore": round2(best_score) if best else None,
        "lineupLoss": round2(lineup_loss) if best else None,
        "lineupEfficiency": round2(efficiency) if efficiency is not None else None,
        "actualFormation": _count_positions(active),
        "bestFormation": (best or {}).get("formation"),
        "actualXI": actual_xi,
        "bestPossibleXI": [player_brief(p) for p in (best["players"] if best else [])],
        "benchPoints": round2(sum(p["gwPoints"] for p in reserve)),
        "highestBenchPlayer": highest_bench,
        "bestSingleSwap": swap,
        "singleSwapResultFlip": single_swap_flip,
        "bestXIResultFlip": best_xi_flip,
        "whatIf": {
            "opponentId": (opponent or {}).get("teamId"),
            "opponentName": (opponent or {}).get("teamName"),
            "opponentScore": round2(opp_score),
            "actualResult": official_result,
            "bestSingleSwapGain": (swap or {}).get("gain"),
            "adjustedScore": round2(adjusted) if swap else round2(official),
            "resultFlip": single_swap_flip,
            "benchPlayerIn": ((swap or {}).get("playerIn") or {}).get("name"),
            "activePlayerOut": ((swap or {}).get("playerOut") or {}).get("name"),
            "bestXIScore": round2(best_score) if best else None,
            "bestXIResultFlip": best_xi_flip,
        },
        "managerCallCandidates": calls[:5],
        "injuryCandidates": [row for row in injuries if row["injuryLike"]],
        "zeroPointActives": injuries,
        "scoreValidation": validation,
        "teamEventHints": event_hints or [],
        "activeCount": len(active),
        "reserveCount": len(reserve),
    }


def _count_positions(players):
    counts = {pos: 0 for pos in POSITIONS}
    for player in players:
        pos = (player.get("position") or player.get("primaryPosition") or "")[:1]
        if pos in counts:
            counts[pos] += 1
    return counts


def _pick_extrema(rows, key, reverse=True):
    scored = [row for row in rows if row.get(key) is not None]
    if not scored:
        return None
    winner = max(scored, key=lambda row: row[key]) if reverse else min(scored, key=lambda row: row[key])
    return {
        "team": winner["teamName"],
        "teamId": winner["teamId"],
        key: winner.get(key),
    }


def build_summary(period, team_rows, snapshot_note):
    disasters = []
    calls = []
    swap_flips = []
    best_xi_flips = []
    injuries = []

    for row in team_rows:
        swap = row.get("bestSingleSwap")
        if swap and parse_float(swap.get("gain")) > 0:
            disasters.append(
                {
                    "team": row["teamName"],
                    "teamId": row["teamId"],
                    "benchPlayer": (swap.get("playerIn") or {}).get("name"),
                    "benchPoints": (swap.get("playerIn") or {}).get("points"),
                    "replacedPlayer": (swap.get("playerOut") or {}).get("name"),
                    "replacedPlayerPoints": (swap.get("playerOut") or {}).get("points"),
                    "potentialGain": swap.get("gain"),
                }
            )
        for call in row.get("managerCallCandidates") or []:
            calls.append(
                {
                    "team": row["teamName"],
                    "teamId": row["teamId"],
                    "activePlayer": (call.get("activePlayer") or {}).get("name"),
                    "activePoints": (call.get("activePlayer") or {}).get("points"),
                    "benchAlternative": (call.get("bestCompatibleBench") or {}).get("name"),
                    "benchPoints": (call.get("bestCompatibleBench") or {}).get("points"),
                    "managerGain": call.get("managerGain"),
                }
            )
        if row.get("singleSwapResultFlip"):
            swap_flips.append(
                {
                    "team": row["teamName"],
                    "opponent": (row.get("whatIf") or {}).get("opponentName"),
                    "actualScore": row["actualScore"],
                    "opponentScore": (row.get("whatIf") or {}).get("opponentScore"),
                    "bestSingleSwapGain": (row.get("whatIf") or {}).get("bestSingleSwapGain"),
                    "adjustedScore": (row.get("whatIf") or {}).get("adjustedScore"),
                    "benchPlayerIn": (row.get("whatIf") or {}).get("benchPlayerIn"),
                    "activePlayerOut": (row.get("whatIf") or {}).get("activePlayerOut"),
                    "resultFlip": True,
                }
            )
        if row.get("bestXIResultFlip"):
            best_xi_flips.append(
                {
                    "team": row["teamName"],
                    "opponent": (row.get("whatIf") or {}).get("opponentName"),
                    "actualScore": row["actualScore"],
                    "opponentScore": (row.get("whatIf") or {}).get("opponentScore"),
                    "bestPossibleScore": row.get("bestPossibleScore"),
                    "resultFlip": True,
                }
            )
        for inj in row.get("injuryCandidates") or []:
            injuries.append(
                {
                    "team": row["teamName"],
                    "player": (inj.get("player") or {}).get("name"),
                    "fantasyStatus": inj.get("fantasyStatus"),
                    "gwPoints": inj.get("gwPoints"),
                    "playerStatus": inj.get("playerStatus"),
                    "bestBenchReplacement": (inj.get("bestBenchReplacement") or {}).get("name"),
                    "replacementPoints": inj.get("replacementPoints"),
                }
            )

    disasters.sort(key=lambda row: (-parse_float(row.get("potentialGain")), row.get("team") or ""))
    calls.sort(key=lambda row: (-parse_float(row.get("managerGain")), row.get("team") or ""))

    return {
        "period": int(period),
        "snapshotNote": snapshot_note,
        "largestLineupLoss": _pick_extrema(team_rows, "lineupLoss", reverse=True),
        "lowestLineupEfficiency": _pick_extrema(team_rows, "lineupEfficiency", reverse=False),
        "highestLineupEfficiency": _pick_extrema(team_rows, "lineupEfficiency", reverse=True),
        "topBenchDisasters": disasters[:10],
        "topManagerCalls": calls[:10],
        "singleSwapResultFlips": swap_flips,
        "bestXIResultFlips": best_xi_flips,
        "injuryTaxCandidates": injuries,
        "scoreValidation": [
            {
                "team": row["teamName"],
                "teamId": row["teamId"],
                "actualScore": row["actualScore"],
                "bestPossibleScore": row["bestPossibleScore"],
                "lineupLoss": row["lineupLoss"],
                "lineupEfficiency": row["lineupEfficiency"],
                "benchPoints": row["benchPoints"],
                "highestBenchPlayer": row.get("highestBenchPlayer"),
                "bestSingleSwap": row.get("bestSingleSwap"),
                "scoreValidation": row.get("scoreValidation"),
            }
            for row in team_rows
        ],
        "scoringBreakdown": {
            "playerLevelAvailable": False,
            "reason": "getPlayerStats default STATS view only returns FPts. Team-level RC/OG/PK/error totals are stored as teamEventHints.",
        },
    }


def snapshot_quality_note(period, roster_snapshot, validations, league_info, now=None):
    status = period_status(period, league_info, now=now)
    created = roster_snapshot.get("snapshot_time")
    api_period = roster_snapshot.get("api_period")
    invalid = [row for row in validations if not row["scoreValidation"]["valid"]]
    notes = [
        "Roster source: {0}.".format(roster_snapshot.get("source") or "unknown"),
        "Snapshot written at {0}.".format(created or "unknown"),
        "Period state at analysis time: {0}.".format(status.get("state")),
    ]
    if api_period is not None and int(api_period) != int(period):
        notes.append(
            "DATA LIMIT: API roster period is {0}, requested {1}.".format(api_period, period)
        )
    if invalid:
        notes.append(
            "DATA LIMIT: ACTIVE FPts sum does not match Fantrax score for {0} team(s). "
            "Do not invent a lock lineup to force a match.".format(len(invalid))
        )
    else:
        notes.append(
            "ACTIVE FPts sums match Fantrax matchup scores within tolerance. "
            "This is a historical getTeamRosters?period snapshot, not a lock-time file captured during GW{0}.".format(
                period
            )
        )
    return " ".join(notes)


def build_historical_period_analysis(inputs, period, now=None):
    """GW results-side manager analysis from historical/period_N only."""
    return build_manager_analysis(inputs, period, now=now)


def build_manager_analysis(inputs, period, now=None):
    data_dir = Path(inputs["data_dir"])
    config = inputs.get("config") or {}
    league_info = inputs.get("league_info") or {}
    teams_dir = inputs.get("teams") or {}
    payloads = inputs.get("payloads") or {}
    payload = payloads.get(int(period))
    if not payload:
        raise RuntimeError("No matchup payload for period {0}.".format(period))

    rules = load_lineup_rules(league_info, config)
    tolerance = parse_float(((config.get("manager_analysis") or {}).get("score_tolerance")), 0.03)
    roster_snapshot = load_roster_snapshot(data_dir, period)
    if not roster_snapshot:
        raise RuntimeError(
            "Missing roster snapshot {0}. Run a fetch so getTeamRosters?period={1} can be saved.".format(
                roster_snapshot_path(data_dir, period), period
            )
        )

    players_payload, players_path = build_period_players(
        data_dir, period, roster_snapshot, league_info=league_info, config=config
    )
    by_team = {}
    for player in players_payload["players"]:
        by_team.setdefault(player["fantasyTeamId"], []).append(player)

    scores = team_scores_from_payload(payload)
    opponents = {}
    for row in extract_sides(payload):
        opponents[row["home"]["teamId"]] = {
            "teamId": row["away"]["teamId"],
            "teamName": display_name(teams_dir, row["away"]["teamId"], row["away"]["teamName"]),
            "score": row["away"]["score"],
        }
        opponents[row["away"]["teamId"]] = {
            "teamId": row["home"]["teamId"],
            "teamName": display_name(teams_dir, row["home"]["teamId"], row["home"]["teamName"]),
            "score": row["home"]["score"],
        }
    event_hints = extract_team_event_hints(payload)

    team_rows = []
    for team_id, team_players in by_team.items():
        fantrax = scores.get(team_id) or {}
        analyzed = analyze_team(
            team_players,
            fantrax.get("score"),
            opponents.get(team_id),
            rules,
            event_hints.get(team_id) or [],
            tolerance,
        )
        analyzed["teamName"] = display_name(teams_dir, team_id, analyzed.get("teamName"))
        analyzed["teamId"] = team_id
        team_rows.append(analyzed)
    team_rows.sort(key=lambda row: (-parse_float(row.get("lineupLoss")), row.get("teamName") or ""))

    note = snapshot_quality_note(
        period, roster_snapshot, team_rows, league_info, now=now
    )
    analysis = {
        "period": int(period),
        "generated_at": utc_now_iso(),
        "lineup_rules": rules,
        "snapshotNote": note,
        "scoringBreakdownAvailable": False,
        "teams": team_rows,
    }
    from player_rankings import build_period_player_rankings

    rankings = build_period_player_rankings(inputs, period)
    summary = build_summary(period, team_rows, note)
    summary["playerRankings"] = {
        "path": "historical/period_{0}/player_rankings.json".format(int(period)),
        "dataSource": rankings.get("dataSource"),
        "fetchedAt": rankings.get("fetchedAt"),
        "playerCount": rankings.get("playerCount"),
        "bestVI": rankings.get("bestVI"),
        "topPlayersByPosition": {
            pos: (rankings.get("allPlayersRanking") or {}).get(pos, [])[:5]
            for pos in ("G", "D", "M", "F")
        },
    }

    analysis_path = historical_path(data_dir, period, "manager_analysis.json")
    summary_path = historical_path(data_dir, period, "summary.json")
    write_json(analysis_path, analysis)
    write_json(summary_path, summary)
    return {
        "analysis": analysis,
        "summary": summary,
        "player_rankings": rankings,
        "paths": {
            "rosters": str(roster_snapshot_path(data_dir, period)),
            "players": str(players_path),
            "analysis": str(analysis_path),
            "summary": str(summary_path),
            "player_rankings": rankings.get("path"),
        },
    }
