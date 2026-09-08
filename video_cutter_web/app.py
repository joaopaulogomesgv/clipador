import os
import sys

# Ensure video_cutter_web directory is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

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
export_progress = {}
export_threads = {}


def find_ffmpeg():
    candidates = [
        "ffmpeg",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"),
    ]

    # Search winget install locations
    winget_base = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Links')
    if os.path.isdir(winget_base):
        winget_ffmpeg = os.path.join(winget_base, 'ffmpeg.exe')
        if os.path.exists(winget_ffmpeg):
            candidates.insert(0, winget_ffmpeg)

    # Search common winget package paths
    winget_packages = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Packages')
    if os.path.isdir(winget_packages):
        for root, dirs, files in os.walk(winget_packages):
            if 'ffmpeg.exe' in files:
                candidates.insert(0, os.path.join(root, 'ffmpeg.exe'))
                break

    # Also search in PATH-accessible tools directory
    tools_dir = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Links')
    if os.path.isdir(tools_dir):
        possible = os.path.join(tools_dir, 'ffmpeg.exe')
        if os.path.exists(possible):
            candidates.insert(0, possible)

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
            cmd = [FFMPEG_PATH, "-i", video_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
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


def _ensure_video(video_id):
    if video_id in videos and videos[video_id].get("path") and os.path.exists(videos[video_id]["path"]):
        if not videos[video_id].get("info"):
            videos[video_id]["info"] = get_video_info(videos[video_id]["path"])
        return videos[video_id]

    upload_folder = app.config['UPLOAD_FOLDER']
    for ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm']:
        p = os.path.join(upload_folder, f"{video_id}{ext}")
        if os.path.exists(p):
            info = get_video_info(p)
            cuts = []
            cuts_path = os.path.join(upload_folder, f"{video_id}_cuts.json")
            if os.path.exists(cuts_path):
                try:
                    with open(cuts_path, 'r', encoding='utf-8') as f:
                        cuts = json.load(f)
                except Exception as e:
                    logger.warning(f"Failed to load cuts for {video_id}: {e}")

            videos[video_id] = {
                "id": video_id,
                "path": p,
                "filename": f"{video_id}{ext}",
                "info": info,
                "cuts": cuts
            }
            return videos[video_id]
    return None


@app.route('/api/video/<video_id>', methods=['GET'])
def get_video_details(video_id):
    video = _ensure_video(video_id)
    if not video:
        return jsonify({"error": "Vídeo não encontrado"}), 404

    trans_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{video_id}_transcription.json")
    export_dir = os.path.join(app.config['EXPORT_FOLDER'], video_id)
    zip_path = os.path.join(export_dir, "cortes.zip")

    return jsonify({
        "video_id": video_id,
        "filename": video.get("filename", f"{video_id}.mp4"),
        "info": video.get("info", {}),
        "cuts": video.get("cuts", []),
        "has_transcription": os.path.exists(trans_path),
        "has_export": os.path.exists(zip_path)
    })


@app.route('/api/videos/latest', methods=['GET'])
def get_latest_video():
    upload_folder = app.config['UPLOAD_FOLDER']
    candidates = []
    if os.path.exists(upload_folder):
        for f in os.listdir(upload_folder):
            if any(f.endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm']) and not f.startswith('temp_'):
                full_p = os.path.join(upload_folder, f)
                vid = os.path.splitext(f)[0]
                candidates.append((os.path.getmtime(full_p), vid))

    if not candidates:
        return jsonify({"error": "Nenhum vídeo disponível"}), 404

    candidates.sort(key=lambda x: x[0], reverse=True)
    latest_id = candidates[0][1]
    return get_video_details(latest_id)


@app.route('/api/video/<video_id>/cuts/clear', methods=['POST'])
def clear_video_cuts(video_id):
    video = _ensure_video(video_id)
    if not video:
        return jsonify({"error": "Vídeo não encontrado"}), 404

    # 1. Clear in-memory cuts
    videos[video_id]["cuts"] = []

    # 2. Remove cuts JSON from disk
    cuts_json_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{video_id}_cuts.json")
    if os.path.exists(cuts_json_path):
        try:
            os.remove(cuts_json_path)
        except Exception as e:
            logger.warning(f"Error removing cuts file {cuts_json_path}: {e}")

    # 3. Clear exported clips and zip
    export_dir = os.path.join(app.config['EXPORT_FOLDER'], video_id)
    if os.path.exists(export_dir):
        try:
            import shutil
            shutil.rmtree(export_dir)
            os.makedirs(export_dir, exist_ok=True)
        except Exception as e:
            logger.warning(f"Error clearing export dir {export_dir}: {e}")

    # 4. Reset export progress state
    if video_id in export_progress:
        del export_progress[video_id]

    return jsonify({"success": True, "message": "Todos os cortes e exportações foram removidos com sucesso!"})


@app.route('/api/video/<video_id>/cuts/delete', methods=['POST'])
def delete_video_cuts(video_id):
    video = _ensure_video(video_id)
    if not video:
        return jsonify({"error": "Vídeo não encontrado"}), 404

    data = request.get_json() or {}
    ids_to_delete = data.get("ids", [])
    single_id = data.get("id")
    if single_id is not None:
        ids_to_delete.append(single_id)

    if not ids_to_delete:
        return jsonify({"error": "Nenhum ID de corte informado para exclusão"}), 400

    ids_set = set(int(x) for x in ids_to_delete)
    current_cuts = video.get("cuts", [])
    remaining_cuts = [c for c in current_cuts if int(c.get("id", 0)) not in ids_set]

    # Re-index remaining cuts sequentially
    for idx, c in enumerate(remaining_cuts):
        c["id"] = idx + 1

    videos[video_id]["cuts"] = remaining_cuts

    # Save to disk
    cuts_json_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{video_id}_cuts.json")
    try:
        with open(cuts_json_path, 'w', encoding='utf-8') as f:
            json.dump(remaining_cuts, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Error saving updated cuts JSON: {e}")

    # Remove deleted individual mp4 exports if present
    export_dir = os.path.join(app.config['EXPORT_FOLDER'], video_id)
    if os.path.exists(export_dir):
        for del_id in ids_set:
            fpath = os.path.join(export_dir, f"corte_{del_id:03d}.mp4")
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass
        # Remove old zip so next export regenerates fresh zip
        zip_path = os.path.join(export_dir, "cortes.zip")
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except Exception:
                pass

    return jsonify({"success": True, "remaining_count": len(remaining_cuts), "cuts": remaining_cuts})


@app.route('/api/video/<video_id>/exports/clear', methods=['POST'])
def clear_video_exports(video_id):
    export_dir = os.path.join(app.config['EXPORT_FOLDER'], video_id)
    if os.path.exists(export_dir):
        try:
            import shutil
            shutil.rmtree(export_dir)
            os.makedirs(export_dir, exist_ok=True)
        except Exception as e:
            logger.warning(f"Error clearing export dir: {e}")

    if video_id in export_progress:
        del export_progress[video_id]

    return jsonify({"success": True, "message": "Arquivos exportados descartados."})


@app.route('/video/<video_id>')
def serve_video(video_id):
    logger.debug(f"Serving video: {video_id}")
    video = _ensure_video(video_id)
    if not video:
        return "Video nao encontrado", 404

    video_path = video["path"]
    if not os.path.exists(video_path):
        return "Arquivo nao encontrado", 404

    ext = Path(video_path).suffix.lower()
    mimetype = MIMETYPE_MAP.get(ext, 'video/mp4')
    return send_file(video_path, mimetype=mimetype)


@app.route('/analyze/<video_id>', methods=['POST'])
def analyze_video(video_id):
    video = _ensure_video(video_id)
    if not video:
        return jsonify({"error": "Video nao encontrado"}), 404

    data = request.get_json() or {}
    silence_threshold = data.get("silence_threshold", -40)
    min_silence = data.get("min_silence", 2)
    scene_threshold = data.get("scene_threshold", 30)
    use_whisper = data.get("use_whisper", False)
    min_clip_duration = float(data.get("min_clip_duration", 40))
    max_clip_duration = float(data.get("max_clip_duration", 120))
    min_score = int(data.get("min_score", 50))

    analysis_id = str(uuid.uuid4())[:8]
    analysis_progress[analysis_id] = {
        'status': 'starting',
        'percent': 0,
        'step': 'Iniciando...',
        'done': False,
        'result': None,
        'error': None
    }

    video_path = video["path"]
    info = video.get("info")
    if not info:
        info = get_video_info(video_path)
        video["info"] = info
    total_duration = info.get("duration", 0) if info else 0

    thread = threading.Thread(
        target=_do_analyze,
        args=(analysis_id, video_id, video_path, total_duration,
              silence_threshold, min_silence, scene_threshold, use_whisper,
              min_clip_duration, max_clip_duration, min_score),
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


@app.route('/reanalyze_hooks/<video_id>', methods=['POST'])
def reanalyze_hooks(video_id):
    data = request.get_json() or {}
    min_duration = float(data.get("min_clip_duration", 40))
    max_duration = float(data.get("max_clip_duration", 120))
    min_score = int(data.get("min_score", 50))

    trans_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{video_id}_transcription.json")
    if not os.path.exists(trans_path):
        return jsonify({"error": "Transcrição não encontrada para este vídeo. Ative 'Usar Whisper (IA)' e analise o vídeo primeiro."}), 404

    try:
        with open(trans_path, 'r', encoding='utf-8') as f:
            whisper_segments = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Erro ao ler transcrição: {e}"}), 500

    video = videos.get(video_id, {})
    total_duration = video.get("info", {}).get("duration", 0)
    if not total_duration and whisper_segments:
        total_duration = whisper_segments[-1]["end"]

    cuts = generate_smart_cuts(whisper_segments, total_duration, min_duration=min_duration, max_duration=max_duration, min_score=min_score)

    if video_id in videos:
        videos[video_id]["cuts"] = cuts

    cuts_json_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{video_id}_cuts.json")
    try:
        with open(cuts_json_path, 'w', encoding='utf-8') as f:
            json.dump(cuts, f)
    except Exception as e:
        logger.warning(f"Failed to persist cuts: {e}")

    return jsonify({"success": True, "cuts": cuts, "count": len(cuts)})


@app.route('/api/config', methods=['GET'])
def api_get_config():
    from core.gemini_analyzer import get_config
    cfg = get_config()
    raw_key = cfg.get("gemini_api_key", "")
    masked = ""
    if raw_key:
        masked = raw_key[:6] + "..." + raw_key[-4:] if len(raw_key) > 10 else "******"
    return jsonify({
        "use_gemini": cfg.get("use_gemini", True),
        "gemini_model": cfg.get("gemini_model", "gemini-3.6-flash"),
        "has_key": bool(raw_key),
        "masked_key": masked,
        "gemini_api_key": raw_key
    })


@app.route('/api/config', methods=['POST'])
def api_save_config():
    from core.gemini_analyzer import get_config, save_config
    data = request.get_json() or {}
    cfg = get_config()
    if "use_gemini" in data:
        cfg["use_gemini"] = bool(data["use_gemini"])
    if "gemini_model" in data:
        cfg["gemini_model"] = str(data["gemini_model"])
    if "gemini_api_key" in data and data["gemini_api_key"].strip():
        cfg["gemini_api_key"] = str(data["gemini_api_key"]).strip()
    save_config(cfg)
    return jsonify({"success": True, "message": "Configurações salvas com sucesso!"})


@app.route('/api/config/test_gemini', methods=['POST'])
def api_test_gemini():
    from core.gemini_analyzer import test_gemini_key, get_config
    data = request.get_json() or {}
    api_key = data.get("gemini_api_key", "").strip()
    if not api_key:
        cfg = get_config()
        api_key = cfg.get("gemini_api_key", "")
    model = data.get("gemini_model", "gemini-3.6-flash")
    success, msg = test_gemini_key(api_key, model=model)
    return jsonify({"success": success, "message": msg})


def generate_smart_cuts(whisper_segments, total_duration, min_duration=40.0, max_duration=120.0, min_score=50):
    """
    Tries Gemini context analysis first if key is configured,
    otherwise uses intelligent local rule-based topic segmenter.
    """
    from core.gemini_analyzer import get_config, analyze_with_gemini
    from core.hook_analyzer import find_smart_cuts

    cfg = get_config()
    use_gemini = cfg.get("use_gemini", True)
    gemini_key = cfg.get("gemini_api_key", "").strip()
    gemini_model = cfg.get("gemini_model", "gemini-2.0-flash")

    if use_gemini and gemini_key:
        try:
            logger.info(f"Analyzing transcript with Gemini AI ({gemini_model})...")
            cuts = analyze_with_gemini(
                whisper_segments,
                total_duration,
                min_duration=min_duration,
                max_duration=max_duration,
                api_key=gemini_key,
                model=gemini_model
            )
            if cuts:
                logger.info(f"Gemini successfully returned {len(cuts)} intelligent cuts")
                return cuts
        except Exception as e:
            logger.warning(f"Gemini analysis failed ({e}), falling back to local hook analyzer")

    logger.info("Using local smart hook & topic analyzer...")
    return find_smart_cuts(
        whisper_segments,
        total_duration,
        min_duration=min_duration,
        max_duration=max_duration,
        min_score=min_score
    )


def _format_elapsed(seconds):
    """Format seconds into human-readable duration string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m = seconds // 60
        s = seconds % 60
        return f"{m}m {s:02d}s"
    else:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h}h {m:02d}m {s:02d}s"


def _do_analyze(analysis_id, video_id, video_path, total_duration,
                silence_threshold, min_silence, scene_threshold, use_whisper=False,
                min_clip_duration=40.0, max_clip_duration=120.0, min_score=50):
    try:
        prog = analysis_progress[analysis_id]
        analysis_start = time.time()
        trans_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{video_id}_transcription.json")
        whisper_segments = []

        # FAST PATH: If Whisper is enabled and transcription is ALREADY cached, skip all heavy processing!
        if use_whisper and os.path.exists(trans_path) and os.path.getsize(trans_path) > 100:
            prog.update({'percent': 20, 'step': 'Reaproveitando transcrição em cache... (0.05s)'})
            try:
                with open(trans_path, 'r', encoding='utf-8') as f:
                    whisper_segments = json.load(f)
                logger.info(f"Loaded {len(whisper_segments)} cached segments instantly")
            except Exception as e:
                logger.warning(f"Failed to read cached transcription: {e}")
                whisper_segments = []

            if whisper_segments:
                prog.update({'percent': 50, 'step': f'Analisando ganchos e temas com IA ({len(whisper_segments)} falas)...'})
                cuts = generate_smart_cuts(
                    whisper_segments,
                    total_duration,
                    min_duration=min_clip_duration,
                    max_duration=max_clip_duration,
                    min_score=min_score
                )
                videos[video_id]["cuts"] = cuts
                try:
                    cuts_json_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{video_id}_cuts.json")
                    with open(cuts_json_path, 'w', encoding='utf-8') as f:
                        json.dump(cuts, f)
                except Exception as e:
                    logger.warning(f"Failed to auto-save cuts JSON: {e}")

                total_elapsed = _format_elapsed(time.time() - analysis_start)
                result = {
                    "total_duration": total_duration,
                    "audio_segments": [],
                    "scene_timestamps": [],
                    "silent_regions": [],
                    "whisper_segments": whisper_segments[:50],
                    "cuts": cuts
                }
                prog.update({
                    'status': 'done',
                    'percent': 100,
                    'step': f'{len(cuts)} cortes inteligentes identificados em {total_elapsed}! (Cache Whisper)',
                    'done': True,
                    'result': result
                })
                logger.info(f"Fast-path analysis completed in {total_elapsed} ({len(cuts)} cuts)")
                return

        # SLOW PATH: If no cache or not using whisper
        step_start = time.time()
        prog.update({'status': 'running', 'percent': 10, 'step': 'Extraindo áudio do vídeo...'})
        temp_audio = None
        if FFMPEG_PATH:
            temp_audio = os.path.join(tempfile.gettempdir(), f"temp_audio_{video_id}.wav")
            extract_audio(video_path, temp_audio)
            file_size = os.path.getsize(temp_audio) if os.path.exists(temp_audio) else 0
            elapsed = _format_elapsed(time.time() - step_start)
            logger.info(f"Audio extracted: {file_size} bytes in {elapsed}")
            prog.update({'percent': 20, 'step': f'Áudio extraído em {elapsed}'})
        else:
            prog.update({'percent': 15, 'step': 'Sem FFmpeg - analisando movimento visual...'})

        audio_segments = []
        scene_timestamps = []
        silent_regions = []

        if not use_whisper:
            step_start = time.time()
            prog.update({'percent': 25, 'step': 'Analisando silêncio...'})
            if temp_audio and os.path.exists(temp_audio):
                silent_regions = analyze_silence(temp_audio, silence_threshold, min_silence)
            else:
                silent_regions = analyze_silence_opencv(video_path, silence_threshold, min_silence)

            elapsed = _format_elapsed(time.time() - step_start)
            prog.update({'percent': 45, 'step': f'{len(silent_regions)} regiões de silêncio encontradas em {elapsed}'})
            time.sleep(0.2)

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

            step_start = time.time()
            prog.update({'percent': 55, 'step': 'Detectando mudanças de cena...'})
            scene_timestamps = detect_scenes(video_path, scene_threshold)
            elapsed = _format_elapsed(time.time() - step_start)
            prog.update({'percent': 70, 'step': f'{len(scene_timestamps)} cenas detectadas em {elapsed}'})

        if use_whisper and not whisper_segments:
            if temp_audio and os.path.exists(temp_audio):
                prog.update({'percent': 30, 'step': 'Carregando modelo Whisper...'})
                whisper_done = threading.Event()
                whisper_start = [None]

                def _whisper_monitor():
                    estimated_total = max(30, total_duration / 12)
                    while not whisper_done.is_set():
                        if whisper_start[0] is None:
                            whisper_done.wait(1)
                            continue
                        elapsed = time.time() - whisper_start[0]
                        elapsed_str = _format_elapsed(elapsed)

                        whisper_pct = min(95, (elapsed / estimated_total) * 100)
                        overall_pct = 30 + int(whisper_pct * 0.45)

                        if elapsed < estimated_total:
                            remaining = estimated_total - elapsed
                            remaining_str = _format_elapsed(remaining)
                            step = f'Whisper transcrevendo... ⏱ {elapsed_str} | ~{remaining_str} restantes'
                        else:
                            step = f'Whisper transcrevendo... ⏱ {elapsed_str} (quase pronto...)'

                        prog.update({'percent': overall_pct, 'step': step})
                        whisper_done.wait(3)

                monitor = threading.Thread(target=_whisper_monitor, daemon=True)
                monitor.start()

                whisper_start[0] = time.time()
                whisper_segments = transcribe_with_whisper(temp_audio, model_size="tiny")
                whisper_elapsed = time.time() - whisper_start[0]
                whisper_done.set()

                elapsed_str = _format_elapsed(whisper_elapsed)
                prog.update({
                    'percent': 80,
                    'step': f'Whisper concluído em {elapsed_str}: {len(whisper_segments)} falas. Analisando ganchos...'
                })

                # Save full transcription permanently to disk
                try:
                    with open(trans_path, 'w', encoding='utf-8') as f:
                        json.dump(whisper_segments, f)
                except Exception as e:
                    logger.warning(f"Failed to persist transcription: {e}")


        if temp_audio and os.path.exists(temp_audio):
            os.remove(temp_audio)

        prog.update({'percent': 85, 'step': 'Curando os melhores cortes com alta retencao...'})
        time.sleep(0.1)

        if use_whisper and whisper_segments:
            cuts = generate_smart_cuts(
                whisper_segments,
                total_duration,
                min_duration=min_clip_duration,
                max_duration=max_clip_duration,
                min_score=min_score
            )
        else:
            cuts = combine_results(audio_segments, scene_timestamps, total_duration)
            for c in cuts:
                c["title"] = f"Corte {c['id']:02d}"
                c["hook"] = "Corte baseado em silêncio e troca de cena"
                c["hook_type"] = "Cena / Pausa"
                c["score"] = 60 if c.get("has_audio") else 20

        videos[video_id]["cuts"] = cuts
        # Automatically persist cuts to disk
        try:
            cuts_json_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{video_id}_cuts.json")
            with open(cuts_json_path, 'w', encoding='utf-8') as f:
                json.dump(cuts, f)
        except Exception as e:
            logger.warning(f"Failed to auto-save cuts JSON: {e}")

        result = {
            "total_duration": total_duration,
            "audio_segments": audio_segments,
            "scene_timestamps": scene_timestamps,
            "silent_regions": silent_regions,
            "whisper_segments": whisper_segments[:50] if whisper_segments else [],
            "cuts": cuts
        }

        total_elapsed = _format_elapsed(time.time() - analysis_start)
        prog.update({
            'status': 'done',
            'percent': 100,
            'step': f'Concluido em {total_elapsed}! {len(cuts)} cortes encontrados.',
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
        "-ar", "16000", "-ac", "1",
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
            samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32)
        elif sample_width == 4:
            samples = np.frombuffer(raw_data, dtype=np.int32).astype(np.float32)
        else:
            samples = (np.frombuffer(raw_data, dtype=np.uint8).astype(np.float32) - 128) * 256

        if n_channels > 1:
            samples = samples.reshape(-1, n_channels).mean(axis=1)

        max_val = np.max(np.abs(samples))
        if max_val > 0:
            samples = samples / max_val

        chunk_size = int(frame_rate * 0.1)
        n_chunks = len(samples) // chunk_size
        if n_chunks > 0:
            reshaped = samples[:n_chunks * chunk_size].reshape(n_chunks, chunk_size)
            rms = np.sqrt(np.mean(reshaped ** 2, axis=1))
            rms = np.maximum(rms, 1e-10)
            db_levels_arr = 20 * np.log10(rms)
            time_arr = np.arange(n_chunks) * 0.1
            db_levels = list(zip(time_arr, db_levels_arr))
        else:
            db_levels = []

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
        frame_pos = 0

        while frame_pos < total_frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_pos)
            ret, frame = cap.read()
            if not ret:
                break

            small = cv2.resize(frame, (64, 48))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

            if prev_gray is not None:
                diff = cv2.absdiff(prev_gray, gray)
                motion = np.mean(diff)
                db = -100 + min(motion * 3, 100)
                time_s = frame_pos / fps
                audio_levels.append((time_s, db))

            prev_gray = gray
            frame_pos += sample_interval

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


def detect_scenes_ffmpeg(video_path, threshold=30.0):
    """Fast scene detection using FFmpeg's select filter."""
    if not FFMPEG_PATH:
        return None
    try:
        scene_val = max(0.1, min(1.0, threshold / 100.0))
        cmd = [
            FFMPEG_PATH, "-i", video_path,
            "-vf", f"scale=320:-1,select='gt(scene,{scene_val})',showinfo",
            "-vsync", "vfr",
            "-f", "null", "-"
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        scene_changes = []
        for line in result.stderr.split("\n"):
            if "pts_time:" in line:
                try:
                    pts_str = line.split("pts_time:")[1].split()[0]
                    timestamp = float(pts_str)
                    if not scene_changes or (timestamp - scene_changes[-1]) >= 5.0:
                        scene_changes.append(round(timestamp, 2))
                except (ValueError, IndexError):
                    pass
        logger.info(f"FFmpeg scene detection found {len(scene_changes)} scenes")
        return scene_changes
    except Exception as e:
        logger.warning(f"FFmpeg scene detection failed: {e}")
        return None


def detect_scenes(video_path, threshold=30.0):
    # Try FFmpeg first (much faster)
    ffmpeg_result = detect_scenes_ffmpeg(video_path, threshold)
    if ffmpeg_result is not None:
        return ffmpeg_result

    # Fallback to OpenCV with optimized seeking
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        if fps == 0:
            cap.release()
            return []

        scene_changes = []
        prev_frame = None
        sample_interval = max(1, int(fps * 2))
        frame_pos = 0

        while frame_pos < total_frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_pos)
            ret, frame = cap.read()
            if not ret:
                break

            small = cv2.resize(frame, (64, 48))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

            if prev_frame is not None:
                diff = cv2.absdiff(prev_frame, gray)
                mean_diff = np.mean(diff)

                if mean_diff > threshold:
                    timestamp = frame_pos / fps
                    if not scene_changes or (timestamp - scene_changes[-1]) >= 5.0:
                        scene_changes.append(round(timestamp, 2))

            prev_frame = gray
            frame_pos += sample_interval

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


def transcribe_with_whisper(audio_path, model_size="tiny", progress_callback=None):
    try:
        import whisper
        import torch
        torch.set_num_threads(min(8, os.cpu_count() or 4))
        logger.info(f"Loading whisper model: {model_size}")
        model = whisper.load_model(model_size)
        logger.info("Transcribing audio...")

        result = model.transcribe(audio_path, language="pt", fp16=False, condition_on_previous_text=False, verbose=False)


        segments = []
        for seg in result.get("segments", []):
            segments.append({
                "start": round(seg["start"], 2),
                "end": round(seg["end"], 2),
                "text": seg["text"].strip()
            })

        logger.info(f"Whisper found {len(segments)} segments")
        return segments
    except Exception as e:
        logger.error(f"Whisper transcription failed: {e}")
        return []


def detect_topic_changes(segments, max_segment_duration=300, min_segment_duration=60):
    if not segments:
        return []

    topic_boundaries = [0]
    current_text = ""

    for i, seg in enumerate(segments):
        current_text += " " + seg["text"]

        if i > 0 and i % 10 == 0:
            prev_boundary = topic_boundaries[-1]
            elapsed = seg["end"] - prev_boundary

            if elapsed >= max_segment_duration:
                topic_boundaries.append(seg["end"])
                current_text = ""
                continue

            if elapsed < min_segment_duration:
                continue

            prev_text = ""
            for j in range(max(0, i-10), i):
                prev_text += " " + segments[j]["text"]

            curr_text = ""
            for j in range(i, min(len(segments), i+10)):
                curr_text += " " + segments[j]["text"]

            prev_words = set(prev_text.lower().split())
            curr_words = set(curr_text.lower().split())

            if prev_words and curr_words:
                overlap = len(prev_words & curr_words)
                total = len(prev_words | curr_words)
                similarity = overlap / total if total > 0 else 1.0

                if similarity < 0.5:
                    topic_boundaries.append(seg["end"])
                    current_text = ""

    return topic_boundaries


def combine_results_with_whisper(audio_segments, scene_timestamps, whisper_boundaries, total_duration):
    all_boundaries = set()
    for seg in audio_segments:
        all_boundaries.add(round(seg["start"], 2))
        all_boundaries.add(round(seg["end"], 2))
    for ts in scene_timestamps:
        all_boundaries.add(round(ts, 2))
    for ts in whisper_boundaries:
        all_boundaries.add(round(ts, 2))

    all_boundaries.add(0)
    all_boundaries.add(round(total_duration, 2))
    sorted_boundaries = sorted(all_boundaries)

    cuts = []
    for i in range(len(sorted_boundaries) - 1):
        start = sorted_boundaries[i]
        end = sorted_boundaries[i + 1]
        if end - start > 5.0:
            has_audio = any(
                seg["start"] <= start and seg["end"] >= end
                for seg in audio_segments
            )
            is_topic_change = any(
                abs(ts - start) < 1.0 for ts in whisper_boundaries
            )
            cuts.append({
                "id": len(cuts) + 1,
                "start": start,
                "end": end,
                "duration": round(end - start, 2),
                "has_audio": has_audio,
                "is_topic_change": is_topic_change,
                "selected": has_audio
            })

    merged = []
    for cut in cuts:
        if not merged:
            merged.append(cut)
            continue
        last = merged[-1]
        gap = cut["start"] - last["end"]
        is_same_type = last["has_audio"] == cut["has_audio"] and not cut.get("is_topic_change")
        if gap <= 2.0 and is_same_type and (last["duration"] + cut["duration"]) < 600:
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
        FFMPEG_PATH,
        "-ss", str(start_time),
        "-i", video_path,
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k",
        "-avoid_negative_ts", "make_zero",
        output_path, "-y"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        logger.error(f"FFmpeg cut error: {result.stderr}")
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


def _run_export_task(video_id, cuts, video_path, export_dir):
    prog = export_progress.get(video_id)
    if not prog:
        return

    total = len(cuts)
    exported_files = []
    completed_count = 0

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _export_single(cut):
            filename = f"corte_{cut['id']:03d}.mp4"
            output_path = os.path.join(export_dir, filename)
            # Reuse if already exported previously
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                logger.info(f"Reusing existing cut {cut['id']}: {filename}")
                return filename, cut
            logger.info(f"Exporting cut {cut['id']}: {cut['start']}s - {cut['end']}s")
            try:
                success = cut_video(video_path, cut["start"], cut["end"], output_path)
                if success and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    logger.info(f"Cut {cut['id']} exported: {filename}")
                    return filename, cut
                else:
                    logger.error(f"Cut {cut['id']} failed: success={success}")
            except Exception as e:
                logger.error(f"Cut {cut['id']} error: {e}")
            return None, cut

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_to_cut = {executor.submit(_export_single, cut): cut for cut in cuts}
            for future in as_completed(future_to_cut):
                res_filename, cut_info = future.result()
                completed_count += 1
                if res_filename:
                    exported_files.append(res_filename)
                    if res_filename not in prog["completed_files"]:
                        prog["completed_files"].append(res_filename)

                title = cut_info.get("title") or cut_info.get("hook") or f"Corte {cut_info['id']}"
                prog["current"] = completed_count
                prog["current_title"] = title
                prog["current_file"] = f"corte_{cut_info['id']:03d}.mp4"
                prog["percent"] = min(92, int((completed_count / total) * 90))

        if not exported_files:
            prog["status"] = "error"
            prog["error"] = "Nenhum corte foi exportado. Verifique se o FFmpeg está funcionando."
            return

        # Create zip if more than 1 file
        if len(exported_files) > 1:
            prog["current_title"] = "Compactando arquivos em pacote ZIP..."
            prog["percent"] = 96
            import zipfile
            zip_path = os.path.join(export_dir, "cortes.zip")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zf:
                for f in sorted(exported_files):
                    zf.write(os.path.join(export_dir, f), f)
            prog["zip_ready"] = True
        else:
            prog["zip_ready"] = False

        prog["current"] = total
        prog["percent"] = 100
        prog["status"] = "completed"
        prog["current_title"] = f"{len(exported_files)} cortes exportados com sucesso!"
    except Exception as e:
        logger.error(f"Export task error for {video_id}: {e}", exc_info=True)
        prog["status"] = "error"
        prog["error"] = str(e)


@app.route('/export/start/<video_id>', methods=['POST'])
def export_start(video_id):
    data = request.get_json() or {}
    selected_ids = data.get("selected_ids", [])
    client_cuts = data.get("cuts", [])

    video_path = None
    cuts = []

    # 1. Check in-memory videos
    if video_id in videos:
        video = videos[video_id]
        video_path = video.get("path")
        cuts = video.get("cuts", [])

    # 2. Check uploads folder
    if not video_path or not os.path.exists(video_path):
        for ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm']:
            p = os.path.join(app.config['UPLOAD_FOLDER'], f"{video_id}{ext}")
            if os.path.exists(p):
                video_path = p
                break

    if not video_path or not os.path.exists(video_path):
        return jsonify({"error": "Vídeo não encontrado no servidor"}), 404

    # 3. Restore cuts from client or saved json file
    if client_cuts:
        cuts = client_cuts
        cuts_json_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{video_id}_cuts.json")
        try:
            with open(cuts_json_path, 'w', encoding='utf-8') as f:
                json.dump(cuts, f)
        except Exception as e:
            logger.warning(f"Failed to persist cuts: {e}")
    elif not cuts:
        cuts_json_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{video_id}_cuts.json")
        if os.path.exists(cuts_json_path):
            try:
                with open(cuts_json_path, 'r', encoding='utf-8') as f:
                    cuts = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load cuts JSON: {e}")

    if video_id in videos:
        videos[video_id]["cuts"] = cuts
        if not videos[video_id].get("info"):
            videos[video_id]["info"] = get_video_info(video_path)
    else:
        videos[video_id] = {
            "id": video_id,
            "path": video_path,
            "filename": os.path.basename(video_path),
            "info": get_video_info(video_path),
            "cuts": cuts
        }

    if selected_ids:
        cuts = [c for c in cuts if c["id"] in selected_ids]

    if not cuts:
        return jsonify({"error": "Nenhum corte selecionado"}), 400

    export_dir = os.path.join(app.config['EXPORT_FOLDER'], video_id)
    os.makedirs(export_dir, exist_ok=True)

    # Check if already running
    cur = export_progress.get(video_id)
    if cur and cur.get("status") == "running":
        return jsonify({"status": "running", "video_id": video_id, "total": cur.get("total", len(cuts))})

    # Check if all already exist on disk
    all_exist = True
    existing_files = []
    for c in cuts:
        fpath = os.path.join(export_dir, f"corte_{c['id']:03d}.mp4")
        if os.path.exists(fpath) and os.path.getsize(fpath) > 1000:
            existing_files.append(f"corte_{c['id']:03d}.mp4")
        else:
            all_exist = False

    zip_path = os.path.join(export_dir, "cortes.zip")

    # If all requested cuts already exist as MP4s, generate a FRESH zip specifically with ONLY these requested cuts!
    if all_exist:
        if len(cuts) > 1:
            import zipfile
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zf:
                for f in sorted(existing_files):
                    zf.write(os.path.join(export_dir, f), f)

        export_progress[video_id] = {
            "status": "completed",
            "current": len(cuts),
            "total": len(cuts),
            "percent": 100,
            "current_title": f"{len(cuts)} corte(s) selecionado(s) pronto(s) para download!",
            "current_file": "",
            "completed_files": sorted(existing_files),
            "zip_ready": len(cuts) > 1,
            "error": None
        }
        return jsonify({"status": "completed", "video_id": video_id, "total": len(cuts), "already_done": True})

    # Remove any stale zip from previous exports so it's not served while rendering
    if os.path.exists(zip_path):
        try:
            os.remove(zip_path)
        except Exception:
            pass

    # Start fresh export task in background for missing cuts
    export_progress[video_id] = {
        "status": "running",
        "current": len(existing_files),
        "total": len(cuts),
        "percent": int((len(existing_files) / len(cuts)) * 90) if len(cuts) else 0,
        "current_title": f"Iniciando exportação de {len(cuts)} corte(s) selecionado(s)...",
        "current_file": "",
        "completed_files": sorted(existing_files),
        "zip_ready": False,
        "error": None
    }

    t = threading.Thread(
        target=_run_export_task,
        args=(video_id, cuts, video_path, export_dir),
        daemon=True
    )
    t.start()
    export_threads[video_id] = t

    return jsonify({"status": "started", "video_id": video_id, "total": len(cuts)})


@app.route('/export/status/<video_id>', methods=['GET'])
def export_status(video_id):
    prog = export_progress.get(video_id)
    if prog:
        return jsonify(prog)

    # Check filesystem if no memory state
    export_dir = os.path.join(app.config['EXPORT_FOLDER'], video_id)
    if os.path.exists(export_dir):
        zip_path = os.path.join(export_dir, "cortes.zip")
        if os.path.exists(zip_path) and os.path.getsize(zip_path) > 1000:
            import zipfile
            try:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zip_files = sorted(zf.namelist())
                    return jsonify({
                        "status": "completed",
                        "current": len(zip_files),
                        "total": len(zip_files),
                        "percent": 100,
                        "completed_files": zip_files,
                        "zip_ready": True,
                        "current_title": f"{len(zip_files)} cortes prontos para download",
                        "current_file": zip_files[-1] if zip_files else "",
                        "error": None
                    })
            except Exception:
                pass

        files = sorted([f for f in os.listdir(export_dir) if f.startswith('corte_') and f.endswith('.mp4') and os.path.getsize(os.path.join(export_dir, f)) > 1000])
        if files:
            return jsonify({
                "status": "completed" if len(files) == 1 else "idle",
                "current": len(files),
                "total": len(files),
                "percent": 100 if len(files) == 1 else 0,
                "completed_files": files,
                "zip_ready": False,
                "current_title": f"{len(files)} cortes disponíveis",
                "current_file": files[-1] if files else "",
                "error": None
            })

    return jsonify({
        "status": "idle",
        "current": 0,
        "total": 0,
        "percent": 0,
        "completed_files": [],
        "zip_ready": False,
        "current_title": "",
        "current_file": "",
        "error": None
    })


@app.route('/export/download/<video_id>', methods=['GET'])
def export_download(video_id):
    export_dir = os.path.join(app.config['EXPORT_FOLDER'], video_id)
    if not os.path.exists(export_dir):
        return jsonify({"error": "Exportação não encontrada"}), 404

    prog = export_progress.get(video_id)
    if prog and prog.get("total") == 1 and prog.get("completed_files"):
        single_name = prog["completed_files"][0]
        single_path = os.path.join(export_dir, single_name)
        if os.path.exists(single_path):
            return send_file(single_path, as_attachment=True, download_name=single_name)

    zip_path = os.path.join(export_dir, "cortes.zip")
    if os.path.exists(zip_path) and os.path.getsize(zip_path) > 1000:
        return send_file(zip_path, as_attachment=True, download_name="cortes.zip")

    files = sorted([f for f in os.listdir(export_dir) if f.startswith('corte_') and f.endswith('.mp4')])
    if len(files) == 1:
        return send_file(os.path.join(export_dir, files[0]), as_attachment=True, download_name=files[0])
    elif len(files) > 1:
        import zipfile
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(os.path.join(export_dir, f), f)
        return send_file(zip_path, as_attachment=True, download_name="cortes.zip")

    return jsonify({"error": "Nenhum arquivo para download"}), 404



@app.route('/export/file/<video_id>/<filename>', methods=['GET'])
def export_file(video_id, filename):
    export_dir = os.path.join(app.config['EXPORT_FOLDER'], video_id)
    safe_name = secure_filename(filename)
    path = os.path.join(export_dir, safe_name)
    if os.path.exists(path):
        return send_file(path, as_attachment=True, download_name=safe_name)
    return jsonify({"error": "Arquivo não encontrado"}), 404


@app.route('/export/<video_id>', methods=['POST'])
def export_cuts(video_id):
    # Synchronous backward compatible route
    start_resp = export_start(video_id)
    if start_resp.status_code != 200:
        return start_resp

    t = export_threads.get(video_id)
    if t and t.is_alive():
        t.join()

    prog = export_progress.get(video_id, {})
    if prog.get("status") == "error":
        return jsonify({"error": prog.get("error", "Erro na exportação")}), 500

    return export_download(video_id)



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
