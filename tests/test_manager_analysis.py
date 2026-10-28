#!/usr/bin/env python3
"""Unit tests for legal XI, efficiency, and single-swap math."""

import unittest

from manager_analysis import (
    best_legal_xi,
    best_single_swap,
    is_legal_xi,
    legal_formations,
    load_lineup_rules,
)


def P(pid, name, pos, status, pts, elig=None):
    return {
        "playerId": pid,
        "playerName": name,
        "primaryPosition": pos,
        "position": pos,
        "eligiblePositions": elig or [pos],
        "fantasyStatus": status,
        "gwPoints": pts,
        "eplTeam": "TST",
        "normalizedStatus": None,
        "opponent": "",
        "icons": [],
    }


RULES = load_lineup_rules(
    {
        "rosterInfo": {
            "positionConstraints": {
                "G": {"maxActive": 1},
                "D": {"maxActive": 5},
                "M": {"maxActive": 5},
                "F": {"maxActive": 3},
            },
            "maxTotalActivePlayers": 11,
        }
    },
    {
        "lineup_rules": {
            "positions": {
                "G": {"min": 1, "max": 1},
                "D": {"min": 0, "max": 5},
                "M": {"min": 0, "max": 5},
                "F": {"min": 0, "max": 3},
            }
        }
    },
)


class LineupRulesTests(unittest.TestCase):
    def test_official_maxes_and_gk_min(self):
        self.assertEqual(RULES["positions"]["G"], {"min": 1, "max": 1})
        self.assertEqual(RULES["positions"]["D"]["max"], 5)
        self.assertEqual(RULES["positions"]["F"]["max"], 3)
        self.assertEqual(RULES["totalStarters"], 11)

    def test_legal_formations(self):
        forms = legal_formations(RULES)
        as_tuples = {(f["G"], f["D"], f["M"], f["F"]) for f in forms}
        self.assertIn((1, 4, 5, 1), as_tuples)
        self.assertIn((1, 3, 4, 3), as_tuples)
        self.assertIn((1, 5, 5, 0), as_tuples)
        self.assertIn((1, 2, 5, 3), as_tuples)
        self.assertNotIn((0, 5, 5, 1), as_tuples)
        self.assertNotIn((1, 5, 5, 1), as_tuples)
        for form in forms:
            self.assertEqual(sum(form.values()), 11)


class BestXiTests(unittest.TestCase):
    def setUp(self):
        self.players = [
            P("g1", "GK A", "G", "ACTIVE", 10),
            P("g2", "GK B", "G", "RESERVE", 2),
            P("d1", "DEF 1", "D", "ACTIVE", 8),
            P("d2", "DEF 2", "D", "ACTIVE", 7),
            P("d3", "DEF 3", "D", "ACTIVE", 6),
            P("d4", "DEF 4", "D", "ACTIVE", 1),
            P("d5", "DEF 5", "D", "RESERVE", 20),
            P("m1", "MID 1", "M", "ACTIVE", 9),
            P("m2", "MID 2", "M", "ACTIVE", 8),
            P("m3", "MID 3", "M", "ACTIVE", 7),
            P("m4", "MID 4", "M", "ACTIVE", 6),
            P("m5", "MID 5", "M", "ACTIVE", 0),
            P("f1", "FWD 1", "F", "ACTIVE", 5),
            P("f2", "FWD 2", "F", "RESERVE", 40),
            P("f3", "FWD 3", "F", "RESERVE", 3),
            P("m6", "MID 6", "M", "RESERVE", 1),
        ]

    def test_best_xi_uses_high_bench_and_legal_shape(self):
        best = best_legal_xi(self.players, RULES)
        ids = {p["playerId"] for p in best["players"]}
        self.assertIn("f2", ids)
        self.assertIn("d5", ids)
        self.assertNotIn("m5", ids)
        self.assertEqual(best["formation"]["G"], 1)
        self.assertEqual(sum(best["formation"].values()), 11)
        self.assertAlmostEqual(best["score"], 10 + 8 + 7 + 6 + 20 + 9 + 8 + 7 + 6 + 40 + 5)

    def test_zero_best_xi_is_zero(self):
        zeros = [P(str(i), "P{0}".format(i), pos, "ACTIVE", 0) for i, pos in enumerate("GDDDDMMMMFF")]
        best = best_legal_xi(zeros, RULES)
        self.assertEqual(best["score"], 0)

    def test_illegal_swap_rejected(self):
        active = [p for p in self.players if p["fantasyStatus"] == "ACTIVE"]
        # 4-5-1. Swapping a second GK in for a FWD is illegal (G max 1).
        reserve_gk = [p for p in self.players if p["playerId"] == "g2"]
        outgoing = [p for p in active if p["playerId"] != "f1"] + reserve_gk
        self.assertFalse(is_legal_xi(outgoing, RULES))


class SwapTests(unittest.TestCase):
    def test_best_swap_gain(self):
        active = [
            P("g1", "GK", "G", "ACTIVE", 10),
            P("d1", "D1", "D", "ACTIVE", 8),
            P("d2", "D2", "D", "ACTIVE", 7),
            P("d3", "D3", "D", "ACTIVE", 6),
            P("d4", "D4", "D", "ACTIVE", 1),
            P("m1", "M1", "M", "ACTIVE", 9),
            P("m2", "M2", "M", "ACTIVE", 8),
            P("m3", "M3", "M", "ACTIVE", 7),
            P("m4", "M4", "M", "ACTIVE", 6),
            P("m5", "M5", "M", "ACTIVE", 0),
            P("f1", "F1", "F", "ACTIVE", 5),
        ]
        reserve = [
            P("f2", "Jackson", "F", "RESERVE", 47.16),
            P("m6", "M6", "M", "RESERVE", 1),
            P("d5", "D5", "D", "RESERVE", 2),
            P("g2", "G2", "G", "RESERVE", 2),
            P("f3", "F3", "F", "RESERVE", 3),
        ]
        swap = best_single_swap(active, reserve, RULES)
        self.assertEqual(swap["playerIn"]["name"], "Jackson")
        self.assertEqual(swap["playerOut"]["name"], "M5")
        self.assertAlmostEqual(swap["gain"], 47.16)


class RankingTests(unittest.TestCase):
    def test_points_then_name_and_unique_ranks(self):
        from player_rankings import _rank_group

        rows = _rank_group(
            [
                {"playerId": "b", "playerName": "Beta", "gwPoints": 10},
                {"playerId": "a", "playerName": "Alpha", "gwPoints": 10},
                {"playerId": "c", "playerName": "Gamma", "gwPoints": 9},
            ],
            10,
        )
        self.assertEqual([r["playerName"] for r in rows], ["Alpha", "Beta", "Gamma"])
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
