"""
Gemini AI Analyzer for Intelligent Video Cuts
Uses Google Gemini API to analyze full transcript context and extract
high-retention viral clips with narrative arc (setup, body, punchline/conclusion).
"""

import json
import logging
import urllib.request
import urllib.error
import re
import os

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def get_config():
    """Loads configuration from config.json."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read config.json: {e}")
    return {}


def save_config(cfg):
    """Saves configuration to config.json."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Failed to save config.json: {e}")
        return False


def resolve_model(model):
    """Maps deprecated model names to active working models."""
    if not model or "2.0" in model or "1.5" in model or "2.5" in model or "3.6" in model:
        return "gemini-3.7-flash"
    return model


# Direct HTTP opener bypassing Windows system proxy auto-discovery delays
HTTP_CLIENT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def test_gemini_key(api_key, model="gemini-3.7-flash"):
    """
    Tests if a Gemini API key is valid with a minimal prompt.
    Returns (success: bool, message: str).
    """
    if not api_key or not api_key.strip():
        return False, "Chave de API não informada."

    api_key = api_key.strip()
    active_model = resolve_model(model)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{active_model}:generateContent?key={api_key}"
    
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": "Responda apenas 'OK'."}
                ]
            }
        ]
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    try:
        with HTTP_CLIENT.open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return True, f"Conexão bem-sucedida com {active_model}!"
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        # Try fallback if model was not found
        if e.code == 404 and active_model != "gemini-flash-latest":
            return test_gemini_key(api_key, model="gemini-flash-latest")
        try:
            err_json = json.loads(err_msg)
            message = err_json.get("error", {}).get("message", f"HTTP {e.code}")
        except Exception:
            message = f"Erro HTTP {e.code}"
        return False, f"Falha na validação: {message}"
    except Exception as e:
        return False, f"Erro de conexão: {e}"


def align_cut_to_sentences(raw_cut, sentences, total_duration, min_duration=30.0, max_duration=120.0):
    """
    Guarantees that a cut starts EXACTLY at the beginning of the hook sentence (not after it),
    includes host question if applicable, and ends at a natural sentence conclusion.
    """
    from core.hook_analyzer import normalize_text

    raw_start = float(raw_cut.get("start", 0))
    raw_end = float(raw_cut.get("end", 0))
    hook = raw_cut.get("hook", "").strip()
    title = raw_cut.get("title", "").strip()
    sid = raw_cut.get("start_id")
    eid = raw_cut.get("end_id")

    best_start_idx = None

    # Case A: Gemini returned valid start_id
    if sid is not None and isinstance(sid, (int, str)):
        try:
            s_int = int(sid)
            if 0 <= s_int < len(sentences):
                best_start_idx = s_int
        except Exception:
            pass

    # Case B: Locate by hook keyword matching in transcription around raw_start
    if best_start_idx is None:
        norm_hook = normalize_text(hook or title)
        hook_words = [w for w in norm_hook.split() if len(w) > 3]

        if hook_words:
            best_match_count = 0
            # Search within a generous window around raw_start (-60s to +15s)
            nearby_indices = [
                i for i, s in enumerate(sentences)
                if (raw_start - 60) <= s["start"] <= (raw_start + 15)
            ]
            for i in nearby_indices:
                s = sentences[i]
                s_norm = normalize_text(s["text"])
                matches = sum(1 for w in hook_words if w in s_norm)
                if matches > best_match_count:
                    best_match_count = matches
                    best_start_idx = i

    # Case C: Fallback to closest sentence start
    if best_start_idx is None:
        best_start_idx = min(range(len(sentences)), key=lambda i: abs(sentences[i]["start"] - raw_start))

    # Dialogue context expansion: If the immediately preceding sentence is a question under 8.5s, include it!
    if best_start_idx > 0:
        prev_s = sentences[best_start_idx - 1]
        prev_dur = prev_s["end"] - prev_s["start"]
        if "?" in prev_s["text"] and prev_dur <= 8.5 and (sentences[best_start_idx]["start"] - prev_s["end"]) < 2.5:
            best_start_idx = best_start_idx - 1

    corrected_start = max(0.0, sentences[best_start_idx]["start"] - 0.35)

    # Find ending sentence
    best_end_idx = None
    if eid is not None and isinstance(eid, (int, str)):
        try:
            e_int = int(eid)
            if best_start_idx < e_int < len(sentences):
                best_end_idx = e_int
        except Exception:
            pass

    if best_end_idx is None:
        end_candidates = [
            i for i, s in enumerate(sentences)
            if s["end"] >= (corrected_start + min_duration) and s["end"] <= (corrected_start + max_duration)
        ]
        if end_candidates:
            best_end_idx = min(end_candidates, key=lambda i: abs(sentences[i]["end"] - raw_end))
        else:
            best_end_idx = min(range(len(sentences)), key=lambda i: abs(sentences[i]["end"] - (corrected_start + 60)))

    corrected_end = min(total_duration, sentences[best_end_idx]["end"] + 0.4)
    dur = round(corrected_end - corrected_start, 1)

    return {
        "start": round(corrected_start, 2),
        "end": round(corrected_end, 2),
        "duration": dur,
        "title": title or "CORTE VIRAL",
        "hook": hook or sentences[best_start_idx]["text"],
        "hook_type": raw_cut.get("hook_type", "Destaque IA"),
        "score": int(raw_cut.get("score", 85)),
        "summary": raw_cut.get("summary", ""),
        "has_audio": True,
        "selected": True,
        "ai_curated": True
    }


def analyze_with_gemini(whisper_segments, total_duration, min_duration=35.0, max_duration=120.0, api_key=None, model="gemini-3.6-flash"):
    """
    Analyzes video transcript using Gemini to extract the best viral topic clips.
    Uses sentence-level reconstruction and automatic hook boundary alignment to guarantee
    that opening hooks are never cut off or chopped in half.
    """
    from core.hook_analyzer import reconstruct_sentences

    if not api_key:
        cfg = get_config()
        api_key = cfg.get("gemini_api_key")
        model = cfg.get("gemini_model", model)

    if not api_key or not api_key.strip():
        raise ValueError("Chave de API do Gemini não configurada.")

    api_key = api_key.strip()
    model = resolve_model(model)

    # 1. Reconstruct sentences to provide coherent thoughts
    sentences = reconstruct_sentences(whisper_segments)
    if not sentences:
        logger.warning("No valid sentences reconstructed from whisper segments.")
        return []

    for idx, s in enumerate(sentences):
        s["id"] = idx

    formatted_lines = []
    for s in sentences:
        m_s, sec_s = divmod(int(s['start']), 60)
        m_e, sec_e = divmod(int(s['end']), 60)
        formatted_lines.append(f"[ID:{s['id']:04d} | {m_s:02d}:{sec_s:02d}-{m_e:02d}:{sec_e:02d}]: {s['text']}")

    full_transcript_text = "\n".join(formatted_lines)
    # Cap safely to ~180k chars to prevent API 503 or overload
    if len(full_transcript_text) > 180000:
        full_transcript_text = full_transcript_text[:180000]

    system_prompt = f"""Você é um editor sênior de cortes virais para TikTok, Instagram Reels e YouTube Shorts.
Sua missão é selecionar de 10 a 20 dos MELHORES CORTES DESTE PODCAST (distribuídos pelo início, meio e fim).

REGRAS OBRIGATÓRIAS:
1. start_id: ID da sentença [ID:XXXX] onde o assunto ou história COMEÇA.
   - O PRIMEIRO SEGUNDO do corte DEVE conter o gancho, a pergunta instigante ou a declaração polêmica que prende a atenção.
   - NUNCA comece no meio de uma frase ou com vícios soltos ("e aí", "mas", "porque", "quando ele", "de 1889").
   - Se for uma resposta a uma pergunta importante do apresentador, comece no ID da PERGUNTA!
2. end_id: ID da sentença [ID:XXXX] onde a explicação, história ou punchline TERMINA.
   - O corte DEVE ser completo (começo, meio e fim). NUNCA corte a fala pela metade!
3. Duração: O intervalo entre start_id e end_id deve ficar entre {int(min_duration)}s e {int(max_duration)}s.
4. Título: Caixa alta, viral e intrigante (máximo 60 caracteres).
5. Hook: A frase exata falada na abertura dos primeiros segundos.
6. Score: Nota de 70 a 98 avaliando a probabilidade de viralizar.

Retorne EXCLUSIVAMENTE um array JSON:
[
  {{
    "start_id": 142,
    "end_id": 147,
    "title": "TÍTULO VIRAL",
    "hook": "Frase falada na abertura",
    "hook_type": "Polêmica / Revelação / História / Pergunta",
    "score": 95,
    "summary": "Resumo do que acontece no corte"
  }}
]"""

    user_prompt = f"Aqui estão as sentenças numeradas de todo o podcast (duração total: {total_duration:.1f}s):\n\n{full_transcript_text}"

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": user_prompt}]}
        ],
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json"
        }
    }

    fallback_chain = [model, "gemini-3.7-flash", "gemini-flash-latest", "gemini-3.5-flash"]
    seen = set()
    models_to_try = [m for m in fallback_chain if not (m in seen or seen.add(m))]

    data = None
    model_used = None
    last_err = ""

    for curr_model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{curr_model}:generateContent?key={api_key}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        try:
            logger.info(f"Analyzing transcript with Gemini AI ({curr_model})...")
            with HTTP_CLIENT.open(req, timeout=85) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                model_used = curr_model
                logger.info(f"Gemini model {curr_model} succeeded!")
                break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            last_err = f"HTTP {e.code}: {err_body[:120]}"
            logger.warning(f"Gemini model {curr_model} failed ({last_err}), attempting next fallback...")
        except Exception as e:
            last_err = str(e)
            logger.warning(f"Gemini model {curr_model} connection failed ({last_err}), attempting next fallback...")

    if not data:
        logger.error(f"All Gemini models in fallback chain failed. Last error: {last_err}")
        raise RuntimeError(f"Erro na API do Gemini (todos os modelos esgotados): {last_err}")

    try:
        candidate_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError("A resposta do Gemini veio vazia ou sem texto.")

    cleaned_json = candidate_text.strip()
    if cleaned_json.startswith("```"):
        cleaned_json = re.sub(r"^```(?:json)?\s*", "", cleaned_json)
        cleaned_json = re.sub(r"\s*```$", "", cleaned_json)

    try:
        raw_cuts = json.loads(cleaned_json)
    except Exception as e:
        logger.error(f"Failed to parse Gemini JSON output: {e}\nRaw: {cleaned_json[:500]}")
        raise RuntimeError("O Gemini não retornou um JSON válido.")

    if not isinstance(raw_cuts, list):
        if isinstance(raw_cuts, dict) and "cuts" in raw_cuts:
            raw_cuts = raw_cuts["cuts"]
        else:
            raw_cuts = [raw_cuts]

    # Align each cut to true sentence and hook boundaries
    aligned_cuts = []
    used_spans = []

    for c in raw_cuts:
        aligned = align_cut_to_sentences(c, sentences, total_duration, min_duration=min_duration, max_duration=max_duration)
        if aligned["duration"] < 20 or aligned["duration"] > 140:
            continue

        # Prevent high overlap (> 50%)
        overlap = False
        for us, ue in used_spans:
            intersection = max(0, min(aligned["end"], ue) - max(aligned["start"], us))
            if intersection > (aligned["duration"] * 0.5):
                overlap = True
                break
        if overlap:
            continue

        used_spans.append((aligned["start"], aligned["end"]))
        aligned_cuts.append(aligned)

    aligned_cuts.sort(key=lambda x: x["score"], reverse=True)
    for idx, c in enumerate(aligned_cuts):
        c["id"] = idx + 1
        c["full_text"] = c.get("summary") or c.get("hook", "")
        c["ai_model"] = model_used
        c["engine"] = f"Gemini AI ({model_used})"

    logger.info(f"Gemini ({model_used}) generated {len(aligned_cuts)} perfectly aligned viral cuts!")
    return aligned_cuts
