# beets-quality-playlist

Generate and regenerate quality-specific M3U/M3U8 playlists from reference
playlists, replacing tracks with matching versions already in your Beets library.
No audio is downloaded, converted, moved, or retagged.

## Install

Requires Python 3.12+ and Beets 2.14.1+:

```sh
python -m pip install .
```

Installation includes [beets-similarity](https://github.com/djhibee/beets-similarity),
pinned to a compatible GitHub revision (it is not currently published on PyPI).
Enable all three plugins in your Beets configuration:

```yaml
plugins: [playlist, similarity, quality_playlist]

playlist:
  playlist_dir: ~/Music/playlists
  relative_to: library  # alternatively: playlist, or a directory path
  forward_slash: false
  auto: true

quality_playlist:
  algorithm: similarity
  quality: LOSSLESS
  min_threshold: 0.8
  strong_rec_thresh: 0.04
  max_gap: 0.05
  quiet: false
  timid: false
  cache_file: quality-playlist-choices.json
```

`quality_playlist` is independent of the official `playlist` plugin: it adds
only `qualityPlaylist`. The official plugin alone owns `playlist:` searches
and path maintenance, including generated playlists. Enable `playlist.auto`
to maintain their paths after Beets moves/removes tracks.

Populate the similarity matrix first, using the similarity plugin's command,
for example `beet similarity --force`. Quality playlists only read stored
matches; an empty matrix leaves the original tracks in place.
Configure similarity's weights and artist/acoustic settings there, not here.
The reserved `algorithm: legacy` setting reports that its algorithm is not yet
defined; it does not silently use a different matching method.

## Usage

```sh
beet qualityPlaylist -q LOSSLESS -p favorites
beet qualityPlaylist -q LOSSY -p /path/to/favorites.m3u
beet qualityPlaylist --min-threshold 0.9 --quiet
beet qualityPlaylist --timid -p favorites
beet qualityPlaylist --pretend -p favorites
```

Quality names are case-insensitive; the default is `LOSSLESS`. `-p` accepts a
filename or a name in `playlist.playlist_dir` (with optional `.m3u`/`.m3u8`).
Without `-p`, all reference playlists in that directory are processed.
`favorites.m3u` produces `favorites.quality-lossless.m3u` alongside the source.
Generated files contain a marker so they never become input playlists;
subsequent calls regenerate them. Reference files are never overwritten, and
an unmarked file occupying the output name is rejected.

Paths use the official plugin's `relative_to`, `playlist_dir`, and
`forward_slash` configuration. Ordering, duplicates, comments, and entries
not found in the library are preserved. Unknown entries are logged.

### Selection policy

* Matches are ordered by distance (`1 - similarity score`). `min_threshold`
  is the **minimum similarity**, inclusive, in `(0, 1]`, not a distance limit.
* In `LOSSLESS` mode, a lossless original is retained. Otherwise, a matching
  lossless version takes precedence over closer lossy versions; if none is
  available, an eligible lossy upgrade is used.
* In `LOSSY` mode, lossy originals may upgrade but never downgrade. A lossless
  original may be replaced by the highest-quality matching lossy version.
* The quality ordering includes
  `CBR128 < CBR192 < V2 < V0 < CBR320 < lossless`.
  Known LAME `-V 2`/`-V 0` (or `V2`/`V0`) presets in Beets'
  `encoder_settings` are ranked at 225/300 kbit/s respectively. Other lossy
  files use measured `bitrate` (bits/s); missing bitrate ranks lowest.
  Presets cannot reliably be inferred from average bitrate alone.
* A lossy original above V0 never upgrades to another lossy version, but can
  upgrade to lossless. At V0, a lossy upgrade is still permitted.
* Among candidates of the desired type, prefer higher quality when its
  distance minus the nearest candidate's distance is **less than** `max_gap`.
  A higher-quality upgrade outside that gap requires a choice.
* An unambiguous candidate is automatically accepted only when its distance
  is **less than** `strong_rec_thresh`. If omitted, this setting inherits
  Beets' `match.strong_rec_thresh`. Otherwise, the user selects a numbered
  candidate, keeps the original, or aborts.

`--timid` always asks before a new replacement, even for strong matches.
`--quiet` never prompts: unresolved matches retain the original and log the
candidate IDs/distances for offline review. When both are active, quiet wins
and unconfirmed replacements are not made. Local settings also honor Beets'
shared `import.quiet` and `import.timid` configuration; those import flags are
not root-level Beets CLI flags. Use `--quiet` and `--timid` on this command.

Interactive choices (including “keep”) are cached in an external JSON file,
keyed by library and target strategy, with original/trump item IDs. A choice
is reused if its replacement remains an eligible candidate under the current
threshold and quality rules. Deleted or ineligible replacements are ignored.
Delete the relevant cache entry to reconsider a choice. Automatic choices
are not cached.

Selections below V0 or outside the requested quality type are logged for
review. To retain logs, redirect command output, for example
`beet qualityPlaylist --quiet 2>quality-review.log`.

`--pretend` performs selection and reports intended outputs without writing
playlists, the choice cache, or similarity tables/indexes. Interactive choices
made during a pretend run are not persisted.

## Tests

```sh
python -m unittest discover -s tests -v
```
