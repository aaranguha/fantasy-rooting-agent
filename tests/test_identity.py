"""Canonical player identity across platforms (spec section 16)."""

from __future__ import annotations

import pytest

from app.models import CanonicalPlayer, normalize_name, normalize_position, normalize_team
from app.playerids import PlayerRegistry, espn_dst_team_id


@pytest.fixture()
def reg():
    r = PlayerRegistry()
    r.espn_team_abbrev = {21: "PHI", 6: "DAL", 12: "KC"}
    r.add(CanonicalPlayer("sleeper:4866", "Saquon Barkley", "RB", "PHI", "4866", 3929630))
    r.add(CanonicalPlayer("sleeper:11628", "Marvin Harrison", "WR", "ARI", "11628", None))
    r.add(CanonicalPlayer("sleeper:11533", "Brandon Aubrey", "K", "DAL", "11533", None))
    r.add(CanonicalPlayer("sleeper:6794", "Ja'Marr Chase", "WR", "CIN", "6794", 4362628))
    r.add(CanonicalPlayer("dst:PHI", "Philadelphia Eagles", "DST", "PHI", "PHI", None))
    # Two real players who share a name - the classic ambiguity trap.
    r.add(CanonicalPlayer("sleeper:2212", "Josh Allen", "QB", "BUF", "2212", 3918298))
    r.add(CanonicalPlayer("sleeper:5011", "Josh Allen", "LB", "JAX", "5011", 3929920))
    return r


def test_name_normalization_handles_suffixes_punctuation_and_case():
    assert normalize_name("Marvin Harrison Jr.") == "marvinharrison"
    assert normalize_name("Ja'Marr Chase") == "jamarrchase"
    assert normalize_name("A.J. Brown") == "ajbrown"
    assert normalize_name("Michael Pittman Jr") == "michaelpittman"
    assert normalize_name("Robert Griffin III") == "robertgriffin"
    assert normalize_name("Amon-Ra St. Brown") == "amonrastbrown"


def test_relocated_team_abbreviations_normalize():
    assert normalize_team("JAC") == "JAX"
    assert normalize_team("WAS") == "WSH"
    assert normalize_team("OAK") == normalize_team("LV") == "LV"
    assert normalize_team("SD") == "LAC"
    assert normalize_team(None) == ""


def test_position_spellings_collapse():
    assert normalize_position("D/ST") == normalize_position("DEF") == "DST"
    assert normalize_position("PK") == "K"
    assert normalize_position("FB") == "RB"


def test_hard_id_crosswalk_wins_over_names(reg):
    hit = reg.resolve(name="TOTALLY WRONG NAME", position="RB", team="XXX", espn_id=3929630)
    assert hit.key == "sleeper:4866"
    assert reg.by_sleeper_id("4866").name == "Saquon Barkley"


def test_player_without_an_espn_id_still_matches_by_name_team_position(reg):
    """Sleeper has no espn_id for Brandon Aubrey; the triple must carry it."""
    hit = reg.resolve(name="Brandon Aubrey", position="K", team="DAL", espn_id=17_000_001)
    assert hit.key == "sleeper:11533"


def test_suffix_mismatch_between_platforms_still_matches(reg):
    """ESPN says 'Marvin Harrison Jr.', Sleeper says 'Marvin Harrison'."""
    hit = reg.resolve(name="Marvin Harrison Jr.", position="WR", team="ARI")
    assert hit.key == "sleeper:11628"


def test_apostrophes_do_not_break_matching(reg):
    assert reg.resolve(name="JaMarr Chase", position="WR", team="CIN").key == "sleeper:6794"


def test_duplicate_names_are_disambiguated_by_team_and_position(reg):
    qb = reg.resolve(name="Josh Allen", position="QB", team="BUF")
    lb = reg.resolve(name="Josh Allen", position="LB", team="JAX")
    assert qb.key == "sleeper:2212" and lb.key == "sleeper:5011"


def test_a_traded_player_is_found_by_id_despite_a_stale_team(reg):
    """Team changed mid-week; the id crosswalk still resolves him."""
    hit = reg.resolve(name="Saquon Barkley", position="RB", team="NYG", espn_id=3929630)
    assert hit.key == "sleeper:4866"


def test_team_defenses_resolve_from_both_platforms(reg):
    assert reg.by_team_dst("PHI").key == "dst:PHI"
    assert reg.resolve(name="Eagles D/ST", position="DST", team="PHI").key == "dst:PHI"
    assert reg.resolve(espn_id=-16021, name="Eagles D/ST", position="DST", team="").key == "dst:PHI"
    assert espn_dst_team_id(-16006) == 6


def test_unmatchable_espn_player_is_minted_rather_than_dropped(reg):
    p = reg.ensure_espn(999999, "Rookie Nobody", "WR", "KC")
    assert p.key == "espn:999999" and p.espn_id == 999999
    assert reg.unmatched and "Rookie Nobody" in reg.unmatched[0]
    # ...and is stable on a second lookup.
    assert reg.ensure_espn(999999, "Rookie Nobody", "WR", "KC").key == p.key


def test_search_prefers_exact_names(reg):
    assert reg.search("Saquon Barkley")[0].key == "sleeper:4866"
    assert reg.search("saquon")[0].key == "sleeper:4866"
    assert reg.search("nobody at all") == []


def test_short_name_formatting():
    p = CanonicalPlayer("k", "Saquon Barkley", "RB", "PHI")
    assert p.short_name == "S. Barkley"
    dst = CanonicalPlayer("dst:PHI", "Philadelphia Eagles", "DST", "PHI")
    assert dst.short_name == "Philadelphia Eagles"
