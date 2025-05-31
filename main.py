from src.motion_recorder import MotionRecorder

if __name__ == "__main__":
    recorder = MotionRecorder(output_dir="/home/pi/cat_videos")
    recorder.monitor()
