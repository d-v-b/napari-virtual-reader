"""Combined asynchronous file opener and read-only metadata inspector.

The Qt thread owns dialogs, controls and layer insertion. A preparation worker
opens reference stores, builds lazy layer tuples and optionally saves a VZIP.
It does not load a whole image; napari/Dask request pixels when displaying it.
The embedded MetadataPanel follows selection independently of the open action.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import napari
import numpy as np
from napari.layers import Image
from napari.qt.threading import create_worker
from napari.utils.transforms import CompositeAffine
from qtpy.QtCore import QSignalBlocker, Qt, Slot
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ._metadata import MetadataPanel
from ._reader import (
    IMAGE_SUFFIXES,
    LayerData,
    _canvas_czi_layers,
    _compact_czi_layers,
    read_and_save_image,
    read_images,
)

logger = logging.getLogger(__name__)


@dataclass
class _SceneView:
    """Source-local controls and the state needed to reverse display changes."""

    slider: bool = False
    index: int = 0
    # Hidden scenes must not overwrite the user's own channel visibility.
    visibility: dict[Image, bool] = field(default_factory=dict)
    # Restore original arrays from here, never by slicing the composed canvas.
    canvas_sources: list[LayerData] = field(default_factory=list)


def _is_scene_stack(layer: Image) -> bool:
    return "scenes" in layer.metadata and not layer.metadata.get("czi_scene_canvas")


def _scene_ids(layers: list[Image]) -> list[str]:
    return sorted(
        {
            scene["series"] for layer in layers
            for scene in layer.metadata.get("scenes", [layer.metadata])
        },
        key=lambda name: (not name.isdecimal(), int(name) if name.isdecimal() else name),
    )


class VirtualReaderWidget(QWidget):
    """Open images through refkv, preparing layers away from the Qt main thread.

    One active worker makes the dock busy until its finished signal restores
    the controls. Closing the widget discards late results; it does not cancel
    an in-flight preparation or archive write.
    """

    def __init__(self, viewer: napari.Viewer) -> None:
        super().__init__()
        self.viewer = viewer
        self._worker = None
        self._closed = False
        self._archive_path = None
        self._scene_views: dict[str, _SceneView] = {}
        self._updating_scenes = False
        self.setMinimumWidth(260)
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.filename = QLineEdit()
        self.filename.setPlaceholderText("Image file or VZIP archive")
        self.filename.setAcceptDrops(False)
        self.filename.textChanged.connect(self._update_open)
        self.filename.returnPressed.connect(self._open)
        self.browse = QToolButton()
        self.browse.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        self.browse.setToolTip("Choose image file or VZIP archive")
        self.browse.setAccessibleName("Choose image file")
        self.browse.clicked.connect(self._choose_file)
        row.addWidget(self.filename)
        row.addWidget(self.browse)
        layout.addLayout(row)
        self.save_archive = QCheckBox("Save VZIP archive")
        self.save_archive.setToolTip("Save references without copying image pixels")
        layout.addWidget(self.save_archive)
        self.open_button = QPushButton("Open")
        self.open_button.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.open_button.clicked.connect(self._open)
        layout.addWidget(self.open_button)
        self.progress = QProgressBar()
        # Metadata work has no reliable byte-based completion percentage.
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        row.addWidget(QLabel("CZI scenes"))
        self.scene_mode = QComboBox()
        self.scene_mode.addItems(["One canvas", "Scene slider"])
        self.scene_mode.setToolTip(
            "Display all scenes at their recorded positions, or one scene at a time. "
            "Works with matching and different scene sizes."
        )
        self.scene_mode.currentIndexChanged.connect(self._scene_mode_changed)
        row.addWidget(self.scene_mode)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.scene_slider = QSlider(Qt.Orientation.Horizontal)
        self.scene_slider.setAccessibleName("CZI scene")
        self.scene_slider.valueChanged.connect(self._scene_changed)
        self.scene_number = QSpinBox()
        self.scene_number.setRange(1, 1)
        self.scene_number.setPrefix("Scene ")
        self.scene_number.setAccessibleName("CZI scene number")
        self.scene_number.valueChanged.connect(
            lambda number: self._scene_changed(number - 1)
        )
        self.scene_label = QLabel("No multiple CZI scenes")
        row.addWidget(self.scene_slider)
        row.addWidget(self.scene_number)
        row.addWidget(self.scene_label)
        layout.addLayout(row)
        self.metadata_panel = MetadataPanel(viewer)
        # Keep loading controls compact at the top; the inspector gets extra height.
        layout.addWidget(self.metadata_panel, stretch=1)
        viewer.layers.selection.events.active.connect(self._sync_scene_controls)
        viewer.layers.events.inserted.connect(self._sync_scene_controls)
        viewer.layers.events.removed.connect(self._scene_removed)
        viewer.dims.events.current_step.connect(self._native_scene_changed)
        self._sync_scene_controls()
        self._update_open()

    def _scene_layers(self) -> list[Image]:
        active = self.viewer.layers.selection.active
        if not isinstance(active, Image):
            return []
        load_id = active.metadata.get("czi_load_id")
        if not load_id:
            return []
        return self._scene_group_layers(load_id)

    def _scene_group_layers(self, load_id: str) -> list[Image]:
        return [
            layer for layer in self.viewer.layers
            if isinstance(layer, Image)
            and layer.metadata.get("czi_load_id") == load_id
        ]

    def _scene_removed(self, event) -> None:
        if self._updating_scenes:
            return
        load_id = event.value.metadata.get("czi_load_id")
        if load_id in self._scene_views:
            view = self._scene_views[load_id]
            if event.value in view.visibility:
                event.value.events.visible.disconnect(self._scene_visibility_changed)
            view.visibility.pop(event.value, None)
            layers = self._scene_group_layers(load_id)
            if layers:
                scenes = _scene_ids(layers)
                view.index = min(view.index, len(scenes) - 1)
                self._apply_scene_view(layers, scenes, view)
            else:
                del self._scene_views[load_id]
        self._sync_scene_controls()

    def _sync_scene_controls(self, event=None) -> None:
        if self._updating_scenes:
            return
        layers = self._scene_layers()
        scenes = _scene_ids(layers)
        self.scene_mode.setEnabled(len(scenes) > 1)
        self.scene_slider.setEnabled(False)
        self.scene_number.setEnabled(False)
        if not scenes:
            self.scene_label.setText("No multiple CZI scenes")
            return
        load_id = layers[0].metadata["czi_load_id"]
        native = _is_scene_stack(layers[0])
        view = self._scene_views.setdefault(
            load_id, _SceneView(slider=native or self.scene_mode.currentIndex() == 1),
        )
        if native:
            scene_axis = self.viewer.dims.ndim - layers[0].ndim
            view.index = self.viewer.dims.current_step[scene_axis]
        view.index = min(view.index, len(scenes) - 1)
        # Native S, the dock slider and the 1-based number field update each
        # other. Programmatic changes must not start another scene conversion.
        with (
            QSignalBlocker(self.scene_mode),
            QSignalBlocker(self.scene_slider),
            QSignalBlocker(self.scene_number),
        ):
            self.scene_mode.setCurrentIndex(int(view.slider))
            self.scene_slider.setRange(0, len(scenes) - 1)
            self.scene_slider.setValue(view.index)
            self.scene_number.setRange(1, len(scenes))
            self.scene_number.setValue(view.index + 1)
        self.scene_slider.setVisible(not native)
        self.scene_slider.setEnabled(view.slider and len(scenes) > 1 and not native)
        self.scene_number.setEnabled(view.slider and len(scenes) > 1)
        self.scene_label.setText(
            f"/ {len(scenes)} (series {scenes[view.index]})"
        )
        self._apply_scene_view(layers, scenes, view)

    def _apply_scene_view(
        self, layers: list[Image], scenes: list[str], view: _SceneView,
        *, fit: bool = False,
    ) -> None:
        if "scenes" in layers[0].metadata:
            if fit:
                self.viewer.reset_view(layers=layers)
            return
        selected = [
            layer for layer in layers
            if not view.slider or layer.metadata["series"] == scenes[view.index]
        ]
        self._updating_scenes = True
        try:
            for layer in layers:
                if view.slider:
                    if layer not in view.visibility:
                        view.visibility[layer] = layer.visible
                        layer.events.visible.connect(self._scene_visibility_changed)
                    layer.visible = view.visibility[layer] and layer in selected
                elif layer in view.visibility:
                    layer.visible = view.visibility.pop(layer)
                    layer.events.visible.disconnect(self._scene_visibility_changed)
            if fit:
                self.viewer.layers.selection.active = next(
                    (layer for layer in selected if layer.visible), selected[0],
                )
                self.viewer.reset_view(layers=selected)
        finally:
            self._updating_scenes = False

    def _scene_visibility_changed(self, event) -> None:
        if self._updating_scenes:
            return
        layer = event.source
        view = self._scene_views[layer.metadata["czi_load_id"]]
        view.visibility[layer] = layer.visible
        layers = self._scene_group_layers(layer.metadata["czi_load_id"])
        self._apply_scene_view(layers, _scene_ids(layers), view)

    def _scene_mode_changed(self, index: int) -> None:
        self._change_scene_view(slider=index == 1)

    def _scene_changed(self, index: int) -> None:
        self._change_scene_view(index=index)

    def _native_scene_changed(self, event=None) -> None:
        if self._updating_scenes:
            return
        layers = self._scene_layers()
        if layers and _is_scene_stack(layers[0]):
            self._sync_scene_controls()

    def _scene_descriptions(self, layers: list[Image]) -> list[LayerData]:
        """Recover individual scenes without reading pixels or changing layers."""
        descriptions = []
        for layer in layers:
            description = layer.as_layer_data_tuple()
            _, kwargs, _ = description
            for key in ("scale", "translate", "axis_labels"):
                kwargs[key] = tuple(kwargs[key])
            if layer.metadata.get("czi_scene_canvas"):
                descriptions.extend(self._scenes_from_canvas(layer, description))
            elif _is_scene_stack(layer):
                descriptions.extend(self._scenes_from_stack(layer, description))
            else:
                descriptions.append(description)
        return descriptions

    def _scenes_from_canvas(
        self, layer: Image, description: LayerData,
    ) -> list[LayerData]:
        """Restore source pixels while keeping current canvas display settings."""
        _, kwargs, kind = description
        view = self._scene_views[layer.metadata["czi_load_id"]]
        sources = [
            source for source in view.canvas_sources
            if source[1]["metadata"]["channel"] == layer.metadata["channel"]
            and source[1]["metadata"]["multiscale_index"]
            == layer.metadata["multiscale_index"]
        ]
        origin = np.min([kw["translate"] for _, kw, _ in sources], axis=0)
        descriptions = []
        for source_data, source_kwargs, _ in sources:
            metadata = {**layer.metadata, **source_kwargs["metadata"]}
            metadata.pop("scenes", None)
            metadata.pop("czi_scene_canvas", None)
            # A user may have moved or rescaled the canvas. Apply that change
            # to each scene's relative origin, not to the cached source pixels.
            relative_origin = np.asarray(source_kwargs["translate"]) - origin
            scale_ratio = layer.scale / np.asarray(source_kwargs["scale"])
            descriptions.append((
                source_data,
                {
                    **kwargs,
                    "metadata": metadata,
                    "multiscale": source_kwargs["multiscale"],
                    "translate": tuple(layer.translate + relative_origin * scale_ratio),
                },
                kind,
            ))
        return descriptions

    @staticmethod
    def _scenes_from_stack(layer: Image, description: LayerData) -> list[LayerData]:
        """Remove the synthetic S axis and restore original scene positions."""
        data, kwargs, kind = description
        spatial_axes = list(range(1, layer.ndim))
        # Every dimensional transform must lose S, not just scale/translation.
        transform = CompositeAffine(
            scale=layer.scale, translate=layer.translate,
            rotate=layer.rotate, shear=layer.shear,
        ).set_slice(spatial_axes)
        spatial = {
            key: getattr(transform, key) for key in ("scale", "rotate", "shear")
        }
        spatial["affine"] = layer.affine.set_slice(spatial_axes)
        spatial["axis_labels"] = tuple(layer.axis_labels[1:])
        spatial["units"] = tuple(layer.units[1:])
        levels = data if layer.multiscale else [data]
        scenes = layer.metadata["scenes"]
        original_origins = np.asarray([scene["translate"] for scene in scenes])
        # Stacking retained only origins shared by all scenes. Subtract that
        # retained origin before applying any translation edits to the stack.
        shared_origin = np.where(
            np.all(original_origins == original_origins[0], axis=0),
            original_origins[0], 0,
        )
        descriptions = []
        for index, scene in enumerate(scenes):
            metadata = {**layer.metadata, **scene}
            metadata.pop("scenes")
            scene_levels = [level[index] for level in levels]
            descriptions.append((
                scene_levels if layer.multiscale else scene_levels[0],
                {
                    **kwargs, **spatial,
                    "translate": np.asarray(scene["translate"])
                    + layer.translate[1:] - shared_origin,
                    "name": f"{layer.name} | scene {scene['series']}",
                    "metadata": metadata,
                },
                kind,
            ))
        return descriptions

    def _replace_scene_layers(
        self, layers: list[Image], descriptions: list[LayerData],
    ) -> list[Image]:
        # Validate replacements before removing any current viewer content.
        replacements = [Image(data, **kwargs) for data, kwargs, _ in descriptions]
        load_id = layers[0].metadata["czi_load_id"]
        view = self._scene_views[load_id]
        position = min(self.viewer.layers.index(layer) for layer in layers)
        self._updating_scenes = True
        try:
            # Render the source hierarchy once, not once per replacement scene.
            with self.metadata_panel.defer_updates():
                for layer in view.visibility:
                    layer.events.visible.disconnect(self._scene_visibility_changed)
                view.visibility.clear()
                for layer in layers:
                    self.viewer.layers.remove(layer)
                for index, layer in enumerate(replacements):
                    self.viewer.add_layer(layer)
                    self.viewer.layers.move(len(self.viewer.layers) - 1, position + index)
                self.viewer.layers.selection.active = replacements[0]
                if _is_scene_stack(replacements[0]):
                    axis = self.viewer.dims.ndim - replacements[0].ndim
                    self.viewer.dims.set_current_step(axis, view.index)
        finally:
            self._updating_scenes = False
        return replacements

    def _change_scene_view(
        self, *, slider: bool | None = None, index: int | None = None,
    ) -> None:
        layers = self._scene_layers()
        if not layers:
            return
        view = self._scene_views[layers[0].metadata["czi_load_id"]]
        if slider is not None:
            view.slider = slider
        if index is not None:
            view.index = index
            if _is_scene_stack(layers[0]):
                axis = self.viewer.dims.ndim - layers[0].ndim
                self.viewer.dims.set_current_step(axis, index)
                self._sync_scene_controls()
                return
        native = _is_scene_stack(layers[0])
        if slider is not None and slider != native:
            # Normalize both compact representations back to original scenes.
            # Only then choose a new stack, canvas, or separate-layer fallback.
            descriptions = self._scene_descriptions(layers)
            if slider:
                descriptions = _compact_czi_layers(descriptions)
            else:
                canvas = _canvas_czi_layers(descriptions)
                if any(kw["metadata"].get("czi_scene_canvas") for _, kw, _ in canvas):
                    view.canvas_sources = descriptions
                descriptions = canvas
            if native or any("scenes" in kw["metadata"] for _, kw, _ in descriptions):
                layers = self._replace_scene_layers(layers, descriptions)
        self._sync_scene_controls()
        scenes = _scene_ids(layers)
        self._apply_scene_view(layers, scenes, view, fit=True)

    def _update_open(self) -> None:
        """Enable opening only when there is an input and no active worker."""
        self.filename.setToolTip(self.filename.text())
        self.open_button.setEnabled(bool(self.filename.text().strip()) and self._worker is None)
        self.save_archive.setEnabled(
            self._worker is None and Path(self.filename.text().strip()).suffix.lower() != ".vzip"
        )

    def _choose_file(self) -> None:
        """Select an input without generating files or changing source images."""
        patterns = " ".join(f"*{suffix}" for suffix in IMAGE_SUFFIXES)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open as virtual OME-Zarr",
            self.filename.text(),
            f"Images and archives ({patterns});;All files (*)",
        )
        if path:
            self.filename.setText(path)

    def _open(self) -> None:
        """Snapshot the source and build metadata and lazy layer graphs off-thread."""
        path = self.filename.text().strip()
        if not path or self._worker is not None:
            return
        self._archive_path = None
        operation = read_images
        arguments = (path,)
        options = {}
        if self.save_archive.isChecked() and Path(path).suffix.lower() != ".vzip":
            # Resolve the destination and replacement consent before starting any
            # work. Cancelling either dialog cancels the entire open action.
            destination, _ = QFileDialog.getSaveFileName(
                self,
                "Save VZIP archive",
                str(Path(path).with_suffix(".vzip")),
                "VZIP archives (*.vzip)",
                options=QFileDialog.DontConfirmOverwrite,
            )
            if not destination:
                return
            output = Path(destination).expanduser().resolve()
            if not output.suffix:
                output = output.with_suffix(".vzip")
            if output == Path(path).expanduser().resolve() or output.suffix.lower() != ".vzip":
                self._failed(ValueError("Choose a separate .vzip archive destination."))
                return
            overwrite = output.exists()
            # QFileDialog's automatic overwrite prompt is disabled so consent
            # applies to the normalized path, including an appended .vzip suffix.
            if (
                overwrite
                and QMessageBox.question(
                    self,
                    "Replace VZIP archive?",
                    f"Replace {output.name}?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            self._archive_path = output
            operation = read_and_save_image
            arguments = (path, str(output))
            options["overwrite"] = overwrite
        self._closed = False
        self.status.setText(f"Opening {Path(path).name}...")
        # Connect every signal before starting, including for very fast loads.
        # _ignore_errors prevents the worker's automatic re-raise, not reporting:
        # the errored signal below logs and notifies through _failed.
        self._worker = create_worker(
            operation,
            *arguments,
            _start_thread=False,
            _ignore_errors=True,
            stack_scenes=False,
            **options,
        )
        self._worker.returned.connect(self._loaded)
        self._worker.errored.connect(self._failed)
        self._worker.finished.connect(self._finished)
        self.filename.setEnabled(False)
        self.browse.setEnabled(False)
        # Freeze the visible input while the worker uses the captured arguments.
        self._update_open()
        self.progress.show()
        self._worker.start()

    @Slot(object)
    def _loaded(self, layers: list[LayerData]) -> None:
        """Add layers on the owning viewer's Qt thread, not in the worker."""
        if self._closed:
            return
        added = []
        try:
            for data, kwargs, _ in _compact_czi_layers(layers):
                added.append(self.viewer.add_image(data, **kwargs))
        except Exception as error:
            # Roll back only layers from this load, preserving earlier viewer
            # content. An archive already saved by the worker remains on disk.
            for layer in added:
                self.viewer.layers.remove(layer)
            self._failed(error)
            return
        message = f"Opened {len(added)} image layer(s)"
        if self._archive_path is not None:
            message += f"; saved {self._archive_path.name}"
        self.status.setText(message)
        self.status.setToolTip(str(self._archive_path) if self._archive_path else "")

    @Slot(object)
    def _failed(self, error: Exception) -> None:
        """Report external file/codec errors without silently switching readers."""
        from napari.utils.notifications import show_error

        # Keep the traceback even if the widget closed before the worker failed.
        # Only the on-screen notification is suppressed for a closed widget.
        logger.error(
            "Virtual image opening failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        if not self._closed:
            self.status.setText(str(error))
            show_error(str(error))

    def _finished(self) -> None:
        """Restore controls after either successful preparation or failure."""
        self._worker = None
        self.progress.hide()
        self.filename.setEnabled(True)
        self.browse.setEnabled(True)
        self._update_open()

    def dragEnterEvent(self, event) -> None:
        """Accept a single local file when no load is in progress."""
        urls = event.mimeData().urls()
        if self._worker is None and len(urls) == 1 and urls[0].isLocalFile():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        """Select a dropped input; opening remains an explicit action."""
        # Unlike a canvas drop, a dock drop must allow review of the save option
        # and path before starting potentially expensive metadata preparation.
        urls = event.mimeData().urls()
        if self._worker is None and len(urls) == 1 and urls[0].isLocalFile():
            self.filename.setText(urls[0].toLocalFile())
            event.acceptProposedAction()
        else:
            event.ignore()

    def closeEvent(self, event) -> None:
        """Discard worker results if the dock is closed during preparation."""
        self._closed = True
        super().closeEvent(event)
