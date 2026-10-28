#!/usr/bin/env python3
"""Fetch Fantrax data (optional) and write the weekly match report markdown."""

from __future__ import print_function

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from analytics import (
    build_weekly_model,
    latest_completed_period,
    load_config,
    load_report_inputs,
    parse_fantrax_datetime,
)
from data_layout import historical_path, migrate_legacy_snapshots, write_json as write_layout_json
from fantrax_report_fetcher import (
    ROOT,
    fetch_current_data,
    fetch_historical_through,
    load_current_cache,
)
from manager_analysis import build_historical_period_analysis
from player_rankings import format_ranking_block
from preview import build_next_round_preview


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Generate the Fantrax weekly match report.")
    parser.add_argument("--period", type=int, default=None, help="Focus period, e.g. 8")
    parser.add_argument("--skip-fetch", action="store_true", help="Use already saved JSON only")
    parser.add_argument("--config", default=None, help="Path to config.json")
    parser.add_argument(
        "--as-of",
        default=None,
        help="Evaluate period completion at this timestamp, e.g. 2026-10-25T12:00:00-04:00",
    )
    return parser.parse_args(argv)


def fmt_score(value):
    if value is None:
        return "-"
    return "{0:.2f}".format(value)


def fmt_prob(value):
    if value is None:
        return "-"
    return "{0:.1f}%".format(value * 100.0)


def fmt_luck(value):
    if value is None:
        return "-"
    return "{0:+.3f}".format(value)


def fmt_change(value):
    if value is None:
        return "NEW"
    if value > 0:
        return "▲{0}".format(value)
    if value < 0:
        return "▼{0}".format(abs(value))
    return "—"


def fmt_date(raw):
    if not raw:
        return None
    text = str(raw)
    return text[:10] if len(text) >= 10 else text


def team_label(team):
    return team.get("teamName") or team.get("name") or team.get("teamId") or "?"


def winner_loser(matchup):
    home = matchup["home"]
    away = matchup["away"]
    if matchup.get("winnerId") == home.get("teamId"):
        return home, away
    if matchup.get("winnerId") == away.get("teamId"):
        return away, home
    return home, away


def render_matchup_block(matchup):
    home = matchup["home"]
    away = matchup["away"]
    if matchup.get("result") == "tie":
        verdict = "TIE"
    else:
        top, _bot = winner_loser(matchup)
        verdict = "{0} W".format(team_label(top))
    return [
        "- **{0}** {1} — {2} **{3}** ({4})".format(
            team_label(home),
            fmt_score(home.get("score")),
            fmt_score(away.get("score")),
            team_label(away),
            verdict,
        )
    ]


def matchup_comment(matchup):
    home = team_label(matchup["home"])
    away = team_label(matchup["away"])
    margin = matchup.get("margin")
    if matchup.get("result") == "tie":
        return "{0}와 {1}는 {2}점으로 비겼다.".format(home, away, fmt_score(matchup["home"]["score"]))
    top, bot = winner_loser(matchup)
    return "{0}가 {1}를 {2}점 차로 이겼다 ({3}–{4}).".format(
        team_label(top),
        team_label(bot),
        fmt_score(margin),
        fmt_score(top.get("score")),
        fmt_score(bot.get("score")),
    )


def score_ranks(luck_rows):
    ranked = sorted(luck_rows or [], key=lambda x: (-x.get("score", 0), x.get("teamName") or ""))
    return {row["teamId"]: idx + 1 for idx, row in enumerate(ranked)}


def potw_comment(player, label):
    if not player:
        return "data unavailable"
    pts = fmt_score(player.get("fpts"))
    team = player.get("fantasy_team") or player.get("csv_status") or "FA"
    return "이번 GW {0} 1위 {1} FPts ({2}).".format(label, pts, team)


def power_notes(rankings):
    notes = []
    if not rankings:
        return notes
    leader = rankings[0]
    notes.append(
        "1위 {0}: Power {1}, 시즌 평균 {2}, 최근 3R {3}, Exp Win% {4}.".format(
            leader["teamName"],
            fmt_score(leader["power_score"]),
            fmt_score(leader["season_avg"]),
            fmt_score(leader["recent_avg"]),
            fmt_prob(leader["expected_win_pct"]),
        )
    )
    movers = [row for row in rankings if row.get("rank_change") not in (None, 0)]
    if movers:
        up = max(movers, key=lambda x: x.get("rank_change") or 0)
        down = min(movers, key=lambda x: x.get("rank_change") or 0)
        if (up.get("rank_change") or 0) > 0:
            notes.append(
                "급상승 {0}: {1} → {2}위 ({3}).".format(
                    up["teamName"],
                    up.get("prev_rank"),
                    up["rank"],
                    fmt_change(up.get("rank_change")),
                )
            )
        if (down.get("rank_change") or 0) < 0:
            notes.append(
                "급하락 {0}: {1} → {2}위 ({3}).".format(
                    down["teamName"],
                    down.get("prev_rank"),
                    down["rank"],
                    fmt_change(down.get("rank_change")),
                )
            )
    return notes


def luck_story(row, ranks, period, title):
    if not row:
        return ["data unavailable"]
    field = 13
    beat = int(round(row["expected_win"] * field))
    rank = ranks.get(row["teamId"])
    lines = [
        "**GW{0} {1} — {2}**".format(period, title, row["teamName"]),
        "",
        "{0}점, 전체 {1}위 점수. 상대 {2} {3}. Actual {4:.1f} / Expected {5:.3f} / Round Luck {6}.".format(
            fmt_score(row.get("score")),
            rank if rank is not None else "?",
            row.get("opponentName") or "?",
            fmt_score(row.get("opponentScore")),
            row.get("actual_win") or 0,
            row.get("expected_win") or 0,
            fmt_luck(row.get("round_luck")),
        ),
        "같은 GW 다른 {0}팀 기준, 이길 수 있는 상대는 {1}팀이었다.".format(field, beat),
    ]
    return lines


def render_potw_block(player, heading, label):
    lines = ["### {0}".format(heading), ""]
    if not player:
        lines.append("data unavailable")
        lines.append("")
        return lines
    lines.append("- **Player:** {0}".format(player.get("player") or "?"))
    lines.append("- **Fantasy Team:** {0}".format(player.get("fantasy_team") or player.get("csv_status") or "FA"))
    lines.append("- **EPL Team:** {0}".format(player.get("epl_team") or "?"))
    lines.append("- **FPts:** {0}".format(fmt_score(player.get("fpts"))))
    lines.append("- **Note:** {0}".format(potw_comment(player, label)))
    lines.append("")
    return lines


def render_markdown(model):
    period = model["period"]
    meta = model.get("period_meta") or {}
    start = fmt_date(meta.get("startDate"))
    end = fmt_date(meta.get("endDate"))
    power = model["power_ranking"]
    weights = power.get("weights") or {}
    preview = model.get("preview") or {}
    potw = model["potw"]
    luck = model["luck"]
    results = model["round_results"]
    sources = model.get("player_sources") or {}

    lines = []
    lines.append("# {0} — GW {1} Weekly Report".format(model["league_name"], period))
    lines.append("")
    if start and end:
        lines.append("Scoring window: {0} ~ {1}".format(start, end))
        lines.append("")
    if model.get("period_note"):
        lines.append("> {0}".format(model["period_note"]))
        lines.append("")

    # 1. Round Results
    lines.append("## 1. Round Results")
    lines.append("")
    lines.append("### Matchups")
    lines.append("")
    if not results.get("started"):
        lines.append("data unavailable — 이 period는 아직 시작되지 않았습니다.")
        lines.append("")
    else:
        for matchup in results["matchups"]:
            lines.extend(render_matchup_block(matchup))
            lines.append("  - {0}".format(matchup_comment(matchup)))
        lines.append("")
        lines.append("### Round Highlights")
        lines.append("")
        if results["highest"]:
            t = results["highest"][0]
            lines.append("- **Highest Score:** {0} ({1})".format(t["teamName"], fmt_score(t["score"])))
        if results["lowest"]:
            t = results["lowest"][0]
            lines.append("- **Lowest Score:** {0} ({1})".format(t["teamName"], fmt_score(t["score"])))
        if results["largest_margin"]:
            m = results["largest_margin"][0]
            top, bot = winner_loser(m)
            lines.append(
                "- **Largest Margin:** {0} ({1} {2}–{3} {4})".format(
                    fmt_score(m["margin"]),
                    team_label(top),
                    fmt_score(top.get("score")),
                    fmt_score(bot.get("score")),
                    team_label(bot),
                )
            )
        if results["closest"]:
            m = results["closest"][0]
            lines.append(
                "- **Closest Match:** {0}점 차 — {1} {2}–{3} {4}".format(
                    fmt_score(m["margin"]),
                    team_label(m["home"]),
                    fmt_score(m["home"]["score"]),
                    fmt_score(m["away"]["score"]),
                    team_label(m["away"]),
                )
            )
            if m.get("margin") is not None:
                lines.append(
                    "- {0}와 {1}는 {2}점 차의 이번 라운드 최고의 접전을 만들었다.".format(
                        team_label(m["home"]),
                        team_label(m["away"]),
                        fmt_score(m["margin"]),
                    )
                )
        lines.append("")

    # 2. POTW
    lines.append("## 2. Player of the Week")
    lines.append("")
    if not potw.get("available"):
        lines.append("data unavailable")
        lines.append("")
        lines.append(potw.get("reason") or "해당 GW 선수 FPts가 없습니다.")
        lines.append("")
    else:
        by_pos = potw.get("by_position") or {}
        lines.extend(render_potw_block(by_pos.get("G"), "Goalkeeper", "GK"))
        lines.extend(render_potw_block(by_pos.get("D"), "Defender", "DEF"))
        lines.extend(render_potw_block(by_pos.get("M"), "Midfielder", "MID"))
        lines.extend(render_potw_block(by_pos.get("F"), "Forward", "FWD"))
        lines.extend(render_potw_block(potw.get("overall"), "Overall POTW", "Overall"))
        lines.append("점수는 해당 GW FPts 기준. Source: `{0}`.".format(potw.get("source") or "players"))
        lines.append("")

    # 3. Power Ranking
    lines.append("## 3. Power Ranking")
    lines.append("")
    lines.append(
        "Standings가 아니라 현재 전력 지표. Formula: Season Avg {0:.0f}% / Recent {1}R {2:.0f}% / Expected Win% {3:.0f}%{4}.".format(
            weights.get("season_avg", 0) * 100,
            weights.get("recent_rounds", 3),
            weights.get("recent_avg", 0) * 100,
            weights.get("expected_win", 0) * 100,
            " (components min-max normalized)" if weights.get("normalize_components") else "",
        )
    )
    lines.append("")
    lines.append("| Rank | Team | Power | Season Avg | Recent 3R | Exp Win% | Prev | Move |")
    lines.append("| ---: | --- | ---: | ---: | ---: | ---: | ---: | :---: |")
    for row in power.get("rankings") or []:
        prev = row.get("prev_rank")
        lines.append(
            "| {rank} | {team} | {power} | {season} | {recent} | {exp} | {prev} | {chg} |".format(
                rank=row["rank"],
                team=row["teamName"],
                power=fmt_score(row["power_score"]),
                season=fmt_score(row["season_avg"]),
                recent=fmt_score(row["recent_avg"]),
                exp=fmt_prob(row["expected_win_pct"]),
                prev=prev if prev is not None else "—",
                chg=fmt_change(row.get("rank_change")),
            )
        )
    lines.append("")
    lines.append("### Power Ranking Notes")
    lines.append("")
    notes = power_notes(power.get("rankings") or [])
    if notes:
        for note in notes:
            lines.append("- {0}".format(note))
    else:
        lines.append("- data unavailable")
    lines.append("")

    # 4. Luck Index
    lines.append("## 4. Luck Index")
    lines.append("")
    lines.append(
        "Expected Win = 같은 GW 다른 13팀과 모두 붙었을 때의 승률 (동점 0.5). "
        "Actual Win = 승 1 / 무 0.5 / 패 0. Round Luck = Actual − Expected. "
        "Season Luck = Round Luck 합계."
    )
    lines.append("")
    ranks = score_ranks(luck.get("current") or [])
    lines.append("### GW {0} 억까왕".format(period))
    lines.append("")
    lines.extend(luck_story(luck.get("unlucky_king"), ranks, period, "억까왕"))
    lines.append("")
    lines.append("### GW {0} 꿀대진왕".format(period))
    lines.append("")
    lines.extend(luck_story(luck.get("lucky_king"), ranks, period, "꿀대진왕"))
    lines.append("")
    lines.append("### Season Luck Index")
    lines.append("")
    season_rows = sorted(luck.get("season") or [], key=lambda x: x["season_luck"])
    if not season_rows:
        lines.append("data unavailable")
        lines.append("")
    else:
        lines.append("| Rank | Team | Season Luck | Actual | Expected |")
        lines.append("| ---: | --- | ---: | ---: | ---: |")
        ordered = sorted(season_rows, key=lambda x: x["season_luck"])
        for idx, row in enumerate(ordered, start=1):
            lines.append(
                "| {0} | {1} | {2} | {3:.1f} | {4:.3f} |".format(
                    idx,
                    row["teamName"],
                    fmt_luck(row["season_luck"]),
                    row.get("season_actual") or 0,
                    row.get("season_expected") or 0,
                )
            )
        lines.append("")
        lines.append("위 표는 Season Luck 오름차순 (위가 억까, 아래가 꿀대진).")
        lines.append("")

    # 5. Preview
    lines.append("## 5. Next Round Preview")
    lines.append("")
    if not preview.get("available"):
        lines.append("data unavailable")
        lines.append("")
        lines.append(preview.get("reason") or "다음 라운드 스케줄이 없습니다.")
        lines.append("")
    else:
        lines.append("다음 라운드: **GW {0}**".format(preview.get("previewPeriod") or preview.get("period")))
        lines.append("")
        if not preview.get("has_projections"):
            lines.append("data unavailable — 시즌 FP/G가 없어 대진만 표시합니다.")
            lines.append("")
        for matchup in preview.get("matchups") or []:
            home = matchup.get("home") or {"teamName": matchup.get("teamA"), "expected_score": matchup.get("teamAProjectedScore")}
            away = matchup.get("away") or {"teamName": matchup.get("teamB"), "expected_score": matchup.get("teamBProjectedScore")}
            lines.append("### {0} vs {1}".format(team_label(home), team_label(away)))
            lines.append("")
            if preview.get("has_projections") and matchup.get("home_win_prob") is not None:
                lines.append("- **Projected Score:** {0} — {1}".format(
                    fmt_score(home.get("expected_score")),
                    fmt_score(away.get("expected_score")),
                ))
                lines.append("- **Win Probability:** {0} — {1}".format(
                    fmt_prob(matchup.get("home_win_prob")),
                    fmt_prob(matchup.get("away_win_prob")),
                ))
            else:
                lines.append("- Projected Score: data unavailable")
                lines.append("- Win Probability: data unavailable")
            lines.append("")

        scored = [m for m in preview.get("matchups") or [] if m.get("home_win_prob") is not None]
        watch = preview.get("watch")
        blowout = max(scored, key=lambda m: abs((m.get("home_win_prob") or 0.5) - 0.5)) if scored else None
        lines.append("### Matchup to Watch")
        lines.append("")
        if watch:
            lines.append(
                "- 가장 접전: **{0} vs {1}** (win prob {2} — {3})".format(
                    team_label(watch["home"]),
                    team_label(watch["away"]),
                    fmt_prob(watch.get("home_win_prob")),
                    fmt_prob(watch.get("away_win_prob")),
                )
            )
        else:
            lines.append("- data unavailable")
        if blowout:
            lines.append(
                "- 가장 일방적: **{0} vs {1}** (win prob {2} — {3})".format(
                    team_label(blowout["home"]),
                    team_label(blowout["away"]),
                    fmt_prob(blowout.get("home_win_prob")),
                    fmt_prob(blowout.get("away_win_prob")),
                )
            )
        lines.append("")

    fetched = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append("---")
    lines.append("")
    lines.append("Data period: GW {0}".format(period))
    lines.append("API fetched at: {0}".format(fetched))
    lines.append(
        "Power Ranking formula: season_avg {0:.0f}% / recent_{1}r {2:.0f}% / expected_win% {3:.0f}% (normalize={4})".format(
            weights.get("season_avg", 0) * 100,
            weights.get("recent_rounds", 3),
            weights.get("recent_avg", 0) * 100,
            weights.get("expected_win", 0) * 100,
            weights.get("normalize_components"),
        )
    )
    if preview.get("has_projections"):
        lines.append(
            "Prediction model: CURRENT ACTIVE roster season FP/G sum; win prob = logistic((home-away)/σ), σ={0:.1f}".format(
                preview.get("sigma") or 0
            )
        )
        lines.append(
            "Preview source: {0} + {1} (fetched {2}, usedCachedData={3})".format(
                (preview.get("sources") or {}).get("rosters") or "current/team_rosters.json",
                (preview.get("sources") or {}).get("players") or "current/players_season.json",
                preview.get("sourceDataFetchedAt") or "unknown",
                preview.get("usedCachedData"),
            )
        )
    else:
        lines.append("Prediction model: schedule only (player FP/G unavailable)")
    lines.append("Player sources: potw={0}".format(sources.get("potw") or "unavailable"))
    lines.append("Join keys: teamId, player ID. Zero scores are kept as recorded.")
    lines.append("")
    return "\n".join(lines)


def write_power_history(data_dir, power):
    path = Path(data_dir) / "power_ranking_history.json"
    history = {}
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            history = {}
    slim = [
        {
            "teamId": row["teamId"],
            "teamName": row["teamName"],
            "rank": row["rank"],
            "power_score": row["power_score"],
            "rank_change": row.get("rank_change"),
        }
        for row in power.get("rankings") or []
    ]
    history[str(power["period"])] = slim
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    write_layout_json(historical_path(data_dir, power["period"], "power_ranking.json"), power)


def print_preview_freshness(preview):
    print("Preview period: {0}".format(preview.get("previewPeriod")))
    print("Current Fantrax period: {0}".format(preview.get("currentFantraxPeriod")))
    print("Roster fetched at: {0}".format(preview.get("rosterFetchedAt")))
    print("Player stats fetched at: {0}".format(preview.get("playerStatsFetchedAt")))
    print("Player stats through GW: {0}".format(preview.get("playerStatsThroughPeriod")))
    print("Fresh fetch successful: {0}".format(preview.get("freshDataUsed")))
    print("Cached data used: {0}".format(preview.get("usedCachedData")))
    if preview.get("warning"):
        print("Warning: {0}".format(preview["warning"]))


def main(argv=None):
    args = parse_args(argv)
    config = load_config(args.config)
    paths = config.get("paths") or {}
    data_dir = Path(paths.get("data_dir") or "fantrax_data")
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    migrate_legacy_snapshots(data_dir)
    now = parse_fantrax_datetime(args.as_of) if args.as_of else None

    cached = load_current_cache(data_dir, config=config)
    if args.period is not None:
        result_period = args.period
    else:
        league_info = cached.get("league_info") or {}
        result_period = latest_completed_period(league_info, now=now)
        if result_period is None:
            raise RuntimeError(
                "No scoring period has passed its Fantrax endDate yet. "
                "Pass --period N to generate a report for an in-progress round."
            )

    if not args.skip_fetch:
        print("=== STEP 1: Historical data through GW {0} ===".format(result_period))
        fetch_historical_through(
            result_period,
            config=config,
            out_dir=data_dir,
            league_info=cached.get("league_info"),
            player_ids=cached.get("player_ids"),
            now=now,
        )
        print("")

    print("=== STEP 2: Historical analysis GW {0} ===".format(result_period))
    inputs = load_report_inputs(config=config, data_dir=data_dir)
    model = build_weekly_model(inputs, period=result_period, now=now)
    try:
        manager = build_historical_period_analysis(inputs, period=model["period"], now=now)
        summary = manager["summary"]
        print("Snapshot: {0}".format(summary.get("snapshotNote")))
        print("Wrote {0}".format(manager["paths"]["summary"]))
        print("Wrote {0}".format(manager["paths"]["analysis"]))
        rankings = manager.get("player_rankings") or {}
        if rankings:
            print("Wrote {0}".format(rankings.get("path")))
            src = rankings.get("source") or {}
            print("Player rankings source: {0}".format(rankings.get("dataSource")))
            print("Player rankings period: {0}".format(rankings.get("period")))
            print("Player rankings fetchedAt: {0}".format(rankings.get("fetchedAt")))
            print("Player rankings player count: {0}".format(rankings.get("playerCount")))
            all_rank = rankings.get("allPlayersRanking") or {}
            print("")
            print(format_ranking_block("GW{0} Top GK 5".format(model["period"]), all_rank.get("G") or [], 5))
            print("")
            print(format_ranking_block("GW{0} Top DEF 10".format(model["period"]), all_rank.get("D") or [], 10))
            print("")
            print(format_ranking_block("GW{0} Top MID 10".format(model["period"]), all_rank.get("M") or [], 10))
            print("")
            print(format_ranking_block("GW{0} Top FWD 10".format(model["period"]), all_rank.get("F") or [], 10))
            print("")
            vi = rankings.get("bestVI") or {}
            print("GW{0} Best VI (1 GK / 2 DEF / 2 MID / 1 FWD, all-player pool)".format(model["period"]))
            for pos, label in (("G", "GK"), ("D", "DEF"), ("M", "MID"), ("F", "FWD")):
                for row in vi.get(pos) or []:
                    print("  {0}: {1} ({2}) {3:.2f} — {4} [{5}]".format(
                        label,
                        row.get("playerName"),
                        row.get("eplTeam"),
                        row.get("gwPoints") or 0,
                        row.get("fantasyTeam") or "FA",
                        row.get("fantasyStatus") or "FA",
                    ))
    except Exception as exc:
        print("Manager analysis skipped: {0}".format(exc))
    write_power_history(inputs["data_dir"], model["power_ranking"])
    print("")

    print("=== STEP 3: CURRENT Fantrax fetch (Preview only) ===")
    if args.skip_fetch:
        current = cached
        current["used_cache"] = True
        current["fresh"] = False
        current["warning"] = "Fresh Fantrax fetch skipped (--skip-fetch). Cached data used."
        current["config"] = config
        current["out_dir"] = data_dir
        print(current["warning"])
    else:
        current = fetch_current_data(config=config, out_dir=data_dir)
    print("")

    print("=== STEP 4–5: Next-round Preview from current data ===")
    preview = build_next_round_preview(
        model["period"] + 1,
        current,
        payloads=inputs["payloads"],
        report_period=model["period"],
        now=now,
    )
    model["preview"] = preview
    print_preview_freshness(preview)
    print("")

    print("=== STEP 6: Report package ===")
    markdown = render_markdown(model)
    output_dir = Path(paths.get("output_dir") or "output")
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "gw_{0}_report.md".format(model["period"])
    out_path.write_text(markdown, encoding="utf-8")

    print("Report period: {0}".format(model["period"]))
    print("Latest completed period: {0}".format(model.get("latest_completed_period")))
    status = model.get("period_status") or {}
    if status:
        print("Period {0} state: {1}".format(status.get("period"), status.get("state")))
    if model.get("period_note"):
        print(model["period_note"])
    if model.get("validation") is not None:
        validation = model["validation"]
        if validation["ok"]:
            print("Period 7 validation: PASS ({0} matchups)".format(validation["matchup_count"]))
        else:
            print("Period 7 validation: FAIL missing={0} unmatched={1}".format(
                validation["missing"], validation["unmatched"]
            ))
    print("POTW available: {0}".format(model["potw"].get("available")))
    print("Preview available: {0}".format((model.get("preview") or {}).get("available")))
    print("Wrote {0}".format(out_path.resolve()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
