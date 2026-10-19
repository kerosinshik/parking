"""Кадры видео для поста «ставка, которая не стала покупкой»: числа и тексты сценария (без браузера)."""

import json

import pytest

from parkan.channel import overbid

FACTS = {"address": "Фестивальная ул., 15, корп. 2", "district": "Левобережный", "place": "м/м 56", "area": 18.2,
         "start": 1_290_000, "bid": 2_128_887_000, "bid_words": "два миллиарда сто двадцать восемь миллионов",
         "bid_date": "21 ноября 2025", "resale_date": "5 марта 2026", "resale_price": 3_483_000, "months": "3,5",
         "word": "миллиард"}


def test_money_and_times():
    assert overbid.money(2_128_887_000) == ("2,13", "млрд ₽")
    assert overbid.money(3_483_000) == ("3,48", "млн ₽")
    assert overbid.exact(2_128_887_000) == "2 128 887 000 ₽"
    assert overbid.times(2_128_887_000, 1_290_000) == "1 650"
    assert overbid.times(2_128_887_000, 3_483_000) == "611"
    assert overbid.times(3_483_000, 1_290_000) == "2,7"


def test_frames_follow_script():
    fr = overbid.frames(FACTS)
    assert [f["name"] for f in fr] == ["01-bid", "02-place", "03-ratio", "04-words", "05-relist", "06-result",
                                       "07-why", "08-cta"]
    assert "2,13" in fr[0]["html"] and "млрд ₽" in fr[0]["html"]
    assert "м/м 56" in fr[1]["html"] and "18,2 м²" in fr[1]["html"] and "САО" in fr[1]["html"]
    assert "×1 650" in fr[2]["html"]
    assert "два миллиарда" in fr[3]["html"]
    assert "3,5" in fr[4]["html"] and "1,29 млн ₽" in fr[4]["html"]
    assert "в 611 раз" in fr[5]["html"] and "в 2,7 раза" in fr[5]["html"]
    assert "МИЛЛИАРД" in fr[7]["html"]
    assert all("подвал" not in f["html"].lower() for f in fr)


def test_load_facts(tmp_path):
    p = tmp_path / "f.json"
    p.write_text(json.dumps({**FACTS, "photo": "photo.jpg"}), encoding="utf-8")
    assert overbid.load_facts(p)["photo"] == str(tmp_path / "photo.jpg")
    p.write_text(json.dumps({"bid": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="resale_price"):
        overbid.load_facts(p)


def test_card_is_square_without_steps():
    html = overbid.card(FACTS)
    assert "height:1080px" in html and 'class="steps"' not in html and "2,13" in html
