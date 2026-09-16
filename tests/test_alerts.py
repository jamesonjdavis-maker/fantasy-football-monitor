"""Discord payload splitting must respect the 10-embed / 6000-char message caps."""

from ffmonitor import alerts


def _embed(title, n_fields, field_len):
    return {"title": title, "color": 0x2F9E44,
            "fields": [{"name": f"S{i}", "value": "x" * field_len, "inline": False}
                       for i in range(n_fields)]}


def test_chunk_splits_when_over_char_limit():
    # 8 embeds ~1240 chars each ≈ 10k total, over the 6000 cap.
    embeds = [_embed(f"L{i}", 4, 300) for i in range(8)]
    embeds[-1]["footer"] = {"text": "footer"}
    chunks = alerts._chunk_embeds(embeds)
    assert len(chunks) >= 2
    for group in chunks:
        assert len(group) <= alerts._DISCORD_MAX_EMBEDS
        total = sum(alerts._embed_len(e) for e in group)
        assert total <= alerts._DISCORD_MAX_CHARS or len(group) == 1
    # Nothing dropped, footer preserved somewhere.
    assert sum(len(g) for g in chunks) == 8
    assert any("footer" in e for g in chunks for e in g)


def test_single_oversized_embed_is_split_by_fields():
    big = _embed("Huge league", 20, 1000)  # ~20k chars in one embed
    big["footer"] = {"text": "f"}
    big["timestamp"] = "2026-09-16T13:00:00+00:00"
    chunks = alerts._chunk_embeds([big])
    fields_total = 0
    for g in chunks:
        for e in g:
            assert alerts._embed_len(e) <= alerts._DISCORD_MAX_CHARS
            fields_total += len(e["fields"])
    assert fields_total == 20  # every field kept


def test_small_digest_stays_one_message():
    assert len(alerts._chunk_embeds([_embed("One", 1, 50)])) == 1


def test_footer_appends_track_record():
    snap = {"platforms": {"espn:BBL": {"enabled": True, "label": "BBL",
                                        "team_name": "Jamo"}},
            "track_record": {"start_sit": {"graded": 17, "correct": 12,
                                           "accuracy": 70.6, "avg_pts_gained": 3.4}}}
    footer = alerts._footer(snap)
    assert "Start/sit record: 12-5" in footer
