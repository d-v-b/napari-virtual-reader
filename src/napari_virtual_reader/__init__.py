"""Public reader entry points for refkv's virtual OME-Zarr images.

The npe2 manifest resolves the widget separately. Keep Qt widget imports out of
this module so importing the reader does not construct UI objects or require a
running QApplication. ``read_images`` also supports scripted layer preparation.
"""

from ._reader import napari_get_reader, read_images

__all__ = ["napari_get_reader", "read_images"]
__version__ = "0.1.0"
