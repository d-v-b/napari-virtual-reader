"""Assemble refkv's 2D CZI tiles without materialising a complete slide scan.

The reader already provides layers for regular image levels. This module adds
the irregular tiles those levels cannot represent:

* ``_Piece`` describes one source rectangle for one acquisition channel.
* ``_Mosaic`` exposes one resolution as an array that Dask reads in small chunks.
* ``assemble_czi_layers`` combines the sources and replaces each supported layer.
* ``compose_scene_canvas`` uses the same chunk reader for matching scene layers.

Placement uses native CZI pixel coordinates (x, y, width, height); array indexing
uses (y, x), with an optional trailing RGB(A) axis. Physical units are applied
only when updating napari's layer transforms. Preparation reads metadata, not
pixels; decoding starts in ``_Piece.read`` when Dask requests a chunk.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import uuid4

import dask.array as da
import numpy as np

# This bounds Dask output blocks, not decoder buffers or concurrent task memory.
_CHUNK_EDGE = 1024


class _FromArray(Protocol):
    """The documented Dask constructor arguments used by this adapter.

    Pylance infers chunks as str from Dask's default "auto", rejecting supported
    tuple chunk sizes. This narrow contract checks our arguments without
    suppressing diagnostics or changing the selected runtime Dask backend.
    """

    def __call__(
        self,
        x: object,
        *,
        chunks: tuple[int, ...],
        asarray: bool,
        fancy: bool,
        name: str,
        meta: np.ndarray,
    ) -> da.Array: ...


def _pyramid_factor(size: list[int], stored: list[int]) -> int | None:
    """Classify a tile's downsampling using the CZI profile's libCZI rules.

    ``size`` is its native footprint; ``stored`` is its encoded pixel count.
    Clipped tiles need not have an exact integer ratio. The longer stored axis,
    tolerances, and power-of-two-before-power-of-three search order come from
    refkv's CZI profile, section 3.4, rather than a nearest-power heuristic.
    Returning None leaves an unclassified tile available for the reader warning.
    """
    if size == stored:
        return 1
    axis = 0 if stored[0] > stored[1] else 1
    ratio = size[axis] / stored[axis]

    for base, tolerances in (
        (2, (0.1, 0.2, 0.4, 0.8, 1, 1, 1, 2, 4, 10)),
        (3, (0.1, 0.2, 0.8, 1.5, 2, 5, 15)),
    ):
        for exponent, tolerance in enumerate(tolerances, 1):
            factor = base**exponent
            if abs(ratio - factor) <= tolerance:
                return factor
    return None


def _sample_axis(
    positions: np.ndarray, start: float, extent: float, pixel_count: int
) -> tuple[np.ndarray, np.ndarray]:
    """Map output centres inside a piece to that piece's stored pixel indexes.

    Coordinates here measure from pixel edges: centres at 0.5 and 1.5 select
    source indexes 0 and 1 at native resolution. Scaling by the actual footprint
    handles clipped tiles whose nominal pyramid factor is only approximate.
    The half-open interval excludes the far edge; clipping guards floating-point
    rounding at that edge, not missing coverage outside the piece.
    """
    output_indices = np.flatnonzero((positions >= start) & (positions < start + extent))
    source_indices = np.floor(
        (positions[output_indices] - start) * pixel_count / extent
    ).astype(int)
    return output_indices, np.clip(source_indices, 0, pixel_count - 1)


@dataclass(frozen=True)
class _Piece:
    """One regular level or tile, placed in the native coordinate frame.

    ``channel`` is local to this source array, not the CZI's absolute C index.
    An empty ``order`` means scalar pixels; otherwise it selects stored colour
    components in RGB(A) order. ``factor`` classifies the pyramid level, while
    width/height and the source shape determine the actual sampling pitch.
    Raw refkv arrays use YX or CYX; prepared scenes use YX or YXC instead.
    ``channels_last`` distinguishes those layouts without reordering pixels.
    """

    array: Any
    x: float
    y: float
    width: float
    height: float
    factor: int
    channel: int
    order: tuple[int, ...]
    has_c: bool
    channels_last: bool = False

    @property
    def spatial_shape(self) -> tuple[int, int]:
        """Return Y/X sizes independently of the source's colour-axis layout."""
        return self.array.shape[:2] if self.channels_last else self.array.shape[-2:]

    def read(self, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
        """Read a source rectangle and select the requested rows and columns.

        Zarr receives basic slices so it can fetch just the intersecting chunks
        (a compressed chunk may still need decoding in full). Selection of
        repeated/skipped sample indexes happens afterwards in NumPy. These are
        stored source indexes, already mapped from output centres by _sample_axis.
        """
        # A basic rectangular slice works with the source codecs even when the
        # final sample indexes repeat or skip positions at clipped coarse edges.
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        selection = (slice(y0, y1), slice(x0, x1))

        if self.has_c:
            # refkv puts packed colour samples on C: one acquisition channel
            # occupies three/four consecutive indexes, not one scalar index.
            if self.order:
                start = self.channel * len(self.order)
                channel_selection = slice(start, start + len(self.order))
            else:
                channel_selection = self.channel
            selection = (channel_selection,) + selection

        # This is the pixel-I/O boundary. Let errors propagate: unavailable
        # source data must not be mistaken for an intentionally uncovered gap.
        pixels = np.asarray(self.array[selection])

        if self.order:
            pixels = np.moveaxis(pixels, 0, -1)[..., list(self.order)]
        # np.ix_ selects the Cartesian product, not paired (y, x) coordinates.
        return pixels[np.ix_(ys - y0, xs - x0)]


class _Mosaic:
    """One lazy resolution, with a spatial index of candidate source pieces.

    Dask normally calls __getitem__ for one output chunk. Each call allocates
    only its requested rectangle, reads intersecting pieces, and overwrites
    overlaps in source order. Concurrent tasks have separate output buffers.

    ``factor`` determines the output shape unless ``shape`` supplies an explicit
    Y/X size for a scene-canvas level. The actual pitch comes from that shape and
    the common bounds. ``samples`` is zero for scalar data or the packed RGB(A)
    component count; an acquisition C already has its own layer.
    """

    def __init__(
        self,
        pieces: list[_Piece],
        bounds: tuple[float, float, float, float],
        factor: int,
        dtype: np.dtype,
        samples: int,
        shape: tuple[int, int] | None = None,
    ) -> None:
        self.pieces = pieces
        self.bounds = bounds
        _, _, width, height = bounds
        self.shape = shape or (math.ceil(height / factor), math.ceil(width / factor))
        if samples:
            self.shape += (samples,)
        self.ndim = len(self.shape)
        self.dtype = dtype
        # Dask clips edge blocks to the array shape. Keep packed components in
        # one block so a colour pixel is decoded and reordered together.
        self.chunks = (_CHUNK_EDGE, _CHUNK_EDGE) + ((samples,) if samples else ())
        # Napari derives level transforms from shape ratios. Sample every level
        # over the same extent so those transforms also align clipped/odd edges.
        self.step = (width / self.shape[1], height / self.shape[0])
        self.index = self._index_pieces()

    def _index_pieces(self) -> dict[tuple[int, int], list[int]]:
        """Map output-chunk bins to source pieces without reading pixels."""
        x, y, _, _ = self.bounds
        step_x, step_y = self.step
        index: dict[tuple[int, int], list[int]] = defaultdict(list)
        # Index output-chunk bins, not individual pixels. Conservative rounded
        # bounds can include extra candidates; the read path checks exact coverage.
        for piece_index, piece in enumerate(self.pieces):
            # Clip bin coverage to the canvas: coarse source footprints can
            # extend beyond the finest-level bounds without enlarging the view.
            left = max(0, math.floor((piece.x - x) / step_x))
            top = max(0, math.floor((piece.y - y) / step_y))
            right = min(
                self.shape[1], math.ceil((piece.x + piece.width - x) / step_x)
            )
            bottom = min(
                self.shape[0], math.ceil((piece.y + piece.height - y) / step_y)
            )
            for row in range(top // _CHUNK_EDGE, math.ceil(bottom / _CHUNK_EDGE)):
                for col in range(left // _CHUNK_EDGE, math.ceil(right / _CHUNK_EDGE)):
                    index[row, col].append(piece_index)
        return index

    def __getitem__(self, selection: tuple[slice, ...]) -> np.ndarray:
        """Compose the basic slices passed by Dask, retaining zero-filled gaps.

        This is a small internal array adapter, not a complete NumPy indexing
        implementation. Dask handles higher-level indexing around these reads.
        """
        ys, xs = (
            np.arange(*part.indices(size))
            for part, size in zip(selection[:2], self.shape[:2])
        )
        # Allocate only this request, not the entire slide. Components stay
        # together during composition; a trailing sample slice is applied last.
        shape = (len(ys), len(xs)) + self.shape[2:]
        result = np.zeros(shape, dtype=self.dtype)
        if not len(ys) or not len(xs):
            return result
        # A request can touch several bins. Deduplicate their candidates so one
        # source is not decoded repeatedly just because it spans a bin boundary.
        candidates = set()
        for row in range(
            int(ys.min()) // _CHUNK_EDGE, int(ys.max()) // _CHUNK_EDGE + 1
        ):
            for col in range(
                int(xs.min()) // _CHUNK_EDGE, int(xs.max()) // _CHUNK_EDGE + 1
            ):
                candidates.update(self.index.get((row, col), ()))
        # Sample at output pixel centres, including at coarse levels. Using
        # corners instead would shift images when switching pyramid levels.
        world_x = self.bounds[0] + (xs + 0.5) * self.step[0]
        world_y = self.bounds[1] + (ys + 0.5) * self.step[1]
        # The set removes duplicate candidates from adjacent bins. Sorting
        # restores source order so overlap results do not depend on set order.
        for index in sorted(candidates):
            piece = self.pieces[index]
            output_x, source_x = _sample_axis(
                world_x, piece.x, piece.width, piece.spatial_shape[1]
            )
            output_y, source_y = _sample_axis(
                world_y, piece.y, piece.height, piece.spatial_shape[0]
            )
            if not len(output_x) or not len(output_y):
                continue
            result[np.ix_(output_y, output_x)] = piece.read(source_y, source_x)
        return result[(slice(None), slice(None)) + selection[2:]]

    def lazy(self) -> da.Array:
        """Build a graph without probing pixels or hashing the source hierarchy."""
        # An explicit name avoids tokenising thousands of Zarr-backed pieces.
        # Explicit meta supplies dtype/dimensions without a trial __getitem__.
        return cast(_FromArray, da.from_array)(
            self,
            chunks=self.chunks,
            asarray=False,
            fancy=False,
            name=f"czi-mosaic-{uuid4().hex}",
            meta=np.empty((0,) * self.ndim, dtype=self.dtype),
        )


def compose_scene_canvas(
    scene_levels: list[list[da.Array]],
    origins: np.ndarray,
    scale: np.ndarray,
    *,
    rgb: bool,
) -> tuple[list[da.Array], tuple[float, float]]:
    """Build a lazy canvas pyramid and its physical Y/X origin.

    The caller has already checked that scenes share shapes, dtype, calibration
    and pyramid depth. ``scene_levels`` contains one pyramid per scene; origins
    and scale use physical Y/X units. Pieces use X/Y native-pixel coordinates.
    """
    origin = origins.min(axis=0)
    offsets = (origins - origin) / scale
    scene_size = np.asarray(scene_levels[0][0].shape[:2])
    extent = (offsets + scene_size).max(axis=0)
    # Avoid an extra pixel caused only by calibration rounding near an integer.
    rounded_extent = np.rint(extent)
    near_integer = np.isclose(extent, rounded_extent, rtol=0, atol=1e-7)
    extent = np.ceil(np.where(near_integer, rounded_extent, extent))

    # Transpose scenes-by-levels into levels-by-scenes, keeping each output
    # shape with its sources so the two cannot get out of sync.
    pyramid = []
    for resolution in zip(*scene_levels):
        downsampling = scene_size / np.asarray(resolution[0].shape[:2])
        shape = tuple(int(np.ceil(length)) for length in extent / downsampling)
        pyramid.append((resolution, shape))
    # A sparse plate can span gigabytes even when its scenes are tiny. Reuse
    # the coarsest stored sources to sample overviews, not a dense fine canvas.
    while max(pyramid[-1][1]) > _CHUNK_EDGE:
        resolution, shape = pyramid[-1]
        overview_shape = tuple((length + 1) // 2 for length in shape)
        pyramid.append((resolution, overview_shape))

    samples = scene_levels[0][0].shape[-1] if rgb else 0
    bounds = (0, 0, float(extent[1]), float(extent[0]))
    canvases = []
    for resolution, shape in pyramid:
        pieces = [
            _Piece(
                array=array,
                x=float(offset[1]),
                y=float(offset[0]),
                width=float(scene_size[1]),
                height=float(scene_size[0]),
                factor=1,
                channel=0,
                order=(),
                has_c=False,
                channels_last=True,
            )
            for array, offset in zip(resolution, offsets)
        ]
        canvas = _Mosaic(
            pieces, bounds, 1, resolution[0].dtype, samples, shape=shape,
        )
        canvases.append(canvas.lazy())
    return canvases, (origin[0], origin[1])


def assemble_czi_layers(
    root: Any,
    layers: list[tuple[Any, dict, str]],
    image_factors: list,
) -> set[str]:
    """Replace supported layer data in place; return fully incorporated tile names.

    The live refkv summary supplies regular-image factors. Archives do not
    retain it, so callers without that calibration leave tiles unassembled.
    ``image_factors[series]`` lists (factor, tile columns, tile rows) per regular
    level; only the factor is needed here to recover native pixel calibration.

    Pieces are grouped by their stored factor: no missing resolution is
    synthesized, and a gap is never filled using a different resolution.
    Existing layer names, channel settings and OME metadata are retained.
    Unsupported tiles are deliberately absent from the returned set: the
    reader uses that difference to warn about incomplete views.
    """
    from ._reader import _czi_rgb_order

    if "tiles" not in root or not image_factors:
        return set()
    # Metadata-only discovery. Numeric ordering matters: tile "10" must be
    # drawn after "2". No source pixel decoding occurs in this loop.
    tiles = []

    for name, array in sorted(root["tiles"].arrays(), key=lambda item: int(item[0])):
        # Do not infer placement from a tile's filename or array shape alone.
        # Missing metadata leaves it out of the consumed set and hence warned.
        placement = array.attrs.get("refkv_zarrified", {}).get("czi", {})
        if not all(
            key in placement for key in ("size", "stored_size", "x", "y", "planes")
        ):
            continue
        axes = tuple(array.metadata.dimension_names or ())

        # _Piece.read assumes these axis orders. Flattening extra axes here
        # could silently mix time points or Z planes into one spatial mosaic.
        if axes not in (("y", "x"), ("c", "y", "x")):
            continue
        factor = _pyramid_factor(placement["size"], placement["stored_size"])
        if factor is not None:
            tiles.append((name, array, placement, factor, axes))
    used_channels: dict[str, set[int]] = defaultdict(set)
    tile_channels = {}

    # Work per unstacked acquisition layer: its scene and physical calibration
    # must still be available before the reader adds a shared S dimension.
    for layer_index, (_, kwargs, kind) in enumerate(layers):
        metadata = kwargs["metadata"]
        series = metadata["series"]
        if (
            kwargs["axis_labels"] != ("y", "x")
            or not series.isdecimal()
            or int(series) >= len(image_factors)
            or metadata["multiscale_index"] != 0
        ):
            continue
        group = root[series]
        # Match all native block dimensions, not just S; tiles from another
        # scene or block must never contribute even if their XY rectangles overlap.
        dimensions = (
            group.attrs.get("refkv_zarrified", {}).get("czi", {}).get("dimensions", {})
        )
        matching = [
            tile for tile in tiles if tile[2].get("dimensions", {}) == dimensions
        ]
        # Without a common native plane origin it is unsafe to mix time/Z planes.
        if (
            not matching
            or len({(p["planes"]["t"], p["planes"]["z"]) for _, _, p, _, _ in matching})
            != 1
        ):
            continue
        first_channel = min(p["planes"]["c"] for _, _, p, _, _ in matching)
        channel = metadata["channel"]
        factors = image_factors[int(series)]
        multiscale = metadata["ome"]["multiscales"][0]
        datasets = multiscale["datasets"]

        if len(factors) != len(datasets):
            raise ValueError(f"CZI pyramid summary differs from series {series}")

        base_factor = factors[0][0]
        # The original layer may start at an overview, not at native resolution.
        # Undo its downsampling to recover physical units per native pixel.
        pixel_size_x = kwargs["scale"][1] / base_factor
        pixel_size_y = kwargs["scale"][0] / base_factor
        pieces: dict[int, list[_Piece]] = defaultdict(list)
        dtype = group[datasets[0]["path"]].dtype
        samples = len(metadata.get("color_components", ""))

        # Retain regular levels as sources too. They precede irregular tiles
        # within each level, giving subsequently appended tiles overlap priority.
        for dataset, (factor, _, _) in zip(datasets, factors):
            array = group[dataset["path"]]
            axes = tuple(array.metadata.dimension_names or ())
            if not axes:
                axes = tuple(
                    axis if isinstance(axis, str) else axis["name"]
                    for axis in multiscale["axes"]
                )
            transforms = dataset.get("coordinateTransformations", [])
            translations = [
                t["translation"] for t in transforms if t["type"] == "translation"
            ]
            if len(translations) != 1 or factor < 1:
                raise ValueError(
                    f"CZI series {series} has no unambiguous level placement"
                )
            translation = translations[0]
            # refkv translates the centre of a downsampled pixel by (F - 1)/2
            # native pixels. Reverse that offset before placing its rectangle.
            x = translation[axes.index("x")] / pixel_size_x - (factor - 1) / 2
            y = translation[axes.index("y")] / pixel_size_y - (factor - 1) / 2
            if not np.allclose((x, y), (round(x), round(y)), rtol=0, atol=1e-6):
                raise ValueError(
                    f"CZI series {series} has a non-integral native origin"
                )
            x, y = round(x), round(y)
            order = _czi_rgb_order(array, 0) if "c" in axes else ()

            # Regular arrays express size in stored pixels. Convert that
            # footprint back to native units to share a frame with tile placement.
            pieces[factor].append(
                _Piece(
                    array,
                    x,
                    y,
                    array.shape[-1] * factor,
                    array.shape[-2] * factor,
                    factor,
                    channel,
                    order,
                    "c" in axes,
                )
            )

        incorporated = []

        # The tile's C plane gives an absolute origin; its stored C axis can
        # contain several acquisition channels, each with packed colour samples.
        for name, array, placement, factor, axes in matching:
            order = _czi_rgb_order(array, 0) if "c" in axes else ()
            count = array.shape[0] // (len(order) or 1) if "c" in axes else 1
            tile_channels[name] = count
            # CZI plane indexes can start above zero. Convert the layer's
            # relative acquisition channel to this particular tile's C offset.
            tile_channel = first_channel + channel - placement["planes"]["c"]
            # Do not cast dtype or invent missing components to force a match.
            # An incompatible tile/channel remains visible in the warning count.
            if (
                not 0 <= tile_channel < count
                or len(order) != samples
                or array.dtype != dtype
            ):
                continue
            pieces[factor].append(
                _Piece(
                    array,
                    placement["x"],
                    placement["y"],
                    placement["size"][0],
                    placement["size"][1],
                    factor,
                    tile_channel,
                    order,
                    "c" in axes,
                )
            )
            incorporated.append((name, tile_channel))
        if not incorporated:
            continue
        # Only the finest level defines the canvas. Including coarse footprints
        # here could expand it because coarse edge tiles extend beyond the scan.
        pyramid_factors = sorted(pieces)
        finest = pieces[pyramid_factors[0]]
        x0, y0 = min(p.x for p in finest), min(p.y for p in finest)
        width = max(p.x + p.width for p in finest) - x0
        height = max(p.y + p.height for p in finest) - y0
        bounds = (x0, y0, width, height)
        pyramids = [
            _Mosaic(pieces[f], bounds, f, dtype, samples).lazy()
            for f in pyramid_factors
        ]
        kwargs["multiscale"] = len(pyramids) > 1
        # Napari expects transforms for the finest displayed level. Keep the
        # same pixel-centre convention even when native resolution is absent.
        kwargs["scale"] = (
            pixel_size_y * height / pyramids[0].shape[0],
            pixel_size_x * width / pyramids[0].shape[1],
        )
        kwargs["translate"] = (
            pixel_size_y * (y0 + (height / pyramids[0].shape[0] - 1) / 2),
            pixel_size_x * (x0 + (width / pyramids[0].shape[1] - 1) / 2),
        )
        metadata["czi_assembly"] = {
            "bounds": bounds,
            "factors": pyramid_factors,
            "full_resolution": pyramid_factors[0] == 1,
            "tile_arrays": len(incorporated),
            "overlap": "later numeric tile wins",
            "sampling": "nearest neighbour on a common extent; gaps are zero",
        }
        layers[layer_index] = (
            pyramids if kwargs["multiscale"] else pyramids[0],
            kwargs,
            kind,
        )
        for name, tile_channel in incorporated:
            used_channels[name].add(tile_channel)
    # A multichannel tile is only fully consumed after ALL its acquisition
    # channels were used. Partial consumption must not suppress the warning.
    return {
        name
        for name, channels in used_channels.items()
        if len(channels) == tile_channels[name]
    }
