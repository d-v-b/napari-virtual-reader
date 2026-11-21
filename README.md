# napari-virtual-reader

A napari plugin for lazy, reference-backed OME-Zarr images using
[refkv](https://github.com/d-v-b/refkv).

## Remarks

**Experimental software - Beta status.** APIs and behavior may change without
notice. Validate results independently before relying on them for analysis.

Open images without first converting them, writing a pixel copy, or creating
an intermediate archive. Requested regions are still decoded and composed in
memory for display; "virtual" does not mean zero memory use.

The plugin uses refkv directly. It does not depend on czitools, pylibCZIrw,
or the napari-czitools plugin.

This README describes the source checkout. Published releases may not yet include
all features below; see the [changelog](CHANGELOG.md) for unreleased changes.

The screenshots show the earlier two-dock layout. The current UI combines the
loading controls and metadata inspector in one dock.

<img src="https://raw.githubusercontent.com/sebi06/napari-virtual-reader/main/_images/czi_virtual_omezarr1.png" alt="CZI image displayed in napari with the earlier separate opener and metadata docks" style="width:80%; height:auto;">

## Visual guides

- [How the plugin works](https://sebi06.github.io/napari-virtual-reader/plugin-workflow.html):
  lazy loading, the combined dock, metadata and VZIP archives.
- [How CZI tile assembly works](https://sebi06.github.io/napari-virtual-reader/czi-tile-assembly.html):
  tile placement, chunk reads, coordinate mapping, pyramids and limitations.

These links open rendered HTML on GitHub Pages, not GitHub's HTML source viewer.
The editable sources and standalone pages are in [_docs](_docs/);
see the [documentation publishing notes](_docs/README.md) for regeneration and deployment.

## Install

Python 3.12 or newer is required. Install into the environment running napari:

```powershell
python -m pip install napari-virtual-reader
```

A working Qt backend is required. For a fresh environment:

```powershell
python -m pip install "napari[pyqt6]" napari-virtual-reader
napari
```

Restart napari after installation. The package registers an npe2 reader and
widget through its `napari.manifest` entry point; no manual registration is
required. Check **Plugins > Plugin Manager** for **napari-virtual-reader**.

The plugin is also listed on
[napari hub](https://napari-hub.org/plugins/napari-virtual-reader.html).

## Format-specific features

### CZI image files

- **RGB images:** brightfield colour images open as one RGB/RGBA layer per
  acquisition channel, not separate red, green, and blue layers. Fluorescence
  channels remain separate.
- **Large 2D images and pyramids:** supported 2D mosaics are assembled on demand,
  including full resolution when native tiles are available. Napari switches
  between stored resolution levels as you zoom, without loading the whole image
  or writing a converted copy.
- **Multiple scenes:** choose **One canvas** or **Scene slider** in the dock.
  Both modes support matching and different scene sizes, scales, dtypes, and
  pyramid depths without padding or resampling.

**Why assembly helps:** refkv can expose irregular or overlapping CZI tiles
separately from its regular image levels. Reading only those levels can omit
native resolution or show just an overview. The plugin places supported tiles
using their CZI coordinates and groups them into the available resolution levels.
Placement restores the missing view; lazy, chunk-by-chunk reads make it practical
without building the whole slide in memory. It does not invent missing pixels
or resolution levels.

**This extra assembly is CZI-only.** Other formats can display images and pyramids
already exposed by refkv, but the plugin does **not yet assemble their separate
irregular tile arrays**. General lazy loading is not a general tile-assembly solution.

Open the **original CZI** to use mosaic assembly; reopening a VZIP archive
does not currently restore this assembly. Overlapping tiles use a fixed
"later tile wins" rule, so overlap regions can look different from ZEN.
If some tiles cannot be assembled, the plugin warns that the view may be
incomplete. See [Scope](#scope) for technical details and limitations.

## Open an image

In napari, choose **Plugins > napari-virtual-reader > Open Data as virtual OME-ZARR**,
select the input, and click **Open**. This dock prepares the image in a background
worker; napari provides its normal layer, contrast, time, and Z controls.

The plugin uses one dock: the file selector, **Save VZIP archive**, and **Open**
controls stay at the top, with the metadata inspector below. The inspector
receives the remaining vertical space as the dock is resized.

You can also use **File > Open** or drop a file on the napari canvas and choose
the napari-virtual-reader reader. Drop a file on the plugin dock to select it without
opening it yet.

```python
import napari

viewer = napari.Viewer()
viewer.open("image.czi", plugin="napari-virtual-reader")
napari.run()
```

Raw images are virtualized in memory through `refkv.zarrify.zarrify`. Existing
`.vzip` archives are opened directly. Original image files must remain available
and unchanged for as long as their layers are used. Nothing is written beside
the source by default.

### Save a VZIP Archive

In **Plugins > napari-virtual-reader > Open Data as virtual OME-ZARR**, select an image and enable
**Save VZIP archive** before clicking **Open**. Choose the destination in the
save dialog; the suggested filename is a sibling `.vzip`. Saving runs in the
background, and the resulting image still opens lazily with the metadata panel.
The status shows the saved filename, with its full path available on hover.

Saving stores refkv's reference layout, not converted pixel data. It retains
the image series, pyramid levels and separate tile arrays exposed by refkv,
not just the selected napari layer. It is not a self-contained image backup:
the referenced source must remain available and unchanged.
Existing destinations require explicit replacement confirmation; failed writes
leave the old archive intact. Cancelling the save dialog cancels the open action.
The save option is disabled for inputs that are already VZIP archives.
File/Open and canvas drops remain in-memory operations without automatic saving.

The saved archive retains refkv's original reference layout, not the plugin's
runtime mosaic composition. Refkv's live pyramid-factor summary is not saved
in VZIP. If a CZI-derived archive contains separate tile arrays, reopening it
still warns about unassembled tiles; open the original CZI to use mosaic assembly.
Archive reads also depend on refkv's source-access policy; an archive can fail to
open if that policy does not permit its referenced local files.

## Metadata Inspector

The metadata inspector is embedded below the loading controls in the
**Open Data as virtual OME-ZARR** dock, which opens on the right by default.
Using the refkv reader through File/Open or canvas drag-and-drop automatically
opens this same dock, without requiring the plugin menu first. Opening the
plugin from the menu before or after a reader load reuses the dock rather than
creating a second panel. A new reader load also reopens the dock if it was hidden.
The inspector follows the selected image layer, independently of how it was loaded.

### CZI scene display

Select a layer from a multi-scene CZI, then use **CZI scenes** in the dock:

- **One canvas** (default): display the individual scene layers together at their
  recorded physical positions, including differently sized scenes.
- **Scene slider**: show only the selected scene's channels. Move the slider to
  switch scenes in numeric series order; the camera fits the selected scene.
  Each scene retains its original lazy arrays, pyramid levels, dtype, scale and
  translation. No common-sized scene array or converted pixel copy is created.

Switch modes after loading without reopening the file. The controls apply to
the selected layer's source load; other images and repeated loads of the same
file remain independent. Channel visibility settings are retained across scene
switches and when returning to canvas. Selecting an unrelated layer disables
the controls. CZI-derived VZIPs use the same scene controls, but retain their
existing tile-assembly limitations.

This is a dock slider, not a padded napari **S** dimension. A single CZI scene
containing multiple separated scan regions still counts as one scene; the reader
does not invent scene boundaries. The controls require multiple OME image series
exposed by refkv.

- **Zarr structure** shows expandable groups and arrays, including original
  shapes, dtypes, attributes, chunk grids and codec configuration.
- **Metadata** shows the selected layer's OME metadata, source and channel
  information, plus original scene transforms for stacked CZI images.
- A filter finds matching properties; selecting a row shows its complete,
  selectable value below the tree.

<img src="https://raw.githubusercontent.com/sebi06/napari-virtual-reader/main/_images/czi_virtual_omezarr2.png" alt="Zarr structure and image metadata in the earlier separate metadata dock" style="width:80%; height:auto;">

The inspector uses a metadata-only snapshot collected during reader preparation
(in the background when using the plugin's **Open** button). It does not scan
pixel data or reconstruct the source hierarchy from displayed layer shapes.
Selecting an unrelated layer clears the inspector.

## Scope

- **Lazy CZI mosaic assembly:** for 2D slide-scan series with an OME image and
  refkv's live pyramid-factor summary, the plugin assembles irregular and
  overlapping tiles alongside the existing image levels. Full resolution is
  included when native tiles are available. Each requested output chunk is at
  most 1024 x 1024 pixels per acquisition channel and reads only intersecting
  source regions. This bounds output tasks, not total memory: a compressed source
  chunk can require full decoding, and concurrent tasks use separate buffers.
  No full mosaic or converted image is written.
- Mosaic levels share the finest level's spatial extent. Coarse levels use
  nearest-neighbour sampling to align differing origins and clipped/odd edges
  with napari's shape-derived pyramid transforms. Native full-resolution
  pixels are not resampled. Gaps are zero; overlaps use **later numeric refkv
  tile wins**, with tiles drawn over any existing assembled level. This is
  deterministic, not blending or image registration, and is not guaranteed to
  match ZEN/libCZI overlap selection or codec rounding pixel-for-pixel.
  `metadata["czi_assembly"]` records bounds, factors, full-resolution availability,
  tile count, and compositing policy. The inspector's Zarr tree still describes
  the original reference store, not the runtime mosaic.
  Missing resolution levels are not generated, and gaps at one level are not
  filled from another. Source read/decode errors propagate; they are not replaced
  with zero-filled pixels.
- **Remaining incomplete CZI views:** time/Z tile stacks, scenes without an
  OME image, unclassified tile scales, and VZIPs without the live calibration
  summary are not assembled by this implementation. Unsupported tiles retain
  the napari warning once per source load and `metadata["reader_warning"]`.
  An input without any supported OME image still fails explicitly. Use
  napari-czitools for those cases; do not assume a displayed pyramid is complete
  when the warning is present.
- CZI, TIFF/OME-TIFF, and other refkv-supported files that expose OME image series.
  Reader registration includes common microscopy and volumetric image extensions;
  actual support is determined by refkv's parser and the file's metadata/codecs.
  The extra tile assembler and incomplete-tile warning are CZI-specific; absence
  of that warning for another format does not establish that its view is complete.
- One layer per acquisition channel. Packed-colour CZI images (`Bgr24`, `Bgr48`,
  `Bgr96Float`, and `Bgra32`) open as RGB/RGBA layers rather than separate colour
  components. Detection uses refkv's CZI profile and component chunk layout,
  not channel count or names. Component order is corrected per pyramid level:
  raw/Zstd/LZW BGR(A) is reordered, while JPEG/JPEG-XR RGB(A) is retained.
  Complex-valued packed-colour CZI pixels are not supported.
- GUI loads retain individual CZI scene layers for canvas/slider display.
  Other formats retain separate series layers without CZI scene controls.
  Scripted `read_images(path)` calls retain the earlier behavior of stacking
  compatible CZI scenes lazily on a leading **S** axis. Pass
  `stack_scenes=False` to retain individual scenes for the dock controls.
- RGB/RGBA keeps its component axis last, outside napari's dimension sliders;
  ordinary fluorescence channels remain separate additive layers. Loading and
  reordering stay lazy, preserve pixel dtype, and do not write converted images.
- CZI stacking requires matching axes, shapes, dtypes, physical scales and
  pyramid levels. Incompatible scenes remain separate without cropping or
  resampling. Axes with differing scene origins use zero translation on the
  stacked layer; shared origins are retained. Original origins and metadata
  remain available in the layer's `metadata["scenes"]` list. These compatibility
  restrictions apply only to scripted S stacks, not the dock's scene slider.
- Stored multiscale pyramids, OME axis ordering, physical scale/origin, channel
  names, colors, and display windows.
- Regular and rectilinear chunk grids, without scanning complete lazy arrays
  for contrast limits. Files without display windows use dtype bounds (0-1 for
  floating point); adjust contrast in napari as needed.
- OME-Zarr 0.4 and 0.5 image metadata, including nested image hierarchies.

This is deliberately not a replacement for every refkv dataset interface:
non-image tables, audio, arbitrary tensors, remote inputs, and stores without
OME image metadata are outside this first version. Time and Z use napari's
dimension sliders; the dock adds CZI scene-display controls.

## Development

```powershell
git clone https://github.com/sebi06/napari-virtual-reader.git
cd napari-virtual-reader
python -m pip install -e ".[testing]"
python -m pytest
python -m ruff check .
```

Install a Qt backend as above if the development environment does not already
provide one. Development installs register the same napari plugin as PyPI installs.

Source guide:

- [_reader.py](src/napari_virtual_reader/_reader.py): reference stores, lazy
  layers, axes, colours, scenes, warnings and archive saving.
- [_czi_mosaic.py](src/napari_virtual_reader/_czi_mosaic.py): native tile
  placement, spatial indexing and on-demand pyramid composition.
- [_widget.py](src/napari_virtual_reader/_widget.py): loading controls,
  preparation workers and Qt-thread delivery of layers.
- [_metadata.py](src/napari_virtual_reader/_metadata.py): shared dock access
  and a read-only inspector built from metadata snapshots.

## Releases

Version [0.0.3](_release_notes/v0.0.3.md) adds lazy 2D CZI mosaic assembly,
packed-colour CZI handling, a combined loading and metadata dock, and visual
documentation. The package remains experimental Beta software; review
[Scope](#scope) before using it for analysis.

The package workflow tests pull requests and pushes to `main` against
`refkv[zarr,images]==0.2.0` from PyPI on Linux, Windows, and macOS with
Python 3.12, 3.13, and 3.14. The runtime requirement remains refkv 0.2.0 or newer.
PyPI publishing
runs only after its test matrix passes on a pushed tag beginning with `v`;
use the release convention `vX.Y.Z`.

Set `project.version` in `pyproject.toml` to the intended release version before
tagging; tags do not change the package version. PyPI requires a new
version for every upload. Manual runs of the package workflow test and build
without publishing to PyPI. The separate documentation workflow publishes
GitHub Pages on relevant pushes to `main` or a manual run on `main`.

See the [changelog](CHANGELOG.md) for the change history and the
[release notes](_release_notes/) for release-specific features, validation, and
limitations. Published tags and release details are available on
[GitHub Releases](https://github.com/sebi06/napari-virtual-reader/releases).
