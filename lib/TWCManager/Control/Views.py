import hashlib
import json
import math
import time

# Service start; folded into every fingerprint so a restart invalidates pages
STARTED = time.time()


def fingerprint(*shape):
    # Short digest of the page structure; changes when the shape or the service start changes
    raw = json.dumps([STARTED] + list(shape), sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def jsonSafe(obj):
    # Copy of obj that json.dumps(allow_nan=False) always accepts
    if isinstance(obj, dict):
        return {str(k): jsonSafe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonSafe(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    return str(obj)
