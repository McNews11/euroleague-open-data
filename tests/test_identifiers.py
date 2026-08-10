"""Identifier normalisation.

These tests exist because of a real defect: Sergio Llull is 'TGB' in the v2 boxscore and
'PTGB' in the live play-by-play feed. A digits-only rule for stripping the 'P' prefix
silently split him into two different people, and the split was invisible -- totals still
reconciled, because each half was internally consistent.
"""

from euroleague_open_data.sources import strip_id
from euroleague_open_data.warehouse import _person_key_live, _person_key_v2


def test_strip_id_removes_upstream_padding():
    assert strip_id("P002328   ") == "P002328"
    assert strip_id("PAN       ") == "PAN"


def test_strip_id_treats_blank_as_absent():
    """Upstream encodes 'no player' as a run of spaces, not as null."""
    assert strip_id("          ") is None
    assert strip_id("") is None
    assert strip_id(None) is None


def test_numeric_codes_reconcile_across_sources():
    assert _person_key_v2("006590") == _person_key_live("P006590   ")


def test_alphabetic_codes_reconcile_across_sources():
    """The Llull case. A digits-only rule would leave 'PTGB' unstripped and split him."""
    assert _person_key_v2("TGB") == _person_key_live("PTGB      ")


def test_non_player_actors_are_preserved_not_stripped():
    """Coach events are legitimate play-by-play rows, not corrupt ones."""
    assert _person_key_live("CO_A") == "CO_A"
    assert _person_key_live("CO_B") == "CO_B"


def test_absent_actor_stays_absent():
    assert _person_key_live("          ") is None
