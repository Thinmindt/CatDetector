from picamera2 import Picamera2, Preview
import time
import os


def test_camera():
    print("Initializing camera...")
    try:
        # Initialize camera
        picam2 = Picamera2()

        # Configure camera for still capture (no preview needed)
        camera_config = picam2.create_still_configuration()
        picam2.configure(camera_config)

        # Start camera without preview
        picam2.start()
        time.sleep(2)  # Allow time for camera to adjust

        print(f"Camera configuration: {camera_config}")

        print("Camera started successfully")

        # Create test directory if it doesn't exist
        if not os.path.exists("test_images"):
            os.makedirs("test_images")

        # Capture test image
        print("Capturing test image...")
        time.sleep(2)  # Give camera time to adjust
        picam2.capture_file("test_images/test_image.jpg")
        print(f"Test image saved to test_images/test_image.jpg")

        # Show camera properties
        print("Camera properties:")
        properties = picam2.camera_properties
        for prop in properties:
            print(f"  {prop}: {properties[prop]}")

        # Cleanup
        picam2.close()
        print("\nCamera test completed successfully")
        return True

    except Exception as e:
        print(f"Camera test failed: {e}")
        raise e


if __name__ == "__main__":
    test_camera()
