"""Weekly-projection fallback used for other teams' rosters (trade lookups)."""

from types import SimpleNamespace

from ffmonitor.platforms.espn import _weekly_projection


def test_uses_weekly_projected_points_when_available():
    p = SimpleNamespace(stats={2: {"projected_points": 15.4}},
                        projected_avg_points=9.0, avg_points=8.0)
    assert _weekly_projection(p, 2) == 15.4


def test_falls_back_to_projected_avg_then_avg():
    p = SimpleNamespace(stats={}, projected_avg_points=11.2, avg_points=8.0)
    assert _weekly_projection(p, 2) == 11.2

    p2 = SimpleNamespace(stats=None, projected_avg_points=None, avg_points=7.5)
    assert _weekly_projection(p2, 2) == 7.5


def test_returns_none_when_nothing_available():
    p = SimpleNamespace(stats=None, projected_avg_points=None, avg_points=None)
    assert _weekly_projection(p, 2) is None
