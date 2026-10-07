import os
import sys
import json
import time
import requests
import subprocess
import glob
import re
import uuid
import firebase_admin
from firebase_admin import credentials, firestore, db
import pysubs2
from requests_toolbelt.multipart.encoder import MultipartEncoder
from faster_whisper import WhisperModel
import urllib.parse
import concurrent.futures
import random
from deep_translator import GoogleTranslator

# --- 🗣️ SPOKEN SINHALA DICTIONARY ---
try:
    from spoken_dict import SPOKEN_DICT
except ImportError:
    SPOKEN_DICT = {}

def apply_spoken_sinhala(text):
    if not text or not SPOKEN_DICT: return text
    sorted_keys = sorted(SPOKEN_DICT.keys(), key=len, reverse=True)
    result_text = str(text)
    for key in sorted_keys:
        value = SPOKEN_DICT[key]
        pattern = r'(?<![\w\u0D80-\u0DFF])' + re.escape(key) + r'(?![\w\u0D80-\u0DFF])'
        result_text = re.sub(pattern, value, result_text)
    return result_text

# --- ⚙️ SETUP FIREBASE ---
cred = credentials.Certificate("serviceAccountKey.json")
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "https://anishift-5d14b-default-rtdb.firebaseio.com")

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_DB_URL})
fs_db = firestore.client()

# Bot 1 Database Node
RTDB_WORKER_FEEDBACK = "worker_job_status_short"

# --- 🤖 AI AUDIO TRANSCRIPTION KEYS ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

payload = json.loads(os.environ.get("JOB_PAYLOAD", "{}"))
anime_id = payload.get("anilist_id")
ep_num = payload.get("episode")
magnet = payload.get("magnet")
raw_backups = payload.get("backup_magnets") or []
if isinstance(raw_backups, str): raw_backups = [raw_backups]
backup_magnets = [m.strip() for m in raw_backups if isinstance(m, str) and m.strip()]

all_magnets = []
if magnet and str(magnet).strip() and str(magnet).strip().upper() != "AUTO_TORRENT":
    all_magnets.append(str(magnet).strip())
for bm in backup_magnets:
    if bm and bm not in all_magnets:
        all_magnets.append(bm)

job_type = payload.get("job_type")
search_type = payload.get("search_type")
category = payload.get("category", "tv")
anime_title = payload.get("title", "Unknown Anime")

# Dynamic Abyss account credentials with env fallback
abyss_obj = payload.get("abyss") or {}
ABYSS_API_KEY = payload.get("abyss_api_key") or abyss_obj.get("key") or os.environ.get("ABYSS_API_KEY", "")
ABYSS_EMAIL = payload.get("abyss_email") or abyss_obj.get("email") or os.environ.get("ABYSS_EMAIL", "")       
ABYSS_PASSWORD = payload.get("abyss_password") or abyss_obj.get("password") or os.environ.get("ABYSS_PASSWORD", "") 
ABYSS_ACCOUNT_NAME = payload.get("abyss_account_name") or abyss_obj.get("name", "Default")
ABYSS_ACCOUNT_ID = payload.get("abyss_account_id") or abyss_obj.get("id", "")

DEDICATED_RTDB_URL = payload.get("rtdb_url") or abyss_obj.get("rtdb_url") or os.environ.get("FIREBASE_DB_URL", "https://anihsift-sever-2-default-rtdb.firebaseio.com")
ABYSS_UPLOAD_URL = f"https://up.abyss.to/{ABYSS_API_KEY}"

safe_anime_title = re.sub(r'[\\/*?:"<>|]', "", anime_title).strip()
print(f"🚀 [WORKER STARTED - V21 BOT-1 ABYSS ONLY] Anime: {safe_anime_title} | Ep: {ep_num} | Account: {ABYSS_ACCOUNT_NAME}", flush=True)

BASE_DIR = f"downloads_{anime_id}_ep_{ep_num}_{uuid.uuid4().hex[:6]}"
TEMP_SUB_DIR = f"temp_subs_{anime_id}_ep_{ep_num}_{uuid.uuid4().hex[:6]}"
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TEMP_SUB_DIR, exist_ok=True)

def notify_status(status="failed", file_size=0, file_code=None):
    try:
        p_key = payload.get("job_key")
        job_key = p_key if p_key else f"{anime_id}_ep_{ep_num}"
        fb_data = {
            "status": status,
            "anilist_id": str(anime_id),
            "episode": int(ep_num),
            "file_size": file_size,
            "file_code": file_code,
            "video_id": file_code,
            "job_type": job_type,
            "job_key": job_key,
            "account_id": ABYSS_ACCOUNT_ID,
            "account_name": ABYSS_ACCOUNT_NAME,
            "timestamp": time.time()
        }
        # 1. Direct REST PUT to Dedicated RTDB (0% Firestore reads, high reliability)
        if DEDICATED_RTDB_URL:
            try:
                clean_url = DEDICATED_RTDB_URL.rstrip('/')
                requests.put(f"{clean_url}/worker_feedback/{job_key}.json", json=fb_data, timeout=5)
                fallback_key = f"{anime_id}_ep_{ep_num}"
                if job_key != fallback_key:
                    requests.put(f"{clean_url}/worker_feedback/{fallback_key}.json", json=fb_data, timeout=5)
                requests.put(f"{clean_url}/worker_job_status_short/{job_key}.json", json=fb_data, timeout=5)
            except Exception: pass

        # 2. Legacy firebase_admin SDK update if available
        try:
            db.reference(RTDB_WORKER_FEEDBACK).child(job_key).set(fb_data)
            db.reference(RTDB_WORKER_FEEDBACK).update({
                "status": status,
                "anilist_id": str(anime_id),
                "episode": int(ep_num),
                "file_size": file_size,
                "file_code": file_code,
                "timestamp": time.time()
            })
        except Exception: pass
    except: pass


def extract_ep_number(filename):
    # 1. Clean bracketed tags e.g. [Judas], [1080p], and release years like (2011), (1999)
    clean = re.sub(r'\(?(19\d{2}|20\d{2})\)?', ' ', filename)
    clean = re.sub(r'\[.*?\]', ' ', clean)
    clean = re.sub(r'\b(1080p|720p|480p|2160p|x264|x265|h264|hevc|10bit|8bit|av1)\b', ' ', clean, flags=re.IGNORECASE)

    # 2. Check S01E72 or s1e72
    m = re.search(r'[sS]\d+[eE]0*(\d+)', clean)
    if m: return int(m.group(1))

    # 3. Check " - 072" or " - 72" or "_072" or "#72"
    m = re.search(r'(?:\s-\s|_|#\s?|e)0*(\d{1,4})(?:v\d)?(?:\b|_|\s|\.)', clean, flags=re.IGNORECASE)
    if m: return int(m.group(1))

    # 4. Check Ep. 72 or Episode 72
    m = re.search(r'\b(?:ep|episode)\.?\s?0*(\d+)\b', clean, flags=re.IGNORECASE)
    if m: return int(m.group(1))

    # 5. Check parenthesized absolute episode number e.g. (072) or (72)
    m_paren = re.search(r'\(0*(\d{1,4})\)', clean)
    if m_paren:
        val = int(m_paren.group(1))
        if 0 < val < 2000:
            return val

    # 6. Fallback after stripping season
    clean_no_season = re.sub(r'\b(?:s|season|series)\s?\d+\b', ' ', clean, flags=re.IGNORECASE)
    clean_no_season = re.sub(r'\b\d+(?:st|nd|rd|th)\s?season\b', ' ', clean_no_season, flags=re.IGNORECASE)
    numbers = re.findall(r'\b0*(\d{1,4})\b', clean_no_season)
    if numbers:
        for num in reversed(numbers):
            if int(num) < 2000:
                return int(num)
    return None

def detect_encoding(file_path):
    for enc in ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252']:
        try:
            with open(file_path, 'r', encoding=enc) as f:
                f.read(1024)
            return enc
        except Exception:
            continue
    return 'utf-8'

def clean_vtt_tags(text):
    if not text: return ""
    text = re.sub(r'\{.*?\}', '', text).replace('\\h', ' ').replace('\\N', ' ').replace('\\n', ' ')
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[♪♫♩♬]+', '', text)
    # Strip Unicode directional embedding marks (RTL marks from Arabic ASS subtitles)
    text = re.sub(r'[\u200e\u200f\u202a-\u202e\ufeff]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def has_letters(text):
    if not text: return False
    cl = clean_vtt_tags(text)
    return any(c.isalpha() for c in cl)

def is_garbage_sub(text):
    if not text: return True
    cl = clean_vtt_tags(text)
    if not cl:
        return True  # Empty or only contained styling/notes
    # Detect ASS drawing mode / vector shapes (e.g. m 0 0 l 10 10...)
    if re.match(r'^m\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+(?:l|b|s|c|m)\s+', cl):
        return True
    # Check if there is actual dialogue text (letters or digits in any human language/script)
    if not any(c.isalnum() for c in cl):
        return True
    return False

def has_sinhala_characters(text):
    return bool(re.search(r'[\u0D80-\u0DFF]', str(text)))

WARP_PROXIES = {
    "http": "socks5://127.0.0.1:40000",
    "https": "socks5://127.0.0.1:40000"
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0"
]

def translate_guaranteed_sinhala(text):
    if not text or len(text.strip()) == 0: return ""
    if not has_letters(text): return text

    ua = random.choice(USER_AGENTS)

    # Strategy 1: Chrome Extension API - Direct (Fastest, High Success Rate)
    for attempt in range(2):
        try:
            url = "https://clients5.google.com/translate_a/t"
            params = {"client": "dict-chrome-ex", "sl": "auto", "tl": "si", "q": text}
            headers = {"User-Agent": ua}
            resp = requests.get(url, params=params, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    res_text = str(data[0][0]) if isinstance(data[0], list) else str(data[0])
                    if res_text and has_sinhala_characters(res_text):
                        return apply_spoken_sinhala(res_text)
        except Exception:
            pass

    # Strategy 2: GoogleTranslator (deep-translator) Direct
    for attempt in range(2):
        try:
            translator = GoogleTranslator(source='auto', target='si')
            res = translator.translate(text)
            if res and has_sinhala_characters(res):
                return apply_spoken_sinhala(res)
        except Exception:
            time.sleep(0.2)

    # Strategy 3: Chrome Extension API via WARP proxy
    try:
        url = "https://clients5.google.com/translate_a/t"
        params = {"client": "dict-chrome-ex", "sl": "auto", "tl": "si", "q": text}
        headers = {"User-Agent": ua}
        resp = requests.get(url, params=params, headers=headers, proxies=WARP_PROXIES, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                res_text = str(data[0][0]) if isinstance(data[0], list) else str(data[0])
                if res_text and has_sinhala_characters(res_text):
                    return apply_spoken_sinhala(res_text)
    except Exception:
        pass

    # Strategy 4: GoogleTranslator via WARP proxy
    try:
        translator = GoogleTranslator(source='auto', target='si', proxies=WARP_PROXIES)
        res = translator.translate(text)
        if res and has_sinhala_characters(res):
            return apply_spoken_sinhala(res)
    except Exception:
        pass

    return ""

def download_video():
    print(f"📥 Starting Download (Total Magnets Available: {len(all_magnets)})...", flush=True)
    timeout_arg = '--bt-stop-timeout=300'
    target_ep_int = int(ep_num)

    for mag_idx, current_mag in enumerate(all_magnets, start=1):
        mag_short = current_mag[:45] + "..." if len(current_mag) > 45 else current_mag
        print(f"\n🧲 [Candidate {mag_idx}/{len(all_magnets)}] Checking: {mag_short}", flush=True)

        # Clear existing torrent metadata files
        for tf in glob.glob("*.torrent"):
            try: os.remove(tf)
            except: pass

        is_batch_mag = (search_type == "BATCH" or "batch" in current_mag.lower() or len(all_magnets) > 1)
        if is_batch_mag:
            print(f"🔍 Fetching metadata for Ep {ep_num} from magnet #{mag_idx}...", flush=True)
            subprocess.run(['aria2c', '--bt-metadata-only=true', '--bt-save-metadata=true', '--seed-time=0', '--bt-stop-timeout=90', current_mag])
            torrent_files = glob.glob("*.torrent")
            if torrent_files:
                from torrentool.api import Torrent
                try:
                    my_torrent = Torrent.from_file(torrent_files[0])
                    target_idx = None
                    for idx, f in enumerate(my_torrent.files, start=1):
                        fname = os.path.basename(f.name)
                        if fname.lower().endswith(('.mkv', '.mp4')) and extract_ep_number(fname) == target_ep_int:
                            target_idx = idx
                            print(f"🎯 Found Episode {ep_num} in magnet #{mag_idx} (File #{idx}: {fname})", flush=True)
                            break

                    if target_idx:
                        print(f"⬇️ Downloading Ep {ep_num} using aria2c (File #{target_idx})...", flush=True)
                        subprocess.run(['aria2c', '--seed-time=0', f'--select-file={target_idx}', f'--dir={BASE_DIR}', timeout_arg, torrent_files[0]])
                        
                        # Verify file was downloaded
                        for root, dirs, files in os.walk(BASE_DIR):
                            for f in files:
                                if f.endswith(('.mkv', '.mp4')) and extract_ep_number(f) == target_ep_int:
                                    print(f"✅ Successfully downloaded Ep {ep_num} from magnet #{mag_idx}!", flush=True)
                                    return os.path.join(root, f)
                        for root, dirs, files in os.walk(BASE_DIR):
                            for f in files:
                                if f.endswith(('.mkv', '.mp4')):
                                    return os.path.join(root, f)
                        print(f"⚠️ Aria2 download did not yield Ep {ep_num}. Trying next magnet...", flush=True)
                    else:
                        print(f"⚠️ Episode {ep_num} not in torrent #{mag_idx}. Trying next backup magnet...", flush=True)
                except Exception as e_tor:
                    print(f"⚠️ Error parsing torrent metadata for #{mag_idx}: {e_tor}. Trying next magnet...", flush=True)
            else:
                print(f"⚠️ Metadata download timed out/failed for magnet #{mag_idx}. Trying next magnet...", flush=True)
        else:
            # Single episode download attempt
            subprocess.run(['aria2c', '--seed-time=0', f'--dir={BASE_DIR}', timeout_arg, current_mag])
            for root, dirs, files in os.walk(BASE_DIR):
                for f in files:
                    if f.endswith(('.mkv', '.mp4')) and extract_ep_number(f) == target_ep_int:
                        print(f"✅ Successfully downloaded Ep {ep_num} from single magnet #{mag_idx}!", flush=True)
                        return os.path.join(root, f)
            for root, dirs, files in os.walk(BASE_DIR):
                for f in files:
                    if f.endswith(('.mkv', '.mp4')):
                        return os.path.join(root, f)
            print(f"⚠️ Single download failed for Ep {ep_num}. Trying next magnet...", flush=True)

    print(f"❌ Exhausted all {len(all_magnets)} magnets. Episode {ep_num} could not be downloaded.", flush=True)
    return None

def extract_and_score_subtitles(video_path):
    """
    Scans and extracts all soft subtitle tracks from MKV/MP4 using ffprobe & ffmpeg with '-c:s srt'.
    Scores tracks using language, title, and line count.
    Penalizes Signs & Songs, Romaji, and non-dialogue tracks.
    Returns: (winner_type, winner_sub_path) where winner_type is 'sinhala' or 'english' (or (None, None))
    """
    print("🔍 Scanning video for softsubs...", flush=True)
    
    probe_cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 's',
        '-show_entries', 'stream=index:stream_tags=language,title',
        '-of', 'json', video_path
    ]
    
    streams = []
    try:
        result = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25)
        if result.returncode == 0:
            streams = json.loads(result.stdout).get('streams', [])
    except Exception as e:
        print(f"⚠️ ffprobe subtitle scan error: {e}", flush=True)

    # Fallback if ffprobe returned no streams: try first 6 subtitle streams by index selector
    if not streams:
        streams = [{'index': f"s:{i}", 'tags': {'title': f"Track {i}"}} for i in range(6)]

    si_candidates = []
    other_candidates = []

    for s_idx_num, s in enumerate(streams):
        s_idx = s.get('index', f"s:{s_idx_num}")
        tags = s.get('tags', {}) or {}
        lang = str(tags.get('language', '')).lower()
        title = str(tags.get('title', f"Track {s_idx}")).lower()
        
        temp_sub = os.path.join(TEMP_SUB_DIR, f"temp_track_{s_idx_num}.srt")
        
        # Mapping strategy 1: stream index 0:{s_idx} with -c:s srt
        map_arg = f"0:{s_idx}" if (str(s_idx).startswith('s:') or ':' in str(s_idx)) else f"0:{s_idx}"
        cmd = ['ffmpeg', '-y', '-i', video_path, '-map', map_arg, '-c:s', 'srt', temp_sub]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
        except Exception:
            pass

        # Mapping strategy 2 (fallback): subtitle stream selector 0:s:{s_idx_num}
        if (not os.path.exists(temp_sub) or os.path.getsize(temp_sub) < 100) and not str(s_idx).startswith('s:'):
            cmd_alt = ['ffmpeg', '-y', '-i', video_path, '-map', f'0:s:{s_idx_num}', '-c:s', 'srt', temp_sub]
            try:
                subprocess.run(cmd_alt, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
            except Exception:
                pass

        if not os.path.exists(temp_sub) or os.path.getsize(temp_sub) < 100:
            continue

        try:
            enc = detect_encoding(temp_sub)
            try: subs = pysubs2.load(temp_sub, encoding=enc)
            except Exception: subs = pysubs2.load(temp_sub, encoding='latin-1')

            line_count = len(subs.events)
            if line_count < 15: # Ignore micro-stubs or empty tracks
                if os.path.exists(temp_sub): os.remove(temp_sub)
                continue

            score = line_count
            name_combined = f"{title} {lang}"

            # 1. Existing Sinhala Subtitle in video (highest priority)
            if any(x in name_combined for x in ['si', 'sinhala', 'සිංහල']) or lang in ['si', 'sin']:
                score += 200000
            # 2. English Dialogue Track
            elif any(x in name_combined for x in ['en', 'eng', 'english']) or lang in ['en', 'eng']:
                score += 100000
            else:
                score += 10000 # Valid other dialogue track (French, Spanish, etc.)

            # 3. Penalize Signs & Songs, lyrics, commentary, forced
            if any(x in title for x in ['sign', 'song', 'forced', 'credit', 'op/ed', 'oped']):
                score -= 100000
            # 4. Penalize Japanese / Romaji
            if any(x in name_combined for x in ['ja', 'jap', 'romaji']) or lang in ['ja', 'jpn']:
                score -= 100000

            # Line threshold scoring: dialogue tracks usually have >= 40 lines
            if line_count < 40:
                score -= 40000

            print(f"   📄 Sub Track #{s_idx_num} ('{title}') | Lang: {lang or 'N/A'} | Lines: {line_count} | Score: {score}", flush=True)

            if score <= 0:
                if os.path.exists(temp_sub): os.remove(temp_sub)
                continue

            track_info = {
                'path': temp_sub,
                'lines': line_count,
                'score': score,
                'name': title,
                'lang': lang
            }

            if any(x in name_combined for x in ['si', 'sinhala', 'සිංහල']) or lang in ['si', 'sin']:
                si_candidates.append(track_info)
            else:
                other_candidates.append(track_info)

        except Exception as e:
            if os.path.exists(temp_sub):
                try: os.remove(temp_sub)
                except: pass

    # 1. If an existing Sinhala sub is found, use it directly!
    if si_candidates:
        si_candidates.sort(key=lambda x: x['score'], reverse=True)
        winner = si_candidates[0]
        print(f"🏆 WINNER (Embedded Sinhala): Track '{winner['name']}' with {winner['lines']} lines!", flush=True)
        winner_path = os.path.join(TEMP_SUB_DIR, "winner_sinhala.srt")
        os.rename(winner['path'], winner_path)
        for c in si_candidates[1:] + other_candidates:
            if os.path.exists(c['path']):
                try: os.remove(c['path'])
                except: pass
        return 'sinhala', winner_path

    # 2. If translation source (English) is found, return it for translation!
    if other_candidates:
        other_candidates.sort(key=lambda x: x['score'], reverse=True)
        winner = other_candidates[0]
        print(f"🏆 WINNER (Translation Source): Track '{winner['name']}' with {winner['lines']} lines (Score: {winner['score']})!", flush=True)
        winner_path = os.path.join(TEMP_SUB_DIR, "extracted.srt")
        os.rename(winner['path'], winner_path)
        for c in other_candidates[1:]:
            if os.path.exists(c['path']):
                try: os.remove(c['path'])
                except: pass
        return 'english', winner_path

    return None, None

def process_sinhala_sub(sub_path):
    out_name = os.path.join(TEMP_SUB_DIR, "sinhala_sub.srt")
    try:
        print("🧹 Cleaning dialogs & unwanted lines...", flush=True)
        try: subs = pysubs2.load(sub_path, encoding=detect_encoding(sub_path))
        except: subs = pysubs2.load(sub_path, encoding='latin-1')
        
        cleaned_events = []
        unique_texts = set()
        prev_text = ""
        seen_texts_count = {}
        bad_words = ['subtitle by', 'translated by', 'sync by', 'encoded by', 'www.', '.com', 'discord', 'telegram', 'netlify', 'anishift', 'download කිරීමට', 'නැරඹීමට']
        
        for e in subs:
            if is_garbage_sub(e.text): continue
            txt = clean_vtt_tags(e.text)
            t_low = txt.lower()
            if any(x in t_low for x in bad_words) or len(txt) > 300 or not has_letters(txt): continue
            
            if txt == prev_text:
                if cleaned_events: cleaned_events[-1].end = max(cleaned_events[-1].end, e.end)
                continue
                
            seen_texts_count[txt] = seen_texts_count.get(txt, 0) + 1
            if len(txt) > 30 and seen_texts_count[txt] > 10: continue
            
            e.text = txt
            cleaned_events.append(e)
            unique_texts.add(txt)
            prev_text = txt
            
        if not cleaned_events:
            print("⚠️ No events remaining after strict cleaning. Preserving base events.", flush=True)
            cleaned_events = [e for e in subs if e.text and clean_vtt_tags(e.text)]
            unique_texts = {clean_vtt_tags(e.text) for e in cleaned_events if has_letters(e.text)}
            if not cleaned_events:
                return None
        
        uni_list = list(unique_texts)
        total_lines = len(uni_list)
        print(f"🚀 Translating {total_lines} unique lines (Guaranteed Sinhala Mode ⚡)...", flush=True)
        
        translation_map = {}
        def process_single(text):
            return text, translate_guaranteed_sinhala(text)

        # Use 6 parallel workers to prevent Google 429 rate limit
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(process_single, t) for t in uni_list]
            done_lines = 0
            for future in concurrent.futures.as_completed(futures):
                orig, trans = future.result()
                translation_map[orig] = trans
                done_lines += 1
                if done_lines % 25 == 0 or done_lines == total_lines:
                    print(f"   📊 Progress: {int((done_lines/total_lines)*100)}% ({done_lines}/{total_lines})", flush=True)
                    
        final_events = []
        translated_count = 0
        for event in cleaned_events: 
            translated_text = translation_map.get(event.text, "")
            if translated_text: 
                event.text = translated_text
                translated_count += 1
                final_events.append(event)
            elif event.text and clean_vtt_tags(event.text):
                # CRITICAL: If individual line translation failed, NEVER drop the line!
                # Fall back to cleaned original text so the subtitle is NEVER blank/empty!
                event.text = clean_vtt_tags(event.text)
                final_events.append(event)

        print(f"✅ Final Subtitle Yield: {len(final_events)} dialogue lines ({translated_count} translated to Sinhala)", flush=True)

        if not final_events:
            print("⚠️ Warning: final_events was empty, restoring cleaned_events.", flush=True)
            final_events = cleaned_events
            
        subs.events = final_events
        subs.save(out_name, encoding="utf-8")
        return out_name
    except Exception as e:
        print(f"❌ Error in process_sinhala_sub: {e}", flush=True)
        return None

def transcribe_with_groq(audio_path, out_srt_path):
    if not GROQ_API_KEY:
        return False
    print("⚡ [AI Engine] Transcribing with Groq Cloud (whisper-large-v3)...", flush=True)
    try:
        url = "https://api.groq.com/openai/v1/audio/translations"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
            data = {
                "model": "whisper-large-v3",
                "response_format": "verbose_json",
                "temperature": "0.0"
            }
            res = requests.post(url, headers=headers, files=files, data=data, timeout=180)
            
        if res.status_code == 200:
            result = res.json()
            segments = result.get("segments", [])
            subs = pysubs2.SSAFile()
            for seg in segments:
                t_str = seg.get("text", "").strip()
                if t_str:
                    s_ms = int(seg.get("start", 0) * 1000)
                    e_ms = int(seg.get("end", 0) * 1000)
                    subs.events.append(pysubs2.SSAEvent(start=s_ms, end=e_ms, text=t_str))
            if subs.events:
                subs.save(out_srt_path, encoding="utf-8")
                print(f"🎉 Groq whisper-large-v3 generated {len(subs.events)} dialogue lines!", flush=True)
                return True
        else:
            print(f"⚠️ Groq API error ({res.status_code}): {res.text[:160]}", flush=True)
    except Exception as e:
        print(f"⚠️ Groq transcription failed: {e}", flush=True)
    return False

def transcribe_with_gemini(audio_path, out_srt_path):
    if not GEMINI_API_KEY:
        return False
    print("🧠 [AI Engine] Transcribing with Google Gemini Flash Multimodal Audio...", flush=True)
    try:
        file_size = os.path.getsize(audio_path)
        upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_API_KEY}"
        headers = {
            "X-Goog-Upload-Command": "start, upload, finalize",
            "X-Goog-Upload-Header-Content-Length": str(file_size),
            "X-Goog-Upload-Header-Content-Type": "audio/mp3",
            "Content-Type": "audio/mp3"
        }
        with open(audio_path, "rb") as f:
            up_res = requests.post(upload_url, headers=headers, data=f, timeout=120)
        
        if up_res.status_code != 200:
            print(f"⚠️ Gemini audio upload error: {up_res.text[:150]}", flush=True)
            return False

        file_uri = up_res.json().get("file", {}).get("uri")
        if not file_uri:
            return False

        gen_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        prompt = (
            "You are a professional anime subtitle translator. Listen to the Japanese audio track and generate accurate, natural English subtitle lines with standard SRT timestamps.\n"
            "Strictly output only raw valid SRT format starting with 1. No markdown fences, no conversational text."
        )
        body = {
            "contents": [{
                "parts": [
                    {"file_data": {"mime_type": "audio/mp3", "file_uri": file_uri}},
                    {"text": prompt}
                ]
            }],
            "generationConfig": {"temperature": 0.2}
        }
        gen_res = requests.post(gen_url, json=body, timeout=180)
        if gen_res.status_code == 200:
            candidates = gen_res.json().get("candidates", [])
            if candidates:
                srt_content = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                srt_content = re.sub(r'^```(?:srt)?\n|```$', '', srt_content.strip(), flags=re.MULTILINE)
                try:
                    test_subs = pysubs2.SSAFile.from_string(srt_content, format_="srt")
                    if len(test_subs) > 0:
                        test_subs.save(out_srt_path, encoding="utf-8")
                        print(f"🎉 Gemini Flash generated {len(test_subs)} dialogue lines!", flush=True)
                        return True
                except Exception:
                    pass
        else:
            print(f"⚠️ Gemini API error ({gen_res.status_code}): {gen_res.text[:160]}", flush=True)
    except Exception as e:
        print(f"⚠️ Gemini transcription failed: {e}", flush=True)
    return False

def transcribe_with_local_whisper(audio_path, out_srt_path):
    print("🐢 [AI Fallback] Using Local faster-whisper (CPU)...", flush=True)
    try:
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, info = model.transcribe(audio_path, task="translate", vad_filter=True, beam_size=5)
        subs = pysubs2.SSAFile()
        for segment in segments:
            t_str = segment.text.strip()
            if t_str:
                subs.events.append(pysubs2.SSAEvent(start=int(segment.start * 1000), end=int(segment.end * 1000), text=t_str))
        if subs.events:
            subs.save(out_srt_path, encoding="utf-8")
            print(f"✅ Local faster-whisper generated {len(subs.events)} dialogue lines.", flush=True)
            return True
    except Exception as e:
        print(f"⚠️ Local Whisper AI failed: {e}", flush=True)
    return False

def process_and_translate_subtitle(video_path):
    sub_type, extracted_path = extract_and_score_subtitles(video_path)

    # 1. If embedded Sinhala sub was already present in video:
    if sub_type == 'sinhala' and extracted_path and os.path.exists(extracted_path):
        print("🎉 Using Embedded Sinhala Subtitle directly!", flush=True)
        return process_sinhala_sub(extracted_path)

    # 2. If English / other dialogue sub was extracted:
    if sub_type == 'english' and extracted_path and os.path.exists(extracted_path):
        return process_sinhala_sub(extracted_path)

    # 3. AI Multi-Tier fallback only if video has NO softsubs at all
    print("⚠️ No softsubs found in video tracks. Starting AI Audio Transcription fallback...", flush=True)
    audio_path = os.path.join(TEMP_SUB_DIR, "audio.mp3")
    eng_sub = os.path.join(TEMP_SUB_DIR, "extracted.srt")
    
    # Extract audio in optimized 16kHz mono 48k mp3 (compact ~7MB size, ideal for speech recognition)
    subprocess.run([
        'ffmpeg', '-i', video_path, '-vn',
        '-ac', '1', '-ar', '16000', '-b:a', '48k',
        audio_path, '-y'
    ], stderr=subprocess.DEVNULL)

    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
        success = (
            transcribe_with_groq(audio_path, eng_sub) or
            transcribe_with_gemini(audio_path, eng_sub) or
            transcribe_with_local_whisper(audio_path, eng_sub)
        )
        if success and os.path.exists(eng_sub):
            return process_sinhala_sub(eng_sub)
        
    return None

def get_abyss_token():
    print("🔑 Authenticating with Abyss...", flush=True)
    if not ABYSS_EMAIL or not ABYSS_PASSWORD: return None
    try:
        res = requests.post("https://api.abyss.to/auth/login", json={"email": ABYSS_EMAIL, "password": ABYSS_PASSWORD}).json()
        return res.get("token")
    except Exception: return None

def upload_video_to_abyss(video_path):
    print("☁️ Uploading Video to Abyss.to...", flush=True)
    upload_filename = os.path.basename(video_path)
    mime_type = 'video/x-matroska' if upload_filename.endswith('.mkv') else 'video/mp4'

    for attempt in range(3):
        try:
            fields = {'file': (upload_filename, open(video_path, 'rb'), mime_type)}
            multipart_data = MultipartEncoder(fields=fields)
            headers = {'Content-Type': multipart_data.content_type, 'User-Agent': 'Mozilla/5.0'}

            up_resp = requests.post(ABYSS_UPLOAD_URL, data=multipart_data, headers=headers, timeout=1200)
            try: resp_data = up_resp.json()
            except: 
                if attempt < 2: time.sleep(15)
                continue

            if str(resp_data.get("status")) in ["True", "200", "true"]:
                vhd_code = resp_data.get("slug") or resp_data.get("id") or resp_data.get("code")
                if vhd_code: return vhd_code, os.path.getsize(video_path)
        except:
            if attempt < 2: time.sleep(15)
    return None, 0

def upload_subtitle_to_abyss_api(vhd_code, srt_path, token):
    print("☁️ Uploading Sinhala Subtitle to Abyss...", flush=True)
    if not srt_path or not os.path.exists(srt_path):
        print("⚠️ No subtitle file found to upload.", flush=True)
        return False

    sub_size = os.path.getsize(srt_path)
    if sub_size < 100:
        print(f"⚠️ Subtitle file is suspiciously small ({sub_size} bytes). Aborting upload.", flush=True)
        return False

    try:
        # 1. Delete old/stale Sinhala subtitle from Abyss if present
        try:
            list_res = requests.get(f"https://api.abyss.to/v1/subtitles/{vhd_code}/list", headers={'Authorization': f'Bearer {token}'}, timeout=10)
            if list_res.status_code == 200:
                for item in list_res.json().get('items', []):
                    name = (item.get('name') or '').lower()
                    lang = (item.get('language') or '').lower()
                    if 'sinhala' in name or 'sinhala' in lang or lang in ['si', 'sin']:
                        sid = item.get('id')
                        requests.delete(f"https://api.abyss.to/v1/subtitles/{sid}", headers={'Authorization': f'Bearer {token}'}, timeout=10)
        except Exception:
            pass

        # 2. Upload new validated Sinhala subtitle (.srt)
        url = f"https://api.abyss.to/v1/upload/subtitles/{vhd_code}?language=Sinhala&filename=sinhala.srt"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"} 
        with open(srt_path, "rb") as f: sub_data = f.read()
        resp = requests.put(url, headers=headers, data=sub_data, timeout=60)
        if resp.status_code in [200, 201]:
            print(f"🎉 Subtitle Attached Successfully! ({len(sub_data)} bytes)", flush=True)
            return True
        else:
            print(f"⚠️ Abyss sub upload status {resp.status_code}: {resp.text}", flush=True)
    except Exception as e:
        print(f"⚠️ Abyss sub upload error: {e}", flush=True)
    return False


# ==========================================
# 💾 FIRESTORE UPDATE (Abyss Only)
# ==========================================
def update_database(file_code):
    print("💾 Updating Firestore with Abyss link...", flush=True)
    ep_doc_id = f"episode_{int(ep_num):04d}" if str(ep_num).isdigit() else f"episode_{ep_num}"
    
    data = {
        'status': 'uploaded',
        'links': {
            'abyss_video_id': file_code, 
            'abyss_embed': f"https://abyss.to/embed/{file_code}",
            'account': ABYSS_ACCOUNT_NAME
        },
        'account_name': ABYSS_ACCOUNT_NAME,
        'server_3_uploaded': True,
        'last_updated': firestore.SERVER_TIMESTAMP
    }
    
    col_name = 'anime_movies' if (category == 'movie' or job_type == 'movie') else 'anime_series'
    try:
        fs_db.collection(col_name).document(str(anime_id)).collection('episodes').document(ep_doc_id).set(data, merge=True)
        print(f"✅ Firestore Updated in {col_name}!", flush=True)
    except Exception as e:
        print(f"⚠️ Firestore update error: {e}", flush=True)

def cleanup_temp_files():
    print("🧹 Cleaning up downloaded video and temporary files to free disk space...", flush=True)
    import shutil
    try:
        if os.path.exists(BASE_DIR):
            shutil.rmtree(BASE_DIR, ignore_errors=True)
    except Exception: pass

    try:
        if os.path.exists(TEMP_SUB_DIR):
            shutil.rmtree(TEMP_SUB_DIR, ignore_errors=True)
    except Exception: pass

# --- MAIN EXECUTION ---
original_video = download_video()

if original_video:
    srt_sub_path = process_and_translate_subtitle(original_video)
    jwt_token = get_abyss_token()
    
    print("✂️ Processing Dual-Audio & removing internal subtitles...", flush=True)
    original_filename = os.path.basename(original_video)
    clean_video = os.path.join(TEMP_SUB_DIR, original_filename)
    
    # 🔥 DUAL-AUDIO SMART LOGIC 
    try:
        probe_cmd = ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index:stream_tags=language:stream_tags=title', '-of', 'json', original_video]
        probe_res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        audio_streams = json.loads(probe_res.stdout).get('streams', [])
        
        audio_map = ['-map', '0:a?'] 
        
        if len(audio_streams) > 1:
            print(f"🔊 Dual-Audio detected! ({len(audio_streams)} audio tracks). Finding Japanese track...", flush=True)
            jpn_index = None
            non_eng_index = None
            
            for s in audio_streams:
                lang = s.get('tags', {}).get('language', '').lower()
                title = s.get('tags', {}).get('title', '').lower()
                
                if lang in ['ja', 'jpn', 'japanese'] or 'japanese' in title or '日本語' in title or 'nihongo' in title:
                    jpn_index = s['index']
                    break
                
                if lang not in ['en', 'eng', 'english'] and 'english' not in title and non_eng_index is None:
                    non_eng_index = s['index']
            
            if jpn_index is not None:
                audio_map = ['-map', f'0:{jpn_index}']
            elif non_eng_index is not None:
                audio_map = ['-map', f'0:{non_eng_index}']
            else:
                audio_map = ['-map', '0:a:0']
        else:
            audio_map = ['-map', '0:a:0?']
            
        ff_cmd = ['ffmpeg', '-i', original_video, '-map', '0:v:0'] + audio_map + ['-c', 'copy', '-sn', clean_video, '-y']
        subprocess.run(ff_cmd, stderr=subprocess.DEVNULL)
        
    except Exception as e:
        print(f"⚠️ Audio parsing failed, falling back to basic cleanup...", flush=True)
        subprocess.run(['ffmpeg', '-i', original_video, '-c', 'copy', '-sn', clean_video, '-y'], stderr=subprocess.DEVNULL)
    
    # වීඩියෝ එක Abyss එකට අප්ලෝඩ් කිරීම
    video_to_upload = clean_video if os.path.exists(clean_video) else original_video
    upload_result = upload_video_to_abyss(video_to_upload)
    
    if upload_result and upload_result[0]:
        file_code, file_size = upload_result
        
        # සබ් එක Abyss එකට ඇටෑච් කිරීම
        if srt_sub_path and os.path.exists(srt_sub_path) and jwt_token:
            sub_ok = upload_subtitle_to_abyss_api(file_code, srt_sub_path, jwt_token)
            if not sub_ok:
                print("⚠️ Retrying subtitle upload after 5s...", flush=True)
                time.sleep(5)
                upload_subtitle_to_abyss_api(file_code, srt_sub_path, jwt_token)
        else:
            print(f"⚠️ Subtitle attachment skipped (srt_sub_path={srt_sub_path}, exists={os.path.exists(srt_sub_path) if srt_sub_path else False}, jwt_token={'VALID' if jwt_token else 'MISSING'})", flush=True)
            
        # Database එක අප්ඩේට් කිරීම
        update_database(file_code)
        
        notify_status("success", file_size, file_code=file_code)
        print("🎉 WORKER COMPLETED SUCCESSFULLY (Abyss Upload Only)!", flush=True)
        cleanup_temp_files()
        sys.exit(0)
    else:
        print("❌ Video Upload Failed!", flush=True)
        notify_status("failed", 0)
        cleanup_temp_files()
        sys.exit(1)
else:
    print("❌ Download Failed!", flush=True)
    notify_status("failed", 0)
    cleanup_temp_files()
    sys.exit(1)

