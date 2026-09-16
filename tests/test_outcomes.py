"""Live outcome logging: record, dedup, grade-when-final, and summarize."""

import json

from ffmonitor import outcomes


def _flag():
    return {"platform": "espn:BBL", "kind": "bench_over_starter",
            "message": "Start Bench Guy over Start Guy",
            "bench_player": {"id": "b1", "name": "Bench Guy", "position": "WR",
                             "proj_points": 12.3},
            "starter": {"id": "s1", "name": "Start Guy", "position": "WR",
                        "proj_points": 9.0}}


def _snap(week, actuals=None):
    roster = []
    if actuals is not None:
        roster = [{"id": "b1", "name": "Bench Guy", "position": "WR",
                   "actual_points": actuals[0]},
                  {"id": "s1", "name": "Start Guy", "position": "WR",
                   "actual_points": actuals[1]}]
    return {"generated_at": f"2026-09-{7 + week:02d}T15:30:00+00:00",
            "platforms": {"espn:BBL": {"enabled": True, "week": week,
                                       "league_name": "BBL", "roster": roster}}}


def test_record_and_dedup(tmp_path):
    assert outcomes.record(tmp_path, _snap(1), [_flag()]) == 1
    assert outcomes.record(tmp_path, _snap(1), [_flag()]) == 0  # same call, no dup


def test_not_graded_until_week_is_final(tmp_path):
    outcomes.record(tmp_path, _snap(1), [_flag()])
    # Still week 1 -> the call's week isn't complete, nothing to grade.
    assert outcomes.grade(tmp_path, _snap(1)) == 0


def test_grade_and_summary(tmp_path):
    outcomes.record(tmp_path, _snap(1), [_flag()])
    # A finalized week-1 snapshot on disk (bench 20.5 beat starter 10.0).
    (tmp_path / "snapshot-2026-09-09.json").write_text(
        json.dumps(_snap(1, actuals=(20.5, 10.0))))
    # Now the run is week 2, so week 1 is complete and gradable.
    assert outcomes.grade(tmp_path, _snap(2)) == 1

    track = outcomes.summary(tmp_path)
    ss = track["start_sit"]
    assert ss["graded"] == 1 and ss["correct"] == 1
    assert ss["accuracy"] == 100.0 and ss["avg_pts_gained"] == 10.5
    assert "1-0" in outcomes.summary_line(track)


def test_incorrect_call_is_scored(tmp_path):
    outcomes.record(tmp_path, _snap(1), [_flag()])
    (tmp_path / "snapshot-2026-09-09.json").write_text(
        json.dumps(_snap(1, actuals=(5.0, 14.0))))  # bench flopped
    outcomes.grade(tmp_path, _snap(2))
    track = outcomes.summary(tmp_path)
    assert track["start_sit"]["correct"] == 0
    assert track["start_sit"]["avg_pts_gained"] == -9.0
