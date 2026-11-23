"""Turn reference-backed OME images into napari layer-data tuples.

``read_images`` opens a reference store; ``_layers`` maps its OME metadata onto
lazy Dask arrays and napari display settings. Raw CZI inputs can additionally
use ``_czi_mosaic`` to place irregular tiles before compatible scenes are stacked.
The returned tuples are (data, keyword arguments, "image"), not live Qt layers.

Preparation reads layout and attributes, not complete image arrays. Pixel
decoding happens later through the Zarr-backed Dask tasks. The metadata inspector
receives a separate snapshot of the original store, not the displayed mosaic.
"""

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import dask.array as da
import numpy as np

LayerData = tuple[Any, dict, str]

# Keep this offer-to-read list aligned with napari.yaml's filename_patterns.
# A suffix selects this reader; refkv and the OME metadata decide actual support.
IMAGE_SUFFIXES = (
    ".czi",
    ".tif",
    ".tiff",
    ".svs",
    ".ndpi",
    ".nd2",
    ".lif",
    ".ims",
    ".vsi",
    ".qptiff",
    ".dcm",
    ".dicom",
    ".nii",
    ".nii.gz",
    ".nrrd",
    ".nhdr",
    ".mrc",
    ".map",
    ".vzip",
)


class _ChunkedArray:
    """Adapt regular and rectilinear Zarr grids to Dask's array protocol.

    Rectilinear grids can have different chunk lengths along an axis. Prefer
    their explicit read sizes rather than assuming Zarr's uniform chunk shape.
    The wrapper exposes metadata eagerly and delegates only requested slices.
    """

    def __init__(self, array: Any) -> None:
        self._array = array
        self.shape = array.shape
        self.dtype = array.dtype
        self.ndim = array.ndim
        self.chunks = getattr(array, "read_chunk_sizes", None)
        if self.chunks is None:
            self.chunks = array.chunks

    def __getitem__(self, selection: Any) -> np.ndarray:
        """Read only the slice requested by a Dask task."""
        return self._array[selection]


def _image_groups(group: Any, name: str = "") -> Iterator[tuple[str, Any, dict]]:
    """Find OME 0.4/0.5 image groups, including scenes and plate fields.

    OME 0.5 nests image metadata under ``ome``; 0.4 keeps it at the group root.
    Once an image group is found, its pyramid datasets belong to that image,
    rather than becoming additional layers through recursive discovery.
    """
    attributes = dict(group.attrs)
    ome = attributes.get("ome", attributes)
    if ome.get("multiscales"):
        yield name, group, ome
        return
    # Numeric ordering keeps scene 2 before scene 10, including in the S slider.
    children = sorted(
        group.groups(),
        key=lambda item: (
            not item[0].isdecimal(),
            int(item[0]) if item[0].isdecimal() else item[0],
        ),
    )
    for child_name, child in children:
        # These groups are provenance, unassembled pieces, or labels, not the
        # intensity images handled here. CZI tiles have their own assembly pass.
        if child_name not in {"refkv_source", "tiles", "labels"}:
            yield from _image_groups(child, f"{name}/{child_name}".strip("/"))


def _lazy_array(array: Any) -> da.Array:
    """Build a graph without probing pixels or using Zarr's regular-only chunks."""
    adapted = _ChunkedArray(array)
    # Supplying empty meta lets Dask infer dtype/dimensions without a trial read.
    return da.from_array(
        adapted,
        chunks=adapted.chunks,
        meta=np.empty((0,) * array.ndim, dtype=array.dtype),
    )


def _contrast_limits(dtype: np.dtype, channel: dict) -> tuple[float, float]:
    """Use embedded windows or dtype bounds instead of scanning a lazy volume.

    Display windows are optional hints. Invalid/missing hints use safe display
    defaults, not measured intensity statistics. In particular, float data can
    exceed 0..1 and may need a manual contrast adjustment in napari.
    """
    fallback = (0.0, 1.0)
    if np.issubdtype(dtype, np.integer):
        limits = np.iinfo(dtype)
        fallback = (float(limits.min), float(limits.max))
    window = channel.get("window", {})
    low, high = window.get("start"), window.get("end")
    if low is not None and high is not None:
        if np.isfinite(low) and np.isfinite(high) and low < high:
            return float(low), float(high)
    return fallback


def _czi_rgb_order(array: Any, channel_axis: int) -> tuple[int, ...]:
    """Return a packed-colour permutation, or () for scalar CZI samples.

    Call only after identifying refkv's CZI profile: a generic three-channel
    image is not evidence of RGB. Here the profile's C chunk layout distinguishes
    acquisition channels from the components packed into each colour pixel.
    """
    chunks = _ChunkedArray(array).chunks[channel_axis]
    sizes = (chunks,) if isinstance(chunks, int) else chunks
    samples = sizes[0]
    if samples not in (3, 4):
        return ()
    # CZI profile v0 uses one acquisition channel per chunk: p packed samples.
    if any(size != samples for size in sizes) or array.shape[channel_axis] % samples:
        raise ValueError(f"Unsupported CZI component chunks: {sizes}")
    supported = ("uint8", "uint16", "float32") if samples == 3 else ("uint8",)
    if array.dtype.name not in supported:
        raise ValueError(f"Unsupported CZI RGB pixel dtype: {array.dtype}")
    codecs = {codec["name"] for codec in array.metadata.to_dict()["codecs"]}
    # JPEG-family decoders have already converted the stored BGR order to RGB.
    # Reordering those again would exchange red and blue a second time.
    if codecs & {"imagecodecs_jpeg", "imagecodecs_ljpeg", "imagecodecs_jpegxr"}:
        return tuple(range(samples))
    if "bytes" not in codecs:
        raise ValueError(f"Unsupported CZI RGB codecs: {sorted(codecs)}")
    return (2, 1, 0, 3) if samples == 4 else (2, 1, 0)


def _group_by_channel(layers: list[LayerData]) -> dict[tuple[int, int, bool], list[LayerData]]:
    """Keep acquisition channels and multiscale series separate within a load."""
    channels = {}
    for layer in layers:
        kwargs = layer[1]
        metadata = kwargs["metadata"]
        key = (metadata["multiscale_index"], metadata["channel"], kwargs["rgb"])
        channels.setdefault(key, []).append(layer)
    return channels


def _scene_metadata(kwargs: dict) -> dict:
    """Keep per-scene provenance without duplicating the shared source tree."""
    metadata = dict(kwargs["metadata"])
    metadata.pop("zarr_structure", None)
    return {**metadata, "scale": kwargs["scale"], "translate": kwargs["translate"]}


def _stack_czi_scenes(layers: list[LayerData], source: Path) -> list[LayerData]:
    """Stack compatible CZI series per channel while retaining scene provenance.

    This is a scene selector, not spatial stitching: every scene keeps its pixels
    and must fit the same axes, scale, shape, dtype and pyramid structure. A lazy
    stack adds S in front of each level; incompatible scene groups stay separate.
    """
    result = []
    for scenes in _group_by_channel(layers).values():
        first = scenes[0][1]
        levels = [
            data if kwargs["multiscale"] else [data] for data, kwargs, _ in scenes
        ]
        compatible = all(
            kwargs["axis_labels"] == first["axis_labels"]
            and np.allclose(kwargs["scale"], first["scale"])
            and len(scene_levels) == len(levels[0])
            and all(
                level.shape == reference.shape and level.dtype == reference.dtype
                for level, reference in zip(scene_levels, levels[0])
            )
            for scene_levels, (_, kwargs, _) in zip(levels, scenes)
        )
        if (
            len(scenes) < 2
            or "s" in (axis.lower() for axis in first["axis_labels"])
            or not compatible
        ):
            result.extend(scenes)
            continue
        # Zip corresponding resolutions, not entire pyramids: napari expects
        # a list of multiscale arrays, each with its own leading scene dimension.
        stacked = [da.stack(level, axis=0) for level in zip(*levels)]
        metadata = {
            **first["metadata"],
            "scenes": [_scene_metadata(kwargs) for _, kwargs, _ in scenes],
        }
        # One napari layer has one transform. Retain origins shared by all
        # scenes; use zero only on axes whose origins differ. The scenes list
        # above preserves every original transform for inspection.
        kwargs = {
            **first,
            "name": f"{source.stem} | {metadata['channel_label']}",
            "axis_labels": ("S",) + first["axis_labels"],
            "scale": (1.0,) + first["scale"],
            "translate": (0.0,)
            + tuple(
                origin
                if all(scene[1]["translate"][index] == origin for scene in scenes)
                else 0.0
                for index, origin in enumerate(first["translate"])
            ),
            "metadata": metadata,
        }
        if "units" in first:
            kwargs["units"] = ("pixel",) + tuple(first["units"])
        result.append((stacked if first["multiscale"] else stacked[0], kwargs, "image"))
    return result


def _compact_czi_layers(layers: list[LayerData]) -> list[LayerData]:
    """Use native S stacks only when the complete source has matching scenes."""
    groups = {}
    for layer in layers:
        load_id = layer[1].get("metadata", {}).get("czi_load_id")
        groups.setdefault(load_id, []).append(layer)
    result = []
    for load_id, scenes in groups.items():
        if load_id is None:
            result.extend(scenes)
            continue
        # A stack has one visibility setting per channel, not per scene.
        channel_groups = _group_by_channel(scenes)
        if any(
            len({kwargs.get("visible", True) for _, kwargs, _ in channel}) > 1
            for channel in channel_groups.values()
        ):
            result.extend(scenes)
            continue
        stacked = _stack_czi_scenes(scenes, Path(scenes[0][1]["metadata"]["source"]))
        scene_ids = [
            [scene["series"] for scene in kwargs["metadata"].get("scenes", [])]
            for _, kwargs, _ in stacked
        ]
        # Do not partially stack a load: every channel needs the same S axis.
        if scene_ids and all(ids and ids == scene_ids[0] for ids in scene_ids):
            result.extend(stacked)
        else:
            result.extend(scenes)
    return result


def _read_gui_images(path: str | list[str]) -> list[LayerData]:
    return _compact_czi_layers(read_images(path, stack_scenes=False))


def _canvas_czi_layers(layers: list[LayerData]) -> list[LayerData]:
    """Compose scenes in Y/X, retaining T/Z axes and separate channel layers."""
    from ._czi_mosaic import _OverviewCache, compose_scene_canvas

    compact = _compact_czi_layers(layers)
    if not compact or any(
        "scenes" not in kwargs["metadata"]
        or tuple(kwargs["axis_labels"])[-2:] != ("y", "x")
        or kwargs["axis_labels"][0] != "S"
        or any(axis not in ("t", "z") for axis in kwargs["axis_labels"][1:-2])
        for _, kwargs, _ in compact
    ):
        return layers
    result = []
    overview_cache = _OverviewCache()
    for _, stacked_kwargs, _ in compact:
        metadata = stacked_kwargs["metadata"]
        scenes = [
            layer for layer in layers
            if layer[1]["metadata"]["channel"] == metadata["channel"]
            and layer[1]["metadata"]["multiscale_index"] == metadata["multiscale_index"]
        ]
        first = scenes[0][1]
        ndim = len(first["axis_labels"])
        # A rotated scene cannot be placed by an axis-aligned source rectangle.
        if any(
            not np.allclose(kwargs.get("rotate", np.eye(ndim)), np.eye(ndim))
            or not np.allclose(kwargs.get("shear", 0), 0)
            or (
                "affine" in kwargs
                and not np.allclose(kwargs["affine"].affine_matrix, np.eye(ndim + 1))
            )
            or not np.array_equal(kwargs["translate"][:-2], first["translate"][:-2])
            for _, kwargs, _ in scenes
        ):
            return layers
        scene_levels = [
            data if kwargs["multiscale"] else [data] for data, kwargs, _ in scenes
        ]
        canvases, origin = compose_scene_canvas(
            scene_levels,
            origins=np.asarray([kwargs["translate"] for _, kwargs, _ in scenes]),
            scale=np.asarray(first["scale"]),
            rgb=first["rgb"],
            overview_cache=overview_cache,
        )
        result.append((
            canvases if len(canvases) > 1 else canvases[0],
            {
                **first, "name": stacked_kwargs["name"],
                "multiscale": len(canvases) > 1,
                "translate": origin,
                "metadata": {
                    **stacked_kwargs["metadata"], "czi_scene_canvas": True,
                },
            },
            "image",
        ))
    return result


def _zarr_structure(group: Any) -> dict:
    """Snapshot groups, array layout and attributes without fetching pixels.

    Store plain metadata rather than live Zarr objects so selection changes in
    the GUI never reopen the file or trigger a pixel read. Include tile groups
    here even though normal image discovery deliberately skips them.
    """
    children = []
    for name, node in sorted(
        group.members(),
        key=lambda item: (
            not item[0].isdecimal(),
            int(item[0]) if item[0].isdecimal() else item[0],
        ),
    ):
        if hasattr(node, "shape"):
            children.append(
                {"name": name, "kind": "array", "metadata": node.metadata.to_dict()}
            )
        else:
            children.append({"name": name, **_zarr_structure(node)})
    return {"kind": "group", "attributes": dict(group.attrs), "children": children}


def _layers(
    root: Any, source: Path, czi_image_factors: list | None = None,
    *, stack_scenes: bool = True,
) -> list[LayerData]:
    """Translate OME axes, stored pyramids, transforms and channels to layers.

    First build ordinary per-channel image layers. Then assemble eligible CZI
    mosaics, optionally stack compatible scenes, and attach metadata and warnings.
    All array slicing, transposition and stacking in preparation remain lazy.
    ``czi_image_factors`` is raw refkv calibration data, not a request to create
    additional pyramid resolutions.
    GUI callers prepare individual scenes, then compact the complete source
    when every channel supports the same native scene axis.
    """
    from napari.utils.colormaps import Colormap

    layers = []
    is_czi = root.attrs.get("refkv_zarrified", {}).get("profile") == "czi"
    for group_name, group, ome in _image_groups(root):
        for series_index, multiscale in enumerate(ome["multiscales"]):
            axes = tuple(
                (axis if isinstance(axis, str) else axis["name"]).lower()
                for axis in multiscale["axes"]
            )
            if len(set(axes)) != len(axes) or not {"y", "x"}.issubset(axes):
                raise ValueError(f"Unsupported image axes in {group_name}: {axes}")
            datasets = multiscale["datasets"]
            arrays = [group[dataset["path"]] for dataset in datasets]
            if not arrays or any(array.ndim != len(axes) for array in arrays):
                raise ValueError(f"Invalid OME image dimensions in {group_name}")
            channel_axis = axes.index("c") if "c" in axes else None
            channel_count = (
                arrays[0].shape[channel_axis] if channel_axis is not None else 1
            )
            if channel_axis is not None and any(
                array.shape[channel_axis] != channel_count for array in arrays
            ):
                raise ValueError(f"Channel counts differ across levels in {group_name}")
            # Levels can use different codecs, so determine colour order for
            # each level while requiring the same packed component count.
            sample_orders = (
                [_czi_rgb_order(array, channel_axis) for array in arrays]
                if is_czi and channel_axis is not None
                else [()] * len(arrays)
            )
            samples = len(sample_orders[0]) or 1
            if any((len(order) or 1) != samples for order in sample_orders):
                raise ValueError(f"CZI pixel types differ across levels in {group_name}")
            is_rgb = samples > 1
            remaining_axes = tuple(axis for axis in axes if axis != "c")
            # Napari displays the final spatial axes. Scalar selection removes
            # C; packed colour keeps C as a trailing RGB(A) component axis that
            # is intentionally absent from axis_labels and dimension sliders.
            display_axes = tuple(
                axis for axis in remaining_axes if axis not in {"y", "x"}
            ) + ("y", "x")
            permutation = (
                tuple(axes.index(axis) for axis in (*display_axes, "c"))
                if is_rgb
                else tuple(remaining_axes.index(axis) for axis in display_axes)
            )
            scale = np.ones(len(axes))
            translate = np.zeros(len(axes))
            # Compose transforms in order. A later scale multiplies an earlier
            # translation too; merely adding all offsets would place it wrongly.
            transforms = datasets[0].get(
                "coordinateTransformations", []
            ) + multiscale.get("coordinateTransformations", [])
            for transform in transforms:
                if transform["type"] == "scale":
                    factor = np.asarray(transform["scale"])
                    scale *= factor
                    translate *= factor
                elif transform["type"] == "translation":
                    translate += np.asarray(transform["translation"])
                else:
                    raise ValueError(f"Unsupported OME transform: {transform['type']}")
            lazy_levels = [_lazy_array(array) for array in arrays]
            channels = ome.get("omero", {}).get("channels", [])
            for channel_index in range(channel_count // samples):
                # Slice the packed components together, or index away the one
                # scalar C axis. Both paths yield one layer per acquisition C.
                component_start = channel_index * samples
                selection: list[slice | int] = [slice(None)] * len(axes)
                if channel_axis is not None:
                    selection[channel_axis] = (
                        slice(component_start, component_start + samples)
                        if is_rgb else channel_index
                    )
                levels = [
                    level[tuple(selection)].transpose(permutation)
                    for level in lazy_levels
                ]
                if is_rgb:
                    levels = [
                        da.take(level, list(order), axis=-1)
                        for level, order in zip(levels, sample_orders)
                    ]
                # refkv's packed-component metadata repeats the acquisition
                # channel information. Use the first component as its label/window.
                channel = (
                    channels[component_start] if component_start < len(channels) else {}
                )
                colormap = "gray"
                color = channel.get("color", "")
                # A scalar channel colour is an optional display hint. Malformed
                # hex keeps the initial gray map; packed colour needs no colormap.
                if not is_rgb and isinstance(color, str) and len(color) == 6:
                    try:
                        rgb = [
                            int(color[start : start + 2], 16) / 255
                            for start in (0, 2, 4)
                        ]
                        colormap = Colormap([[0, 0, 0], rgb], name=f"refkv_{color}")
                    except ValueError:
                        pass
                series_name = multiscale.get("name") or group_name or source.stem
                if series_index:
                    series_name = f"{series_name}/{series_index}"
                label = channel.get("label", f"Channel {channel_index}")
                # Strip refkv's first-component suffix, not the channel's name.
                if is_rgb and label.endswith((" B", " R")):
                    label = label[:-2]
                kwargs = {
                    "name": f"{source.stem} | {series_name} | {label}",
                    "axis_labels": display_axes,
                    "scale": tuple(scale[axes.index(axis)] for axis in display_axes),
                    "translate": tuple(
                        translate[axes.index(axis)] for axis in display_axes
                    ),
                    "contrast_limits": _contrast_limits(arrays[0].dtype, channel),
                    "colormap": colormap,
                    "blending": "translucent" if is_rgb else "additive",
                    "multiscale": len(levels) > 1,
                    "rgb": is_rgb,
                    "metadata": {
                        "source": str(source),
                        "series": group_name,
                        "multiscale_index": series_index,
                        "channel": channel_index,
                        "channel_label": label,
                        "ome": ome,
                    },
                }
                if is_rgb:
                    kwargs.pop("colormap")
                    kwargs["metadata"]["color_components"] = (
                        "RGBA" if samples == 4 else "RGB"
                    )
                layers.append(
                    (levels if len(levels) > 1 else levels[0], kwargs, "image")
                )
    if not layers:
        raise ValueError("refkv found no supported OME image series in this file.")
    assembled_tiles = set()
    if is_czi:
        from ._czi_mosaic import assemble_czi_layers

        # Assembly needs individual scene coordinates. Stack only afterwards,
        # once each scene's final extent and available resolutions are known.
        assembled_tiles = assemble_czi_layers(root, layers, czi_image_factors or [])
        if stack_scenes:
            layers = _stack_czi_scenes(layers, source)
        else:
            load_id = uuid4().hex
            for _, kwargs, _ in layers:
                kwargs["metadata"]["czi_load_id"] = load_id
    structure = _zarr_structure(root)
    warning = None
    if is_czi:
        # Count from the snapshot, never by loading tiles. The assembler returns
        # only tiles whose acquisition channels were all incorporated, so partial
        # use still warns. Report once per source, then retain it on every layer.
        tiles = next(
            (node for node in structure["children"] if node["name"] == "tiles"),
            None,
        )
        tile_count = sum(
            node["name"] not in assembled_tiles for node in tiles.get("children", [])
        ) if tiles else 0
        if tile_count:
            from napari.utils.notifications import show_warning

            warning = (
                f"{source.name}: incomplete CZI view. refkv exposes "
                f"{tile_count} unassembled tile array(s) that this plugin does "
                "not fully display. Only supported assembled image levels are shown; "
                "full-resolution data or additional pyramid levels may be missing. "
                "Use napari-czitools to view the assembled CZI mosaic."
            )
            show_warning(warning)
    for _, kwargs, _ in layers:
        # Share the read-only source snapshot instead of copying a potentially
        # large hierarchy for each channel. It is not the runtime mosaic layout.
        kwargs["metadata"]["zarr_structure"] = structure
        if warning:
            kwargs["metadata"]["reader_warning"] = warning
    return layers


def read_images(
    path: str | list[str], *, stack_scenes: bool = True,
) -> list[LayerData]:
    """Open local images or VZIP archives as lazy virtual OME-Zarr layers.

    Args:
        path: One local source path or a list of source paths.
        stack_scenes: Stack compatible CZI scenes on S for scripted use.
            False keeps individual scenes for the dock's canvas/slider controls.

    Returns:
        napari layer-data tuples containing lazy arrays and display metadata.

    Raises:
        FileNotFoundError: A source is not an existing file.
        ValueError: Image axes, transforms or OME metadata are unsupported.

    Notes:
        Raw images are virtualized in memory. No archive or converted pixels
        are written. Sources must remain available and unchanged while layers
        are being used. VZIP source access is subject to refkv's policy.
        This function does not start a worker; the plugin dock supplies one.
    """
    import refkv.zarr
    import zarr
    from refkv.zarrify import zarrify

    paths = path if isinstance(path, list) else [path]
    layers = []
    for item in paths:
        source = Path(item).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Image file does not exist: {source}")
        image_factors = None
        if source.suffix.lower() == ".vzip":
            # Saved references have no live image-factor summary. Regular image
            # levels still work; separate CZI tiles cannot be newly assembled.
            store = refkv.zarr.open(str(source))
        else:
            reference = zarrify(str(source))
            store = refkv.zarr.store(reference)
            # This summary calibrates CZI regular levels against native tiles.
            # Keep it separate from OME metadata; it is not persisted in VZIP.
            image_factors = reference.info.get("images")
        root = zarr.open_group(store, mode="r")
        layers.extend(_layers(root, source, image_factors, stack_scenes=stack_scenes))
    return layers


def read_and_save_image(
    path: str, archive_path: str, *, overwrite: bool = False,
    stack_scenes: bool = True,
) -> list[LayerData]:
    """Prepare lazy layers and atomically save their reference archive, not pixels.

    Args:
        path: Local image source; existing VZIP archives are not accepted.
        archive_path: Destination ending in .vzip, separate from the source.
        overwrite: Allow atomic replacement of an existing destination.
        stack_scenes: Stack compatible CZI scenes on S, or retain individual
            scenes for the dock's canvas/slider controls.

    Returns:
        napari layer-data tuples retaining references to the original source.

    Raises:
        FileNotFoundError: The source or destination directory does not exist.
        FileExistsError: The destination exists and overwrite is disabled.
        ValueError: The source, destination or OME image metadata is unsupported.
        OSError: Staging or atomically publishing the archive fails.

    Notes:
        Save the original refkv reference layout, not the derived Dask mosaic.
        Build valid layer descriptions before publishing an archive. Pixel
        decode errors can still surface later when those lazy layers are viewed.
    """
    import refkv.zarr
    import zarr
    from refkv.zarrify import zarrify

    source = Path(path).expanduser().resolve()
    destination = Path(archive_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Image file does not exist: {source}")
    if source.suffix.lower() == ".vzip":
        raise ValueError("The selected input is already a VZIP archive.")
    if destination == source or destination.suffix.lower() != ".vzip":
        raise ValueError("Choose a separate .vzip archive destination.")
    if not destination.parent.is_dir():
        raise FileNotFoundError(
            f"Archive directory does not exist: {destination.parent}"
        )
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Archive already exists: {destination}")
    reference = zarrify(str(source))
    root = zarr.open_group(refkv.zarr.store(reference), mode="r")
    layers = _layers(
        root, source, reference.info.get("images"), stack_scenes=stack_scenes,
    )
    # Stage beside the destination so publication stays on one filesystem.
    # Never stream an incomplete archive over an existing user's file.
    with TemporaryDirectory(prefix=".refkv-", dir=destination.parent) as temporary:
        staged = Path(temporary) / destination.name
        reference.save(staged)
        if overwrite:
            os.replace(staged, destination)
        else:
            # A prior exists() check alone has a race. Creating a hard link
            # fails if a destination appeared meanwhile, rather than replacing it.
            os.link(staged, destination)
    for _, kwargs, _ in layers:
        kwargs["metadata"]["vzip"] = str(destination)
    return layers


def napari_get_reader(path: str | list[str]):
    """Offer the reader for supported image extensions without opening the file.

    Args:
        path: A local source path or list of paths supplied by napari.

    Returns:
        The reader callable when every suffix is supported, otherwise None.
        A matching suffix does not guarantee that the image can be decoded.
    """
    paths = path if isinstance(path, list) else [path]
    if paths and all(str(item).lower().endswith(IMAGE_SUFFIXES) for item in paths):
        # Reader discovery also runs without a GUI. Only attach inspectors when
        # Qt is already active; never create an application just to offer a reader.
        qt_widgets = sys.modules.get("qtpy.QtWidgets")
        if qt_widgets is not None and qt_widgets.QApplication.instance() is not None:
            from ._metadata import prepare_metadata_inspectors

            prepare_metadata_inspectors()
        return _read_gui_images
    return None
