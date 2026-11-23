"""Irregular CZI mosaics stay reference-backed and are composed chunk by chunk."""

import pickle
from pathlib import Path

import dask.array as da
import numpy as np
import pytest
import zarr

from napari_virtual_reader._czi_mosaic import _Mosaic, _OverviewCache, _Piece
from napari_virtual_reader._reader import _canvas_czi_layers, _layers


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


@pytest.mark.parametrize("cached", [False, True])
def test_tile_decode_failure_is_not_replaced_by_black_pixels(monkeypatch, cached):
    array = zarr.array(np.ones((4, 4), dtype="uint8"))
    mosaic = _Mosaic(
        [_Piece(array, 0, 0, 4, 4, 1, 0, (), False)],
        (0, 0, 4, 4),
        1,
        np.dtype("uint8"),
        0,
        overview_cache=_OverviewCache() if cached else None,
    ).lazy()

    def fail(*args, **kwargs):
        raise OSError("Source tile unavailable")

    monkeypatch.setattr(zarr.Array, "__getitem__", fail)
    with pytest.raises(OSError, match="Source tile unavailable"):
        mosaic.compute()


@pytest.mark.parametrize("rgb", [False, True])
def test_generated_overviews_cache_planes_but_not_native_pixels(monkeypatch, rgb):
    monkeypatch.setattr("napari_virtual_reader._czi_mosaic._CHUNK_EDGE", 8)
    layers = []
    for channel in range(2):
        for scene in range(2):
            shape = (2, 2, 4, 4) + ((3,) if rgb else ())
            data = np.empty(shape, dtype="uint8")
            for t, z in np.ndindex(2, 2):
                data[t, z] = 1 + channel * 40 + scene * 10 + t * 2 + z
            layers.append((
                da.from_array(data, chunks=(1, 1, 4, 4) + ((3,) if rgb else ())),
                {
                    "axis_labels": ("t", "z", "y", "x"),
                    "scale": (1, 1, 1, 1), "translate": (0, 0, 0, scene * 28),
                    "rgb": rgb, "multiscale": False,
                    "metadata": {
                        "source": "cache.czi", "czi_load_id": "cache",
                        "series": str(scene), "channel": channel,
                        "channel_label": f"C{channel}", "multiscale_index": 0,
                    },
                },
                "image",
            ))
    reads = []
    original = _Piece.read

    def record(self, ys, xs, planes=()):
        reads.append(planes)
        return original(self, ys, xs, planes)

    monkeypatch.setattr(_Piece, "read", record)
    canvas = _canvas_czi_layers(layers)
    assert not reads
    for channel, (levels, _, _) in enumerate(canvas):
        for t, z in np.ndindex(2, 2):
            for level in levels[1:]:
                reads.clear()
                first = level[t, z].compute()
                assert reads
                assert np.all(first[:, 0] == 1 + channel * 40 + t * 2 + z)
                assert np.all(first[:, -1] == 11 + channel * 40 + t * 2 + z)
                width = first.shape[1] // 8
                assert not first[:, width:-width].any()
                reads.clear()
                first[:] = 255
                again = level[t, z].compute()
                assert not reads
                assert not (again == 255).any()
    # All channel/level/plane entries can coexist without colliding.
    for levels, _, _ in canvas:
        for level in levels[1:]:
            reads.clear()
            level[0, 0].compute()
            assert not reads
        reads.clear()
        levels[0][0, 0, :4, :4].compute()
        assert reads
        reads.clear()
        levels[0][0, 0, :4, :4].compute()
        assert reads


def test_overview_cache_evicts_least_recent_chunks_and_skips_oversized_entries():
    cache = _OverviewCache(max_bytes=8)
    first = ("channel0-level1", ((0, 1, 1),))
    second = ("channel1-level1", ((0, 1, 1),))
    third = ("channel0-level2", ((0, 1, 1),))
    pixels = np.arange(4, dtype="uint8")
    cache.put(first, pixels)
    cache.put(second, pixels + 10)
    pixels[:] = 255
    np.testing.assert_array_equal(cache.get(first), np.arange(4))
    cache.put(third, np.arange(4, dtype="uint8") + 20)
    assert cache.get(second) is None
    np.testing.assert_array_equal(cache.get(first), np.arange(4))
    np.testing.assert_array_equal(cache.get(third), np.arange(4) + 20)
    cache.put(second, np.zeros(9, dtype="uint8"))
    assert cache.get(second) is None
    assert cache.get(first) is not None
    assert cache.get(third) is not None
    # Replacing an entry must not double-count its bytes or evict its neighbour.
    cache.put(first, np.arange(4, dtype="uint8") + 30)
    assert cache.get(third) is not None
    np.testing.assert_array_equal(cache.get(first), np.arange(4) + 30)


def test_cached_overview_graph_remains_serializable():
    cache = _OverviewCache(max_bytes=8)
    mosaic = _Mosaic(
        [_Piece(np.arange(16, dtype="uint8").reshape(4, 4), 0, 0, 4, 4, 1, 0, (), False)],
        (0, 0, 4, 4), 1, np.dtype("uint8"), 0,
        shape=(2, 2), overview_cache=cache,
    ).lazy()
    expected = mosaic.compute()
    restored = pickle.loads(pickle.dumps(mosaic))
    np.testing.assert_array_equal(restored.compute(), expected)


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
