"""The image array every module passes around: HxWx3 uint8, as picamera2 delivers it."""

import numpy as np
from numpy.typing import NDArray

Frame = NDArray[np.uint8]
