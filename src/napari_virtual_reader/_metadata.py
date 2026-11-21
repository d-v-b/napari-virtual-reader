"""Read-only inspector for virtual Zarr structure and selected-layer metadata.

The reader snapshots store metadata during preparation. This module renders that
snapshot and follows napari's active-layer selection; it never indexes image
arrays. Dock creation is shared with the opener so File/Open does not create a
second, competing metadata window.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from superqt.utils import ensure_main_thread


def ensure_metadata_panel(viewer: Any) -> "MetadataPanel":
    """Reuse the plugin dock for both reader and opener workflows.

    Use napari's registered plugin dock rather than constructing the widget
    directly: the plugin menu and reader-triggered loads then share ownership.
    Showing an existing dock also restores it after the user has hidden it.
    """
    dock, widget = viewer.window.add_plugin_dock_widget("napari-virtual-reader")
    dock.show()
    return widget.metadata_panel


@ensure_main_thread
def prepare_metadata_inspectors() -> None:
    """Attach reader-only loads to live viewers, even when called by a worker.

    The reader hook receives paths, not a target viewer. Attach to the current
    viewer instances and let each inspector follow its own selection. The
    decorator marshals all widget creation onto the Qt main thread.
    """
    from napari import Viewer

    # Snapshot napari's instance collection before any UI creation can alter it.
    for viewer in tuple(Viewer._instances):
        ensure_metadata_panel(viewer)


class MetadataPanel(QWidget):
    """Browse source hierarchy and attributes without accessing array values.

    Tree cells contain short summaries. Qt.UserRole retains the complete value
    for the detail pane, so long metadata does not force the dock to expand.
    """

    def __init__(self, viewer: Any) -> None:
        super().__init__()
        self.viewer = viewer
        self.setMinimumWidth(280)
        # Tell the enclosing dock that this panel can use spare vertical space;
        # otherwise napari can place unused stretch below the entire widget.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.source = QLabel()
        self.source.setWordWrap(True)
        self.source.setTextFormat(Qt.PlainText)
        layout.addWidget(self.source)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter metadata")
        self.search.setClearButtonEnabled(True)
        row.addWidget(self.search)
        self.tabs = QTabWidget()
        self.structure = QTreeWidget()
        self.structure.setHeaderLabels(["Zarr path", "Layout"])
        self.metadata = QTreeWidget()
        self.metadata.setHeaderLabels(["Property", "Value"])
        self.trees = (self.structure, self.metadata)
        for tooltip, icon, method in (
            ("Expand all", QStyle.SP_ArrowDown, "expandAll"),
            ("Collapse all", QStyle.SP_ArrowUp, "collapseAll"),
        ):
            button = QToolButton()
            button.setIcon(self.style().standardIcon(icon))
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
            # Capture this iteration's method, but choose the current tab at
            # click time. Without the default argument both buttons would collapse.
            button.clicked.connect(
                lambda checked=False, method=method: getattr(
                    self.tabs.currentWidget(), method,
                )()
            )
            row.addWidget(button)
        layout.addLayout(row)
        self.tabs.addTab(self.structure, "Zarr structure")
        self.tabs.addTab(self.metadata, "Metadata")
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(100)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.tabs)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)
        for tree in self.trees:
            tree.setAlternatingRowColors(True)
            tree.setUniformRowHeights(True)
            tree.setColumnWidth(0, 170)
            tree.currentItemChanged.connect(self._selected)
        self.search.textChanged.connect(self._filter)
        self.tabs.currentChanged.connect(lambda: self.detail.clear())
        viewer.layers.selection.events.active.connect(self._active_layer_changed)
        self._active_layer_changed()

    def _property(self, parent: Any, name: str, value: Any) -> None:
        """Represent structured attributes as expandable keys and indexed lists."""
        if isinstance(value, dict):
            summary = f"{len(value)} properties"
            children = value.items()
        elif isinstance(value, (list, tuple)):
            summary = f"{len(value)} items"
            children = enumerate(value)
        else:
            summary = str(value)
            children = ()
        item = QTreeWidgetItem(parent, [str(name), summary[:200]])
        item.setData(0, Qt.UserRole, value)
        for key, child in children:
            self._property(item, str(key), child)

    def _active_layer_changed(self, event=None) -> None:
        """Follow image selection regardless of how the image was opened."""
        self.set_layer(self.viewer.layers.selection.active)

    @contextmanager
    def defer_updates(self) -> Iterator[None]:
        """Refresh once after a batch of layer changes, even if it fails."""
        try:
            with self.viewer.layers.selection.events.active.blocker(
                self._active_layer_changed,
            ):
                yield
        finally:
            self.set_layer(self.viewer.layers.selection.active)

    def _node(self, parent: Any, name: str, node: dict) -> None:
        """Show array shape/dtype and make chunk grids/codecs inspectable."""
        metadata = node.get("metadata", {})
        layout = "group"
        if node["kind"] == "array":
            shape = " x ".join(str(size) for size in metadata["shape"])
            # Zarr metadata versions use different dtype keys in their dictionaries.
            dtype = metadata.get("data_type", metadata.get("dtype", ""))
            layout = f"{shape} | {dtype}"
        item = QTreeWidgetItem(parent, [name, layout])
        properties = metadata if node["kind"] == "array" else node["attributes"]
        item.setData(0, Qt.UserRole, properties)
        self._property(item, "Array metadata" if metadata else "Attributes", properties)
        for child in node.get("children", []):
            self._node(item, child["name"], child)

    def set_layer(self, layer: Any) -> None:
        """Display one selected layer, clearing stale metadata for other layers."""
        # Clear before checking provenance: selecting an unrelated layer must not
        # leave the previous virtual image's source or attributes on screen.
        for tree in self.trees:
            tree.clear()
        self.detail.clear()
        self.source.clear()
        metadata = getattr(layer, "metadata", {})
        if not isinstance(metadata, dict) or "zarr_structure" not in metadata:
            return
        source = metadata.get("source", "")
        self.source.setText(Path(source).name)
        self.source.setToolTip(source)
        self._node(self.structure, "/", metadata["zarr_structure"])
        for key, value in metadata.items():
            if key != "zarr_structure":
                self._property(self.metadata, key, value)
        self.structure.expandToDepth(1)
        self.metadata.expandToDepth(0)
        self._filter(self.search.text())

    def _selected(self, item: Any, previous: Any) -> None:
        """Expose complete, selectable values without widening the tree columns."""
        # Plain text avoids interpreting source attributes as HTML. default=str
        # keeps non-JSON metadata inspectable without modifying the stored values.
        text = ""
        if item is not None:
            text = json.dumps(
                item.data(0, Qt.UserRole), indent=2, ensure_ascii=True, default=str,
            )
        self.detail.setPlainText(text)

    def _filter(self, text: str) -> None:
        """Retain matching rows and their ancestors in both inspector views."""
        text = text.casefold()

        def visit(item: QTreeWidgetItem) -> bool:
            # Visit every child before testing the parent. Short-circuiting the
            # recursion would leave stale hidden states after changing a filter.
            # A matching descendant keeps its ancestor path visible and expanded.
            child_matches = [
                visit(item.child(index)) for index in range(item.childCount())
            ]
            row_text = f"{item.text(0)} {item.text(1)}".casefold()
            matches = not text or text in row_text or any(child_matches)
            item.setHidden(not matches)
            if text and any(child_matches):
                item.setExpanded(True)
            return matches

        for tree in self.trees:
            for index in range(tree.topLevelItemCount()):
                visit(tree.topLevelItem(index))
