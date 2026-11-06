"""Conservative plane identification shared by extraction and classification."""

import math


def image_plane(orientation):
    try:
        a, b, c, d, e, f = (float(v) for v in orientation)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (a, b, c, d, e, f)):
        return None
    if (
        abs(a*a + b*b + c*c - 1) > 0.01
        or abs(d*d + e*e + f*f - 1) > 0.01
        or abs(a*d + b*e + c*f) > 0.01
    ):
        return None
    normal = (b*f - c*e, c*d - a*f, a*e - b*d)
    length = math.sqrt(sum(v*v for v in normal))
    axis = max(range(3), key=lambda i: abs(normal[i]))
    if abs(normal[axis]) / length < 0.9:
        return None
    return ("SAGITTAL", "CORONAL", "AXIAL")[axis]


def dominant_plane(counts, total):
    """Require one plane on at least 80% of all instances, including unknowns."""
    if not counts or not total:
        return None
    plane = max(counts, key=counts.get)
    return plane if counts[plane] / total >= 0.8 else None
