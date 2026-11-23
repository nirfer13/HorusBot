# -*- coding: utf-8 -*-
"""The giveaway's rank weights (ranksConfig.json, read by cogs/lotto.py at every $lotto).

Run from the repo root: python -m pytest tests/test_giveaway_ranks.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pairs():
    text = (ROOT / "ranksConfig.json").read_text(encoding="utf-8")
    return json.loads(text, object_pairs_hook=list)


def test_every_rank_has_one_positive_weight():
    pairs = _pairs()
    names = [name for name, _ in pairs]
    assert len(names) == len(set(names)), "a rank listed twice: the later weight would silently win"
    for name, weight in pairs:
        assert name == name.strip() and name
        assert isinstance(weight, (int, float)) and not isinstance(weight, bool) and weight > 0, name


def test_the_pet_collector_of_the_game_has_a_weight():
    # The AlterMMO game bot gives the role "Kolekcjoner Pupili" to the first place of its
    # collection ranking and takes it away again; $lotto knows a role only by its exact name.
    # Renaming the role on the server needs the same change here.
    weights = dict(_pairs())
    assert weights["Kolekcjoner Pupili"] == 4
    assert weights["Kolekcjoner Pupili"] == weights["Pogromca Alterage"]  # like the other "Top 1" rank
