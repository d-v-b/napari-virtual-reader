"""The minimal opener keeps preparation off-thread and display on the Qt thread."""

import threading
from pathlib import Path
from unittest.mock import Mock

import dask.array as da
import numpy as np
import pytest
import zarr
from qtpy.QtCore import Qt

from napari_virtual_reader._reader import _layers
from napari_virtual_reader._widget import VirtualReaderWidget


def test_worker_opens_on_main_thread(qtbot, monkeypatch):
    """Worker preparation must not mutate the viewer."""
    main_thread = threading.get_ident()
    worker_threads = []
    display_threads = []
    viewer = Mock()
    viewer.window._qt_window.findChildren.return_value = []
    viewer.add_image.side_effect = lambda *args, **kwargs: display_threads.append(threading.get_ident())

    def prepare(path, *, stack_scenes):
        assert stack_scenes is False
        worker_threads.append(threading.get_ident())
        return [(da.zeros((8, 10)), {"contrast_limits": (0, 1)}, "image")]

    monkeypatch.setattr("napari_virtual_reader._widget.read_images", prepare)
    widget = VirtualReaderWidget(viewer)
    qtbot.addWidget(widget)
    assert widget.minimumWidth() == 260
    assert not widget.open_button.isEnabled()
    widget.filename.setText("image.czi")
    assert widget.filename.toolTip() == "image.czi"
    qtbot.mouseClick(widget.open_button, Qt.LeftButton)
    assert not widget.open_button.isEnabled()
    qtbot.waitUntil(lambda: widget._worker is None, timeout=10000)
    assert worker_threads[0] != main_thread
    assert display_threads == [main_thread]
    assert widget.open_button.isEnabled()
    assert widget.progress.isHidden()


def test_failure_restores_controls(qtbot, monkeypatch):
    """Errors remain visible, do not add layers, and permit retry."""
    viewer = Mock()
    viewer.window._qt_window.findChildren.return_value = []
    notify = Mock()
    monkeypatch.setattr("napari.utils.notifications.show_error", notify)

    def fail(path, *, stack_scenes):
        raise ValueError("Unsupported image")

    monkeypatch.setattr("napari_virtual_reader._widget.read_images", fail)
    widget = VirtualReaderWidget(viewer)
    qtbot.addWidget(widget)
    widget.filename.setText("image.bin")
    widget._open()
    qtbot.waitUntil(lambda: widget._worker is None, timeout=10000)
    assert widget.status.text() == "Unsupported image"
    assert widget.open_button.isEnabled()
    viewer.add_image.assert_not_called()
    notify.assert_called_once_with("Unsupported image")


def test_closed_dock_discards_results(qtbot):
    """Closing during preparation must not create unexpected viewer layers."""
    viewer = Mock()
    viewer.window._qt_window.findChildren.return_value = []
    widget = VirtualReaderWidget(viewer)
    qtbot.addWidget(widget)
    widget.close()
    widget._loaded([(da.zeros((8, 10)), {}, "image")])
    viewer.add_image.assert_not_called()


@pytest.mark.parametrize("already_open", [False, True])
def test_combined_dock_follows_active_layer(qtbot, make_napari_viewer, already_open):
    """Keep opening controls above the inspector for existing and later images."""
    from napari_virtual_reader._metadata import MetadataPanel

    viewer = make_napari_viewer()
    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    root.create_array("0", data=np.zeros((8, 10), dtype="uint16"))
    root.attrs["ome"] = {"multiscales": [{"axes": ["y", "x"], "datasets": [{"path": "0"}]}]}
    data, kwargs, _ = _layers(root, Path("example.tif"))[0]
    first = viewer.add_image(data, **kwargs) if already_open else None
    dock, widget = viewer.window.add_plugin_dock_widget("napari-virtual-reader")
    if first is None:
        first = viewer.add_image(data, **kwargs)
    assert viewer.window._qt_window.dockWidgetArea(dock) == Qt.RightDockWidgetArea
    panel = widget.metadata_panel
    assert widget.isAncestorOf(panel)
    assert viewer.window._qt_window.findChildren(MetadataPanel) == [panel]
    assert len(viewer.window._qt_window.findChildren(VirtualReaderWidget)) == 1
    viewer.window._qt_window.show()
    qtbot.waitExposed(widget)
    assert widget.open_button.mapTo(widget, widget.open_button.rect().bottomLeft()).y() < panel.y()
    assert panel.geometry().bottom() == widget.contentsRect().bottom() - widget.layout().contentsMargins().bottom()
    assert viewer.window.add_plugin_dock_widget("napari-virtual-reader") == (dock, widget)
    assert panel.source.text() == "example.tif"
    assert panel.structure.topLevelItem(0).text(0) == "/"
    arrays = [
        item
        for item in panel.structure.findItems("0", Qt.MatchExactly | Qt.MatchRecursive, 0)
        if item.parent() is panel.structure.topLevelItem(0)
    ]
    assert len(arrays) == 1
    assert "8 x 10" in arrays[0].text(1)
    properties = panel.structure.findItems("data_type", Qt.MatchExactly | Qt.MatchRecursive, 0)
    panel.structure.setCurrentItem(properties[0])
    assert panel.detail.toPlainText() == '"uint16"'
    panel.search.setText("data_type")
    assert not properties[0].isHidden()
    assert not arrays[0].isHidden()
    hidden = panel.structure.findItems("ome", Qt.MatchExactly | Qt.MatchRecursive, 0)[0]
    assert hidden.isHidden()
    panel.search.clear()
    assert not hidden.isHidden()
    viewer.add_image(np.zeros((8, 10)), name="Unrelated image")
    assert panel.structure.topLevelItemCount() == 0
    assert panel.source.text() == ""
    viewer.layers.selection.active = first
    assert panel.source.text() == "example.tif"
    assert panel.metadata.findItems("ome", Qt.MatchExactly | Qt.MatchRecursive, 0)


@pytest.mark.parametrize("opener_first", [False, True])
def test_reader_and_plugin_share_combined_dock(tmp_path, qtbot, make_napari_viewer, opener_first):
    """File/Open and the plugin menu share one dock in either opening order."""
    import tifffile

    from napari_virtual_reader._metadata import MetadataPanel

    source = tmp_path / "image.tif"
    tifffile.imwrite(source, np.zeros((8, 10), dtype="uint16"), metadata={"axes": "YX"})
    viewer = make_napari_viewer()
    assert not viewer.window._qt_window.findChildren(VirtualReaderWidget)
    if opener_first:
        viewer.window.add_plugin_dock_widget("napari-virtual-reader")
    viewer.open(str(source), plugin="napari-virtual-reader")
    qtbot.waitUntil(lambda: bool(viewer.window._qt_window.findChildren(MetadataPanel)))
    panels = viewer.window._qt_window.findChildren(MetadataPanel)
    assert len(panels) == 1
    panel = panels[0]
    dock, widget = viewer.window.add_plugin_dock_widget("napari-virtual-reader")
    assert widget.metadata_panel is panel
    assert widget.isAncestorOf(panel)
    assert viewer.window._qt_window.findChildren(VirtualReaderWidget) == [widget]
    assert viewer.window._qt_window.dockWidgetArea(dock) == Qt.RightDockWidgetArea
    assert panel.source.text() == "image.tif"
    assert not dock.isHidden()
    dock.hide()
    viewer.open(str(source), plugin="napari-virtual-reader")
    assert not dock.isHidden()
    assert len(viewer.window._qt_window.findChildren(MetadataPanel)) == 1
    assert viewer.window.add_plugin_dock_widget("napari-virtual-reader") == (dock, widget)


def test_widget_saves_archive_in_background(tmp_path, qtbot, make_napari_viewer, monkeypatch):
    """The opt-in worker writes an archive and keeps metadata and viewer loading."""
    import tifffile

    from napari_virtual_reader._reader import read_images

    source = tmp_path / "image.tif"
    output = tmp_path / "chosen.vzip"
    expected = np.arange(80, dtype="uint16").reshape(8, 10)
    tifffile.imwrite(source, expected, metadata={"axes": "YX"})
    viewer = make_napari_viewer()
    widget = VirtualReaderWidget(viewer)
    qtbot.addWidget(widget)
    assert not widget.save_archive.isChecked()
    widget.filename.setText(str(source))
    widget.save_archive.setChecked(True)
    monkeypatch.setattr("napari_virtual_reader._widget.QFileDialog.getSaveFileName", lambda *args, **kwargs: (str(output), ""))
    widget._open()
    assert not widget.save_archive.isEnabled()
    qtbot.waitUntil(lambda: widget._worker is None, timeout=10000)
    assert output.is_file()
    assert len(viewer.layers) == 1
    assert "saved chosen.vzip" in widget.status.text()
    assert widget.save_archive.isEnabled()
    np.testing.assert_array_equal(read_images(str(output))[0][0].compute(), expected)
    widget.filename.setText(str(output))
    assert not widget.save_archive.isEnabled()


@pytest.mark.parametrize("choice", ["cancel", "refuse", "replace"])
def test_save_dialog_cancellation_and_replacement(tmp_path, qtbot, monkeypatch, choice):
    """Do not start a worker without a destination and explicit replacement consent."""
    from qtpy.QtWidgets import QMessageBox

    viewer = Mock()
    viewer.window._qt_window.findChildren.return_value = []
    widget = VirtualReaderWidget(viewer)
    qtbot.addWidget(widget)
    widget.filename.setText(str(tmp_path / "image.tif"))
    widget.save_archive.setChecked(True)
    output = tmp_path / "existing.vzip"
    output.write_bytes(b"existing archive")
    destination = "" if choice == "cancel" else str(output)
    monkeypatch.setattr("napari_virtual_reader._widget.QFileDialog.getSaveFileName", lambda *args, **kwargs: (destination, ""))
    question = Mock(return_value=QMessageBox.Yes if choice == "replace" else QMessageBox.No)
    monkeypatch.setattr("napari_virtual_reader._widget.QMessageBox.question", question)
    create_worker = Mock()
    monkeypatch.setattr("napari_virtual_reader._widget.create_worker", create_worker)
    widget._open()
    if choice == "replace":
        assert create_worker.call_args.args[1:] == (widget.filename.text(), str(output))
        assert create_worker.call_args.kwargs["overwrite"] is True
        assert create_worker.call_args.kwargs["stack_scenes"] is False
        create_worker.return_value.start.assert_called_once()
    else:
        create_worker.assert_not_called()
        assert widget.open_button.isEnabled()
    assert output.read_bytes() == b"existing archive"


@pytest.mark.parametrize("difference", ["shape", "scale", "dtype", "levels", "visibility"])
def test_scene_slider_and_canvas_keep_independent_scene_data(
    qtbot, make_napari_viewer, difference,
):
    """Both modes work without padding/resampling or flattening scene pyramids."""
    viewer = make_napari_viewer()
    unrelated = viewer.add_image(np.zeros((3, 4)), name="Unrelated")
    _, widget = viewer.window.add_plugin_dock_widget("napari-virtual-reader")
    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    root.attrs["refkv_zarrified"] = {"profile": "czi"}
    for scene in (2, 10):
        group = root.create_group(str(scene))
        shape = (2, 12, 16) if scene == 10 and difference == "shape" else (2, 8, 10)
        dtype = "uint8" if scene == 10 and difference == "dtype" else "uint16"
        scale = 0.4 if scene == 10 and difference == "scale" else 0.2
        data = np.full(shape, scene, dtype=dtype)
        group.create_array("0", data=data, chunks=(1, 4, 5))
        datasets = [{
            "path": "0",
            "coordinateTransformations": [
                {"type": "scale", "scale": [1, scale, scale]},
                {"type": "translation", "translation": [0, scene * 10, scene * 20]},
            ],
        }]
        if not (scene == 10 and difference == "levels"):
            group.create_array("1", data=data[:, ::2, ::2], chunks=(1, 2, 3))
            datasets.append({"path": "1"})
        group.attrs["ome"] = {
            "multiscales": [{"axes": ["c", "y", "x"], "datasets": datasets}],
        }
    layers = [
        viewer.add_image(data, **kwargs)
        for data, kwargs, _ in _layers(root, Path("scenes.czi"), stack_scenes=False)
    ]
    original = [(layer.data, tuple(layer.scale), tuple(layer.translate)) for layer in layers]
    assert widget.scene_mode.isEnabled()
    assert all(layer.visible for layer in layers)
    assert not widget.scene_slider.isEnabled()
    # Preserve a manually hidden channel when leaving and returning to canvas.
    layers[1].visible = False
    widget.scene_mode.setCurrentText("Scene slider")
    assert widget.scene_slider.isEnabled()
    assert [layer.visible for layer in layers] == [True, False, False, False]
    np.testing.assert_allclose(viewer.scene.camera.center[-2:], (20.7, 40.9))
    widget.scene_slider.setValue(1)
    assert widget.scene_number.value() == 2
    assert "series 10" in widget.scene_label.text()
    assert [layer.visible for layer in layers] == [False, False, True, True]
    assert viewer.layers.selection.active is layers[2]
    assert widget.metadata_panel.source.text() == "scenes.czi"
    assert unrelated.visible
    widget.scene_number.stepDown()
    assert widget.scene_slider.value() == 0
    assert [layer.visible for layer in layers] == [True, False, False, False]
    widget.scene_number.stepUp()
    assert widget.scene_slider.value() == 1
    layers[3].visible = False
    widget.scene_slider.setValue(0)
    widget.scene_slider.setValue(1)
    assert [layer.visible for layer in layers] == [False, False, True, False]
    for layer, (data, scale, translate) in zip(layers, original):
        assert layer.data is data
        assert tuple(layer.scale) == scale
        assert tuple(layer.translate) == translate
    widget.scene_mode.setCurrentText("One canvas")
    assert [layer.visible for layer in layers] == [True, False, True, False]
    assert unrelated.visible
    # Removing the selected scene clamps the slider and restores its remaining scene.
    widget.scene_mode.setCurrentText("Scene slider")
    if difference == "visibility":
        # Visibility now matches across scenes, so native stacking is possible.
        assert len(viewer.layers) == 3  # Unrelated image and two channels.
        assert widget.scene_slider.isHidden()
    else:
        viewer.layers.remove(layers[2])
        viewer.layers.remove(layers[3])
        assert not widget.scene_mode.isEnabled()
        assert layers[0].visible


def test_scene_controls_isolate_repeated_source_loads(qtbot, make_napari_viewer):
    viewer = make_napari_viewer()
    _, widget = viewer.window.add_plugin_dock_widget("napari-virtual-reader")
    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    root.attrs["refkv_zarrified"] = {"profile": "czi"}
    for scene in (0, 1):
        group = root.create_group(str(scene))
        group.create_array("0", data=np.full((8 + scene, 10), scene, dtype="uint16"))
        group.attrs["ome"] = {
            "multiscales": [{"axes": ["y", "x"], "datasets": [{"path": "0"}]}],
        }
    loads = [
        [viewer.add_image(data, **kwargs) for data, kwargs, _ in
         _layers(root, Path("same.czi"), stack_scenes=False)]
        for _ in range(2)
    ]
    widget.scene_mode.setCurrentText("Scene slider")
    widget.scene_slider.setValue(1)
    assert [layer.visible for layer in loads[0]] == [True, True]
    assert [layer.visible for layer in loads[1]] == [False, True]
    viewer.layers.selection.active = loads[0][0]
    assert widget.scene_mode.currentText() == "One canvas"
    viewer.layers.selection.active = loads[1][1]
    assert widget.scene_mode.currentText() == "Scene slider"
    assert widget.scene_slider.value() == 1


@pytest.mark.parametrize("rgb", [False, True])
@pytest.mark.parametrize("multiscale", [False, True])
def test_matching_scenes_use_native_axis_and_canvas_roundtrip(
    qtbot, make_napari_viewer, monkeypatch, rgb, multiscale,
):
    """Compact before insertion and keep arrows, native S and canvas in sync."""
    from napari_virtual_reader._reader import _compact_czi_layers, _read_gui_images

    viewer = make_napari_viewer()
    _, widget = viewer.window.add_plugin_dock_widget("napari-virtual-reader")
    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    root.attrs["refkv_zarrified"] = {"profile": "czi"}
    for scene in (2, 10):
        group = root.create_group(str(scene))
        data = np.full((2, 8, 10), scene, dtype="uint16")
        group.create_array("0", data=data, chunks=(1, 4, 5))
        datasets = [{
            "path": "0",
            "coordinateTransformations": [
                {"type": "scale", "scale": [1, 0.2, 0.2]},
                {"type": "translation", "translation": [0, scene * 10, 40 + scene * 0.2]},
            ],
        }]
        if multiscale:
            group.create_array("1", data=data[:, ::2, ::2], chunks=(1, 2, 3))
            datasets.append({"path": "1"})
        group.attrs["ome"] = {
            "multiscales": [{"axes": ["c", "y", "x"], "datasets": datasets}],
        }
    prepared = _layers(root, Path("scenes.czi"), stack_scenes=False)
    if rgb:
        # Packed colour is already decoded and ordered by the reader.
        prepared = [
            (
                [da.stack([level] * 3, axis=-1) for level in data]
                if multiscale else da.stack([data] * 3, axis=-1),
                {**kwargs, "rgb": True}, kind,
            )
            for data, kwargs, kind in prepared
        ]
    monkeypatch.setattr(
        "napari_virtual_reader._reader.read_images",
        lambda path, **kwargs: prepared,
    )
    compact = _read_gui_images("scenes.czi")
    assert len(compact) == 2
    for _, kwargs, _ in compact:
        assert kwargs["metadata"]["zarr_structure"] is prepared[0][1]["metadata"]["zarr_structure"]
        assert all("zarr_structure" not in scene for scene in kwargs["metadata"]["scenes"])
    incomplete = _compact_czi_layers(prepared[:-1])
    assert len(incomplete) == len(prepared) - 1
    assert all("scenes" not in kwargs["metadata"] for _, kwargs, _ in incomplete)
    # The opener and File/Open must both compact before adding viewer layers.
    inserted = []
    viewer.layers.events.inserted.connect(lambda event: inserted.append(event.value))
    widget._loaded(prepared)
    assert len(viewer.layers) == len(inserted) == 2
    assert widget.scene_mode.currentText() == "Scene slider"
    assert widget.scene_slider.isHidden()
    assert widget.scene_number.isEnabled()
    for layer in viewer.layers:
        assert layer.axis_labels == ("S", "y", "x")
        finest = layer.data[0] if multiscale else layer.data
        assert finest.shape[:3] == (2, 8, 10)
    widget.scene_number.stepUp()
    assert viewer.dims.current_step[0] == 1
    viewer.dims.set_current_step(0, 0)
    assert widget.scene_number.value() == 1
    viewer.dims.set_current_step(0, 1)
    assert widget.scene_number.value() == 2
    for channel, layer in enumerate(viewer.layers):
        layer.gamma = 1.4
        layer.opacity = 0.6
        layer.contrast_limits = (0, 20)
        layer.visible = channel == 0
    set_metadata = Mock(wraps=widget.metadata_panel.set_layer)
    monkeypatch.setattr(widget.metadata_panel, "set_layer", set_metadata)
    widget.scene_mode.setCurrentText("One canvas")
    set_metadata.assert_called_once()
    assert len(viewer.layers) == 2
    assert viewer.dims.ndim == 2
    for layer in viewer.layers:
        np.testing.assert_allclose(layer.translate, (20, 40.4))
        assert layer.gamma == 1.4
        assert layer.opacity == 0.6
        assert layer.visible == (layer.metadata["channel"] == 0)
        levels = layer.data if multiscale else [layer.data]
        for index, level in enumerate(levels):
            factor = 2 ** index
            pixels = level.compute()
            np.testing.assert_array_equal(pixels[:8 // factor, :10 // factor], 2)
            np.testing.assert_array_equal(
                pixels[400 // factor:408 // factor, 8 // factor:18 // factor], 10,
            )
            assert not pixels[100 // factor, 0].any()
    widget.scene_mode.setCurrentText("Scene slider")
    assert len(viewer.layers) == 2
    assert viewer.dims.ndim == 3
    assert viewer.dims.current_step[0] == 1
    for layer in viewer.layers:
        assert layer.gamma == 1.4
        assert layer.opacity == 0.6
        levels = layer.data if multiscale else [layer.data]
        for level in levels:
            np.testing.assert_array_equal(level[0].compute(), 2)
            np.testing.assert_array_equal(level[1].compute(), 10)
    widget.scene_number.stepDown()
    assert viewer.dims.current_step[0] == 0
    qtbot.keyClick(widget.scene_number, Qt.Key.Key_Up)
    assert viewer.dims.current_step[0] == 1
    qtbot.keyClick(widget.scene_number, Qt.Key.Key_Down)
    assert viewer.dims.current_step[0] == 0
    # More than one round trip also exercises transforms serialized by napari.
    widget.scene_mode.setCurrentText("One canvas")
    assert len(viewer.layers) == 2
    widget.scene_mode.setCurrentText("Scene slider")
    assert len(viewer.layers) == 2


def test_sparse_scene_canvas_has_lazy_overviews_and_bounded_reads():
    from dask import delayed

    from napari_virtual_reader._reader import _canvas_czi_layers

    reads = []

    @delayed
    def pixels(value):
        reads.append(value)
        return np.full((8, 10), value, dtype="uint16")

    layers = []
    for scene, x in enumerate((0, 5, 100000)):
        layers.append((
            da.from_delayed(pixels(scene + 1), shape=(8, 10), dtype="uint16"),
            {
                "axis_labels": ("y", "x"), "scale": (1, 1),
                "translate": (0, x), "rgb": False, "multiscale": False,
                "metadata": {
                    "source": "sparse.czi", "czi_load_id": "sparse",
                    "series": str(scene), "channel": 0, "channel_label": "C0",
                    "multiscale_index": 0,
                },
            },
            "image",
        ))
    [(levels, kwargs, _)] = _canvas_czi_layers(layers)
    assert not reads
    assert kwargs["multiscale"]
    assert levels[0].shape == (8, 100010)
    assert max(levels[-1].shape) <= 1024
    assert levels[0].chunksize == (8, 1024)
    np.testing.assert_array_equal(levels[0][:, :5].compute(), 1)
    assert set(reads) == {1, 2}  # Only scenes intersecting this output chunk.
    reads.clear()
    np.testing.assert_array_equal(levels[0][:, 5:10].compute(), 2)
    assert set(reads) == {1, 2}  # Later numeric scene wins overlaps.
    reads.clear()
    assert not levels[0][:, 2000:2010].compute().any()
    assert not reads
    assert levels[-1].compute().shape == levels[-1].shape


@pytest.mark.parametrize("geometry", ["volume", "rotation"])
def test_scene_canvas_retains_layers_for_unsupported_geometry(geometry):
    from napari_virtual_reader._reader import _canvas_czi_layers

    volume = geometry == "volume"
    layers = [
        (
            da.zeros((2, 8, 10) if volume else (8, 10), dtype="uint16"),
            {
                "axis_labels": ("z", "y", "x") if volume else ("y", "x"),
                "scale": (1, 1, 1) if volume else (1, 1),
                "translate": (0, 0, index * 10) if volume else (0, index * 10),
                **({} if volume else {"rotate": np.array([[0, -1], [1, 0]])}),
                "rgb": False, "multiscale": False,
                "metadata": {
                    "source": "scenes.czi", "czi_load_id": "geometry",
                    "series": str(index), "channel": 0, "channel_label": "C0",
                    "multiscale_index": 0,
                },
            },
            "image",
        )
        for index in range(2)
    ]
    assert _canvas_czi_layers(layers) is layers
