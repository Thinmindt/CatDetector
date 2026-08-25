"""Hardware smoke test: open the real camera and save one still.

Run by hand -- `uv run python tests/manual/check_camera.py`. It seizes the
camera, so it must not run while main.py is running. Named check_*.py so pytest
never collects it.

This one keeps print() rather than logging: it is a standalone script whose
output is the point, not a component of the running application.
"""

import time
from pathlib import Path

from picamera2 import Picamera2

OUTPUT_PATH = Path("test_images/test_image.jpg")
SETTLE_SECONDS = 2


def check_camera() -> bool:
    print("Initializing camera...")
    picam2 = Picamera2()
    try:
        camera_config = picam2.create_still_configuration()
        picam2.configure(camera_config)
        picam2.start()

        # Let auto-exposure and auto-white-balance settle before capturing.
        time.sleep(SETTLE_SECONDS)
        print(f"Camera configuration: {camera_config}")
        print("Camera started successfully")

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        print("Capturing test image...")
        picam2.capture_file(str(OUTPUT_PATH))
        print(f"Test image saved to {OUTPUT_PATH}")

        print("Camera properties:")
        for name, value in picam2.camera_properties.items():
            print(f"  {name}: {value}")
    except Exception as error:
        print(f"Camera test failed: {error}")
        raise
    else:
        print("\nCamera test completed successfully")
        return True
    finally:
        picam2.close()


if __name__ == "__main__":
    check_camera()
