"""Focus / sharpness metrics for live frames.

Used by the status bar to give the operator a numeric proxy for focus
quality. Pure functions — no GUI dependencies, easy to sanity-check
on a still image.
"""

from __future__ import annotations

import cv2
import numpy as np


_SHARPNESS_DOWNSAMPLE = (256, 256)


def sharpness(rgb: np.ndarray) -> float:
    """Variance of the Laplacian on a center-cropped, downsampled luma view.

    Higher = more high-frequency content = sharper focus. The absolute
    value is scene-dependent (texture matters), but for a fixed scene
    the metric peaks at best focus, which is what the operator cares
    about.

    Pipeline: take the center 50%×50% of the frame, downsample to
    256×256 with INTER_AREA (proper area-averaging on the way down),
    convert to single-channel float32, run Laplacian, take variance.
    Sub-millisecond on modern CPUs at this size — safe to run every
    analyzed frame. Running on the full sensor is wasteful and
    disproportionately weights the corners.

    Returns 0.0 if the frame is too small to crop sensibly.
    """
    if rgb.ndim != 3 or rgb.shape[0] < 4 or rgb.shape[1] < 4:
        return 0.0
    h, w, _ = rgb.shape
    cy, cx = h // 2, w // 2
    hh, hw = h // 4, w // 4  # half-extents of the 50%×50% center crop
    crop = rgb[cy - hh : cy + hh, cx - hw : cx + hw]
    small = cv2.resize(crop, _SHARPNESS_DOWNSAMPLE, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.float32, copy=False)
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    return float(lap.var())
