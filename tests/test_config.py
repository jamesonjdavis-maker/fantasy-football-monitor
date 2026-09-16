"""Config parsing: multi-league string and the SWID brace-normalizing."""

from ffmonitor.config import _clean_swid, _parse_espn_leagues


def test_parse_multi_league_string():
    leagues = _parse_espn_leagues(
        "BBL:576110229:3, Masters:1436101602:12", None, None)
    assert [l.label for l in leagues] == ["BBL", "Masters"]
    assert leagues[0].league_id == 576110229 and leagues[0].team_id == 3
    assert leagues[1].team_id == 12


def test_parse_falls_back_to_single_league():
    leagues = _parse_espn_leagues(None, 12345, 7)
    assert len(leagues) == 1
    assert leagues[0].label == "ESPN" and leagues[0].league_id == 12345


def test_parse_skips_malformed_entries():
    leagues = _parse_espn_leagues("Good:111:1, broken, :222:2", None, None)
    assert [l.label for l in leagues] == ["Good"]


def test_clean_swid_wraps_braces():
    assert _clean_swid("ABC-123") == "{ABC-123}"
    assert _clean_swid("{ABC-123}") == "{ABC-123}"
    assert _clean_swid(None) is None
