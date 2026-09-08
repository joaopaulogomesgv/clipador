"""
Intelligent Hook & Topic Analyzer for Live Streams and Podcasts
Extracts cohesive, high-retention clips with strong hooks, complete thoughts,
and natural beginnings and endings.
"""

import re
import json
import logging
import unicodedata

logger = logging.getLogger(__name__)


def normalize_text(text):
    """Normalize text removing accents and casing for robust pattern matching."""
    if not text:
        return ""
    nfkd = unicodedata.normalize('NFKD', text)
    return "".join([c for c in nfkd if not unicodedata.combining(c)]).lower()


def clean_stutters(text):
    """Cleans repeated hallucinated words and punctuation from Whisper."""
    if not text:
        return ""
    # Remove repeated words like "é, é, é, é" or "não, não, não"
    cleaned = re.sub(r'\b([a-zA-Z\u00C0-\u00FF]+)(?:[,\s]+(?:\1\b)){2,}', r'\1', text, flags=re.IGNORECASE)
    # Remove repeated punctuation like ", , ," or ". . ."
    cleaned = re.sub(r'([,.-])(?:\s*\1)+', r'\1', cleaned)
    # Remove excess spaces
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()


def is_garbage_segment(text):
    """Detects Whisper hallucinations, foreign character leaks, or pure stutter loops."""
    t = (text or "").strip()
    if len(t) < 3:
        return True
    # Non-Latin characters (Chinese, Cyrillic, etc.) that Whisper hallucinates during background noise
    if re.search(r'[\u4e00-\u9fff\u0400-\u04ff]', t):
        return True
    # Stutters where almost all words are the same
    words = [w.lower() for w in re.findall(r'\b\w+\b', t)]
    if len(words) >= 4 and len(set(words)) <= 2:
        return True
    if len(words) >= 6:
        bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words)-1)]
        if len(set(bigrams)) <= len(bigrams) * 0.35:
            return True
    return False


# Words that should NEVER be the start of a standalone video clip
# (Dangling connectors, subordinate clauses, mid-sentence markers)
FORBIDDEN_STARTERS = {
    'e', 'mas', 'ai', 'ou', 'porque', 'pq', 'por isso', 'de', 'com', 'do', 'da', 
    'dos', 'das', 'no', 'na', 'nos', 'nas', 'em', 'que', 'se', 'pra', 'para',
    'como', 'ento', 'entao', 'tipo', 'alem', 'portanto', 'porem', 'contudo',
    'enfim', 'ou seja', 'ne', 'ta', 'sabe', 'era', 'eram', 'foram', 'tinha',
    'tambem', 'assim', 'onde', 'neste', 'nesta', 'disso', 'disse', 'pelo', 'pela'
}

# Fluff / filler chatter in live streams to avoid as clip openings
FILLER_STARTERS = [
    r'\b(olha o chat|deixa eu ver o chat|lendo o chat|manda um salve|manda abraco)\b',
    r'\b(ta me ouvindo|ta travando|ta mudo|som teste|testando som)\b',
    r'\b(compartilha a live|deixa o like|se inscreve no canal)\b',
    r'\b(vou ao banheiro|beber uma agua|espera ai|calma ai)\b',
]

# High-retention topic starter patterns (matched on normalized text)
TOPIC_PATTERNS = [
    # 1. Direct Questions & Audience Superchats (Highest retention)
    (r'\b(pergunta (do|da|de)|mandou aqui|superchat|duvida do|o pessoal perguntou)\b', 95, "Pergunta do Público"),
    (r'\b(voce (sabia|ja pensou|ja ouviu|acha|acredita|viu))\b', 92, "Curiosidade / Provocação"),
    (r'\b(qual (e|foi|seria) (o|a) (maior|melhor|pior|principal|diferenca|segredo|problema|desafio))\b', 95, "Pergunta Chave"),
    (r'\b(como (funciona|aconteceu|surgiu|que voce|foi feito|e possivel|se explica))\b', 90, "Como Funciona"),
    (r'\b(por que (o|a|os|as|voce|a gente|nao|tem|isso|acontece))\b', 90, "Por Que Acontece"),
    (r'\b(o que (e|acontece|aconteceria|significa|muda|causa|torna))\b', 88, "Explicação / Conceito"),
    (r'\b(tem esse bicho|tem como|existe algum|da pra saber|sera que)\b', 88, "Dúvida Intrigante"),

    # 2. Strong Claims & Revelations
    (r'\b(o maior (erro|perigo|desafio|acerto|misterio|segredo))\b', 94, "Declaração de Impacto"),
    (r'\b(a verdade sobre|o que ninguem (fala|conta|sabe|percebeu))\b', 95, "Revelação / Fato Oculto"),
    (r'\b(o problema (e que|de|da|do))\b', 88, "Problema Central"),
    (r'\b(esse papo de|essa historia de|essa conversa de)\b', 92, "Debate / Mito"),
    (r'\b(uma das (maiores|principais|coisas|evidencias|teorias|razoes))\b', 92, "Fato Marcante"),

    # 3. Storytelling & Anecdotes
    (r'\b(quando (eu comecai|aconteceu|o ser humano|a terra|o mundo))\b', 85, "História / Origem"),
    (r'\b(aconteceu (uma coisa|algo muito|um caso|um fato))\b', 90, "Caso Real"),
    (r'\b(teve um (caso|momento|dia|artigo|estudo|experimento))\b', 88, "Experimento / Estudo"),
    (r'\b(o caso do|a historia do|a historia da)\b', 86, "História"),
]

# Patterns that signal transition to another topic
TOPIC_TRANSITIONS = [
    r'\b(mas mudando de assunto|mas voltando|outro assunto|proxima pergunta)\b',
    r'\b(mas enfim|mas e isso|basicamente isso|e foi isso)\b',
    r'\b(vamos para o proximo|deixa eu te perguntar outra coisa)\b'
]


def reconstruct_sentences(segments):
    """
    Reconstructs complete sentences and dialogue turns from fragmented Whisper segments.
    """
    clean_segs = []
    for s in segments:
        if not is_garbage_segment(s.get("text", "")):
            txt = clean_stutters(s.get("text", ""))
            if txt:
                clean_segs.append({
                    "start": s["start"],
                    "end": s["end"],
                    "text": txt
                })

    if not clean_segs:
        return []

    sentences = []
    curr_text = []
    curr_start = None
    curr_end = None

    for i, seg in enumerate(clean_segs):
        if curr_start is None:
            curr_start = seg["start"]
        curr_text.append(seg["text"].strip())
        curr_end = seg["end"]

        next_pause = 0
        if i + 1 < len(clean_segs):
            next_pause = clean_segs[i + 1]["start"] - seg["end"]

        text_so_far = " ".join(curr_text).strip()
        ends_punct = bool(re.search(r'[.?!]["\']?\s*$', seg["text"].strip()))

        # Complete sentence if ends with punctuation, or substantial pause > 1.2s, or long enough
        if (ends_punct and len(text_so_far) > 20) or next_pause > 1.2 or len(text_so_far) > 160:
            sentences.append({
                "start": curr_start,
                "end": curr_end,
                "text": clean_stutters(text_so_far),
                "pause_after": round(next_pause, 2)
            })
            curr_text = []
            curr_start = None
            curr_end = None

    if curr_text and curr_start is not None:
        sentences.append({
            "start": curr_start,
            "end": curr_end,
            "text": clean_stutters(" ".join(curr_text).strip()),
            "pause_after": 0
        })

    return sentences


def check_starter(text):
    """
    Checks if a sentence qualifies as a high-retention, standalone topic starter.
    Returns (is_valid, score, topic_type, cleaned_text).
    """
    # Strip leading conversational tics like "É,", "Ah,", "Olha,", "Então,", "Aí,"
    cleaned = re.sub(r'^(?:[eéÉ]\b|ah\b|olha\b|veja\b|bom\b|ent[aã]o\b|a[ií]\b|mas\b|cara\b|fala,\s*\w+\b)[,.\s-]*', '', text, flags=re.IGNORECASE).strip(' ,.-:!?')
    if len(cleaned) < 10:
        return False, 0, "", text

    norm = normalize_text(cleaned).strip()
    words = norm.split()
    if not words:
        return False, 0, "", text

    # Filter out fillers
    for filler in FILLER_STARTERS:
        if re.search(filler, norm):
            return False, 0, "", text

    first_one = words[0]
    first_two = " ".join(words[:2]) if len(words) >= 2 else ""

    if first_one in FORBIDDEN_STARTERS or first_two in FORBIDDEN_STARTERS:
        return False, 0, "", text

    # Filter out stutters at start
    if len(words) >= 3 and len(set(words[:3])) == 1:
        return False, 0, "", text

    # Check against topic patterns
    for pat, score, t_type in TOPIC_PATTERNS:
        if re.search(pat, norm):
            return True, score, t_type, cleaned

    # High quality standalone question (ends with ? and has enough substance)
    if cleaned.endswith('?') and len(words) >= 5 and not norm.startswith(('ne', 'ta', 'certo', 'nao', 'sim')):
        return True, 80, "Pergunta Direta", cleaned

    return False, 0, "", text


def generate_title(first_sentence, topic_type):
    """
    Generates a clean, punchy title for the cut.
    """
    # Clean conversational lead-ins
    cleaned = re.sub(r'^(?:[eéÉ]\b|olha\b|veja\b|cara\b|bom\b|ent[aã]o\b|a[ií]\b|mas\b|fala,\s*\w+\b|tem a pergunta (?:do|da|de)\s*\w+\b|o\s*\w+\s*perguntou)[,.\s-]*', '', first_sentence, flags=re.IGNORECASE).strip(' ,.-:!?')
    
    # Grab the first clause
    clauses = re.split(r'[.?!]', cleaned)
    candidate = clauses[0].strip(' ,.-:!?') if clauses and clauses[0].strip(' ,.-:!?') else cleaned.strip(' ,.-:!?')
    
    if len(candidate) > 8:
        title = candidate[0].upper() + candidate[1:]
        if len(title) > 65:
            title = title[:62] + "..."
        return title
    
    return f"{topic_type}: Destaque"


def find_smart_cuts(whisper_segments, total_duration, min_duration=40.0, max_duration=120.0, min_score=45):
    """
    Finds cohesive, high-retention topic clips with complete thoughts and strong hooks.
    """
    if not whisper_segments:
        logger.warning("No whisper segments provided to find_smart_cuts")
        return []

    # 1. Reconstruct sentences
    sentences = reconstruct_sentences(whisper_segments)
    if not sentences:
        return []

    cuts = []
    used_spans = []

    for i, sent in enumerate(sentences):
        t_start = sent["start"]

        # Avoid overlapping starts within 20 seconds
        if any(abs(t_start - u[0]) < 20.0 for u in used_spans):
            continue

        is_valid, score, topic_type, cleaned_first = check_starter(sent["text"])
        if not is_valid:
            continue

        # Collect subsequent sentences until a complete thought / duration window
        collected = [sent["text"]]
        cut_end = sent["end"]

        for j in range(i + 1, len(sentences)):
            s_next = sentences[j]
            dur = s_next["end"] - t_start
            collected.append(s_next["text"])
            cut_end = s_next["end"]

            norm_next = normalize_text(s_next["text"])
            is_transition = bool(re.search(r'\b(mas mudando|mas voltando|outro assunto|proxima pergunta|enfim|basicamente isso)\b', norm_next))

            # Stop when minimum duration is reached and we hit a natural pause/transition
            if dur >= min_duration:
                if is_transition or s_next["pause_after"] > 1.3 or dur >= max_duration:
                    break

        actual_dur = round(cut_end - t_start, 1)
        if actual_dur < min_duration:
            continue

        # Cap max duration on sentence boundaries
        if actual_dur > max_duration + 5.0:
            actual_dur = round(cut_end - t_start, 1)

        full_body = clean_stutters(" ".join(collected))
        title = generate_title(cleaned_first, topic_type)

        cuts.append({
            "start": round(t_start, 2),
            "end": round(cut_end, 2),
            "duration": actual_dur,
            "title": title,
            "hook": cleaned_first[:110],
            "hook_type": topic_type,
            "score": score,
            "full_text": full_body[:250] + ("..." if len(full_body) > 250 else ""),
            "has_audio": True,
            "selected": (score >= min_score)
        })
        used_spans.append((t_start, cut_end))

    # Fallback if no specific hook patterns matched: create cohesive paragraph blocks
    if not cuts:
        logger.info("No candidates matched hook filters, creating cohesive sentence blocks")
        curr_block = []
        block_start = None
        for s in sentences:
            if block_start is None:
                block_start = s["start"]
            curr_block.append(s["text"])
            dur = s["end"] - block_start
            if dur >= min_duration and (s["pause_after"] > 1.0 or dur >= max_duration):
                cuts.append({
                    "start": round(block_start, 2),
                    "end": round(s["end"], 2),
                    "duration": round(s["end"] - block_start, 1),
                    "title": f"Destaque ({round(block_start)}s)",
                    "hook": curr_block[0][:90],
                    "hook_type": "Momento Geral",
                    "score": 60,
                    "full_text": " ".join(curr_block)[:250] + "...",
                    "has_audio": True,
                    "selected": True
                })
                curr_block = []
                block_start = None

    # Sort by score descending (best viral hooks first)
    cuts.sort(key=lambda x: x["score"], reverse=True)

    # Reassign clean IDs
    for idx, c in enumerate(cuts):
        c["id"] = idx + 1

    logger.info(f"Generated {len(cuts)} intelligent topic cuts")
    return cuts
