import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from beets import config, ui
from beets.library import Item, Library
from beetsplug.playlist import PlaylistPlugin
from beetsplug.similarity import SimilarityPlugin, SimilarityStore
from beetsplug.quality_playlist import (
    MARKER,
    V0,
    V2,
    ChoiceCache,
    QualityPlaylistPlugin,
    quality,
)


class QualityPlaylistTest(unittest.TestCase):
    def setUp(self):
        config.clear()
        config.read(user=False)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config["directory"] = str(self.root)
        config["playlist"] = {
            "playlist_dir": str(self.root),
            "relative_to": "playlist",
            "forward_slash": False,
        }
        self.playlist = PlaylistPlugin()
        self.plugin = QualityPlaylistPlugin()
        self.plugin.config["cache_file"] = str(self.root / "choices.json")
        self.lib = Library(self.root / "library.db")
        self.addCleanup(self.lib._close)
        self.cache = ChoiceCache(str(self.root / "choices.json"), self.lib)
        self.original = self.item("original.mp3", 192_000)
        self.v2 = self.item("v2.mp3", 190_000, encoder_settings="-V 2")
        self.v0 = self.item("v0.mp3", 245_000, encoder_settings="-V0")
        self.cbr = self.item("320.mp3", 320_000)
        self.flac = self.item("lossless.flac", 900_000, format="FLAC")
        self.source = self.root / "reference.m3u"
        self.source.write_bytes(b"#EXTM3U\n#EXTINF:1,Song\noriginal.mp3\n")
        self.similarity = SimilarityPlugin()
        self.similarity.similar_items = Mock()
        self.similarity.similar_items.return_value = [(self.flac, 0.99)]
        self.store = SimilarityStore(self.lib)
        self.store.put(self.original.id, self.flac.id, 0.99)

    def item(self, filename, bitrate, **fields):
        item = Item(
            path=str(self.root / filename).encode(),
            bitrate=bitrate,
            format=fields.pop("format", "MP3"),
            title="Song",
            artist="Artist",
            **fields,
        )
        self.lib.add(item)
        return item

    def choose(self, candidates, strategy="LOSSY", quiet=False, timid=False,
               strong=0.04, gap=0.05, original=None):
        return self.plugin._choose(
            original or self.original, candidates, strategy,
            quiet, timid, strong, gap, self.cache,
        )[0]

    def command(self, *args):
        command = self.plugin.commands()[0]
        opts, positional = command.parse_args(list(args))
        with patch("beets.plugins.find_plugins", return_value=[self.playlist, self.similarity]):
            command.func(self.lib, opts, positional)

    def test_quality_order(self):
        low = self.item("128.mp3", 128_000)
        self.assertEqual(
            sorted([math.inf, quality(self.v0), quality(low), quality(self.cbr),
                    quality(self.original), quality(self.v2)]),
            [128_000, 192_000, V2, V0, 320_000, math.inf],
        )

    def test_beets_lossless_format_names(self):
        for name in ["FLAC", "ALAC", "WAVE", "AIFF", "APE", "WavPack", "DSD Stream File"]:
            with self.subTest(format=name):
                lossless = self.item(f"{name}.audio", 1_000_000, format=name)
                self.assertEqual(quality(lossless), math.inf)
                self.assertEqual(
                    self.choose([(lossless, 0.001)], "LOSSY"), self.original
                )
                self.assertEqual(
                    self.choose([(lossless, 0.001)], "LOSSLESS"), lossless
                )
                self.assertEqual(
                    self.choose([(self.cbr, 0.001)], original=lossless), self.cbr
                )

    def test_lossless_to_lossy_prefers_highest_bitrate_without_upgrade_gap(self):
        self.assertEqual(
            self.choose(
                [(self.v2, 0.001), (self.cbr, 0.03)],
                original=self.flac, quiet=True, gap=0.005,
            ),
            self.cbr,
        )

    def test_lossless_preferred_over_closer_lossy(self):
        self.assertEqual(
            self.choose([(self.cbr, 0.001), (self.flac, 0.03)], "LOSSLESS"),
            self.flac,
        )

    def test_higher_lossy_within_gap_wins(self):
        self.assertEqual(self.choose([(self.v2, 0.001), (self.cbr, 0.03)]), self.cbr)

    def test_gap_conflict_quiet_keeps_original(self):
        with patch("beets.ui.input_options") as prompt:
            self.assertEqual(
                self.choose([(self.v2, 0.01), (self.cbr, 0.15)], quiet=True),
                self.original,
            )
            prompt.assert_not_called()

    def test_conflict_interactive_choice_is_cached(self):
        candidates = [(self.v2, 0.01), (self.cbr, 0.15)]
        with patch("beets.ui.input_options", return_value=2) as prompt:
            self.assertEqual(self.choose(candidates), self.cbr)
            self.cache.save()
            prompt.assert_called_once()
        self.cache = ChoiceCache(str(self.root / "choices.json"), self.lib)
        with patch("beets.ui.input_options") as prompt:
            self.assertEqual(self.choose(candidates, quiet=True), self.cbr)
            prompt.assert_not_called()
        choice = next(iter(self.cache.choices.values()))
        self.assertEqual(choice, {
            "strategy": "LOSSY", "original": self.original.id, "trump": self.cbr.id,
        })

    def test_cache_does_not_cross_strategy_or_accept_stale_candidate(self):
        self.cache.put("LOSSY", self.original, self.cbr)
        self.assertEqual(self.choose([], quiet=True), self.original)
        self.assertEqual(
            self.choose([(self.flac, 0.01)], "LOSSLESS", quiet=True), self.flac
        )

    def test_keep_choice_is_cached(self):
        with patch("beets.ui.input_options", return_value="k"):
            self.choose([(self.cbr, 0.1)])
        self.assertEqual(self.choose([(self.cbr, 0.01)]), self.original)

    def test_timid_requires_prompt_even_for_strong_match(self):
        with patch("beets.ui.input_options", return_value=1) as prompt:
            self.assertEqual(self.choose([(self.cbr, 0.001)], timid=True), self.cbr)
            prompt.assert_called_once()
        self.cache.choices.clear()
        with patch("beets.ui.input_options") as prompt:
            self.assertEqual(
                self.choose([(self.cbr, 0.001)], quiet=True, timid=True), self.original
            )
            prompt.assert_not_called()

    def test_weak_match_quiet_keeps_original(self):
        self.assertEqual(self.choose([(self.cbr, 0.1)], quiet=True), self.original)

    def test_lossy_never_downgrades(self):
        low = self.item("low.mp3", 128_000)
        self.assertEqual(self.choose([(low, 0.001)]), self.original)

    def test_above_v0_only_upgrades_to_lossless(self):
        self.assertEqual(self.choose([(self.cbr, 0.001)], original=self.cbr), self.cbr)
        higher = self.item("higher.mp3", 400_000)
        self.assertEqual(self.choose([(higher, 0.001)], original=self.cbr), self.cbr)
        self.assertEqual(
            self.choose([(higher, 0.001), (self.flac, 0.01)], "LOSSLESS", original=self.cbr),
            self.flac,
        )

    def test_lossless_retained_or_downgraded_to_highest_lossy(self):
        self.assertEqual(
            self.choose([(self.cbr, 0.001)], "LOSSLESS", original=self.flac), self.flac
        )
        self.assertEqual(
            self.choose([(self.v2, 0.001), (self.cbr, 0.03)], original=self.flac), self.cbr
        )

    def test_abort(self):
        with patch("beets.ui.input_options", return_value="b"):
            with self.assertRaises(ui.UserError):
                self.choose([(self.cbr, 0.1)])
        self.assertFalse(self.cache.choices)

    def test_generate_and_regenerate_without_using_generated_sources(self):
        self.command("--quiet")
        output = self.root / "reference.quality-lossless.m3u"
        self.assertIn(b"lossless.flac\n", output.read_bytes())
        self.assertIn(MARKER, output.read_bytes())
        self.assertIn(b"#EXTINF:1,Song\n", output.read_bytes())
        self.assertIn(b"original.mp3", self.source.read_bytes())
        self.command("--quiet")
        self.assertFalse(list(self.root.glob("*quality-lossless.quality-*")))
        self.assertFalse((self.root / "choices.json").exists())

    def test_pretend_writes_neither_playlist_nor_choices(self):
        self.store.put(self.original.id, self.flac.id, 0.9)
        with patch("beets.ui.input_options", return_value=1):
            self.command("-p", "reference", "--pretend")
        self.assertFalse((self.root / "reference.quality-lossless.m3u").exists())
        self.assertFalse((self.root / "choices.json").exists())

    def test_pretend_does_not_create_missing_index(self):
        with self.lib.transaction() as tx:
            tx.mutate("DROP INDEX similarity_item_b")
        self.command("--pretend", "--quiet")
        self.similarity.similar_items.assert_not_called()
        with self.lib.transaction() as tx:
            self.assertFalse(tx.query(
                "SELECT name FROM sqlite_master WHERE name='similarity_item_b'"
            ))

    def test_pretend_honors_manual_similarity_decisions(self):
        self.store.put(self.original.id, self.flac.id, 0.99, manual=-1)
        self.assertEqual(
            self.plugin._candidates(self.lib, self.similarity, self.original, 0.8, True),
            [],
        )
        self.store.put(self.original.id, self.flac.id, 0.1, manual=1)
        self.assertEqual([
            (item.id, distance) for item, distance in
            self.plugin._candidates(self.lib, self.similarity, self.original, 0.8, True)
        ], [(self.flac.id, 0)])

    def test_matrix_not_created_if_missing(self):
        with self.lib.transaction() as tx:
            tx.mutate("DROP TABLE similarity")
        self.command("--pretend", "--quiet")
        self.similarity.similar_items.assert_not_called()
        with self.lib.transaction() as tx:
            self.assertFalse(tx.query("SELECT name FROM sqlite_master WHERE name='similarity'"))

    def test_candidates_are_sorted_and_threshold_is_forwarded(self):
        self.similarity.similar_items.return_value = [
            (self.flac, 0.9), (self.cbr, 0.99), (self.original, 1), (self.v0, 0.2)
        ]
        self.assertEqual(
            [item for item, _ in self.plugin._candidates(self.lib, self.similarity, self.original, 0.8)],
            [self.cbr, self.flac],
        )
        self.similarity.similar_items.assert_called_once_with(self.lib, self.original, 0.8)

    def test_global_import_modes(self):
        config["import"]["timid"] = True
        config["import"]["quiet"] = True
        with patch("beets.ui.input_options") as prompt:
            self.command()
            prompt.assert_not_called()
        self.assertIn(
            b"original.mp3\n", (self.root / "reference.quality-lossless.m3u").read_bytes()
        )

    def test_library_and_custom_relative_bases(self):
        for base in ["library", str(self.root)]:
            with self.subTest(base=base):
                config["playlist"]["relative_to"] = base
                self.command("-p", str(self.source), "--quiet")
                self.assertIn(
                    b"lossless.flac\n",
                    (self.root / "reference.quality-lossless.m3u").read_bytes(),
                )

    def test_absolute_paths_unknown_entries_and_unicode(self):
        self.flac.path = str(self.root / "é.flac").encode()
        self.flac.store()
        self.source.write_bytes(self.original.path + b"\nunknown.mp3\n\n#comment\n")
        self.command("--quiet")
        output = (self.root / "reference.quality-lossless.m3u").read_bytes()
        self.assertIn(self.flac.path + b"\n", output)
        self.assertIn(b"unknown.mp3\n\n#comment\n", output)

    def test_generated_reference_and_overwrite_are_rejected(self):
        self.source.write_bytes(MARKER + b"LOSSLESS\noriginal.mp3\n")
        with self.assertRaises(ui.UserError):
            self.command("-p", str(self.source))
        self.source.write_bytes(b"original.mp3\n")
        output = self.root / "reference.quality-lossless.m3u"
        output.write_bytes(b"unrelated.mp3\n")
        with self.assertRaises(ui.UserError):
            self.command("-p", str(self.source))
        self.assertEqual(output.read_bytes(), b"unrelated.mp3\n")

    def test_invalid_options_and_missing_dependencies(self):
        for args in [("-q", "BEST"), ("--min-threshold", "nan"),
                     ("--min-threshold", "0"), ("-p", "missing")]:
            with self.subTest(args=args), self.assertRaises(ui.UserError):
                self.command(*args)
        with patch("beets.plugins.find_plugins", return_value=[]):
            with self.assertRaises(ui.UserError):
                self.plugin._dependencies()
        self.plugin.config["algorithm"] = "legacy"
        with self.assertRaises(ui.UserError):
            self.command()

    def test_corrupt_cache_is_not_overwritten(self):
        path = self.root / "choices.json"
        path.write_text('{"version": 1, "choices": []}')
        with self.assertRaises(ui.UserError):
            ChoiceCache(path, self.lib)
        self.assertEqual(json.loads(path.read_text())["choices"], [])

    def test_official_plugin_still_owns_queries_and_path_updates(self):
        self.assertFalse(hasattr(self.plugin, "item_queries"))
        self.command("--quiet")
        output = self.root / "reference.quality-lossless.m3u"
        self.playlist.changes[self.flac.path] = str(self.root / "moved.flac").encode()
        self.playlist.update_playlist(str(output), str(self.root).encode())
        self.assertIn(b"moved.flac\n", output.read_bytes())

    def test_real_similarity_matrix_integration(self):
        self.similarity.similar_items = SimilarityPlugin.similar_items.__get__(self.similarity)
        self.command("--quiet")
        self.assertIn(
            b"lossless.flac\n", (self.root / "reference.quality-lossless.m3u").read_bytes()
        )

    def test_strong_recommendation_boundary(self):
        with patch("beets.ui.input_options", return_value="k") as prompt:
            self.choose([(self.cbr, 0.04)])
            prompt.assert_called_once()

    def test_lossless_falls_back_to_better_lossy_and_logs(self):
        with patch.object(self.plugin._log, "warning") as warning:
            trump = self.choose([(self.v2, 0.001)], "LOSSLESS")
            self.assertEqual(trump, self.v2)
            self.plugin._log_selection(self.original, trump, "LOSSLESS")
            warning.assert_called_once()

    def test_m3u8_bom_and_threshold_option(self):
        source = self.root / "unicode.m3u8"
        source.write_bytes(b"\xef\xbb\xbf#EXTM3U\noriginal.mp3\n")
        self.command("-p", "unicode", "--min-threshold", "0.95", "--quiet")
        self.assertIn(
            b"lossless.flac\n", (self.root / "unicode.quality-lossless.m3u8").read_bytes()
        )
        self.similarity.similar_items.assert_called_once_with(
            self.lib, unittest.mock.ANY, 0.95
        )

    def test_external_reference_writes_in_official_playlist_directory(self):
        external = self.root / "external"
        external.mkdir()
        source = external / "outside.m3u"
        source.write_bytes(b"../original.mp3\nunknown.mp3\n")
        self.command("-p", str(source), "--quiet")
        output = self.root / "outside.quality-lossless.m3u"
        self.assertIn(b"lossless.flac\n", output.read_bytes())
        self.assertIn(b"external/unknown.mp3\n", output.read_bytes())
        self.assertFalse((external / output.name).exists())
        self.assertIn(str(output), list(self.playlist.find_playlists()))

    def test_cache_cannot_overwrite_source(self):
        self.plugin.config["cache_file"] = str(self.source)
        with self.assertRaises(ui.UserError):
            self.command("--quiet")
        self.assertIn(b"original.mp3\n", self.source.read_bytes())


if __name__ == "__main__":
    unittest.main()
