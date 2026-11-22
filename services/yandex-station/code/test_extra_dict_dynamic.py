"""extra_dict_dynamic.py and the layered dictionary (ticket #269): the support floor, the entity names that never reach the layer, the atomic
installation, the refresh that keeps the previous layer, the merge order (static wins) and the live reload.

Synthetic transcripts and a synthetic home: nothing of the owner's speech or of a real catalog is a fixture."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import extra_dict
import extra_dict_dynamic as dynamic
import voice_vocab
import voiceclips

HOME = voice_vocab.Catalog(entities=("Люстра", "Гирлянда", "Кондей 2"), areas=("Кухня", "Спальня", "Студия"))
HEARD = "люстара"   # «люстра» as the recogniser might write it: an edit inside the word, not another ending
KNOWN = frozenset({"свет", "включи", "выключи"})   # a stand-in for the grammar's words: the real bundle is tested separately


def turns(*pairs):
    """(clip id, text): the same text in n distinct clips."""
    out = []
    for text, n in pairs:
        out.extend((f"{text}-{index}", text) for index in range(n))
    return out


class Vocabulary(unittest.TestCase):
    def test_names_and_rooms_are_folded_and_the_verbs_come_from_the_grammar(self):
        vocabulary = dynamic.vocabulary_from(HOME)
        self.assertEqual(vocabulary.names, ("гирлянда", "кондей 2", "кухня", "люстра", "спальня", "студия"))
        self.assertIn("включи", vocabulary.verbs)
        self.assertIn("громче", vocabulary.verbs)
        self.assertIn("теплее", vocabulary.verbs)

    def test_names_that_carry_ids_addresses_or_serials_never_enter(self):
        names = ("Лампа 192.168.1.50", "Розетка a1b2c3d4e5f6", "Датчик 12345", "Реле aa:bb:cc:dd:ee:ff", "Чайник serial@home", "Гостиная / вход",
                 "Торшер C:\\temp", "Люстра")
        vocabulary = dynamic.Vocabulary((), names)
        self.assertEqual(vocabulary.names, ("люстра",))
        document = dynamic.build(voice_vocab.Catalog(entities=names), [], KNOWN, now=1)
        for text in json.dumps([document["entries"], document["vocabulary"]], ensure_ascii=False).split('"'):   # the digest is a hash, not a name
            self.assertNotRegex(text, r"\d{4,}|aa:bb|192\.168|a1b2c3|@")

    def test_the_name_list_is_capped(self):
        many = tuple(f"Лампа слово{'абвгде'[index % 6]}{'жзийкл'[index // 6 % 6]}{'мнопрс'[index // 36 % 6]}" for index in range(300))
        self.assertLessEqual(len(dynamic.Vocabulary((), many).names), dynamic.MAX_NAMES)

    def test_the_digest_follows_the_vocabulary_only(self):
        first = dynamic.vocabulary_from(HOME).digest()
        self.assertEqual(first, dynamic.vocabulary_from(voice_vocab.Catalog(entities=("Гирлянда", "Люстра", "Кондей 2"), areas=("Студия", "Кухня", "Спальня"))).digest())
        self.assertNotEqual(first, dynamic.vocabulary_from(voice_vocab.Catalog(entities=("Люстра",), areas=("Кухня",))).digest())

    def test_a_name_longer_than_the_loaders_cap_is_skipped_and_the_layer_still_builds(self):
        long = "Датчик температуры и влажности в гостиной на подоконнике слева у окна рядом с цветами"
        self.assertEqual(dynamic.Vocabulary((), (long, "Люстра")).names, ("люстра",))
        document = dynamic.build(voice_vocab.Catalog(entities=(long, "Люстра")), [], KNOWN, now=1)
        extra_dict.parse(document, "dynamic layer", layer="dynamic")

    def test_an_entity_named_like_the_activation_word_is_never_a_correction_target(self):
        catalog = voice_vocab.Catalog(entities=("Яндекс Станция", "Люстра"))
        heard = [(f"c{n}", "включи янддекс") for n in range(3)]
        document = dynamic.build(catalog, heard, KNOWN, now=1)
        self.assertNotIn("яндекс", [entry["canonical"] for entry in document["entries"]])
        extra_dict.parse(document, "dynamic layer", layer="dynamic")


class Support(unittest.TestCase):
    """Below, at and above the floor, for a home word and for a verb."""
    vocabulary = dynamic.vocabulary_from(HOME)

    def mined(self, *pairs, **options):
        return dynamic.mine(turns(*pairs), self.vocabulary, KNOWN | self.vocabulary.words(), **options)

    def test_one_and_two_occurrences_make_no_correction_three_do(self):
        self.assertEqual(self.mined((f"включи {HEARD}", 1)), [])
        self.assertEqual(self.mined((f"включи {HEARD}", 2)), [])
        entry, = self.mined((f"включи {HEARD}", 3))
        self.assertEqual((entry["kind"], entry["canonical"], entry["variants"], entry["context"]), ("phrase", "люстра", [{"text": HEARD, "n": 3}], "anywhere"))
        self.assertEqual(entry["evidence"]["source"], "observed")

    def test_the_floor_counts_clips_not_repeats_inside_one_clip(self):
        self.assertEqual(self.mined((f"{HEARD} {HEARD} {HEARD} {HEARD}", 1)), [])
        self.assertEqual(len(self.mined((f"{HEARD} {HEARD} {HEARD}", 3))), 1)

    def test_a_verb_is_a_command_entry(self):
        entry, = self.mined(("люстру включай", 0), ("гроомче на кухне", 4))
        self.assertEqual((entry["kind"], entry["canonical"], entry["variants"][0]["text"]), ("command", "громче", "гроомче"))

    def test_a_known_word_is_never_a_variant(self):
        for word in KNOWN | {"кухня", "люстра"}:
            self.assertEqual(self.mined((word + " пожалуйста", 9)), [])

    def test_a_word_far_from_the_vocabulary_is_nobodys(self):
        self.assertEqual(self.mined(("холодильник открыт", 9)), [])

    def test_another_form_of_a_home_word_is_never_a_mishearing(self):
        """«в спальне», «включи гирлянды», «студию»: the cases of a name are said every day and are near it by every measure; correcting them would break the sentence."""
        for heard in ("спальне", "спальню", "гирлянды", "гирляндой", "студию", "кухней"):
            self.assertEqual(self.mined((f"включи свет в {heard}", 9)), [], heard)
        for word, target in (("спальне", "спальня"), ("гирлянды", "гирлянда"), ("громчее", "громче"), ("громч", "громче")):
            self.assertTrue(dynamic.inflected(word, target), word)
        for word, target in (("люстара", "люстра"), ("гирлянта", "гирлянда"), ("спалня", "спальня"), ("гроомче", "громче")):
            self.assertFalse(dynamic.inflected(word, target), word)

    def test_a_word_between_two_vocabulary_words_is_nobodys(self):
        vocabulary = dynamic.Vocabulary((), ("Лампа стола", "Лампа стока"))
        self.assertEqual(dynamic.mine(turns(("лампа столка", 5)), vocabulary, frozenset()), [])

    def test_short_words_are_left_alone(self):
        vocabulary = dynamic.Vocabulary((), ("Свет",))
        self.assertEqual(dynamic.mine(turns(("цвет", 9)), vocabulary, frozenset()), [])

    def test_the_floor_can_be_raised_not_below_the_loaders_minimum(self):
        self.assertEqual(self.mined((HEARD, 3), min_support=5), [])
        with self.assertRaises(extra_dict.ExtraDictError):
            extra_dict.parse(layer_document({"id": "dyn-0123456789", "kind": "phrase", "canonical": "люстра", "context": "anywhere",
                                             "variants": [{"text": HEARD, "n": 2}], "evidence": {"source": "observed"}}), "layer.json", layer="dynamic")

    def test_the_entry_count_is_capped(self):
        words = [f"люст{a}{b}" for a in "абвгдежзиклмнопрст" for b in "ауыэюя"]
        vocabulary = dynamic.Vocabulary((), ("Люстрааа",))
        entries = dynamic.mine([(f"{w}-{i}", w) for w in words for i in range(3)], vocabulary, frozenset(), min_similarity=0.5)
        self.assertLessEqual(len(entries), dynamic.MAX_ENTRIES)


class Layer(unittest.TestCase):
    def test_a_built_layer_loads_through_the_dynamic_loader_and_carries_its_provenance(self):
        document = dynamic.build(HOME, turns((f"включи {HEARD}", 3)), KNOWN, now=100)
        layer = extra_dict.parse(document, "layer.json", layer="dynamic")
        self.assertEqual([entry.id for entry in layer.entries], [dynamic.entry_id("люстра", HEARD)])
        self.assertEqual(document["generated"]["at"], 100)
        self.assertEqual(document["generated"]["vocabulary"], dynamic.vocabulary_from(HOME).digest())
        self.assertIn("люстра", document["vocabulary"]["names"])

    def test_the_static_loader_refuses_a_dynamic_document_and_the_reverse(self):
        document = dynamic.build(HOME, [], KNOWN)
        with self.assertRaises(extra_dict.ExtraDictError):
            extra_dict.parse(document, "layer.json")
        with self.assertRaises(extra_dict.ExtraDictError):
            extra_dict.parse(json.loads(extra_dict.DATA.read_text(encoding="utf-8")), "static.json", layer="dynamic")

    def test_the_layer_is_installed_whole_and_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "dynamic-ru.json"
            self.assertTrue(dynamic.write_layer(target, dynamic.build(HOME, turns((HEARD, 3)), KNOWN, now=1)))
            self.assertEqual(os.listdir(target.parent), ["dynamic-ru.json"])
            self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")
            self.assertEqual(len(extra_dict.load(target, layer="dynamic").entries), 1)

    def test_the_same_content_is_not_written_again_but_a_change_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dynamic-ru.json"
            dynamic.write_layer(target, dynamic.build(HOME, turns((HEARD, 3)), KNOWN, now=1))
            before = target.stat().st_mtime_ns
            self.assertFalse(dynamic.write_layer(target, dynamic.build(HOME, turns((HEARD, 3)), KNOWN, now=2)))
            self.assertEqual(target.stat().st_mtime_ns, before)
            self.assertTrue(dynamic.write_layer(target, dynamic.build(voice_vocab.Catalog(entities=("Люстра", "Торшер"), areas=("Кухня",)), turns((HEARD, 3)), KNOWN, now=3)))

    def test_an_invalid_or_oversized_layer_is_refused_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dynamic-ru.json"
            good = dynamic.build(HOME, turns((HEARD, 3)), KNOWN, now=1)
            dynamic.write_layer(target, good)
            installed = target.read_bytes()
            broken = json.loads(json.dumps(good))
            broken["entries"][0]["kind"] = "nonsense"
            with self.assertRaises(extra_dict.ExtraDictError):
                dynamic.write_layer(target, broken)
            huge = json.loads(json.dumps(good))
            huge["vocabulary"]["names"] = ["слово" * 8] * 3000
            with self.assertRaises(extra_dict.ExtraDictError):
                dynamic.write_layer(target, huge)
            self.assertEqual(target.read_bytes(), installed)
            self.assertEqual(sorted(os.listdir(tmp)), ["dynamic-ru.json"])

    def test_a_failed_install_leaves_the_old_layer_and_no_stray_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dynamic-ru.json"
            dynamic.write_layer(target, dynamic.build(HOME, turns((HEARD, 3)), KNOWN, now=1))
            installed = target.read_bytes()
            with mock.patch("os.replace", side_effect=OSError("disk gone")), self.assertRaises(OSError):
                dynamic.write_layer(target, dynamic.build(voice_vocab.Catalog(entities=("Люстра", "Торшер"), areas=("Кухня",)), turns((HEARD, 3)), KNOWN, now=2))
            self.assertEqual(target.read_bytes(), installed)
            self.assertEqual(sorted(os.listdir(tmp)), ["dynamic-ru.json"])

    def test_the_grammar_bundle_supplies_the_known_words(self):
        words = dynamic.grammar_words()
        self.assertTrue({"включи", "выключи", "свет", "громкость"} <= words)
        with self.assertRaises(extra_dict.ExtraDictError):
            dynamic.grammar_words("/nonexistent/bundle.json")


class Refresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "dynamic-ru.json")
        self.db = os.path.join(self.tmp.name, "dialogs.sqlite3")
        conn = voiceclips.connect(self.db)
        rows = [(f"c{index}", float(index), "sent", "vad", f"включи {HEARD}") for index in range(3)]
        rows += [("s1", 10.0, "sent", "spotter", f"включи {HEARD}"), ("b1", 11.0, "blacklist", "vad", HEARD), ("g1", 12.0, "gate", "vad", None)]
        for clip_id, ts, status, trigger, transcript in rows:
            conn.execute("INSERT INTO voice_clips(id, ts, path, status, trigger, transcript) VALUES (?,?,?,?,?,?)", (clip_id, ts, clip_id + ".opus", status, trigger, transcript))
        conn.commit()
        conn.close()

    def fetch(self, url, strict=False):
        self.assertTrue(strict, "a refresh that installs for hours must not take a degraded catalog")
        return HOME

    def unreachable(self, url, strict=False):
        raise voice_vocab.CatalogUnavailable("Home Assistant gave no device states")

    def test_only_sent_whisper_clips_are_evidence(self):
        self.assertEqual([clip_id for clip_id, _ in reversed(dynamic.stored_transcripts(self.db))], ["c0", "c1", "c2"])

    def test_refresh_installs_the_layer(self):
        self.assertEqual(dynamic.refresh(self.out, self.db, "http://ha", self.fetch), 0)
        self.assertEqual([entry.id for entry in extra_dict.load(self.out, layer="dynamic").entries], [dynamic.entry_id("люстра", HEARD)])

    def test_an_unreadable_home_keeps_the_previous_layer(self):
        dynamic.refresh(self.out, self.db, "http://ha", self.fetch)
        installed = Path(self.out).read_bytes()
        with self.assertLogs("aisis.extra_dict_dynamic", level="WARNING"):
            self.assertEqual(dynamic.refresh(self.out, self.db, "http://ha", self.unreachable), 3)
        self.assertEqual(Path(self.out).read_bytes(), installed)

    def test_a_failed_build_keeps_the_previous_layer(self):
        dynamic.refresh(self.out, self.db, "http://ha", self.fetch)
        installed = Path(self.out).read_bytes()
        with self.assertLogs("aisis.extra_dict_dynamic", level="ERROR"):
            self.assertEqual(dynamic.refresh(self.out, os.path.join(self.tmp.name, "missing.sqlite3"), "http://ha", self.fetch), 1)
        self.assertEqual(Path(self.out).read_bytes(), installed)

    def test_a_refresh_with_no_change_reports_unchanged(self):
        dynamic.refresh(self.out, self.db, "http://ha", self.fetch)
        before = Path(self.out).stat().st_mtime_ns
        with self.assertLogs("aisis.extra_dict_dynamic", level="INFO") as logs:
            self.assertEqual(dynamic.refresh(self.out, self.db, "http://ha", self.fetch), 0)
        self.assertIn("extra_dict_dynamic_unchanged", logs.output[0])
        self.assertEqual(Path(self.out).stat().st_mtime_ns, before)

    def test_the_database_is_opened_read_only(self):
        before = Path(self.db).read_bytes()
        dynamic.refresh(self.out, self.db, "http://ha", self.fetch)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM voice_clips").fetchone(), (6,))
        finally:
            conn.close()
        self.assertEqual(os.stat(self.db).st_size, len(before))


def entry(entry_id, kind, canonical, variant, n, layer_source="observed"):
    return {"id": entry_id, "kind": kind, "canonical": canonical, "context": "anywhere", "variants": [{"text": variant, "n": n}], "evidence": {"source": layer_source}}


def layer_of(*entries):
    return extra_dict.parse(layer_document(*entries), "dynamic.json", layer="dynamic")


def layer_document(*entries):
    return {"version": 1, "locale": "ru", "entries": list(entries), "generated": {"at": 1, "vocabulary": "x", "min_support": 3}, "vocabulary": {"verbs": [], "names": []}}


class Merge(unittest.TestCase):
    static = extra_dict.load()

    def test_static_entries_come_first_and_all_stay(self):
        merged = extra_dict.merge(self.static, layer_of(entry("dyn-a", "phrase", "люстра", "люструй", 3)))
        self.assertEqual([item.id for item in merged.entries][:len(self.static.entries)], [item.id for item in self.static.entries])
        self.assertEqual(merged.entries[-1].layer, "dynamic")
        self.assertTrue(all(item.layer == "static" for item in merged.entries[:len(self.static.entries)]))

    def test_a_dynamic_variant_claimed_by_the_static_file_is_dropped(self):
        merged = extra_dict.merge(self.static, layer_of(entry("dyn-a", "phrase", "громкость", "урок из 5", 9)))
        self.assertEqual([item.id for item in merged.entries if item.layer == "dynamic"], [])
        self.assertEqual(merged.correct("урок из 5").text, "громкость 5")

    def test_a_dynamic_variant_that_is_a_static_canonical_is_dropped(self):
        merged = extra_dict.merge(self.static, layer_of(entry("dyn-a", "phrase", "люстра", "громкость 5", 9)))
        self.assertEqual([item for item in merged.entries if item.layer == "dynamic"], [])

    def test_two_dynamic_entries_for_one_variant_resolve_by_support_then_canonical(self):
        # one layer cannot claim a variant twice: the loader refuses it; two layers can, and the stronger entry wins
        with self.assertRaises(extra_dict.ExtraDictError):
            layer_of(entry("dyn-a", "phrase", "люстра", "лампаа", 3), entry("dyn-b", "phrase", "лампа", "лампаа", 7))
        merged = extra_dict.merge(self.static, layer_of(entry("dyn-a", "phrase", "люстра", "лампаа", 3)), layer_of(entry("dyn-b", "phrase", "лампа", "лампаа", 7)))
        self.assertEqual([(item.id, item.canonical) for item in merged.entries if item.layer == "dynamic"], [("dyn-b", "лампа")])
        tie = extra_dict.merge(self.static, layer_of(entry("dyn-z", "phrase", "торшер", "торшерр", 4)), layer_of(entry("dyn-y", "phrase", "торшеры", "торшерр", 4)))
        self.assertEqual([item.canonical for item in tie.entries if item.layer == "dynamic"], ["торшер"])

    def test_the_merge_is_deterministic_whatever_the_order_of_the_layers(self):
        first = layer_of(entry("dyn-a", "phrase", "люстра", "лампаа", 3), entry("dyn-c", "command", "громче", "громчее", 5))
        second = layer_of(entry("dyn-b", "phrase", "лампа", "лампаа", 3))
        left = extra_dict.merge(self.static, first, second)
        right = extra_dict.merge(self.static, second, first)
        self.assertEqual([(item.id, item.canonical) for item in left.entries], [(item.id, item.canonical) for item in right.entries])

    def test_a_dynamic_variant_that_is_another_dynamic_canonical_is_dropped(self):
        merged = extra_dict.merge(self.static, layer_of(entry("dyn-a", "phrase", "люстра", "люструй", 3)), layer_of(entry("dyn-b", "phrase", "торшер", "люстра", 3)))
        self.assertEqual(sorted(item.id for item in merged.entries if item.layer == "dynamic"), ["dyn-a"])

    def test_the_merged_dictionary_corrects_with_both_layers(self):
        merged = extra_dict.merge(self.static, layer_of(entry("dyn-a", "phrase", "люстра", "люструй", 3)))
        result = merged.correct("Олеса, включи люструй")
        self.assertEqual(result.text, "Алиса, включи люстра")
        self.assertEqual({hit.layer for hit in result.hits}, {"static", "dynamic"})


class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dynamic-ru.json"
        self.clock = [100.0]

    def live(self):
        return extra_dict.Live(extra_dict.load(), path=self.path, clock=lambda: self.clock[0], recheck_s=30.0)

    def install(self, *entries):
        document = {"version": 1, "locale": "ru", "entries": list(entries), "generated": {"at": 1, "vocabulary": "x", "min_support": 3}, "vocabulary": {"verbs": [], "names": []}}
        self.path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        os.utime(self.path, ns=(time.time_ns(), time.time_ns() + len(self.path.read_bytes()) * 1000))   # a different stamp even inside one clock tick

    def test_without_a_layer_it_is_the_static_dictionary(self):
        live = self.live()
        self.assertEqual([item.id for item in live.get().entries], [item.id for item in extra_dict.load().entries])

    def test_a_layer_that_appears_is_picked_up_after_the_recheck_interval(self):
        live = self.live()
        live.get()
        self.install(entry("dyn-a", "phrase", "люстра", "люструй", 3))
        self.assertEqual(len(live.get().entries), len(extra_dict.load().entries))   # inside the interval: not looked at
        self.clock[0] += 31
        self.assertEqual(live.get().correct("люструй").text, "люстра")

    def test_a_broken_layer_keeps_the_previous_dictionary(self):
        live = self.live()
        self.install(entry("dyn-a", "phrase", "люстра", "люструй", 3))
        self.assertEqual(live.get().correct("люструй").text, "люстра")
        self.path.write_text("{not json", encoding="utf-8")
        self.clock[0] += 31
        with self.assertLogs("aisis.extra_dict", level="WARNING"):
            self.assertEqual(live.get().correct("люструй").text, "люстра")
        self.clock[0] += 31
        self.assertEqual(live.get().correct("люструй").text, "люстра")   # the same broken file is not parsed and logged again

    def test_a_removed_layer_falls_back_to_static_only(self):
        live = self.live()
        self.install(entry("dyn-a", "phrase", "люстра", "люструй", 3))
        live.get()
        self.path.unlink()
        self.clock[0] += 31
        self.assertEqual(live.get().correct("люструй").text, "люструй")

    def test_the_version_changes_only_when_the_dictionary_does(self):
        live = self.live()
        first = live.get(), live.version
        self.clock[0] += 31
        self.assertEqual(live.get(), first[0])
        self.assertEqual(live.version, first[1])
        self.install(entry("dyn-a", "phrase", "люстра", "люструй", 3))
        self.clock[0] += 31
        live.get()
        self.assertNotEqual(live.version, first[1])


if __name__ == "__main__":
    unittest.main()
