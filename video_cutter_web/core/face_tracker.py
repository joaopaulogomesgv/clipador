import os
import subprocess
import logging
import cv2
import numpy as np

logger = logging.getLogger(__name__)

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(CURRENT_DIR, "models", "face_detection_yunet_2023mar.onnx")

_detector_instance = None


def get_detector(sample_w=640, sample_h=360):
    """
    Returns or initializes the YuNet FaceDetectorYN instance.
    """
    global _detector_instance
    if not os.path.exists(MODEL_PATH):
        logger.warning(f"YuNet model not found at {MODEL_PATH}")
        return None

    try:
        if _detector_instance is None:
            _detector_instance = cv2.FaceDetectorYN.create(
                MODEL_PATH, "", (sample_w, sample_h), score_threshold=0.6
            )
        else:
            _detector_instance.setInputSize((sample_w, sample_h))
        return _detector_instance
    except Exception as e:
        logger.warning(f"Error initializing FaceDetectorYN: {e}")
        return None


def detect_speaker_trajectory(video_path, start_time, duration, video_w, video_h, sample_fps=1):
    """
    Quickly samples 1 frame per second from the given video clip using FFmpeg,
    runs YuNet face detection on each frame, and returns a smoothed trajectory of crop left-x positions.
    """
    sample_w, sample_h = 640, int(640 * video_h / video_w)
    # Ensure sample dimensions are even
    sample_w = sample_w if sample_w % 2 == 0 else sample_w + 1
    sample_h = sample_h if sample_h % 2 == 0 else sample_h + 1

    detector = get_detector(sample_w, sample_h)
    crop_w = int(video_h * 9 / 16)
    if crop_w % 2 != 0:
        crop_w += 1
    max_x = max(0, video_w - crop_w)
    center_default = max(0, (video_w - crop_w) // 2)

    if not detector or not os.path.exists(video_path):
        return [(0, center_default), (duration, center_default)]

    try:
        # Fast extraction of 1 frame per second via FFmpeg
        cmd = [
            "ffmpeg",
            "-ss", str(max(0, start_time)),
            "-t", str(duration),
            "-i", video_path,
            "-vf", f"fps={sample_fps},scale={sample_w}:{sample_h}",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-v", "quiet",
            "-"
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        raw_data, _ = proc.communicate()
        frame_size = sample_w * sample_h * 3
        num_frames = len(raw_data) // frame_size

        if num_frames == 0:
            return [(0, center_default), (duration, center_default)]

        scale_x = video_w / sample_w
        raw_points = []

        for i in range(num_frames):
            buf = raw_data[i * frame_size: (i + 1) * frame_size]
            img = np.frombuffer(buf, dtype=np.uint8).reshape((sample_h, sample_w, 3))
            _, faces = detector.detect(img)
            
            if faces is not None and len(faces) > 0:
                # Find dominant face (highest confidence & largest area)
                best_face = max(faces, key=lambda f: (f[2] * f[3]) * f[-1])
                face_center_x = (best_face[0] + best_face[2] / 2.0) * scale_x
                raw_points.append((i, face_center_x))
            else:
                last_x = raw_points[-1][1] if raw_points else (video_w / 2.0)
                raw_points.append((i, last_x))

        if not raw_points:
            return [(0, center_default), (duration, center_default)]

        # Convert face center coordinates to left crop coordinates
        target_xs = [max(0, min(max_x, int(pt[1] - crop_w // 2))) for pt in raw_points]

        # Smooth trajectory:
        # 1. Micro-jitter dead-zone: ignore movement < 20px
        # 2. Smooth pan: exponential moving average
        # 3. Scene cut: snap instantly if shift > 280px (e.g. Host <-> Guest angle change)
        smoothed_xs = []
        curr = target_xs[0]
        for tx in target_xs:
            delta = abs(tx - curr)
            if delta > 280:
                # Instant cut to new speaker
                curr = tx
            elif delta < 20:
                # Dead-zone: keep steady
                pass
            else:
                # Smooth pan
                curr = int(0.35 * tx + 0.65 * curr)
            smoothed_xs.append(max(0, min(max_x, curr)))

        key_points = [(i * (1.0 / sample_fps), smoothed_xs[i]) for i in range(len(smoothed_xs))]
        
        # Ensure end point matches duration
        if key_points and key_points[-1][0] < duration:
            key_points.append((duration, key_points[-1][1]))

        return key_points

    except Exception as e:
        logger.error(f"Error in detect_speaker_trajectory: {e}", exc_info=True)
        return [(0, center_default), (duration, center_default)]


def build_smart_reels_filter(video_path, start_time, duration, video_w, video_h, target_w=1080, target_h=1920):
    """
    Builds the dynamic FFmpeg video filter for Smart Auto-Reframe (following the speaker).
    Returns (vf_string, filter_type).
    """
    crop_w = int(video_h * 9 / 16)
    if crop_w % 2 != 0:
        crop_w += 1
    max_x = max(0, video_w - crop_w)
    center_default = max(0, (video_w - crop_w) // 2)

    # 1. Detect trajectory of speaker across the clip
    trajectory = detect_speaker_trajectory(video_path, start_time, duration, video_w, video_h)

    # 2. Build mathematical expression for FFmpeg crop filter
    if not trajectory or len(trajectory) == 1:
        x_val = trajectory[0][1] if trajectory else center_default
        return f"crop=w={crop_w}:h={video_h}:x={x_val}:y=0,scale={target_w}:{target_h}"

    # Build nested if expression from right to left
    expr = str(trajectory[-1][1])
    for i in range(len(trajectory) - 2, -1, -1):
        t0, x0 = trajectory[i]
        t1, x1 = trajectory[i + 1]
        dt = t1 - t0
        if dt <= 0:
            continue
        dx = x1 - x0
        if abs(dx) <= 5:
            val = str(x0)
        elif abs(dx) > 280:
            # Snap cut (angle switch)
            val = str(x0)
        else:
            # Smooth linear camera pan
            val = f"({x0}+({dx})*(t-{t0:.2f})/{dt:.2f})"
        expr = f"if(lt(t\\,{t1:.2f})\\,{val}\\,{expr})"

    return f"crop=w={crop_w}:h={video_h}:x='{expr}':y=0,scale={target_w}:{target_h}"
