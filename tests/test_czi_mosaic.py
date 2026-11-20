"""Irregular CZI mosaics stay reference-backed and are composed chunk by chunk."""

from pathlib import Path

import numpy as np
import pytest
import zarr

from napari_virtual_reader._czi_mosaic import _Mosaic, _Piece
from napari_virtual_reader._reader import _layers


def _source(rgb=True):
    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    root.attrs["refkv_zarrified"] = {"profile": "czi", "version": 0}
    group = root.create_group("0")
    group.attrs["refkv_zarrified"] = {"czi": {"dimensions": {"S": 0}}}
    channels = 3 if rgb else 2
    group.create_array(
        "0",
        data=np.full((channels, 3, 4), 99, dtype="uint8"),
        chunks=(3 if rgb else 1, 3, 4),
        dimension_names=("c", "y", "x"),
    )
    group.attrs["ome"] = {
        "multiscales": [
            {
                "axes": ["c", "y", "x"],
                "datasets": [
                    {
                        "path": "0",
                        "coordinateTransformations": [
                            {"type": "scale", "scale": [1, 0.4, 0.6]},
                            {"type": "translation", "translation": [0, -0.3, -0.75]},
                        ],
                    }
                ],
            }
        ],
        "omero": {
            "channels": [
                {"label": f"Brightfield {letter}" if rgb else f"Fluor {letter}"}
                for letter in ("BGR" if rgb else "AB")
            ]
        },
    }
    tiles = root.create_group("tiles")
    for name, x, y, value in (("2", -3, -2, 10), ("10", 0, 0, 20)):
        data = np.stack(
            [np.full((4, 4), value + c, dtype="uint8") for c in range(channels)]
        )
        tile = tiles.create_array(
            name,
            data=data,
            chunks=(3 if rgb else 1, 4, 4),
            dimension_names=("c", "y", "x"),
        )
        tile.attrs["refkv_zarrified"] = {
            "czi": {
                "dimensions": {"S": 0},
                "x": x,
                "y": y,
                "size": [4, 4],
                "stored_size": [4, 4],
                "planes": {"t": 0, "c": 5, "z": 0},
                "copy": 0,
            }
        }
    return root


@pytest.mark.parametrize("rgb", [True, False])
def test_assembly_restores_native_pixels_and_preserves_coarse_levels(monkeypatch, rgb):
    root = _source(rgb)
    notifications = []
    monkeypatch.setattr("napari.utils.notifications.show_warning", notifications.append)
    reads = []
    original = zarr.Array.__getitem__

    def record(array, key):
        reads.append(array.path)
        return original(array, key)

    monkeypatch.setattr(zarr.Array, "__getitem__", record)
    layers = _layers(root, Path("mosaic.czi"), [[[2, 1, 1]]])
    assert not reads
    assert not notifications
    assert len(layers) == (1 if rgb else 2)
    for channel, (levels, kwargs, _) in enumerate(layers):
        assert kwargs["rgb"] == rgb
        assert kwargs["multiscale"]
        assert kwargs["axis_labels"] == ("y", "x")
        np.testing.assert_allclose(kwargs["scale"], (0.2, 0.3))
        np.testing.assert_allclose(kwargs["translate"], (-0.4, -0.9))
        assembly = kwargs["metadata"]["czi_assembly"]
        assert assembly["full_resolution"]
        assert assembly["factors"] == [1, 2]
        assert assembly["tile_arrays"] == 2
        assert "reader_warning" not in kwargs["metadata"]
        expected = np.zeros((6, 7, 3) if rgb else (6, 7), dtype="uint8")
        expected[:4, :4] = [12, 11, 10] if rgb else 10 + channel
        expected[2:6, 3:7] = [22, 21, 20] if rgb else 20 + channel
        np.testing.assert_array_equal(levels[0].compute(), expected)
        assert levels[1].shape == ((3, 4, 3) if rgb else (3, 4))
        assert np.all(levels[1].compute() == 99)


def test_requested_chunk_does_not_read_distant_tiles(monkeypatch):
    a = zarr.array(np.arange(16, dtype="uint16").reshape(4, 4))
    b = zarr.array(np.full((4, 4), 100, dtype="uint16"))
    mosaic = _Mosaic(
        [
            _Piece(a, 0, 0, 4, 4, 1, 0, (), False),
            _Piece(b, 8192, 8192, 4, 4, 1, 0, (), False),
        ],
        (0, 0, 8196, 8196),
        1,
        np.dtype("uint16"),
        0,
    ).lazy()
    reads = []
    original = zarr.Array.__getitem__

    def record(array, key):
        reads.append((array, key))
        return original(array, key)

    monkeypatch.setattr(zarr.Array, "__getitem__", record)
    np.testing.assert_array_equal(mosaic[1:3, 1:3].compute(), [[5, 6], [9, 10]])
    assert len(reads) == 1
    assert reads[0][0] is a
    assert max(mosaic.chunksize) <= 1024
    reads.clear()
    assert not mosaic[2048:2052, 2048:2052].compute().any()
    assert not reads


def test_shifted_clipped_level_uses_common_extent():
    tile = zarr.array(np.arange(6, dtype="uint8").reshape(2, 3))
    mosaic = _Mosaic(
        [_Piece(tile, -1, 1, 6, 4, 2, 0, (), False)],
        (0, 0, 7, 5),
        2,
        np.dtype("uint8"),
        0,
    ).lazy()
    # Output pixel centres over the common 7x5 extent select these source cells.
    expected = np.array([[0, 0, 0, 0], [0, 1, 2, 0], [3, 4, 5, 0]], dtype="uint8")
    np.testing.assert_array_equal(mosaic.compute(), expected)


@pytest.mark.parametrize(
    "unsupported", ["missing-calibration", "other-scene", "time-stack"]
)
def test_unsupported_tiles_remain_visible_as_warning(monkeypatch, unsupported):
    root = _source()
    if unsupported == "other-scene":
        for _, array in root["tiles"].arrays():
            placement = array.attrs["refkv_zarrified"]
            placement["czi"]["dimensions"]["S"] = 1
            array.attrs["refkv_zarrified"] = placement
    elif unsupported == "time-stack":
        placement = root["tiles/10"].attrs["refkv_zarrified"]
        placement["czi"]["planes"]["t"] = 1
        root["tiles/10"].attrs["refkv_zarrified"] = placement
    notifications = []
    monkeypatch.setattr("napari.utils.notifications.show_warning", notifications.append)
    factors = None if unsupported == "missing-calibration" else [[[2, 1, 1]]]
    layers = _layers(root, Path("mosaic.czi"), factors)
    assert len(notifications) == 1
    assert "2 unassembled" in notifications[0]
    assert not layers[0][1]["multiscale"]
    assert "czi_assembly" not in layers[0][1]["metadata"]


def test_tile_decode_failure_is_not_replaced_by_black_pixels(monkeypatch):
    array = zarr.array(np.ones((4, 4), dtype="uint8"))
    mosaic = _Mosaic(
        [_Piece(array, 0, 0, 4, 4, 1, 0, (), False)],
        (0, 0, 4, 4),
        1,
        np.dtype("uint8"),
        0,
    ).lazy()

    def fail(*args, **kwargs):
        raise OSError("Source tile unavailable")

    monkeypatch.setattr(zarr.Array, "__getitem__", fail)
    with pytest.raises(OSError, match="Source tile unavailable"):
        mosaic.compute()


def test_scene_tiles_are_not_mixed_and_compatible_mosaics_stack():
    root = _source()
    group = root.create_group("1")
    group.attrs.update(dict(root["0"].attrs))
    group.attrs["refkv_zarrified"] = {"czi": {"dimensions": {"S": 1}}}
    group.create_array(
        "0",
        data=np.full((3, 3, 4), 88, dtype="uint8"),
        chunks=(3, 3, 4),
        dimension_names=("c", "y", "x"),
    )
    for original, name in (("2", "20"), ("10", "30")):
        template = root[f"tiles/{original}"]
        placement = template.attrs["refkv_zarrified"]
        placement["czi"]["dimensions"]["S"] = 1
        tile = root["tiles"].create_array(
            name,
            data=np.full((3, 4, 4), 55, dtype="uint8"),
            chunks=(3, 4, 4),
            dimension_names=("c", "y", "x"),
        )
        tile.attrs["refkv_zarrified"] = placement
    levels, kwargs, _ = _layers(root, Path("scenes.czi"), [[[2, 1, 1]]] * 2)[0]
    assert kwargs["axis_labels"] == ("S", "y", "x")
    assert levels[0].shape == (2, 6, 7, 3)
    np.testing.assert_array_equal(levels[0][0, 0, 0].compute(), [12, 11, 10])
    np.testing.assert_array_equal(levels[0][1, 0, 0].compute(), [55, 55, 55])


def test_napari_accepts_assembled_pyramid(make_napari_viewer):
    viewer = make_napari_viewer()
    levels, kwargs, _ = _layers(_source(), Path("mosaic.czi"), [[[2, 1, 1]]])[0]
    layer = viewer.add_image(levels, **kwargs)
    assert layer.rgb and layer.multiscale
    assert layer.ndim == 2
    assert layer.data[0].shape == (6, 7, 3)
    np.testing.assert_allclose(layer.scale, (0.2, 0.3))
