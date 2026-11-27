"""Shared raster/media limits for import adapters and deterministic analyzers."""
from __future__ import annotations

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"})
# Only raster plugins shipped with Pillow are permitted; anything else may need an
# external decoder and must never run during import or analysis.
SUPPORTED_IMAGE_FORMATS = frozenset({"BMP", "GIF", "JPEG", "PNG", "TIFF", "WEBP"})
MAX_MEDIA_FILE_BYTES = 256 * 1024 * 1024
MAX_IMAGE_FRAMES = 1000
MAX_IMAGE_TOTAL_PIXELS = 64_000_000
