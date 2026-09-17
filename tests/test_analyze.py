"""Start/sit slot-eligibility: the model must never suggest an illegal swap."""

from ffmonitor.analyze import _bench_beats_starter, _can_fill
from ffmonitor.config import Thresholds


def _p(name, pos, slot, lslot, proj):
    return {"name": name, "position": pos, "slot": slot, "lineup_slot": lslot,
            "proj_points": proj, "injury_status": "ACTIVE", "on_bye": False}


def test_can_fill_respects_required_slots():
    te_starter = _p("Fannin", "TE", "starter", "TE", 10.1)
    flex_starter = _p("Flex WR", "WR", "starter", "RB/WR/TE", 9.0)
    # A WR cannot fill a required TE slot, but can fill a FLEX slot.
    assert _can_fill("WR", te_starter) is False
    assert _can_fill("WR", flex_starter) is True
    # A QB only fits a QB (or superflex) slot.
    assert _can_fill("QB", te_starter) is False
    assert _can_fill("QB", _p("SF", "QB", "starter", "OP", 18)) is True


def test_can_fill_falls_back_to_same_position_without_slot():
    # No lineup_slot (e.g. Sleeper / old snapshot) -> same-position swaps only.
    starter = {"position": "TE"}
    assert _can_fill("TE", starter) is True
    assert _can_fill("WR", starter) is False


def test_no_illegal_te_swap_but_legal_flex_swap():
    snap = {"roster": [
        _p("Fannin", "TE", "starter", "TE", 10.1),       # required TE slot
        _p("Odunze", "WR", "bench", "bench", 12.3),        # better WR on bench
        _p("Flex WR", "WR", "starter", "RB/WR/TE", 9.0),   # a real FLEX starter
    ]}
    flags = _bench_beats_starter("espn:BBL", snap, Thresholds())
    msgs = " ".join(f["message"] for f in flags)
    assert "over Fannin" not in msgs          # never bench the only TE for a WR
    assert "Odunze" in msgs and "Flex WR" in msgs  # WR->FLEX swap is fine


def _pt(name, pos, slot, proj, team):
    return {"name": name, "position": pos, "slot": slot, "lineup_slot": slot.upper(),
            "proj_points": proj, "pro_team": team, "injury_status": "ACTIVE",
            "on_bye": False}


def test_vegas_note_added_when_environments_differ():
    th = Thresholds()
    snap = {"roster": [_pt("S", "WR", "starter", 10.0, "MIA"),
                       _pt("B", "WR", "bench", 12.5, "BUF")]}  # close ~2.5 pt call
    plain = _bench_beats_starter("x", snap, th)[0]
    assert "Vegas" not in plain["message"]  # no odds -> no note
    with_odds = _bench_beats_starter("x", snap, th, {"BUF": 27.0, "MIA": 17.0})[0]
    assert "Vegas backs it: BUF" in with_odds["message"]


def test_vegas_note_warns_when_environment_disagrees():
    th = Thresholds()
    snap = {"roster": [_pt("S", "WR", "starter", 10.0, "BUF"),
                       _pt("B", "WR", "bench", 12.5, "MIA")]}  # bench team scores less
    f = _bench_beats_starter("x", snap, th, {"BUF": 27.0, "MIA": 17.0})[0]
    assert "but Vegas favors BUF" in f["message"]
