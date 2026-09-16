"""Backtest feature prep must be leakage-safe (no future data leaks into a week).

Uses a tiny synthetic frame — no network / nflverse download.
"""

import pytest

pd = pytest.importorskip("pandas")

from ffmonitor.eval import backtest  # noqa: E402


def _frame():
    rows = []
    # One WR scoring 10, 20, 30 in weeks 1-3.
    for wk, pts in [(1, 10.0), (2, 20.0), (3, 30.0)]:
        rows.append(dict(player_id="p1", player_display_name="Player One",
                         position="WR", season=2024, week=wk, ppr=pts))
    return pd.DataFrame(rows)


def test_prepare_is_leakage_safe():
    d = backtest.prepare(_frame()).sort_values("week")
    by_week = {int(r.week): r for r in d.itertuples()}
    # Week 1 has no prior data -> projection undefined.
    assert pd.isna(by_week[1].roll3)
    assert pd.isna(by_week[1].ttd)
    # Week 2 uses ONLY week 1 (10.0), never week 2's own 20.0.
    assert by_week[2].ttd == 10.0
    assert by_week[2].roll3 == 10.0
    # Week 3 season-to-date = mean(10, 20) = 15.0 (excludes week 3's 30).
    assert by_week[3].ttd == 15.0


def test_forward_window_excludes_current_week():
    d = backtest.prepare(_frame()).sort_values("week")
    by_week = {int(r.week): r for r in d.itertuples()}
    # fut3 at week 1 = mean of weeks 2,3 = 25.0 (the outcome window).
    assert by_week[1].fut3 == 25.0
    assert by_week[1].fut_n == 2


def test_start_sit_returns_expected_shape():
    # Two players; the higher-projected one also scores more -> a correct call.
    rows = []
    for wk, a, b in [(1, 5, 4), (2, 6, 3), (3, 20, 2)]:
        rows.append(dict(player_id="hi", player_display_name="Hi", position="WR",
                         season=2024, week=wk, ppr=a))
        rows.append(dict(player_id="lo", player_display_name="Lo", position="WR",
                         season=2024, week=wk, ppr=b))
    d = backtest.prepare(pd.DataFrame(rows))
    res = backtest.backtest_start_sit(d, margin=0.5, pool_size=30)
    assert "accuracy" in res and 0 <= res["accuracy"] <= 100
