"""Shared synchronization for PDFium's process-wide native runtime.

PDFium is used by request handlers and background workers in the same process.
Keep every operation on a PdfDocument (including closing it) under this lock;
serializing PDF work is preferable to intermittent native-library failures.
"""
import logging
import threading


PDFIUM_LOCK = threading.RLock()
_LOG = logging.getLogger("gradelogy.pdfium")


def close_pdfium_resource(resource, kind, path):
    """Close a native PDFium handle without masking the primary operation error."""
    if resource is None:
        return
    try:
        resource.close()
    except Exception:
        _LOG.exception("PDFium 资源关闭失败 kind=%s path=%s", kind, path)
