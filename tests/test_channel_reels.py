"""Кадры роликов для Instagram Reels: числа и тексты сценария (без браузера)."""

import json

import pytest

from parkan.channel import reels

FACTS = {"type": "parking", "price": 1_290_000, "median": 2_300_000,
         "address": "ул. Примерная, 1", "district": "Кузьминки", "area": 14.1}


def test_money_and_discount():
    assert reels.money(1_290_000) == ("1,29", "млн ₽")
    assert reels.money(2_300_000) == ("2,3", "млн ₽")
    assert reels.money(12_500_000) == ("12,5", "млн ₽")
    assert reels.money(850_000) == ("850", "тыс. ₽")
    assert reels.discount(1_290_000, 2_300_000) == 44


def test_frames_follow_script():
    fr = reels.frames(FACTS)
    assert [f["name"] for f in fr] == ["01-price", "02-place", "03-compare", "04-caveat", "05-cta"]
    assert "1,29" in fr[0]["html"] and "машино-место" in fr[0]["html"]
    assert "ул. Примерная, 1" in fr[1]["html"] and "ЮВАО" in fr[1]["html"] and "14,1 м²" in fr[1]["html"]
    assert "−44%" in fr[2]["html"] and "2,3 млн ₽" in fr[2]["html"]
    assert "стартовая" in fr[3]["html"]
    assert "Попутный метр" in fr[4]["html"]


def test_load_facts_requires_numbers(tmp_path):
    p = tmp_path / "f.json"
    p.write_text(json.dumps({"price": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="median"):
        reels.load_facts(p)
