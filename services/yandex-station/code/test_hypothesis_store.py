"""hypothesis_store: the latest recognition evidence per station, kept 15 s so the quasar hook and the correction dialog can ask for it
by the phrase they were given. Pure functions and an injected clock; the text comparison is the core's same_text (injected here)."""
import unittest

import hypothesis_store
from hypothesis_store import HypothesisStore


def same_text(a, b):
    return " ".join(a.lower().replace("ё", "е").replace(".", " ").split()) == " ".join(b.lower().replace("ё", "е").replace(".", " ").split())


NBEST = {"engine": "gigaam-v3-ctc", "ms": 90, "hypotheses": [{"text": "выключи свет", "percent": 71}, {"text": "включи свет", "percent": 29}]}


def store(**kwargs):
    return HypothesisStore(same_text=same_text, **kwargs)


class PutGet(unittest.TestCase):
    def test_get_returns_the_payload_by_normalised_text(self):
        s = store()
        self.assertTrue(s.put("st1", "Включи свет.", NBEST, source="whisper", ms=420, now=100.0))
        got = s.get("st1", "включи  свет", now=101.0)
        self.assertEqual({k: got[k] for k in ("station", "text", "source", "nbest", "ms")},
                         {"station": "st1", "text": "Включи свет.", "source": "whisper", "nbest": NBEST, "ms": 420})

    def test_other_text_is_a_miss(self):
        s = store()
        s.put("st1", "включи свет", NBEST, source="whisper", ms=1, now=0.0)
        self.assertIsNone(s.get("st1", "выключи свет", now=1.0))

    def test_ttl_is_fifteen_seconds(self):
        s = store()
        s.put("st1", "включи свет", NBEST, source="whisper", ms=1, now=100.0)
        self.assertIsNotNone(s.get("st1", "включи свет", now=114.9))
        self.assertIsNone(s.get("st1", "включи свет", now=115.1))

    def test_station_none_looks_across_stations_and_returns_the_newest(self):
        s = store()
        s.put("st1", "включи свет", {"engine": "old"}, source="whisper", ms=1, now=10.0)
        s.put("st2", "включи свет", {"engine": "new"}, source="spotter", ms=1, now=12.0)
        self.assertEqual(s.get(None, "включи свет", now=13.0)["nbest"], {"engine": "new"})

    def test_named_station_does_not_see_another_stations_entry(self):
        s = store()
        s.put("st2", "включи свет", NBEST, source="whisper", ms=1, now=10.0)
        self.assertIsNone(s.get("st1", "включи свет", now=11.0))

    def test_entry_without_a_station_serves_every_station(self):
        """The gateway's websocket knows no station: its evidence is stored under None and any station's lookup finds it."""
        s = store()
        s.put(None, "включи свет", NBEST, source="whisper", ms=1, now=10.0)
        self.assertEqual(s.get("st1", "включи свет", now=11.0)["station"], None)
        s.put("quasar", "включи свет", {"engine": "q"}, source="whisper", ms=1, now=12.0)
        self.assertEqual(s.get("st1", "включи свет", now=13.0)["nbest"], {"engine": "q"})

    def test_newer_put_for_the_same_phrase_replaces_the_older(self):
        s = store()
        s.put("st1", "включи свет", {"engine": "a"}, source="whisper", ms=1, now=10.0)
        s.put("st1", "Включи свет.", {"engine": "b"}, source="spotter", ms=1, now=11.0)
        self.assertEqual(s.stats()["entries"], 1)
        self.assertEqual(s.get("st1", "включи свет", now=12.0)["source"], "spotter")

    def test_at_most_sixty_four_entries_and_the_oldest_go_first(self):
        s = store()
        for index in range(70):
            s.put("st1", "фраза %d" % index, NBEST, source="whisper", ms=1, now=float(index) / 10)
        self.assertEqual(s.stats()["entries"], 64)
        self.assertIsNone(s.get("st1", "фраза 0", now=8.0))
        self.assertIsNotNone(s.get("st1", "фраза 69", now=8.0))

    def test_expired_entries_are_dropped_on_put(self):
        s = store()
        s.put("st1", "раз", NBEST, source="whisper", ms=1, now=0.0)
        s.put("st1", "два", NBEST, source="whisper", ms=1, now=100.0)
        self.assertEqual(s.stats()["entries"], 1)

    def test_get_hands_out_a_copy(self):
        s = store()
        s.put("st1", "включи свет", NBEST, source="whisper", ms=1, now=0.0)
        s.get("st1", "включи свет", now=1.0)["ms"] = 99999
        self.assertEqual(s.get("st1", "включи свет", now=1.0)["ms"], 1)

    def test_empty_or_non_text_primary_is_refused(self):
        s = store()
        self.assertFalse(s.put("st1", "   ", NBEST, source="whisper", ms=1, now=0.0))
        self.assertFalse(s.put("st1", None, NBEST, source="whisper", ms=1, now=0.0))
        self.assertIsNone(s.get("st1", "", now=0.0))
        self.assertEqual(s.stats()["entries"], 0)

    def test_stats_count_hits_and_misses(self):
        s = store()
        s.put("st1", "включи свет", NBEST, source="whisper", ms=1, now=0.0)
        s.get("st1", "включи свет", now=1.0)
        s.get("st1", "другое", now=1.0)
        self.assertEqual({k: s.stats()[k] for k in ("entries", "puts", "hits", "misses")}, {"entries": 1, "puts": 1, "hits": 1, "misses": 1})


class WithoutTheCore(unittest.TestCase):
    def test_a_missing_core_refuses_and_never_raises(self):
        def missing():
            raise ImportError("no stt_cli")
        s = HypothesisStore(load_same_text=missing)
        self.assertFalse(s.put("st1", "включи свет", NBEST, source="whisper", ms=1, now=0.0))
        self.assertIsNone(s.get("st1", "включи свет", now=1.0))
        self.assertEqual(s.stats()["available"], False)


class Constants(unittest.TestCase):
    def test_documented_bounds(self):
        self.assertEqual((hypothesis_store.TTL_S, hypothesis_store.MAX_ENTRIES), (15.0, 64))


if __name__ == "__main__":
    unittest.main()
