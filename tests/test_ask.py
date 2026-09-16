"""The ask context builder: compact, grounded, no network."""

from pathlib import Path

from ffmonitor import ask


def test_fmt_player_includes_proj_range_and_status():
    p = {"name": "Rome Odunze", "position": "WR", "proj_points": 12.3,
         "proj_floor": 3.3, "proj_ceiling": 23.6, "injury_status": "QUESTIONABLE"}
    s = ask._fmt_player(p)
    assert "Rome Odunze (WR)" in s and "proj 12.3" in s
    assert "[3.3-23.6]" in s and "QUESTIONABLE" in s


def test_build_context_summarizes_league(tmp_path: Path):
    snap = {"platforms": {"espn:BBL": {
        "enabled": True, "week": 2, "league_name": "BBL", "team_name": "Jamo",
        "opponent": "Rivals",
        "roster": [
            {"name": "Allen", "position": "QB", "slot": "starter", "proj_points": 22.1},
            {"name": "Odunze", "position": "WR", "slot": "bench", "proj_points": 12.3},
        ],
        "free_agents": [{"name": "FA", "position": "WR", "proj_points": 11.0}]}},
        "track_record": {"start_sit": {"graded": 10, "correct": 7,
                                       "accuracy": 70.0, "avg_pts_gained": 3.2}}}
    ctx = ask.build_context(snap, tmp_path)
    assert "BBL" in ctx and "week 2" in ctx and "vs Rivals" in ctx
    assert "Allen" in ctx and "Odunze" in ctx and "FA" in ctx
    assert "Start/sit record: 7-3" in ctx  # track record included


def test_build_context_empty_when_no_data(tmp_path: Path):
    assert ask.build_context({}, tmp_path) == "No league data available."
