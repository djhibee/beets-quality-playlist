"""Generate quality playlists; the official playlist plugin owns path maintenance."""

import json
import math
import os
import re
import tempfile
from pathlib import Path

from beets import config, plugins, ui
from beets.dbcore.query import MatchQuery
from beets.plugins import BeetsPlugin
from beets.util import bytestring_path, normpath, path_as_posix, syspath


MARKER = b"# quality-playlist: "
LOSSLESS_FORMATS = {
    "FLAC", "ALAC", "WAV", "WAVE", "AIFF", "APE", "WAVPACK", "DSD", "DSD STREAM FILE"
}
V0 = 300_000
V2 = 225_000


def quality(item):
    """Comparable quality: lossless, then lossy bitrate with LAME preset ranks."""
    if item.get("format", "").upper() in LOSSLESS_FORMATS:
        return math.inf
    settings = str(item.get("encoder_settings", ""))
    preset = re.search(r"(?:-V\s*|(?:^|\s)V)([0-9])\b", settings, re.I)
    if preset:
        level = int(preset[1])
        if level == 0:
            return V0
        if level == 2:
            return V2
    bitrate = max(0, item.get("bitrate", 0) or 0)
    # Untagged VBR is ranked by measured bitrate, never guessed to be V0.
    return bitrate


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class ChoiceCache:
    def __init__(self, path, library):
        self.path = path
        self.library = os.path.realpath(syspath(library.path))
        self.choices = {}
        try:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("unsupported choice cache")
            self.choices = data["choices"]
            if not isinstance(self.choices, dict):
                raise ValueError("invalid choice cache")
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise ui.UserError(f"Cannot read quality playlist cache: {exc}") from exc

    def key(self, strategy, item):
        return json.dumps([self.library, strategy, item.id])

    def get(self, strategy, item):
        return self.choices.get(self.key(strategy, item))

    def put(self, strategy, original, trump):
        self.choices[self.key(strategy, original)] = {
            "strategy": strategy,
            "original": original.id,
            "trump": trump.id,
        }

    def save(self):
        atomic_write(
            self.path,
            json.dumps({"version": 1, "choices": self.choices}, indent=2).encode(),
        )


class QualityPlaylistPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "algorithm": "similarity",
                "quality": "LOSSLESS",
                "min_threshold": 0.8,
                "strong_rec_thresh": config["match"]["strong_rec_thresh"].get(),
                "max_gap": 0.05,
                "quiet": False,
                "timid": False,
                "cache_file": "quality-playlist-choices.json",
            }
        )

    def commands(self):
        command = ui.Subcommand(
            "qualityPlaylist", help="generate quality-specific M3U playlists"
        )
        command.parser.add_option("-q", "--quality", dest="quality")
        command.parser.add_option("-p", "--playlist", dest="playlist")
        command.parser.add_option(
            "--min-threshold", "--min_threshold", type="float", dest="min_threshold"
        )
        command.parser.add_option("--quiet", action="store_true", default=False)
        command.parser.add_option("-t", "--timid", action="store_true", default=False)
        command.parser.add_option("--pretend", action="store_true", default=False)
        command.func = self._command
        return [command]

    def _number(self, name, override=None):
        value = self.config[name].as_number() if override is None else override
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ui.UserError(f"quality_playlist.{name} must be in [0, 1]")
        return value

    def _dependencies(self):
        enabled = list(plugins.find_plugins())
        if not any(plugin.name == "playlist" for plugin in enabled):
            raise ui.UserError("Enable the official playlist plugin")
        algorithm = self.config["algorithm"].get(str)
        if algorithm == "legacy":
            from beetsplug.userrating.similar import get_similar_items

            return get_similar_items
        if algorithm != "similarity":
            raise ui.UserError("quality_playlist.algorithm must be similarity or legacy")
        for plugin in enabled:
            if plugin.name == "similarity" and callable(
                getattr(plugin, "similar_items", None)
            ):
                return plugin
        raise ui.UserError("Enable beets-similarity with its similar_items API")

    @staticmethod
    def _generated(path):
        with open(path, "rb") as stream:
            return any(line.startswith(MARKER) for line in stream)

    def _playlists(self, pattern):
        directory = Path(config["playlist"]["playlist_dir"].as_filename())
        if pattern:
            candidates = [Path(pattern), directory / pattern]
            if not Path(pattern).suffix:
                candidates += [directory / f"{pattern}.m3u", directory / f"{pattern}.m3u8"]
            for path in candidates:
                if path.is_file() and path.suffix.lower() in {".m3u", ".m3u8"}:
                    if self._generated(path):
                        raise ui.UserError("A quality playlist cannot be a reference playlist")
                    return [path.resolve()]
            raise ui.UserError(f"Playlist not found: {pattern}")
        if not directory.is_dir():
            raise ui.UserError(f"Playlist directory not found: {directory}")
        return [
            path.resolve()
            for path in sorted(directory.iterdir())
            if path.is_file()
            and path.suffix.lower() in {".m3u", ".m3u8"}
            and not self._generated(path)
        ]

    @staticmethod
    def _base(path):
        relative_to = config["playlist"]["relative_to"].get(str)
        if relative_to == "playlist":
            return bytestring_path(path.parent)
        if relative_to == "library":
            return bytestring_path(config["directory"].as_filename())
        return bytestring_path(config["playlist"]["relative_to"].as_filename())

    def _candidates(self, lib, similarity, original, threshold, pretend=False):
        if self.config["algorithm"].get(str) == "legacy":
            similar = similarity(lib, original)
            return sorted(
                [(item, 0) for item in similar.items] if similar else [],
                key=lambda pair: pair[0].id,
            )
        # Avoid creating similarity tables when the matrix is not populated.
        with lib.transaction() as tx:
            exists = tx.query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='similarity'"
            )
        if not exists:
            return []
        if pretend:
            # The public API initializes tables/indexes, even when only reading.
            from beetsplug.similarity import effective_score

            penalty = similarity._dissociate_penalty()
            with lib.transaction() as tx:
                rows = tx.query(
                    "SELECT * FROM similarity WHERE item_a=? OR item_b=?",
                    (original.id, original.id),
                )
            matches = []
            for row in rows:
                other_id = row["item_b"] if row["item_a"] == original.id else row["item_a"]
                item = lib.get_item(other_id)
                if item is not None:
                    matches.append((item, effective_score(row, penalty)))
        else:
            matches = similarity.similar_items(lib, original, threshold)
        return sorted(
            [
                (item, 1 - score)
                for item, score in matches
                if item.id != original.id
                and math.isfinite(score)
                and threshold <= score <= 1
            ],
            key=lambda pair: (pair[1], pair[0].id),
        )

    def _choose(self, original, candidates, strategy, quiet, timid, strong, gap, cache):
        origin_quality = quality(original)
        if strategy == "LOSSLESS" and origin_quality == math.inf:
            return original, False
        eligible = [
            (item, distance)
            for item, distance in candidates
            if (
                quality(item) > origin_quality
                and (quality(item) == math.inf or origin_quality <= V0)
                if origin_quality != math.inf
                else quality(item) != math.inf
            )
            and (strategy != "LOSSY" or quality(item) != math.inf)
        ]
        cached = cache.get(strategy, original)
        if isinstance(cached, dict):
            selected = cached.get("trump")
            if selected == original.id:
                return original, False
            for item, _ in eligible:
                if selected == item.id:
                    self._log.info("Using cached choice: {} -> {}", original, item)
                    return item, False
        if not eligible:
            return original, False

        preferred = [
            pair for pair in eligible
            if strategy != "LOSSLESS" or quality(pair[0]) == math.inf
        ] or eligible
        nearest = preferred[0]
        best = min(preferred, key=lambda pair: (-quality(pair[0]), pair[1], pair[0].id))
        conflict = (
            origin_quality != math.inf
            and best[1] - nearest[1] >= gap
            and quality(best[0]) > quality(nearest[0])
        )
        selected = best if not conflict else nearest
        if not timid and not conflict and selected[1] < strong:
            return selected[0], False
        if quiet:
            self._log.warning(
                "Unresolved {} choice for item {}: keeping original; candidates {}",
                strategy, original.id,
                [(item.id, distance) for item, distance in eligible],
            )
            return original, False
        ui.print_(f"{strategy}: {original} (quality {origin_quality:g})")
        for number, (item, distance) in enumerate(eligible, 1):
            ui.print_(f"{number}: {item} (quality {quality(item):g}, distance {distance:.4f})")
        answer = ui.input_options(
            ["Keep original", "aBort"], numrange=(1, len(eligible)), default="k"
        )
        if answer == "b":
            raise ui.UserError("Quality playlist generation aborted")
        trump = original if answer == "k" else eligible[int(answer) - 1][0]
        cache.put(strategy, original, trump)
        return trump, True

    def _log_selection(self, original, trump, strategy):
        self._log.info("{}: item {} -> item {}", strategy, original.id, trump.id)
        rank = quality(trump)
        if rank < V0 or (strategy == "LOSSLESS") != (rank == math.inf):
            self._log.warning(
                "Review {} selection: original {}, trump {}, quality {}",
                strategy, original.id, trump.id, rank,
            )

    def _render(self, lib, source, output, similarity, threshold, strategy,
                quiet, timid, strong, gap, cache, pretend=False):
        base = self._base(source)
        output_base = self._base(output)
        result = [b"#EXTM3U\n", MARKER + strategy.encode() + b"\n"]
        dirty = False
        with open(source, "rb") as stream:
            for line in stream:
                entry = line.rstrip(b"\r\n")
                if not entry or entry.startswith(b"#"):
                    if entry != b"#EXTM3U":
                        result.append(line)
                    continue
                # Accept UTF-8 BOM playlists without changing filesystem encoding.
                entry = entry.removeprefix(b"\xef\xbb\xbf")
                if entry.startswith(b"#"):
                    continue
                if config["playlist"]["forward_slash"].get(bool):
                    entry = entry.replace(b"/", os.sep.encode())
                path = normpath(os.path.join(base, entry))
                original = next(iter(lib.items(MatchQuery("path", path))), None)
                if original is None:
                    self._log.warning("Not in library; preserving playlist entry: {}", syspath(path))
                    if base == output_base or os.path.isabs(entry):
                        result.append(line)
                    else:
                        target = os.path.relpath(path, output_base)
                        if config["playlist"]["forward_slash"].get(bool):
                            target = path_as_posix(target)
                        result.append(target + b"\n")
                    continue
                candidates = []
                if strategy != "LOSSLESS" or quality(original) != math.inf:
                    candidates = self._candidates(lib, similarity, original, threshold, pretend)
                trump, chosen = self._choose(
                    original, candidates, strategy, quiet, timid, strong, gap, cache
                )
                dirty |= chosen
                self._log_selection(original, trump, strategy)
                target = bytestring_path(trump.path)
                if not os.path.isabs(entry):
                    target = os.path.relpath(target, output_base)
                if config["playlist"]["forward_slash"].get(bool):
                    target = path_as_posix(target)
                if b"\n" in target or b"\r" in target:
                    raise ui.UserError("An item path contains a playlist line separator")
                result.append(target + b"\n")
        return b"".join(result), dirty

    def _command(self, lib, opts, args):
        if args:
            raise ui.UserError("Use -p to select a reference playlist")
        similarity = self._dependencies()
        strategy = (opts.quality or self.config["quality"].get(str)).upper()
        if strategy not in {"LOSSY", "LOSSLESS"}:
            raise ui.UserError("Quality must be LOSSY or LOSSLESS")
        threshold = self._number("min_threshold", opts.min_threshold)
        if threshold == 0:
            raise ui.UserError("min_threshold must be greater than zero")
        strong = self._number("strong_rec_thresh")
        gap = self._number("max_gap")
        quiet = opts.quiet or self.config["quiet"].get(bool) or config["import"]["quiet"].get(bool)
        timid = opts.timid or self.config["timid"].get(bool) or config["import"]["timid"].get(bool)
        cache = ChoiceCache(self.config["cache_file"].as_filename(), lib)
        sources = self._playlists(opts.playlist)
        directory = Path(config["playlist"]["playlist_dir"].as_filename())
        try:
            for source in sources:
                output = directory / f"{source.stem}.quality-{strategy.lower()}{source.suffix}"
                if Path(cache.path).resolve() in {
                    source.resolve(), output.resolve(), Path(syspath(lib.path)).resolve()
                }:
                    raise ui.UserError("Choice cache must not overwrite a playlist or the library")
                if output.exists() and not self._generated(output):
                    raise ui.UserError(f"Refusing to overwrite a reference playlist: {output}")
                content, dirty = self._render(
                    lib, source, output, similarity, threshold, strategy,
                    quiet, timid, strong, gap, cache, opts.pretend,
                )
                if opts.pretend:
                    self._log.info("Would write {}", output)
                else:
                    atomic_write(output, content)
                    if dirty:
                        cache.save()
                    self._log.info("Wrote {}", output)
        except OSError as exc:
            raise ui.UserError(f"Quality playlist I/O error: {exc}") from exc
