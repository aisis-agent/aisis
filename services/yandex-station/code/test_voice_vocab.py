"""The Whisper prompt: home vocabulary only, no one-word imperatives, capped, and never built from a degraded catalog."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import voice_vocab
from voice_vocab import Catalog, CatalogUnavailable, estimate_tokens, fetch_catalog, spoken_entity_names, whisper_prompt

HOME = Catalog(
    entities=("Кухня", "Студия", "Глобус", "Кондей 2", "Гирлянда", "Люстра"),
    areas=("Гостиная", "Кухня", "Спальня", "Студия", "Угол", "Второй этаж", "Прихожая"),
    artists=("Eminem", "Drummatix", "Amy Winehouse", "Adele", "Би-2", "Звери"))

# Whisper continues the prompt in its own style on room noise, so no sentence of it may be a bare one-word command: those
# came back as «Громкость.», «Пауза.», «Дальше.», «Включи музыку.» (15 of 59 utterances sent to the adapter between
# 2026-11-09 and 11-15). The wake word is no part of the prompt either: a sentence of its own comes back as an utterance.


def sentences(prompt: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", prompt) if part.strip()]


@pytest.mark.parametrize("catalog", [Catalog(), HOME])
def test_no_sentence_of_the_prompt_is_a_single_word_and_the_wake_word_is_absent(catalog):
    prompt = whisper_prompt(catalog)
    for sentence in sentences(prompt):
        assert len(re.sub(r"[^\w ]+", " ", sentence).split()) > 1, sentence
    assert "алиса" not in prompt.lower()


def test_the_prompt_stays_inside_its_token_cap_however_long_the_catalog_is():
    big = Catalog(entities=tuple(f"Лампа номер {name}" for name in ("один", "два", "три", "четыре", "пять", "шесть", "семь")),
                  areas=tuple(f"Комната {name}" for name in ("первая", "вторая", "третья", "четвертая", "пятая", "шестая", "седьмая")),
                  artists=tuple(f"Исполнитель {name}" for name in ("Аа", "Бб", "Вв", "Гг", "Дд", "Ее", "Жж", "Зз")))
    for cap in (60, 100, 150):
        assert estimate_tokens(whisper_prompt(big, max_tokens=cap)) <= cap


def test_the_prompt_carries_the_climate_words_and_the_live_home_names():
    prompt = whisper_prompt(HOME)
    for word in ("кондиционер", "обогрев", "охлаждение", "второй этаж", "кондей 2", "гостиная"):
        assert word in prompt.lower(), word


def test_the_second_floor_survives_a_long_room_list():
    areas = ("Гостиная", "Кухня", "Спальня", "Студия", "Угол", "Второй этаж", "Прихожая")
    assert "второй этаж" in whisper_prompt(Catalog(areas=areas))


def test_without_home_assistant_the_prompt_is_still_the_static_one():
    prompt = whisper_prompt(Catalog())
    assert "Включи свет на кухне" in prompt
    assert "второй этаж" in prompt          # the default rooms


def test_only_home_vocabulary_reaches_the_prompt():
    """A dev machine's Home Assistant is full of plumbing: sensors, automations, scripts with technical or English names
    («Wyoming Satellite Health»). None of it is something a person says to the speaker."""
    states = [
        {"entity_id": "sensor.wyoming_satellite_health", "attributes": {"friendly_name": "Wyoming Satellite Health"}},
        {"entity_id": "automation.move_wyoming", "attributes": {"friendly_name": "Перенести wyoming на другой хост"}},
        {"entity_id": "script.alice_music_play", "attributes": {"friendly_name": "Alice: play on an MA player"}},
        {"entity_id": "light.agent_debug", "attributes": {"friendly_name": "Agent debug_light"}},
        {"entity_id": "light.lustra", "attributes": {"friendly_name": "Люстра"}},
        {"entity_id": "climate.kondei_2", "attributes": {"friendly_name": "Кондей 2"}},
        {"entity_id": "switch.long", "attributes": {"friendly_name": "Очень длинное название выключателя на кухне и в студии"}},
    ]
    assert spoken_entity_names(states) == ("Люстра", "Кондей 2")


def get_from(responses):
    def get(path, body=None, timeout=4.0):
        value = responses[path.rsplit("/api/", 1)[1].split("/")[0]]
        if isinstance(value, Exception):
            raise value
        return value
    return get


STATES = [{"entity_id": "light.lustra", "attributes": {"friendly_name": "Люстра"}}]


def test_strict_fetch_refuses_a_catalog_without_home_assistant():
    for responses in ({"states": OSError("down"), "template": ["Кухня"]},
                      {"states": STATES, "template": OSError("down")}):
        with pytest.raises(CatalogUnavailable):
            fetch_catalog(get=get_from(responses), strict=True)


def test_a_home_without_configured_rooms_is_a_valid_answer_not_an_outage():
    catalog = fetch_catalog(get=get_from({"states": STATES, "template": []}), strict=True)
    assert catalog.entities == ("Люстра",) and catalog.areas == ()
    assert "люстра" in whisper_prompt(catalog)


def test_strict_fetch_refuses_an_answer_that_is_not_a_list():
    for responses in ({"states": {"message": "401: Unauthorized"}, "template": ["Кухня"]},
                      {"states": STATES, "template": "not json"}):
        with pytest.raises(CatalogUnavailable):
            fetch_catalog(get=get_from(responses), strict=True)


def test_strict_fetch_accepts_missing_artists_and_the_lenient_fetch_never_raises():
    ok = {"states": STATES, "template": ["Кухня"]}
    catalog = fetch_catalog(artists=lambda: (_ for _ in ()).throw(OSError("music assistant down")), get=get_from(ok), strict=True)
    assert catalog.entities == ("Люстра",) and catalog.areas == ("Кухня",) and catalog.artists == ()
    assert fetch_catalog(get=get_from({"states": OSError("down"), "template": OSError("down")})).empty()


def test_the_prompt_command_prints_nothing_and_fails_when_home_assistant_is_down(monkeypatch, capsys):
    def unreachable(**kwargs):
        raise CatalogUnavailable("Home Assistant gave no device states")
    monkeypatch.setattr(voice_vocab, "fetch_catalog", unreachable)
    assert voice_vocab.main(["prompt"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "" and "no prompt built" in captured.err


def test_the_static_prompt_command_needs_no_home_assistant(monkeypatch, capsys):
    monkeypatch.setattr(voice_vocab, "fetch_catalog", lambda **kwargs: pytest.fail("no lookup for --static"))
    assert voice_vocab.main(["prompt", "--static"]) == 0
    assert capsys.readouterr().out.strip() == whisper_prompt(Catalog())


def test_the_default_read_goes_through_the_shared_client(tmp_path, monkeypatch):
    import ha_client
    import ha_fakes
    ha = ha_fakes.FakeHomeAssistant(STATES)
    try:
        store = ha_client.Store(tmp_path / "connection.json")
        store.save(ha_client.Connection(ha.url, "long_lived", "llat"))
        ha.long_lived.add("llat")
        monkeypatch.setattr(ha_client, "default", lambda: ha_client.HaClient(store, discover_fn=lambda **_: []))
        assert fetch_catalog().entities == ("Люстра",)
    finally:
        ha.close()


def test_without_a_connection_strict_refuses_and_lenient_is_empty(tmp_path, monkeypatch):
    import ha_client
    monkeypatch.setattr(ha_client, "default", lambda: ha_client.HaClient(ha_client.Store(tmp_path / "none.json"), discover_fn=lambda **_: []))
    with pytest.raises(CatalogUnavailable):
        fetch_catalog(strict=True)
    assert fetch_catalog().empty()
