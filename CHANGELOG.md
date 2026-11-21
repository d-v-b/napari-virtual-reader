# Changelog

<!-- markdownlint-configure-file {"MD024": {"siblings_only": true}} -->

Notable changes to napari-virtual-reader are recorded here. Publication status
is recorded separately from the release preparation date.

## Unreleased

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
