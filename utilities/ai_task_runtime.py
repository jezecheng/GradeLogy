"""Process-wide gate for background AI work.

The web app has separate schedulers for general AI operations, grading, and
knowledge-graph building. They must share one execution gate so those pools do
not call models or PDFium concurrently inside the same application process.
"""
import threading


AI_EXECUTION_LOCK = threading.Lock()
