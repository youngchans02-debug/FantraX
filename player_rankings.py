#!/usr/bin/env python3
"""GW position rankings from historical period player stats.

POTW / Best VI source. Includes ACTIVE, RESERVE, and FA.
Does not use current season stats.
"""

from __future__ import print_function

from analytics import display_name, load_players_json, normalize_player_id, parse_float, short_name_index
from data_layout import (
    first_existing,
    historical_path,
    players_stats_lookup_paths,
    read_json,
    roster_lookup_paths,
    utc_now_iso,
    write_json,
)


POS_KEYS = ("G", "D", "M", "F")
POS_LABELS = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}
BEST_VI_COUNTS = {"G": 1, "D": 2, "M": 2, "F": 1}
DEFAULT_TOP_N = 10


def _owner_map(roster_snapshot, teams):
    owners = {}
    if not roster_snapshot:
        return owners
    for team in roster_snapshot.get("teams") or []:
        team_id = team.get("teamId")
        team_name = display_name(teams or {}, team_id, team.get("teamName"))
        for item in team.get("players") or []:
            player_id = normalize_player_id(item.get("playerId"))
            if not player_id:
                continue
            owners[player_id] = {
                "teamId": team_id,
                "teamName": team_name,
                "fantasyStatus": item.get("fantasyStatus") or "UNKNOWN",
                "eligiblePositions": list(item.get("eligiblePositions") or []),
                "playerName": item.get("playerName"),
                "eplTeam": item.get("eplTeam"),
                "primaryPosition": item.get("primaryPosition") or item.get("slotPosition"),
            }
    return owners


def _decorate(player, owners, teams, name_index):
    player_id = normalize_player_id(player.get("id"))
    owner = owners.get(player_id)
    status_cell = player.get("status") or ""
    if not owner and name_index:
        guessed = name_index.get(status_cell.strip().lower())
        if guessed:
            owner = {
                "teamId": guessed,
                "teamName": display_name(teams or {}, guessed),
                "fantasyStatus": None,
                "eligiblePositions": [],
            }

    eligible = list(player.get("eligible_positions") or player.get("eligiblePositions") or [])
    if owner and owner.get("eligiblePositions") and not eligible:
        eligible = list(owner["eligiblePositions"])
    position = (player.get("position") or (owner or {}).get("primaryPosition") or "")[:1].upper()
    if position and position not in eligible:
        eligible = [position] + [p for p in eligible if p != position]

    if owner:
        fantasy_team = owner.get("teamName")
        fantasy_status = owner.get("fantasyStatus") or "ROSTERED"
        rostered = True
    else:
        fantasy_team = None
        fantasy_status = "FA"
        rostered = False

    return {
        "playerId": player_id,
        "playerName": player.get("player") or (owner or {}).get("playerName") or player_id,
        "position": position,
        "eligiblePositions": eligible,
        "eplTeam": player.get("epl_team") or (owner or {}).get("eplTeam") or "",
        "fantasyTeam": fantasy_team,
        "fantasyTeamId": (owner or {}).get("teamId"),
        "fantasyStatus": fantasy_status,
        "gwPoints": parse_float(player.get("fpts")),
        "rostered": rostered,
    }


def _rank_group(players, top_n):
    ordered = sorted(
        players,
        key=lambda p: (-p["gwPoints"], (p.get("playerName") or "").lower(), p.get("playerId") or ""),
    )
    ranked = []
    for idx, player in enumerate(ordered[:top_n], start=1):
        row = dict(player)
        row["rank"] = idx
        ranked.append(row)
    return ranked


def _by_position(players, top_n):
    groups = {pos: [] for pos in POS_KEYS}
    for player in players:
        pos = player.get("position")
        if pos in groups:
            groups[pos].append(player)
    return {pos: _rank_group(groups[pos], top_n) for pos in POS_KEYS}


def _best_vi(ranking):
    picked = {}
    for pos, count in BEST_VI_COUNTS.items():
        picked[pos] = (ranking.get(pos) or [])[:count]
    return picked


def build_period_player_rankings(inputs, period, top_n=DEFAULT_TOP_N):
    data_dir = inputs["data_dir"]
    teams = inputs.get("teams") or {}
    period = int(period)
    stats_path = first_existing(*players_stats_lookup_paths(data_dir, period))
    if stats_path is None:
        raise RuntimeError("Missing historical player stats for period {0}.".format(period))

    raw_players = load_players_json(stats_path)
    roster_path = first_existing(*roster_lookup_paths(data_dir, period))
    roster_snapshot = read_json(roster_path) if roster_path else None
    owners = _owner_map(roster_snapshot, teams)
    name_index = short_name_index(teams)

    decorated = [_decorate(player, owners, teams, name_index) for player in raw_players if player.get("id")]
    rostered = [p for p in decorated if p["rostered"]]
    all_ranking = _by_position(decorated, top_n)
    rostered_ranking = _by_position(rostered, top_n)

    source = {
        "dataSource": "historical/period_{0}/players_stats.json".format(period),
        "statsPath": str(stats_path),
        "rosterSource": "historical/period_{0}/rosters.json".format(period),
        "period": period,
        "fetchedAt": utc_now_iso(),
        "playerCount": len(decorated),
        "rosteredCount": len(rostered),
        "faCount": len(decorated) - len(rostered),
        "topN": top_n,
        "sort": ["gwPoints DESC", "playerName ASC"],
        "rankMethod": "sequential after sort; name breaks ties so ranks are unique",
        "includesReserve": True,
        "includesFA": True,
    }

    payload = {
        "period": period,
        "dataSource": source["dataSource"],
        "fetchedAt": source["fetchedAt"],
        "playerCount": source["playerCount"],
        "source": source,
        "allPlayersRanking": all_ranking,
        "rosteredPlayersRanking": rostered_ranking,
        "topPlayersByPosition": all_ranking,
        "bestVI": _best_vi(all_ranking),
        "rosteredBestVI": _best_vi(rostered_ranking),
    }
    out_path = historical_path(data_dir, period, "player_rankings.json")
    write_json(out_path, payload)
    payload["path"] = str(out_path)
    return payload


def format_ranking_block(title, rows, limit=None):
    lines = [title]
    shown = rows[:limit] if limit else rows
    for row in shown:
        team = row.get("fantasyTeam") or "FA"
        status = row.get("fantasyStatus") or "FA"
        lines.append(
            "{rank:>2}. {name} ({epl}) — {team} [{status}] {pts:.2f}".format(
                rank=row.get("rank"),
                name=row.get("playerName") or "?",
                epl=row.get("eplTeam") or "?",
                team=team,
                status=status,
                pts=parse_float(row.get("gwPoints")),
            )
        )
    return "\n".join(lines)
