#!/usr/bin/env python3
"""Collect Fantrax league snapshots used by the weekly report pipeline."""

from __future__ import print_function

import json
import os
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from data_layout import (
    current_path,
    historical_path,
    migrate_legacy_snapshots,
    read_json,
    utc_now_iso,
)

try:
    import requests
except ImportError:
    requests = None


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
DEFAULT_LEAGUE_ID = "kimrr80gmsx9e18z"
DEFAULT_BASE = "https://www.fantrax.com/fxea/general"
FXPA_URL = "https://www.fantrax.com/fxpa/req"

ENDPOINTS = {
    "league_info": "getLeagueInfo",
    "team_rosters": "getTeamRosters",
    "standings": "getStandings",
    "matchup_scores": "getMatchupScores",
    "draft_results": "getDraftResults",
    "player_ids": "getPlayerIds",
}


def load_config(path=None):
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _headers(cookie, user_agent):
    headers = {
        "User-Agent": user_agent or "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.fantrax.com/",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _explain_http_error(status, url):
    if status in (401, 403):
        return (
            "HTTP {status} from {url}. "
            "If this league is private, set FANTRAX_COOKIE to a logged-in Fantrax cookie."
        ).format(status=status, url=url)
    return "HTTP {status} from {url}".format(status=status, url=url)


def fetch_json(base, endpoint, params, cookie=None, timeout=30, user_agent=None, retries=2):
    """GET a Fantrax fxea endpoint and return (payload, url)."""
    url = "{base}/{endpoint}?{qs}".format(
        base=base.rstrip("/"),
        endpoint=endpoint,
        qs=urlencode(params),
    )
    headers = _headers(cookie, user_agent)
    last_error = None

    for attempt in range(retries + 1):
        try:
            if requests is not None:
                resp = requests.get(url, headers=headers, timeout=timeout)
                if resp.status_code >= 400:
                    raise RuntimeError(_explain_http_error(resp.status_code, url))
                return resp.json(), url

            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            return json.loads(body), url
        except HTTPError as exc:
            last_error = RuntimeError(_explain_http_error(exc.code, url))
            break
        except ssl.SSLError as exc:
            last_error = RuntimeError("SSL error calling {0}: {1}".format(url, exc))
            break
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            last_error = RuntimeError("Network error calling {0}: {1}".format(url, reason))
        except Exception as exc:
            last_error = exc

        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))

    raise last_error


def slim_matchup_payload(payload):
    slim = {"period": payload.get("period"), "matchups": []}
    for matchup in payload.get("matchups") or []:
        row = {}
        for side in ("home", "away"):
            team = matchup.get(side) or {}
            row[side] = {
                "teamId": team.get("teamId"),
                "teamName": team.get("teamName"),
                "score": team.get("score"),
                "gamesPlayed": team.get("gamesPlayed"),
            }
        slim["matchups"].append(row)
    return slim


def _fxpa_data(data):
    """Route params on the Players page are strings; ints are ignored (pageNumber especially)."""
    clean = {}
    for key, value in (data or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            clean[key] = "true" if value else "false"
        else:
            clean[key] = str(value)
    return clean


def _post_fxpa_raw(league_id, messages, cookie=None, timeout=45, user_agent=None):
    """POST one or more fxpa methods. Each message is {method, data}."""
    url = "{0}?{1}".format(FXPA_URL, urlencode({"leagueId": league_id}))
    msgs = []
    for item in messages:
        msgs.append({"method": item["method"], "data": _fxpa_data(item.get("data") or {})})
    payload = {
        "msgs": msgs,
        "uiv": 3,
        "refUrl": "https://www.fantrax.com/fantasy/league/{0}/players".format(league_id),
        "dt": 1,
        "at": 0,
        "tz": "Asia/Seoul",
    }
    headers = _headers(cookie, user_agent)
    headers["Accept"] = "application/json"
    headers["Content-Type"] = "application/json"
    headers["Origin"] = "https://www.fantrax.com"
    headers["Referer"] = "https://www.fantrax.com/fantasy/league/{0}/players".format(league_id)
    body = json.dumps(payload).encode("utf-8")

    if requests is not None:
        resp = requests.post(url, data=body, headers=headers, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError(_explain_http_error(resp.status_code, url))
        parsed = resp.json()
    else:
        req = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=timeout) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(_explain_http_error(exc.code, url))

    method_label = messages[0]["method"] if messages else "batch"
    if parsed.get("pageError"):
        code = (parsed["pageError"] or {}).get("code")
        if code in ("WARNING_NOT_LOGGED_IN", "NOT_MEMBER_OF_LEAGUE"):
            raise RuntimeError(
                "fxpa {0} needs a logged-in session. Set FANTRAX_COOKIE.".format(method_label)
            )
        raise RuntimeError("fxpa {0} pageError: {1}".format(method_label, parsed["pageError"]))
    return parsed, url


def post_fxpa(league_id, method, data, cookie=None, timeout=45, user_agent=None):
    """POST the same /fxpa/req batch the Players page uses."""
    parsed, url = _post_fxpa_raw(
        league_id, [{"method": method, "data": data}], cookie, timeout, user_agent
    )
    responses = parsed.get("responses") or []
    if not responses or "data" not in responses[0]:
        raise RuntimeError("fxpa {0} returned no data.".format(method))
    return responses[0]["data"], url


def post_fxpa_batch(league_id, messages, cookie=None, timeout=60, user_agent=None):
    """POST several fxpa methods in one request. Returns (list of data dicts, url)."""
    parsed, url = _post_fxpa_raw(league_id, messages, cookie, timeout, user_agent)
    responses = parsed.get("responses") or []
    if len(responses) != len(messages):
        raise RuntimeError(
            "fxpa batch expected {0} responses, got {1}".format(len(messages), len(responses))
        )
    out = []
    for idx, resp in enumerate(responses):
        if "data" not in resp:
            method = messages[idx]["method"]
            raise RuntimeError("fxpa {0} returned no data.".format(method))
        out.append(resp["data"])
    return out, url


def parse_player_stats_table(payload):
    header = ((payload or {}).get("tableHeader") or {}).get("cells") or []
    keys = []
    short_names = []
    for idx, cell in enumerate(header):
        keys.append(cell.get("key") or cell.get("shortName") or "col_{0}".format(idx))
        short_names.append(cell.get("shortName") or "")

    players = []
    for row in (payload or {}).get("statsTable") or []:
        scorer = row.get("scorer") or {}
        mapped = {}
        cells = row.get("cells") or []
        for idx, key in enumerate(keys):
            if idx < len(cells):
                content = cells[idx].get("content")
                mapped[key] = content
                short_name = short_names[idx]
                if short_name and short_name not in mapped:
                    mapped[short_name] = content
        player_id = str(scorer.get("scorerId") or "").strip()
        if not player_id:
            continue
        minutes, gp = extract_minutes_gp(mapped)
        players.append(
            {
                "id": player_id,
                "player": scorer.get("name") or "",
                "short_name": scorer.get("shortName") or "",
                "position": (scorer.get("posShortNames") or "")[:1],
                "eligible_positions": _split_positions(scorer.get("posShortNames")),
                "epl_team": scorer.get("teamShortName") or "",
                "epl_team_name": scorer.get("teamName") or "",
                "status": mapped.get("status") or "",
                "opponent": mapped.get("opponent") or "",
                "fpts": mapped.get("fpts"),
                "fp_g": mapped.get("fptsPerGame"),
                "rank": mapped.get("rankOv") or scorer.get("rank"),
                "icons": scorer.get("icons") or [],
                "stats": mapped,
                "minutes": minutes,
                "gp": gp,
            }
        )
    return players


def _split_positions(value):
    text = str(value or "").replace("/", ",").replace(" ", "")
    out = []
    for token in text.split(","):
        token = token.strip().upper()[:1]
        if token and token not in out:
            out.append(token)
    return out


def _parse_stat_number(value):
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_optional_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


MINUTE_NAME_KEYS = ("min", "minutes", "minutesplayed")
MINUTE_ID_TOKENS = ("6120",)
GP_NAME_KEYS = ("gp", "gamesplayed")
GP_ID_TOKENS = ("6060",)
MIN_PER_GAME_NAME_KEYS = ("min/g", "ming", "minutespergame")
MIN_PER_GAME_ID_TOKENS = ("6310",)


def _header_tokens(key):
    return [part.strip().lower() for part in str(key or "").replace("_", "#").split("#") if part.strip()]


def _lookup_mapped_stat(mapped, names, id_tokens):
    if not mapped:
        return None
    lower_names = {str(k).strip().lower(): v for k, v in mapped.items()}
    for name in names:
        if name in lower_names and lower_names[name] not in (None, ""):
            return lower_names[name]
    id_set = set(id_tokens)
    for key, value in mapped.items():
        if value in (None, ""):
            continue
        tokens = _header_tokens(key)
        if any(token in id_set for token in tokens):
            return value
    return None


def extract_minutes_gp(mapped):
    """Pull Minutes Played (cat 6120) and Games Played (cat 6060) from a mapped stats row."""
    minutes = _parse_optional_number(_lookup_mapped_stat(mapped, MINUTE_NAME_KEYS, MINUTE_ID_TOKENS))
    gp = _parse_optional_number(_lookup_mapped_stat(mapped, GP_NAME_KEYS, GP_ID_TOKENS))
    if minutes is None:
        min_g = _parse_optional_number(
            _lookup_mapped_stat(mapped, MIN_PER_GAME_NAME_KEYS, MIN_PER_GAME_ID_TOKENS)
        )
        if min_g is not None and gp is not None:
            minutes = round(gp * min_g, 1)
    return minutes, gp


def table_has_minute_columns(payload):
    cells = ((payload or {}).get("tableHeader") or {}).get("cells") or []
    found = []
    for cell in cells:
        label = "{0} {1} {2}".format(
            cell.get("shortName") or "",
            cell.get("name") or "",
            cell.get("key") or "",
        ).lower()
        tokens = _header_tokens(cell.get("key") or "") + _header_tokens(cell.get("shortName") or "")
        if "6120" in tokens or "min" in tokens or "minutes" in label:
            found.append(cell.get("shortName") or cell.get("key"))
    return found


def map_header_row_cells(header_cells, row):
    mapped = {}
    cells = (row or {}).get("cells") or []
    for idx, cell in enumerate(header_cells or []):
        key = cell.get("key") or cell.get("shortName") or "col_{0}".format(idx)
        content = cells[idx].get("content") if idx < len(cells) else None
        mapped[key] = content
        short_name = cell.get("shortName") or ""
        if short_name and short_name not in mapped:
            mapped[short_name] = content
    return mapped


def parse_roster_stats_minutes(payload):
    """Minutes/GP from getTeamRosterInfo STATS tables (one fantasy team)."""
    out = {}
    for table in (payload or {}).get("tables") or []:
        header_cells = ((table.get("header") or {}).get("cells") or [])
        for row in table.get("rows") or []:
            scorer = row.get("scorer") or {}
            player_id = str(scorer.get("scorerId") or "").strip()
            if not player_id:
                continue
            mapped = map_header_row_cells(header_cells, row)
            minutes, gp = extract_minutes_gp(mapped)
            if minutes is None and gp is None:
                continue
            out[player_id] = {
                "minutes": minutes,
                "gp": gp,
                "source": "getTeamRosterInfo STATS",
            }
    return out


def parse_profile_stats_minutes(payload):
    """Minutes/GP from getPlayerProfile tab=STATS. Uses Min, or GP * Min/G."""
    stats = ((payload or {}).get("sectionContent") or {}).get("STATS") or {}
    for table in stats.get("tables") or []:
        header_cells = ((table.get("header") or {}).get("cells") or [])
        for row in table.get("rows") or []:
            mapped = map_header_row_cells(header_cells, row)
            minutes, gp = extract_minutes_gp(mapped)
            if minutes is None and gp is None:
                continue
            return {"minutes": minutes, "gp": gp, "source": "getPlayerProfile STATS"}
    return None


FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FPL_TEAM_ALIASES = {
    "BRF": "BRE",
    "NOT": "NFO",
}


def _norm_person_name(value):
    import unicodedata
    import re

    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("ø", "o").replace("Ø", "o").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _fpl_team_code(fantrax_team):
    code = str(fantrax_team or "").strip().upper()
    return FPL_TEAM_ALIASES.get(code, code)


def fetch_fpl_minutes_index(timeout=30, user_agent=None):
    """Season minutes from FPL bootstrap. Same real-world minutes as Fantrax Min."""
    headers = _headers(None, user_agent)
    if requests is not None:
        resp = requests.get(FPL_BOOTSTRAP_URL, headers=headers, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError(_explain_http_error(resp.status_code, FPL_BOOTSTRAP_URL))
        payload = resp.json()
    else:
        req = Request(FPL_BOOTSTRAP_URL, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    teams = {}
    for team in payload.get("teams") or []:
        teams[team.get("id")] = str(team.get("short_name") or "").upper()
    index = {}
    last_name_index = {}
    for item in payload.get("elements") or []:
        team = teams.get(item.get("team"))
        record = {
            "minutes": _parse_optional_number(item.get("minutes")),
            "gp": None,
            "source": "FPL bootstrap minutes",
        }
        names = [
            _norm_person_name("{0} {1}".format(item.get("first_name") or "", item.get("second_name") or "")),
            _norm_person_name(item.get("web_name")),
            _norm_person_name(item.get("second_name")),
        ]
        for name in names:
            if name:
                index.setdefault((name, team), record)
        last = _norm_person_name(item.get("second_name"))
        if last and team:
            last_name_index.setdefault((last, team), []).append(record)
    return index, last_name_index


def match_fpl_minutes(player, index, last_name_index):
    team = _fpl_team_code(player.get("epl_team"))
    full = _norm_person_name(player.get("player"))
    short = _norm_person_name(player.get("short_name"))
    last = _norm_person_name((player.get("player") or "").split()[-1] if player.get("player") else "")
    for key in ((full, team), (short, team), (last, team)):
        if key[0] and key in index:
            return index[key]
    if last and (last, team) in last_name_index and len(last_name_index[(last, team)]) == 1:
        return last_name_index[(last, team)][0]
    return None


def attach_fpl_minutes(players, timeout=30, user_agent=None):
    missing = [p for p in players if p.get("minutes") is None]
    if not missing:
        return 0
    try:
        index, last_name_index = fetch_fpl_minutes_index(timeout=timeout, user_agent=user_agent)
    except Exception as exc:
        print("ERR minutes FPL fallback: {0}".format(exc))
        return 0
    attached = 0
    for player in missing:
        record = match_fpl_minutes(player, index, last_name_index)
        if not record or record.get("minutes") is None:
            continue
        if record.get("minutes") == 0 and (player.get("fpts") or 0) > 0:
            continue
        _apply_minutes_record(player, record)
        attached += 1
    return attached


def probe_player_stats_minute_columns(league_id, cookie=None, timeout=45, user_agent=None, season_or_projection=None):
    """Log whether getPlayerStats can return Min. This league's default STATS view hides it."""
    variants = [
        {"label": "default STATS", "data": {"view": "STATS", "scoringCategoryType": "5"}},
        {"label": "Standard scoringCategoryType=1", "data": {"view": "STATS", "scoringCategoryType": "1"}},
        {"label": "Extra scoringCategoryType=2", "data": {"view": "STATS", "scoringCategoryType": "2"}},
    ]
    results = []
    for variant in variants:
        data = {
            "timeframeTypeCode": "YEAR_TO_DATE",
            "pageNumber": 1,
            "maxResultsPerPage": 1,
            "statusOrTeamFilter": "ALL",
            "positionOrGroup": "ALL",
            "sortType": "SCORE",
            "leagueId": league_id,
        }
        if season_or_projection:
            data["seasonOrProjection"] = season_or_projection
        data.update(variant["data"])
        try:
            payload, _url = post_fxpa(league_id, "getPlayerStats", data, cookie, timeout, user_agent)
            found = table_has_minute_columns(payload)
            headers = [
                cell.get("shortName") or cell.get("key")
                for cell in ((payload.get("tableHeader") or {}).get("cells") or [])
            ]
            results.append(
                {
                    "label": variant["label"],
                    "ok": True,
                    "minute_columns": found,
                    "headers": headers,
                    "hideStatsFilter": payload.get("hideStatsFilter"),
                    "displayedScoringCategoryType": payload.get("displayedScoringCategoryType"),
                }
            )
            if found:
                print(
                    "OK  minutes probe {0}: Min columns {1}".format(variant["label"], found)
                )
            else:
                print(
                    "WARN minutes probe {0}: no Min column (headers {1}, hideStatsFilter={2})".format(
                        variant["label"], headers, payload.get("hideStatsFilter")
                    )
                )
        except Exception as exc:
            results.append({"label": variant["label"], "ok": False, "error": str(exc)})
            print("ERR minutes probe {0}: {1}".format(variant["label"], exc))
    return results


def _apply_minutes_record(player, record):
    if not record:
        return
    if player.get("minutes") is None and record.get("minutes") is not None:
        player["minutes"] = record["minutes"]
        player["minutes_source"] = record.get("source")
    if player.get("gp") is None and record.get("gp") is not None:
        player["gp"] = record["gp"]


def merge_previous_minutes(players, previous_season):
    """Copy minutes/gp from the last current snapshot so a report fetch does not wipe them."""
    previous = {}
    for item in (previous_season or {}).get("players") or []:
        player_id = str(item.get("id") or "").strip()
        if player_id:
            previous[player_id] = item
    merged = 0
    for player in players or []:
        old = previous.get(player.get("id"))
        if not old:
            continue
        if player.get("minutes") is None and old.get("minutes") is not None:
            player["minutes"] = old.get("minutes")
            player["minutes_source"] = old.get("minutes_source")
            merged += 1
        if player.get("gp") is None and old.get("gp") is not None:
            player["gp"] = old.get("gp")
    return merged


def enrich_players_with_minutes(
    league_id,
    players,
    team_ids=None,
    season_or_projection=None,
    cookie=None,
    timeout=45,
    user_agent=None,
    probe=False,
):
    """Attach minutes/gp. Players page STATS view has no Min; roster STATS does."""
    players = players or []
    meta = {
        "probe": [],
        "from_player_stats": 0,
        "from_roster_stats": 0,
        "from_player_profile": 0,
        "assumed_zero": 0,
        "missing": 0,
        "ok": False,
    }
    if not players:
        return meta

    already = 0
    for player in players:
        if player.get("minutes") is not None:
            already += 1
            player.setdefault("minutes_source", "getPlayerStats")
    meta["from_player_stats"] = already
    if already:
        print("OK  minutes from getPlayerStats: {0} players".format(already))

    if probe:
        meta["probe"] = probe_player_stats_minute_columns(
            league_id,
            cookie=cookie,
            timeout=timeout,
            user_agent=user_agent,
            season_or_projection=season_or_projection,
        )

    need_ids = [p["id"] for p in players if p.get("minutes") is None]
    if need_ids and team_ids:
        roster_map = {}
        messages = []
        for team_id in team_ids:
            data = {
                "leagueId": league_id,
                "teamId": team_id,
                "view": "STATS",
                "timeframeTypeCode": "YEAR_TO_DATE",
            }
            if season_or_projection:
                data["seasonOrProjection"] = season_or_projection
            messages.append({"method": "getTeamRosterInfo", "data": data})
        try:
            payloads, _url = post_fxpa_batch(
                league_id, messages, cookie, max(timeout, 60), user_agent
            )
            for payload in payloads:
                roster_map.update(parse_roster_stats_minutes(payload))
        except Exception as exc:
            print("ERR minutes getTeamRosterInfo batch: {0}".format(exc))
            roster_map = {}
            for team_id in team_ids:
                data = {
                    "leagueId": league_id,
                    "teamId": team_id,
                    "view": "STATS",
                    "timeframeTypeCode": "YEAR_TO_DATE",
                }
                if season_or_projection:
                    data["seasonOrProjection"] = season_or_projection
                try:
                    payload, _url = post_fxpa(
                        league_id, "getTeamRosterInfo", data, cookie, timeout, user_agent
                    )
                    roster_map.update(parse_roster_stats_minutes(payload))
                except Exception as team_exc:
                    print("ERR minutes getTeamRosterInfo {0}: {1}".format(team_id, team_exc))
        attached = 0
        for player in players:
            record = roster_map.get(player["id"])
            if record and player.get("minutes") is None:
                _apply_minutes_record(player, record)
                attached += 1
        meta["from_roster_stats"] = attached
        print("OK  minutes from getTeamRosterInfo: {0} players".format(attached))

    fpl_attached = attach_fpl_minutes(players, timeout=timeout, user_agent=user_agent)
    meta["from_fpl"] = fpl_attached
    if fpl_attached:
        print("OK  minutes from FPL fallback: {0} players".format(fpl_attached))

    missing_positive = [
        p for p in players if p.get("minutes") is None and (p.get("fpts") or 0) > 0
    ]
    if missing_positive and len(missing_positive) <= 20:
        profile_map = {}
        rate_limited = 0
        for player in missing_positive[:40]:
            data = {
                "playerId": player["id"],
                "leagueId": league_id,
                "tab": "STATS",
            }
            try:
                payload, _url = post_fxpa(
                    league_id, "getPlayerProfile", data, cookie, timeout, user_agent
                )
                record = parse_profile_stats_minutes(payload)
                if record:
                    profile_map[player["id"]] = record
                rate_limited = 0
                time.sleep(0.2)
            except Exception as exc:
                print("WARN minutes getPlayerProfile {0}: {1}".format(player["id"], exc))
                if "too quickly" in str(exc).lower():
                    rate_limited += 1
                    time.sleep(1.5 * rate_limited)
                    if rate_limited >= 2:
                        print("WARN minutes getPlayerProfile stopped after rate limit")
                        break
        attached = 0
        for player in players:
            record = profile_map.get(player["id"])
            if record and player.get("minutes") is None:
                _apply_minutes_record(player, record)
                attached += 1
        meta["from_player_profile"] = attached
        print("OK  minutes from getPlayerProfile: {0} players".format(attached))

    assumed = 0
    missing = 0
    for player in players:
        if player.get("minutes") is not None:
            continue
        if (player.get("fpts") or 0) <= 0:
            player["minutes"] = 0.0
            if player.get("gp") is None:
                player["gp"] = 0.0
            player["minutes_source"] = "assumed_zero"
            assumed += 1
        else:
            missing += 1
        if player.get("gp") is None and (player.get("fp_g") or 0) > 0:
            player["gp"] = round((player.get("fpts") or 0) / player["fp_g"], 2)
    meta["assumed_zero"] = assumed
    meta["missing"] = missing
    meta["ok"] = missing == 0
    print(
        "OK  minutes attach: stats={0} roster={1} profile={2} zero={3} missing={4}".format(
            meta["from_player_stats"],
            meta["from_roster_stats"],
            meta["from_player_profile"],
            assumed,
            missing,
        )
    )
    return meta


def normalize_player_records(players):
    out = []
    for raw in players:
        item = dict(raw)
        item["id"] = str(item.get("id") or "").strip()
        item["fpts"] = _parse_stat_number(item.get("fpts"))
        item["fp_g"] = _parse_stat_number(item.get("fp_g"))
        item["position"] = (item.get("position") or "").strip().upper()[:1]
        if "minutes" in item:
            item["minutes"] = _parse_optional_number(item.get("minutes"))
        if "gp" in item:
            item["gp"] = _parse_optional_number(item.get("gp"))
        out.append(item)
    return out


def pick_season_option(payload, timeframe_code):
    options = (payload or {}).get("seasonOrProjections") or []
    matches = [item for item in options if item.get("timeframeTypeCode") == timeframe_code]
    current = [item for item in matches if item.get("showGoBackDays")]
    chosen = (current or matches)
    return chosen[0] if chosen else None


def fetch_player_stats_pages(league_id, data, cookie=None, timeout=45, user_agent=None, page_size=50):
    """Page through getPlayerStats. This league caps maxResultsPerPage at 50."""
    data = dict(data)
    data.setdefault("pageNumber", 1)
    data.setdefault("maxResultsPerPage", page_size)
    data.setdefault("statusOrTeamFilter", "ALL")
    data.setdefault("positionOrGroup", "ALL")
    data.setdefault("view", "STATS")
    data.setdefault("sortType", "SCORE")
    data["leagueId"] = league_id

    first, url = post_fxpa(league_id, "getPlayerStats", data, cookie, timeout, user_agent)
    pager = first.get("paginatedResultSet") or {}
    actual_size = int(pager.get("maxResultsPerPage") or page_size)
    data["maxResultsPerPage"] = actual_size
    total_pages = int(pager.get("totalNumPages") or 1)
    pages = {1: first}

    def _one(page_number):
        page_data = dict(data)
        page_data["pageNumber"] = page_number
        payload, page_url = post_fxpa(league_id, "getPlayerStats", page_data, cookie, timeout, user_agent)
        return page_number, payload, page_url

    remaining = list(range(2, total_pages + 1))
    if remaining:
        workers = min(8, len(remaining))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, page) for page in remaining]
            for future in as_completed(futures):
                page_number, payload, page_url = future.result()
                pages[page_number] = payload
                url = page_url

    ordered = [pages[n] for n in sorted(pages)]
    return ordered, url


def fetch_players_snapshot(league_id, cookie=None, timeout=45, user_agent=None, period=None, season_options=None):
    """Season YTD, or one scoring period, using the Players page getPlayerStats call."""
    if period is None:
        request_data = {
            "timeframeTypeCode": "YEAR_TO_DATE",
        }
        if season_options:
            option = pick_season_option({"seasonOrProjections": season_options}, "YEAR_TO_DATE")
            if option:
                request_data["seasonOrProjection"] = option["code"]
        label = "season"
    else:
        request_data = {
            "timeframeTypeCode": "BY_PERIOD",
            "transactionPeriod": int(period),
            "timeStartType": "PERIOD_ONLY",
        }
        if season_options:
            option = pick_season_option({"seasonOrProjections": season_options}, "BY_PERIOD")
            if option:
                request_data["seasonOrProjection"] = option["code"]
        label = "period_{0}".format(period)

    pages, url = fetch_player_stats_pages(league_id, request_data, cookie, timeout, user_agent)
    players = []
    seen = set()
    for page in pages:
        for player in normalize_player_records(parse_player_stats_table(page)):
            if player["id"] in seen:
                continue
            seen.add(player["id"])
            players.append(player)

    first = pages[0] if pages else {}
    displayed = first.get("displayedSeasonOrProjection") or {}
    snapshot = {
        "source": "fxpa/req getPlayerStats",
        "url": url,
        "label": label,
        "period": period,
        "seasonOrProjection": request_data.get("seasonOrProjection") or displayed.get("code"),
        "timeframeTypeCode": request_data.get("timeframeTypeCode") or displayed.get("timeframeTypeCode"),
        "seasonOrProjections": first.get("seasonOrProjections") or [],
        "count": len(players),
        "players": players,
    }
    return snapshot


def _resolve_out_dir(config, out_dir=None):
    paths = (config or {}).get("paths") or {}
    out_dir = Path(out_dir or paths.get("data_dir") or "fantrax_data")
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    return out_dir


def _http_settings(config, cookie=None):
    config = config or {}
    league_id = (
        os.environ.get("FANTRAX_LEAGUE_ID")
        or config.get("league_id")
        or DEFAULT_LEAGUE_ID
    )
    base = config.get("base_url") or DEFAULT_BASE
    if "](" in base or base.strip().startswith("["):
        raise RuntimeError("config base_url looks like Markdown, not a plain URL: {0}".format(base))
    http_cfg = config.get("http") or {}
    return {
        "league_id": league_id,
        "base": base,
        "timeout": int(http_cfg.get("timeout_seconds") or 30),
        "user_agent": http_cfg.get("user_agent") or "Mozilla/5.0",
        "cookie": cookie if cookie is not None else os.environ.get("FANTRAX_COOKIE"),
    }


def infer_current_period(roster_data, league_info=None):
    if isinstance(roster_data, dict) and roster_data.get("period") is not None:
        return int(roster_data["period"])
    if isinstance(league_info, dict):
        periods = [item.get("number") for item in league_info.get("scoringPeriods") or [] if item.get("number")]
        if periods:
            return max(int(n) for n in periods)
    return None


def _period_started(payload):
    for matchup in (payload or {}).get("matchups") or []:
        for side in ("home", "away"):
            team = matchup.get(side) or {}
            if (team.get("gamesPlayed") or 0) > 0 or (team.get("score") or 0) != 0:
                return True
    return False


def _period_state(period, league_info, now=None):
    from analytics import period_status

    return period_status(period, league_info, now=now).get("state")


def _should_refresh_historical_scores(path, period, league_info, now=None):
    if not Path(path).exists():
        return True
    return _period_state(period, league_info, now=now) != "completed"


def load_current_cache(out_dir, config=None):
    """Load previously fetched current/ files. Does not call Fantrax."""
    config = config or load_config()
    out_dir = _resolve_out_dir(config, out_dir)
    migrate_legacy_snapshots(out_dir)
    metadata = read_json(current_path(out_dir, "fetch_metadata.json")) or {}
    league_info = read_json(current_path(out_dir, "league_info.json")) or read_json(out_dir / "league_info.json") or {}
    rosters = read_json(current_path(out_dir, "team_rosters.json")) or read_json(out_dir / "team_rosters.json") or {}
    standings = read_json(current_path(out_dir, "standings.json")) or read_json(out_dir / "standings.json") or []
    season = read_json(current_path(out_dir, "players_season.json")) or read_json(out_dir / "players_season.json") or {}
    player_ids = read_json(current_path(out_dir, "player_ids.json")) or read_json(out_dir / "player_ids.json") or {}
    next_matchups = read_json(current_path(out_dir, "next_matchups.json")) or {}
    fetched_at = metadata.get("fetchedAt")
    return {
        "ok": bool(rosters or season),
        "used_cache": True,
        "fresh": False,
        "warning": "Using cached current/ data.",
        "fetched_at": fetched_at,
        "league_info": league_info,
        "rosters": rosters,
        "standings": standings,
        "season_players": season,
        "player_ids": player_ids,
        "next_matchups": next_matchups,
        "metadata": metadata,
        "paths": {
            "rosters": "current/team_rosters.json",
            "players": "current/players_season.json",
            "standings": "current/standings.json",
            "league_info": "current/league_info.json",
            "next_matchups": "current/next_matchups.json",
        },
        "out_dir": out_dir,
        "config": config,
    }


def fetch_current_data(config=None, cookie=None, out_dir=None, include_minutes=False):
    """Always overwrite fantrax_data/current/. Preview must use this bundle."""
    config = config or load_config()
    out_dir = _resolve_out_dir(config, out_dir)
    migrate_legacy_snapshots(out_dir)
    http = _http_settings(config, cookie)
    fetched_at = utc_now_iso()
    sources = {}
    errors = []
    used_cache = False

    def _one(name, endpoint, params, dest_name=None):
        dest_name = dest_name or "{0}.json".format(name)
        try:
            data, url = fetch_json(
                http["base"], endpoint, params, http["cookie"], http["timeout"], http["user_agent"]
            )
            write_json(current_path(out_dir, dest_name), data)
            sources[name] = {
                "path": "current/{0}".format(dest_name),
                "fetchedAt": fetched_at,
                "url": url,
                "cached": False,
            }
            print("OK  current {0}: {1}".format(name, url))
            return data
        except Exception as exc:
            errors.append("{0}: {1}".format(name, exc))
            print("ERR current {0}: {1}".format(name, exc))
            cached = read_json(current_path(out_dir, dest_name))
            if cached is None and dest_name:
                cached = read_json(out_dir / dest_name)
            if cached is not None:
                sources[name] = {
                    "path": "current/{0}".format(dest_name),
                    "fetchedAt": ((read_json(current_path(out_dir, "fetch_metadata.json")) or {}).get("sources") or {}).get(name, {}).get("fetchedAt"),
                    "cached": True,
                }
                return cached
            return None

    print("=== Fetching CURRENT Fantrax data ===")
    print("League: {0}".format(http["league_id"]))
    print("Output: {0}".format(current_path(out_dir, "").resolve()))

    league_info = _one("league_info", ENDPOINTS["league_info"], {"leagueId": http["league_id"]})
    rosters = _one("rosters", ENDPOINTS["team_rosters"], {"leagueId": http["league_id"]}, "team_rosters.json")
    standings = _one("standings", ENDPOINTS["standings"], {"leagueId": http["league_id"]})
    player_ids = _one("player_ids", ENDPOINTS["player_ids"], {"sport": "EPL"})

    season = None
    try:
        season = fetch_players_snapshot(
            http["league_id"],
            cookie=http["cookie"],
            timeout=max(http["timeout"], 45),
            user_agent=http["user_agent"],
        )
        season["fetchedAt"] = fetched_at
        previous_season = read_json(current_path(out_dir, "players_season.json")) or {}
        if include_minutes:
            try:
                season["minutesMeta"] = enrich_players_with_minutes(
                    http["league_id"],
                    season.get("players") or [],
                    team_ids=list((rosters or {}).get("rosters") or {}),
                    season_or_projection=season.get("seasonOrProjection"),
                    cookie=http["cookie"],
                    timeout=max(http["timeout"], 45),
                    user_agent=http["user_agent"],
                    probe=False,
                )
            except Exception as minutes_exc:
                season["minutesMeta"] = {"ok": False, "error": str(minutes_exc)}
                print("ERR current minutes: {0}".format(minutes_exc))
                merge_previous_minutes(season.get("players") or [], previous_season)
        else:
            merged = merge_previous_minutes(season.get("players") or [], previous_season)
            season["minutesMeta"] = previous_season.get("minutesMeta") or {"ok": True, "preserved": merged}
            if merged:
                print("OK  current minutes preserved from previous snapshot: {0}".format(merged))
        write_json(current_path(out_dir, "players_season.json"), season)
        sources["players"] = {
            "path": "current/players_season.json",
            "fetchedAt": fetched_at,
            "url": season.get("url"),
            "cached": False,
        }
        print("OK  current players_season: {0} players ({1})".format(season.get("count"), season.get("seasonOrProjection")))
    except Exception as exc:
        errors.append("players_season: {0}".format(exc))
        print("ERR current players_season: {0}".format(exc))
        season = read_json(current_path(out_dir, "players_season.json")) or read_json(out_dir / "players_season.json")
        if season is not None:
            sources["players"] = {
                "path": "current/players_season.json",
                "fetchedAt": season.get("fetchedAt"),
                "cached": True,
            }

    next_period = infer_current_period(rosters, league_info)
    next_matchups = None
    if next_period is not None:
        next_matchups = _one(
            "next_matchups",
            ENDPOINTS["matchup_scores"],
            {"leagueId": http["league_id"], "period": next_period},
            "next_matchups.json",
        )

    used_cache = any((item or {}).get("cached") for item in sources.values())
    warning = None
    if errors:
        warning = "Fresh Fantrax fetch failed. Cached data used." if used_cache else "; ".join(errors)
        if used_cache:
            print("WARNING: {0}".format(warning))

    metadata = {
        "fetchedAt": fetched_at,
        "usedCachedData": used_cache,
        "warning": warning,
        "currentFantraxPeriod": next_period,
        "sources": sources,
        "errors": errors,
    }
    write_json(current_path(out_dir, "fetch_metadata.json"), metadata)
    return {
        "ok": bool(rosters) and bool(season),
        "used_cache": used_cache,
        "fresh": not used_cache and not errors,
        "warning": warning,
        "fetched_at": fetched_at,
        "league_info": league_info or {},
        "rosters": rosters or {},
        "standings": standings or [],
        "season_players": season or {},
        "player_ids": player_ids or {},
        "next_matchups": next_matchups or {},
        "metadata": metadata,
        "paths": {
            "rosters": "current/team_rosters.json",
            "players": "current/players_season.json",
            "standings": "current/standings.json",
            "league_info": "current/league_info.json",
            "next_matchups": "current/next_matchups.json",
        },
        "out_dir": out_dir,
        "config": config,
    }


def fetch_historical_period(period, config=None, cookie=None, out_dir=None, league_info=None, player_ids=None, now=None):
    """Lock GW data into historical/period_N/. Never writes current/."""
    config = config or load_config()
    out_dir = _resolve_out_dir(config, out_dir)
    migrate_legacy_snapshots(out_dir)
    http = _http_settings(config, cookie)
    period = int(period)
    from manager_analysis import save_period_roster_snapshot

    if league_info is None:
        league_info = read_json(current_path(out_dir, "league_info.json")) or read_json(out_dir / "league_info.json") or {}
    if player_ids is None:
        player_ids = read_json(current_path(out_dir, "player_ids.json")) or read_json(out_dir / "player_ids.json") or {}

    result = {"period": period, "actions": {}}

    match_path = historical_path(out_dir, period, "matchups.json")
    if _should_refresh_historical_scores(match_path, period, league_info, now=now):
        data, url = fetch_json(
            http["base"],
            ENDPOINTS["matchup_scores"],
            {"leagueId": http["league_id"], "period": period},
            http["cookie"],
            http["timeout"],
            http["user_agent"],
        )
        write_json(match_path, data)
        write_json(out_dir / "matchups" / "period_{0}.json".format(period), data)
        result["actions"]["matchups"] = "wrote"
        result["matchups"] = data
        print("OK  historical matchup {0}: {1}".format(period, url))
    else:
        result["actions"]["matchups"] = "kept_existing"
        result["matchups"] = read_json(match_path)
        print("SKIP historical matchup {0}: completed snapshot kept".format(period))

    try:
        roster_payload, url = fetch_json(
            http["base"],
            ENDPOINTS["team_rosters"],
            {"leagueId": http["league_id"], "period": period},
            http["cookie"],
            http["timeout"],
            http["user_agent"],
        )
        saved = save_period_roster_snapshot(
            out_dir,
            period,
            roster_payload,
            league_info=league_info,
            player_ids=player_ids,
            config=config,
            now=now,
        )
        result["actions"]["rosters"] = saved["action"]
        print("OK  historical roster {0}: {1} ({2})".format(period, url, saved["action"]))
    except Exception as exc:
        result["actions"]["rosters"] = "error"
        print("ERR historical roster {0}: {1}".format(period, exc))

    stats_path = historical_path(out_dir, period, "players_stats.json")
    payload = result.get("matchups") or {}
    want_stats = _should_refresh_historical_scores(stats_path, period, league_info, now=now)
    if want_stats and (_period_started(payload) or not stats_path.exists()):
        try:
            period_snap = fetch_players_snapshot(
                http["league_id"],
                cookie=http["cookie"],
                timeout=max(http["timeout"], 45),
                user_agent=http["user_agent"],
                period=period,
            )
            write_json(stats_path, period_snap)
            result["actions"]["players_stats"] = "wrote"
            print("OK  historical players {0}: {1} players".format(period, period_snap.get("count")))
        except Exception as exc:
            result["actions"]["players_stats"] = "error"
            print("ERR historical players {0}: {1}".format(period, exc))
    else:
        result["actions"]["players_stats"] = "kept_existing"
        print("SKIP historical players {0}: snapshot kept".format(period))

    return result


def fetch_historical_through(upto_period, config=None, cookie=None, out_dir=None, league_info=None, player_ids=None, now=None):
    config = config or load_config()
    out_dir = _resolve_out_dir(config, out_dir)
    migrate_legacy_snapshots(out_dir)
    print("=== Fetching HISTORICAL periods 1–{0} ===".format(int(upto_period)))
    results = {}
    for period in range(1, int(upto_period) + 1):
        results[period] = fetch_historical_period(
            period,
            config=config,
            cookie=cookie,
            out_dir=out_dir,
            league_info=league_info,
            player_ids=player_ids,
            now=now,
        )
    return results


def fetch_all(config=None, periods=None, cookie=None, out_dir=None, skip_optional=False):
    """Compatibility wrapper: current fetch, then historical periods."""
    del skip_optional
    config = config or load_config()
    out_dir = _resolve_out_dir(config, out_dir)
    current = fetch_current_data(config=config, cookie=cookie, out_dir=out_dir)
    if not periods:
        inferred = infer_current_period(current.get("rosters"), current.get("league_info"))
        if inferred is None:
            raise RuntimeError("Could not infer current period from current rosters or league_info.")
        periods = list(range(1, inferred + 1))
    fetch_historical_through(
        max(periods),
        config=config,
        cookie=cookie,
        out_dir=out_dir,
        league_info=current.get("league_info"),
        player_ids=current.get("player_ids"),
    )
    snapshots = {
        "league_info": current.get("league_info"),
        "team_rosters": current.get("rosters"),
        "standings": current.get("standings"),
        "players_season": current.get("season_players"),
        "player_ids": current.get("player_ids"),
        "periods": list(periods),
        "current": current,
    }
    return snapshots


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    periods = []
    for arg in argv:
        try:
            periods.append(int(arg))
        except ValueError:
            pass

    try:
        fetch_all(periods=periods or None)
    except Exception as exc:
        print("FETCH FAILED: {0}".format(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
