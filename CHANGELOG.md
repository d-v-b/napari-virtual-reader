# Changelog

<!-- markdownlint-configure-file {"MD024": {"siblings_only": true}} -->

Notable changes to napari-virtual-reader are recorded here. Publication status
is recorded separately from the release preparation date.

## Unreleased

No changes yet.

## 0.0.4 - 2026-11-23

### Added

- Added dock controls to switch CZI scenes between one canvas at their recorded
  positions and a one-scene-at-a-time slider. Both modes support matching and
  nonmatching scene sizes, scales, dtypes and pyramid depths without padding,
  resampling, or reopening the file. Scene switching fits the camera and keeps
  channels and metadata together, with independent controls per source load.
- Compatible CZI scenes now open as one lazy S-stack layer per acquisition
  channel, with napari's native scene slider, instead of many separate layers.
  Both the opener and File/Open compact layers before insertion. The dock
  switches between the native S stack and the spatial canvas without reopening;
  incompatible scenes retain the separate-layer dock slider.
- Matching scenes also keep one layer per channel in **One canvas**, using
  lazy, chunk-local spatial composition instead of expanding to one layer per
  scene. Preserve the original arrays for S-stack restoration. Sparse canvases
  gain lazy overview levels to avoid allocating their full spatial extent.
  Gaps are zero and later numeric scenes win overlaps; fractional positions
  use nearest-neighbour sampling. Nonmatching or unsupported geometries retain
  separate layers.
- Extended composed canvases to S/T/C/Z/Y/X data: compose S spatially in Y/X,
  keep C as separate acquisition-channel layers, and retain T/Z sliders with
  one plane per output chunk. T/Z origins must agree across matching scenes.
- Added a shared, byte-bounded 64 MiB in-memory LRU cache for generated scene
  overviews. Revisiting cached T/Z chunks avoids source reads; native/stored
  levels remain uncached, and first visits still read the source. There is no
  background prefetch or disk cache.
- Added **Update while dragging**, off by default for a selected composed CZI
  canvas. Dimension-thumb dragging previews the index and loads only on release;
  opt in for continuous updates. Keyboard, committed numeric entry and playback
  remain immediate. Restore prior slider tracking outside the canvas context.
- Added a synchronized, 1-based scene-number field with up/down arrows, keyboard
  controls and direct entry beside the dock slider. It also controls native S
  stacks, without duplicating napari's slider.
- Scripted readers keep compatible S stacks by default; `stack_scenes=False`
  requests individual scenes.

### Changed

- Updated both README-linked visual guides and their index for native scene
  navigation, multidimensional canvases, overview caching and release-only
  dragging. Refreshed the current-UI screenshot and read-path diagram.
- Commit typed dock scene numbers on Enter or focus loss instead of loading
  intermediate digits; step buttons and arrow keys remain immediate.
- Simplified channel grouping, scene restoration and canvas-pyramid preparation.
  Centralized metadata refresh batching and documented coordinate conventions,
  lazy overview sampling and reversible scene state, preserving viewing behavior.

### Fixed

- Aligned the runtime package version with the distribution version, 0.0.4.

### Release Status

Release tag: `v0.0.4`. Tag-triggered GitHub Actions validates Linux, Windows and
macOS with Python 3.12, 3.13 and 3.14 before publishing to PyPI.
See the [release notes](_release_notes/v0.0.4.md) for highlights, validation
and known limitations.

## 0.0.3 - 2026-11-21

### Changed

- Added lazy, chunk-local mosaic assembly for supported 2D CZI slide scans:
  combine irregular native tiles and stored pyramid levels on a common extent,
  preserve RGB/scalar channels and calibration, fill gaps with zero, and resolve
  overlaps in numeric tile order. Record the assembly policy in layer metadata.
  Unsupported tiles keep the incomplete-view warning. VZIP export retains the
  original refkv layout, not the runtime mosaic.
- Warn when a CZI view omits unassembled tiles, including partial pyramids,
  and preserve the warning in layer metadata. Applies to reader loads and
  the opener, without reading pixels or changing image assembly.
- Packed-colour CZI images now open as one RGB/RGBA layer per acquisition
  channel instead of separate B/G/R(/A) layers, including CZI-derived VZIP
  metadata. Preserve lazy pyramids and scene stacks, handle codec-dependent
  component order, and leave scalar fluorescence channels unchanged.
- Combined the file opener and metadata inspector in one plugin dock, with
  loading and optional VZIP-save controls above the expanding inspector.
- File/Open, canvas drag-and-drop, and the plugin menu now share the same dock
  regardless of opening order; reader loads reopen it when hidden.
- Added rendered visual guides for the plugin workflow and CZI tile assembly,
  with automated GitHub Pages publishing.
- Marked the package as experimental Beta software.
- Pinned CI validation to PyPI's refkv 0.2.0 across the supported platform and
  Python matrix; the runtime requirement remains refkv 0.2.0 or newer.

### Fixed

- Prevented the metadata inspector from appearing as a separate dock above
  the file-loading controls.
- Corrected the package's Beta classifier to the valid PyPI classifier.
- Replaced the legacy Windows CI OpenGL installer with maintained Mesa setup
  for GUI validation.

### Release Status

Release tag: `v0.0.3`. Tag-triggered GitHub Actions validates the supported
platforms and Python versions before publishing to PyPI.
See the [release notes](_release_notes/v0.0.3.md) for highlights, validation,
and known limitations.

## 0.0.2 - 2026-11-06

### Changed

- Added README screenshots of the plugin, image viewer, and metadata inspector.
- Used absolute screenshot URLs for GitHub and PyPI README rendering.
- Simplified the CZI scene-stacking description.
- Made the Releases section version-independent with permanent documentation links.

### Fixed

- Replaced bracketed filename patterns with simple extension filters such as `*.czi` and `*.nii.gz` in the napari manifest.

### Release Status

Patch release covering documentation and reader filename-pattern registration;
dependencies are unchanged.
Prepared for tag `v0.0.2`; GitHub Actions and PyPI publication are pending.
See the [release notes](_release_notes/v0.0.2.md) for details.

## 0.0.1 - 2026-11-05

### Added

- Experimental Alpha napari plugin with npe2 reader and widget registration.
- Lazy, reference-backed OME-Zarr image loading through refkv.
- Optional VZIP reference-archive saving without pixel conversion.
- Shared metadata inspector for Zarr structure and selected-layer metadata.
- Compatible CZI scene stacking and stored multiscale pyramid support.
- Automated tests, package validation, and tag-triggered PyPI publishing.

### Changed

- Renamed the project from napari-refkv to napari-virtual-reader.

### Release Status

Initial experimental Alpha release, prepared for tag `v0.0.1`. GitHub Actions
and PyPI publication are pending. See the [release notes](_release_notes/v0.0.1.md)
for features, validation, and known limitations.
