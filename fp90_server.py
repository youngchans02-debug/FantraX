#!/usr/bin/env python3
"""Local Fantrax FP/90 explorer. python3 fp90_server.py → http://127.0.0.1:8765/"""

from __future__ import print_function

import json
import sys
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from data_layout import utc_now_iso
from fantrax_report_fetcher import fetch_current_data, load_config, load_current_cache

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
SNAPSHOT_PATH = WEB_DIR / "fp90.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}


def _round(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def _team_names(league_info, rosters):
    names = {}
    for team_id, info in (league_info.get("teamInfo") or {}).items():
        if isinstance(info, dict):
            names[str(team_id)] = info.get("name") or info.get("shortName") or str(team_id)
        else:
            names[str(team_id)] = str(info)
    for team_id, team in ((rosters or {}).get("rosters") or {}).items():
        tid = str(team_id)
        if not names.get(tid):
            names[tid] = team.get("teamName") or tid
    return names


def owner_index(rosters, league_info):
    names = _team_names(league_info, rosters)
    owned = {}
    for team_id, team in ((rosters or {}).get("rosters") or {}).items():
        tid = str(team_id)
        team_name = team.get("teamName") or names.get(tid) or tid
        for item in team.get("rosterItems") or []:
            player_id = str(item.get("id") or "").strip()
            if not player_id:
                continue
            owned[player_id] = {
                "teamId": tid,
                "teamName": team_name,
            }
    return owned


def minutes_source_label(source):
    text = str(source or "")
    if not text or text == "assumed_zero":
        return None
    if "FPL" in text:
        return "FPL"
    return "Fantrax"


def estimate_gp(player):
    gp = player.get("gp")
    if gp is not None:
        try:
            return float(gp)
        except (TypeError, ValueError):
            pass
    fpts = float(player.get("fpts") or 0)
    fp_g = float(player.get("fp_g") or 0)
    if fp_g > 0:
        return round(fpts / fp_g, 2)
    return None


def build_fp90_payload(cache=None, config=None):
    """Compute FP/90 rows from cached current/ data only. Does not call Fantrax."""
    config = config or load_config()
    cache = cache or load_current_cache(None, config=config)
    season = cache.get("season_players") or {}
    rosters = cache.get("rosters") or {}
    league_info = cache.get("league_info") or {}
    metadata = cache.get("metadata") or {}
    owned = owner_index(rosters, league_info)

    rows = []
    missing_minutes = 0
    for player in season.get("players") or []:
        player_id = str(player.get("id") or "").strip()
        if not player_id:
            continue
        minutes = player.get("minutes")
        try:
            minutes = float(minutes) if minutes is not None else None
        except (TypeError, ValueError):
            minutes = None
        fpts = float(player.get("fpts") or 0)
        fp_g = float(player.get("fp_g") or 0)
        gp = estimate_gp(player)
        fp90 = None
        if minutes is not None and minutes > 0:
            fp90 = fpts / minutes * 90.0
        else:
            if minutes is None:
                missing_minutes += 1
        owner = owned.get(player_id)
        rows.append(
            {
                "id": player_id,
                "player": player.get("player") or player.get("short_name") or player_id,
                "position": player.get("position") or "",
                "eplTeam": player.get("epl_team") or "",
                "fpts": _round(fpts),
                "fpG": _round(fp_g),
                "minutes": _round(minutes, 1) if minutes is not None else None,
                "gp": _round(gp, 2) if gp is not None else None,
                "fp90": _round(fp90),
                "minutesSource": minutes_source_label(player.get("minutes_source")),
                "rosterStatus": "Rostered" if owner else "Available",
                "ownerTeamId": (owner or {}).get("teamId"),
                "ownerTeamName": (owner or {}).get("teamName"),
            }
        )

    rows.sort(key=lambda row: ((row["fp90"] is None), -(row["fp90"] or 0), row["player"]))
    fetched_at = metadata.get("fetchedAt") or season.get("fetchedAt") or cache.get("fetched_at")
    minutes_meta = season.get("minutesMeta") or {}
    return {
        "ok": True,
        "generatedAt": utc_now_iso(),
        "lastUpdated": fetched_at,
        "count": len(rows),
        "missingMinutes": missing_minutes,
        "minutesMeta": minutes_meta,
        "formula": "FP/90 = FPts / Min * 90",
        "rows": rows,
    }


def public_fp90_payload(payload=None):
    """Snapshot for GitHub Pages: GET-only JSON, no Fantrax refresh."""
    payload = payload or build_fp90_payload()
    return {
        "ok": True,
        "generatedAt": payload.get("generatedAt"),
        "lastUpdated": payload.get("lastUpdated"),
        "count": payload.get("count"),
        "missingMinutes": payload.get("missingMinutes"),
        "formula": payload.get("formula"),
        "rows": payload.get("rows") or [],
    }


def write_fp90_snapshot(payload=None):
    public = public_fp90_payload(payload)
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(public, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return public


class FP90Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[fp90] " + (fmt % args))

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/fp90":
            try:
                self._send_json(200, build_fp90_payload())
            except Exception as exc:
                traceback.print_exc()
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if path in ("/", "/index.html", "/fp90.html"):
            page = WEB_DIR / "index.html"
            if not page.exists():
                page = WEB_DIR / "fp90.html"
            self._serve_file(page)
            return
        rel = path.lstrip("/")
        candidate = (WEB_DIR / rel).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._send(404, "Not found")
            return
        if candidate.is_file():
            self._serve_file(candidate)
            return
        self._send(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/refresh":
            self._send(404, "Not found")
            return
        try:
            fetch_current_data(include_minutes=True)
            payload = build_fp90_payload()
            write_fp90_snapshot(payload)
            payload["refreshed"] = True
            payload["snapshot"] = str(SNAPSHOT_PATH)
            self._send_json(200, payload)
        except Exception as exc:
            traceback.print_exc()
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _serve_file(self, path):
        if not path.exists():
            self._send(404, "Not found")
            return
        content_type = MIME.get(path.suffix.lower(), "application/octet-stream")
        self._send(200, path.read_bytes(), content_type)


def main():
    if "--export" in sys.argv:
        payload = write_fp90_snapshot()
        print(
            "Wrote {0} ({1} players, lastUpdated={2})".format(
                SNAPSHOT_PATH, payload.get("count"), payload.get("lastUpdated")
            )
        )
        return
    try:
        written = write_fp90_snapshot()
        print(
            "Snapshot {0} ({1} players)".format(
                SNAPSHOT_PATH, written.get("count")
            )
        )
    except Exception:
        traceback.print_exc()
        print("Snapshot export failed; serving without updating web/fp90.json")
    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), FP90Handler)
    print("FP/90 explorer: http://{0}:{1}/".format(DEFAULT_HOST, DEFAULT_PORT))
    print("GET /api/fp90  cached current data")
    print("POST /api/refresh  fetch Fantrax, then write web/fp90.json")
    print("python3 fp90_server.py --export  write snapshot and exit")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
