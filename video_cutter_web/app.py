import os
import sys
import uuid
import json
import subprocess
import tempfile
import wave
import struct
import logging
import threading
import time
from pathlib import Path
from flask import (
    Flask, render_template, request, jsonify,
    send_file, send_from_directory, Response
)
from werkzeug.utils import secure_filename
import cv2
import numpy as np

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
app.config['EXPORT_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'exports')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['EXPORT_FOLDER'], exist_ok=True)

videos = {}
yt_progress = {}
yt_downloads = {}
analysis_progress = {}


def find_ffmpeg():
    candidates = [
        "ffmpeg",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]
    try:
        result = subprocess.run(
            ["where", "ffmpeg"],
            capture_output=True, text=True, shell=True, timeout=5
        )
        if result.returncode == 0:
            path = result.stdout.strip().split("\n")[0].strip()
            if path and os.path.exists(path):
                return path
    except Exception:
        pass

    for c in candidates:
        if os.path.exists(c):
            return c
        try:
            result = subprocess.run(
                [c, "-version"], capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return c
        except Exception:
            pass
    return None


FFMPEG_PATH = find_ffmpeg()
logger.info(f"FFmpeg path: {FFMPEG_PATH}")


def get_video_info_opencv(video_path):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {}

    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = frame_count / fps if fps > 0 else 0

    codec_int = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join([chr((codec_int >> 8 * i) & 0xFF) for i in range(4)])

    cap.release()

    return {
        "duration": round(duration, 2),
        "width": width,
        "height": height,
        "fps": round(fps, 2),
        "codec": codec,
        "frame_count": frame_count
    }


def get_video_info(video_path):
    if FFMPEG_PATH:
        try:
            cmd = [FFMPEG_PATH, "-i", video_path, "-f", "null", "-"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            info = {}
            for line in result.stderr.split("\n"):
                if "Duration:" in line:
                    duration_str = line.split("Duration:")[1].split(",")[0].strip()
                    parts = duration_str.replace(",", "").split(":")
                    if len(parts) == 3:
                        h, m, s = parts
                        info["duration"] = float(h) * 3600 + float(m) * 60 + float(s)
                elif "Video:" in line:
                    parts = line.split("Video:")[1].split(",")
                    info["codec"] = parts[0].strip()
                    for p in parts:
                        p = p.strip()
                        if "x" in p and "fps" not in p:
                            try:
                                res = p.split(" ")[0].split("x")
                                info["width"] = int(res[0])
                                info["height"] = int(res[1])
                            except (ValueError, IndexError):
                                pass
                elif "Audio:" in line:
                    parts = line.split("Audio:")[1].split(",")
                    info["audio_codec"] = parts[0].strip()
            if info:
                return info
        except Exception as e:
            logger.warning(f"FFmpeg info failed: {e}")

    return get_video_info_opencv(video_path)


MIMETYPE_MAP = {
    '.mp4': 'video/mp4',
    '.mkv': 'video/x-matroska',
    '.avi': 'video/x-msvideo',
    '.mov': 'video/quicktime',
    '.webm': 'video/webm',
    '.wmv': 'video/x-ms-wmv',
    '.flv': 'video/x-flv',
}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_video():
    logger.debug("Upload request received")

    if 'video' not in request.files:
        logger.error("No video file in request")
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    file = request.files['video']
    if file.filename == '':
        logger.error("Empty filename")
        return jsonify({"error": "Nenhum arquivo selecionado"}), 400

    video_id = str(uuid.uuid4())[:8]
    filename = secure_filename(file.filename)
    ext = Path(filename).suffix or '.mp4'
    saved_name = f"{video_id}{ext}"
    video_path = os.path.join(app.config['UPLOAD_FOLDER'], saved_name)

    logger.info(f"Saving video: {filename} -> {video_path}")
    file.save(video_path)
    file_size = os.path.getsize(video_path)
    logger.info(f"File saved, size: {file_size} bytes")

    logger.info("Getting video info with OpenCV...")
    info = get_video_info(video_path)
    logger.info(f"Video info: {info}")

    videos[video_id] = {
        "path": video_path,
        "filename": filename,
        "info": info,
        "cuts": []
    }

    return jsonify({
        "video_id": video_id,
        "filename": filename,
        "info": info
    })


@app.route('/video/<video_id>')
def serve_video(video_id):
    logger.debug(f"Serving video: {video_id}")

    if video_id not in videos:
        logger.error(f"Video not found: {video_id}")
        return "Video nao encontrado", 404

    video_path = videos[video_id]["path"]

    if not os.path.exists(video_path):
        logger.error(f"File does not exist: {video_path}")
        return "Arquivo nao encontrado", 404

    ext = Path(video_path).suffix.lower()
    mimetype = MIMETYPE_MAP.get(ext, 'video/mp4')

    logger.info(f"Serving {video_path} with mimetype {mimetype}")
    return send_file(video_path, mimetype=mimetype)


@app.route('/analyze/<video_id>', methods=['POST'])
def analyze_video(video_id):
    if video_id not in videos:
        return jsonify({"error": "Video nao encontrado"}), 404

    data = request.get_json() or {}
    silence_threshold = data.get("silence_threshold", -40)
    min_silence = data.get("min_silence", 2)
    scene_threshold = data.get("scene_threshold", 30)

    analysis_id = str(uuid.uuid4())[:8]
    analysis_progress[analysis_id] = {
        'status': 'starting',
        'percent': 0,
        'step': 'Iniciando...',
        'done': False,
        'result': None,
        'error': None
    }

    video = videos[video_id]
    video_path = video["path"]
    total_duration = video["info"].get("duration", 0)

    thread = threading.Thread(
        target=_do_analyze,
        args=(analysis_id, video_id, video_path, total_duration,
              silence_threshold, min_silence, scene_threshold),
        daemon=True
    )
    thread.start()

    return jsonify({"analysis_id": analysis_id})


@app.route('/analyze/progress/<analysis_id>')
def analyze_progress(analysis_id):
    if analysis_id not in analysis_progress:
        return jsonify({"error": "Analise nao encontrada"}), 404
    return jsonify(analysis_progress[analysis_id])


@app.route('/analyze/result/<analysis_id>')
def analyze_result(analysis_id):
    if analysis_id not in analysis_progress:
        return jsonify({"error": "Analise nao encontrada"}), 404
    prog = analysis_progress[analysis_id]
    if not prog.get('done'):
        return jsonify({"error": "Analise ainda nao concluida"}), 400
    if prog.get('error'):
        return jsonify({"error": prog['error']}), 500
    return jsonify(prog.get('result', {}))


def _do_analyze(analysis_id, video_id, video_path, total_duration,
                silence_threshold, min_silence, scene_threshold):
    try:
        prog = analysis_progress[analysis_id]

        if FFMPEG_PATH:
            prog.update({'status': 'running', 'percent': 5, 'step': 'Extraindo audio...'})
            temp_audio = os.path.join(tempfile.gettempdir(), f"temp_audio_{video_id}.wav")
            extract_audio(video_path, temp_audio)
            prog.update({'percent': 25, 'step': 'Analisando silencio...'})
            silent_regions = analyze_silence(temp_audio, silence_threshold, min_silence)
            if os.path.exists(temp_audio):
                os.remove(temp_audio)
        else:
            prog.update({'status': 'running', 'percent': 5, 'step': 'Lendo frames do video...'})
            time.sleep(0.1)
            prog.update({'percent': 10, 'step': 'Analisando movimento visual...'})
            silent_regions = analyze_silence_opencv(video_path, silence_threshold, min_silence)

        prog.update({'percent': 45, 'step': 'Processando segmentos...'})
        time.sleep(0.1)

        audio_segments = []
        last_end = 0
        for silence in sorted(silent_regions, key=lambda x: x["start"]):
            if silence["start"] > last_end:
                audio_segments.append({
                    "start": last_end,
                    "end": silence["start"],
                    "type": "content"
                })
            last_end = silence["end"]
        if last_end < total_duration:
            audio_segments.append({
                "start": last_end,
                "end": total_duration,
                "type": "content"
            })

        prog.update({'percent': 55, 'step': 'Detectando mudancas de cena...'})

        scene_timestamps = detect_scenes(video_path, scene_threshold)

        prog.update({'percent': 90, 'step': 'Combinando resultados...'})
        time.sleep(0.1)

        cuts = combine_results(audio_segments, scene_timestamps, total_duration)
        videos[video_id]["cuts"] = cuts

        result = {
            "total_duration": total_duration,
            "audio_segments": audio_segments,
            "scene_timestamps": scene_timestamps,
            "silent_regions": silent_regions,
            "cuts": cuts
        }

        prog.update({
            'status': 'done',
            'percent': 100,
            'step': 'Concluido!',
            'done': True,
            'result': result
        })

    except Exception as e:
        logger.error(f"Analysis error: {e}")
        analysis_progress[analysis_id].update({
            'status': 'error',
            'error': str(e),
            'done': True,
            'step': 'Erro!'
        })


def extract_audio(video_path, output_path):
    if not FFMPEG_PATH:
        return None
    cmd = [
        FFMPEG_PATH, "-i", video_path,
        "-vn", "-acodec", "pcm_s16le",
        "-ar", "44100", "-ac", "1",
        output_path, "-y"
    ]
    subprocess.run(cmd, capture_output=True, timeout=300)
    return output_path


def analyze_silence(audio_path, silence_threshold_db=-40, min_silence_duration=2.0):
    try:
        with wave.open(audio_path, 'r') as wav:
            n_channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            frame_rate = wav.getframerate()
            n_frames = wav.getnframes()
            raw_data = wav.readframes(n_frames)

        if sample_width == 2:
            fmt = f"<{n_frames * n_channels}h"
            samples = np.array(struct.unpack(fmt, raw_data), dtype=np.float64)
        elif sample_width == 4:
            fmt = f"<{n_frames * n_channels}i"
            samples = np.array(struct.unpack(fmt, raw_data), dtype=np.float64)
        else:
            samples = np.frombuffer(raw_data, dtype=np.uint8).astype(np.float64)
            samples = (samples - 128) * 256

        if n_channels > 1:
            samples = samples.reshape(-1, n_channels).mean(axis=1)

        max_val = np.max(np.abs(samples))
        if max_val > 0:
            samples = samples / max_val

        chunk_size = int(frame_rate * 0.1)
        db_levels = []

        for i in range(0, len(samples), chunk_size):
            chunk = samples[i:i + chunk_size]
            if len(chunk) == 0:
                continue
            rms = np.sqrt(np.mean(chunk ** 2))
            if rms < 1e-10:
                db = -100
            else:
                db = 20 * np.log10(rms)
            time_s = i / frame_rate
            db_levels.append((time_s, db))

        silent_regions = []
        in_silence = False
        silence_start = 0

        for time_s, db in db_levels:
            if db < silence_threshold_db:
                if not in_silence:
                    in_silence = True
                    silence_start = time_s
            else:
                if in_silence:
                    duration = time_s - silence_start
                    if duration >= min_silence_duration:
                        silent_regions.append({
                            "start": round(silence_start, 2),
                            "end": round(time_s, 2),
                            "duration": round(duration, 2)
                        })
                    in_silence = False

        if in_silence and db_levels:
            duration = db_levels[-1][0] - silence_start
            if duration >= min_silence_duration:
                silent_regions.append({
                    "start": round(silence_start, 2),
                    "end": round(db_levels[-1][0], 2),
                    "duration": round(duration, 2)
                })

        return silent_regions
    except Exception as e:
        logger.error(f"Audio analysis failed: {e}")
        return []


def analyze_silence_opencv(video_path, silence_threshold_db=-40, min_silence_duration=0.5):
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        if fps == 0:
            cap.release()
            return []

        audio_levels = []
        prev_gray = None
        sample_interval = max(1, int(fps * 10))
        frame_idx = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_idx % sample_interval == 0:
                small = cv2.resize(frame, (64, 48))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

                if prev_gray is not None:
                    diff = cv2.absdiff(prev_gray, gray)
                    motion = np.mean(diff)
                    db = -100 + min(motion * 3, 100)
                    time_s = frame_idx / fps
                    audio_levels.append((time_s, db))

                prev_gray = gray

            frame_idx += 1

        cap.release()

        silent_regions = []
        in_silence = False
        silence_start = 0

        for time_s, db in audio_levels:
            if db < silence_threshold_db:
                if not in_silence:
                    in_silence = True
                    silence_start = time_s
            else:
                if in_silence:
                    duration = time_s - silence_start
                    if duration >= min_silence_duration:
                        silent_regions.append({
                            "start": round(silence_start, 2),
                            "end": round(time_s, 2),
                            "duration": round(duration, 2)
                        })
                    in_silence = False

        return silent_regions
    except Exception as e:
        logger.error(f"OpenCV silence analysis failed: {e}")
        return []


def detect_scenes(video_path, threshold=30.0):
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)

        if fps == 0:
            cap.release()
            return []

        scene_changes = []
        prev_frame = None
        frame_count = 0
        sample_interval = max(1, int(fps * 2))

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_count % sample_interval == 0:
                small = cv2.resize(frame, (64, 48))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

                if prev_frame is not None:
                    diff = cv2.absdiff(prev_frame, gray)
                    mean_diff = np.mean(diff)

                    if mean_diff > threshold:
                        timestamp = frame_count / fps
                        if not scene_changes or (timestamp - scene_changes[-1]) >= 5.0:
                            scene_changes.append(round(timestamp, 2))

                prev_frame = gray

            frame_count += 1

        cap.release()
        return scene_changes
    except Exception as e:
        logger.error(f"Scene detection failed: {e}")
        return []


def combine_results(audio_segments, scene_timestamps, total_duration):
    all_boundaries = set()
    for seg in audio_segments:
        all_boundaries.add(round(seg["start"], 2))
        all_boundaries.add(round(seg["end"], 2))
    for ts in scene_timestamps:
        all_boundaries.add(round(ts, 2))

    all_boundaries.add(0)
    all_boundaries.add(round(total_duration, 2))
    sorted_boundaries = sorted(all_boundaries)

    cuts = []
    for i in range(len(sorted_boundaries) - 1):
        start = sorted_boundaries[i]
        end = sorted_boundaries[i + 1]
        if end - start > 0.5:
            has_audio = any(
                seg["start"] <= start and seg["end"] >= end
                for seg in audio_segments
            )
            cuts.append({
                "id": len(cuts) + 1,
                "start": start,
                "end": end,
                "duration": round(end - start, 2),
                "has_audio": has_audio,
                "selected": has_audio
            })

    merged = []
    for cut in cuts:
        if not merged:
            merged.append(cut)
            continue
        last = merged[-1]
        gap = cut["start"] - last["end"]
        if gap <= 1.0 and last["has_audio"] == cut["has_audio"]:
            last["end"] = cut["end"]
            last["duration"] = round(last["end"] - last["start"], 2)
        else:
            merged.append(cut)

    for i, cut in enumerate(merged):
        cut["id"] = i + 1

    return merged


def cut_video(video_path, start_time, end_time, output_path):
    if not FFMPEG_PATH:
        return cut_video_opencv(video_path, start_time, end_time, output_path)

    duration = end_time - start_time
    cmd = [
        FFMPEG_PATH, "-i", video_path,
        "-ss", str(start_time),
        "-t", str(duration),
        "-c:v", "libx264", "-c:a", "aac",
        "-avoid_negative_ts", "make_zero",
        output_path, "-y"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return result.returncode == 0


def cut_video_opencv(video_path, start_time, end_time, output_path):
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

        start_frame = int(start_time * fps)
        end_frame = int(end_time * fps)

        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

        for frame_num in range(start_frame, end_frame):
            ret, frame = cap.read()
            if not ret:
                break
            out.write(frame)

        cap.release()
        out.release()
        return True
    except Exception as e:
        logger.error(f"OpenCV cut failed: {e}")
        return False


@app.route('/export/<video_id>', methods=['POST'])
def export_cuts(video_id):
    if video_id not in videos:
        return jsonify({"error": "Video nao encontrado"}), 404

    data = request.get_json() or {}
    selected_ids = data.get("selected_ids", [])

    video = videos[video_id]
    video_path = video["path"]
    cuts = video["cuts"]

    if selected_ids:
        cuts = [c for c in cuts if c["id"] in selected_ids]

    if not cuts:
        return jsonify({"error": "Nenhum corte selecionado"}), 400

    export_dir = os.path.join(app.config['EXPORT_FOLDER'], video_id)
    os.makedirs(export_dir, exist_ok=True)

    exported_files = []
    for cut in cuts:
        filename = f"corte_{cut['id']:03d}.mp4"
        output_path = os.path.join(export_dir, filename)
        success = cut_video(video_path, cut["start"], cut["end"], output_path)
        if success:
            exported_files.append(filename)

    if len(exported_files) == 1:
        return send_file(
            os.path.join(export_dir, exported_files[0]),
            as_attachment=True,
            download_name=exported_files[0]
        )

    import zipfile
    zip_path = os.path.join(export_dir, "cortes.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in exported_files:
            zf.write(os.path.join(export_dir, f), f)

    return send_file(zip_path, as_attachment=True, download_name="cortes.zip")


# ==================== YOUTUBE ====================

@app.route('/yt/info', methods=['POST'])
def yt_info():
    data = request.get_json() or {}
    url = data.get('url', '')

    if not url:
        return jsonify({"error": "URL obrigatoria"}), 400

    try:
        import yt_dlp

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            duration = info.get('duration', 0)
            title = info.get('title', 'Video')
            thumbnail = info.get('thumbnail', '')
            formats = info.get('formats', [])

            return jsonify({
                'title': title,
                'duration': duration,
                'thumbnail': thumbnail,
                'url': url,
                'formats_count': len(formats)
            })

    except Exception as e:
        logger.error(f"YT info error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/yt/download', methods=['POST'])
def yt_download():
    data = request.get_json() or {}
    url = data.get('url', '')
    quality = data.get('quality', '720p')

    if not url:
        return jsonify({"error": "URL obrigatoria"}), 400

    download_id = str(uuid.uuid4())[:8]
    yt_progress[download_id] = {
        'status': 'starting',
        'percent': 0,
        'speed': '',
        'eta': '',
        'filename': '',
        'error': None,
        'done': False
    }

    thread = threading.Thread(
        target=_do_yt_download,
        args=(download_id, url, quality),
        daemon=True
    )
    thread.start()

    return jsonify({"download_id": download_id})


def _progress_hook(d, download_id):
    if download_id not in yt_progress:
        return

    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        downloaded = d.get('downloaded_bytes', 0)
        speed = d.get('speed')
        eta = d.get('eta')

        if total > 0:
            percent = (downloaded / total) * 100
        else:
            percent = 0

        yt_progress[download_id].update({
            'status': 'downloading',
            'percent': round(percent, 1),
            'speed': _format_speed(speed) if speed else '',
            'eta': _format_eta(eta) if eta else '',
            'downloaded': _format_size(downloaded),
            'total': _format_size(total),
        })

    elif d['status'] == 'finished':
        yt_progress[download_id].update({
            'status': 'processing',
            'percent': 100,
            'filename': d.get('filename', ''),
        })


def _format_speed(speed):
    if speed >= 1024 * 1024:
        return f"{speed / (1024 * 1024):.1f} MB/s"
    elif speed >= 1024:
        return f"{speed / 1024:.0f} KB/s"
    return f"{speed:.0f} B/s"


def _format_eta(eta):
    if eta is None:
        return ''
    if eta >= 3600:
        h = eta // 3600
        m = (eta % 3600) // 60
        s = eta % 60
        return f"{h}h {m}m {s}s"
    elif eta >= 60:
        m = eta // 60
        s = eta % 60
        return f"{m}m {s}s"
    return f"{eta}s"


def _format_size(b):
    if b >= 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024 * 1024):.2f} GB"
    elif b >= 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    elif b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b} B"


def _do_yt_download(download_id, url, quality):
    try:
        import yt_dlp

        download_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'yt_downloads')
        os.makedirs(download_dir, exist_ok=True)

        has_ffmpeg = FFMPEG_PATH is not None

        if quality == 'audio':
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(download_dir, '%(title)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
                'progress_hooks': [lambda d: _progress_hook(d, download_id)],
            }
            if has_ffmpeg:
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
        else:
            height = quality.replace('p', '')
            if has_ffmpeg:
                if quality == 'best':
                    format_str = 'bestvideo+bestaudio/bestvideo/best'
                else:
                    format_str = f'bestvideo[height<={height}]+bestaudio/bestvideo[height<={height}]/bestvideo/best'
                ydl_opts = {
                    'format': format_str,
                    'outtmpl': os.path.join(download_dir, '%(title)s.%(ext)s'),
                    'merge_output_format': 'mp4',
                    'quiet': True,
                    'no_warnings': True,
                    'progress_hooks': [lambda d: _progress_hook(d, download_id)],
                }
            else:
                if quality == 'best':
                    format_str = 'bestvideo/best'
                else:
                    format_str = f'bestvideo[height<={height}]/bestvideo/best'
                ydl_opts = {
                    'format': format_str,
                    'outtmpl': os.path.join(download_dir, '%(title)s.%(ext)s'),
                    'quiet': True,
                    'no_warnings': True,
                    'progress_hooks': [lambda d: _progress_hook(d, download_id)],
                }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video')

            time.sleep(1)

            downloaded_file = None
            mp4_files = []
            for f in os.listdir(download_dir):
                if f.endswith('.mp4') or f.endswith('.webm') or f.endswith('.mkv'):
                    fp = os.path.join(download_dir, f)
                    if os.path.isfile(fp) and not f.endswith('.part'):
                        mp4_files.append(fp)

            if mp4_files:
                mp4_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
                downloaded_file = mp4_files[0]
            else:
                all_files = []
                for f in os.listdir(download_dir):
                    fp = os.path.join(download_dir, f)
                    if os.path.isfile(fp) and not f.endswith('.part') and not f.endswith('.ytdl'):
                        all_files.append(fp)
                if all_files:
                    all_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
                    downloaded_file = all_files[0]

            if downloaded_file and os.path.exists(downloaded_file):
                file_size = os.path.getsize(downloaded_file)
                logger.info(f"Downloaded: {downloaded_file} ({file_size} bytes)")
                yt_progress[download_id].update({
                    'status': 'done',
                    'percent': 100,
                    'done': True,
                    'file_path': downloaded_file,
                    'file_name': os.path.basename(downloaded_file),
                })
            else:
                logger.error(f"File not found. Dir contents: {os.listdir(download_dir)}")
                yt_progress[download_id].update({
                    'status': 'error',
                    'error': 'Arquivo nao encontrado apos download',
                    'done': True,
                })

    except Exception as e:
        logger.error(f"YT download error: {e}")
        yt_progress[download_id].update({
            'status': 'error',
            'error': str(e),
            'done': True,
        })


@app.route('/yt/progress/<download_id>')
def yt_download_progress(download_id):
    if download_id not in yt_progress:
        return jsonify({"error": "Download nao encontrado"}), 404
    return jsonify(yt_progress[download_id])


@app.route('/yt/file/<download_id>')
def yt_download_file(download_id):
    if download_id not in yt_progress:
        return jsonify({"error": "Download nao encontrado"}), 404

    prog = yt_progress[download_id]
    if not prog.get('done') or prog.get('status') == 'error':
        return jsonify({"error": "Download ainda nao concluido"}), 400

    file_path = prog.get('file_path')
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "Arquivo nao encontrado"}), 404

    return send_file(
        file_path,
        as_attachment=True,
        download_name=prog.get('file_name', 'video.mp4')
    )


if __name__ == '__main__':
    print("=" * 50)
    print("  Clipador - Cortador de Videos Automatico")
    print("  Acesse: http://localhost:5000")
    print("=" * 50)
    if not FFMPEG_PATH:
        print("  AVISO: FFmpeg nao encontrado!")
        print("  Analise de audio sera baseada em movimento visual.")
        print("  Para melhor precisao, instale FFmpeg.")
    print("=" * 50)
    app.run(debug=True, host='0.0.0.0', port=5000)
