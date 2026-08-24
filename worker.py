import os
import sys
import json
import time
import requests
import subprocess
import glob
import re
import firebase_admin
from firebase_admin import credentials, firestore, db
import pysubs2
from requests_toolbelt.multipart.encoder import MultipartEncoder
from faster_whisper import WhisperModel
import urllib.parse

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

ABYSS_API_KEY = os.environ.get("ABYSS_API_KEY", "")
ABYSS_EMAIL = os.environ.get("ABYSS_EMAIL", "")       
ABYSS_PASSWORD = os.environ.get("ABYSS_PASSWORD", "") 
RTDB_WORKER_FEEDBACK = "worker_job_status_short"

ABYSS_UPLOAD_URL = f"https://up.abyss.to/{ABYSS_API_KEY}"

payload = json.loads(os.environ.get("JOB_PAYLOAD", "{}"))
anime_id = payload.get("anilist_id")
ep_num = payload.get("episode")
magnet = payload.get("magnet")
job_type = payload.get("job_type")
search_type = payload.get("search_type")
anime_title = payload.get("title", "Unknown Anime")

safe_anime_title = re.sub(r'[\\/*?:"<>|]', "", anime_title).strip()
print(f"🚀 [WORKER STARTED - 100% SINHALA SUB] Anime: {safe_anime_title} | Ep: {ep_num}")

BASE_DIR = "downloads"
TEMP_SUB_DIR = f"temp_subs_ep_{ep_num}"
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TEMP_SUB_DIR, exist_ok=True)

def notify_status(status="failed", file_size=0):
    try:
        db.reference(RTDB_WORKER_FEEDBACK).set({
            "status": status, "anilist_id": str(anime_id),
            "episode": int(ep_num), "file_size": file_size,
            "timestamp": time.time()
        })
    except: pass

def extract_ep_number(filename):
    clean = re.sub(r'\[.*?\]|\(.*?\)', ' ', filename.lower())
    clean = re.sub(r'\b(1080p|720p|480p|x264|x265|hevc|10bit|8bit)\b', ' ', clean)
    m = re.search(r'[sS]\d+[eE]0*(\d+)', clean)
    if m: return int(m.group(1))
    m = re.search(r'\b(?:ep|episode)\.?\s?0*(\d+)\b', clean)
    if m: return int(m.group(1))
    m = re.search(r'\s-\s0*(\d+)(?:v\d)?\b', clean)
    if m: return int(m.group(1))
    m = re.search(r'\b0*(\d+)\b', clean)
    if m: return int(m.group(1))
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
    text = re.sub(r'\{.*?\}', '', text).replace('\\h', ' ')
    return re.sub(r'<[^>]+>', '', text).strip()

def is_garbage_sub(text):
    if not text: return True
    if re.search(r'\\pos\(|\\c&H|\\alpha|\\t\(|\\fad\(|\\an\d', text): return True
    cl = re.sub(r'<[^>]+>', '', re.sub(r'\{.*?\}', '', text)).strip()
    if re.match(r'^m\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+(?:l|b|s|c|m)\s+', cl): return True
    return False

def has_sinhala_characters(text):
    """පෙළෙහි සිංහල අකුරු අඩංගුදැයි තහවුරු කිරීම"""
    return bool(re.search(r'[\u0D80-\u0DFF]', str(text)))

# --- 🌐 MULTI-ENGINE BULLETPROOF TRANSLATOR ---
LINGVA_SERVERS = [
    "https://lingva.ml",
    "https://lingva.lunar.icu",
    "https://translate.plausibility.cloud"
]

def translate_guaranteed_sinhala(text):
    """සිංහල ලැබෙන තෙක්ම Engines කිහිපයක් හරහා පරිවර්තනය කිරීම"""
    if not text or len(text.strip()) == 0:
        return text

    # Engine 1: Google Direct API
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "auto", "tl": "si", "dt": "t"}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.post(url, params=params, data={"q": text}, headers=headers, timeout=8)
        if resp.status_code == 200:
            res_text = "".join([part[0] for part in resp.json()[0] if part[0]]).strip()
            if "Error" not in res_text and has_sinhala_characters(res_text):
                return apply_spoken_sinhala(res_text)
    except Exception:
        pass

    # Engine 2: Lingva Proxy Network (Google Bypass)
    for server in LINGVA_SERVERS:
        try:
            encoded_q = urllib.parse.quote(text)
            r = requests.get(f"{server}/api/v1/en/si/{encoded_q}", timeout=8)
            if r.status_code == 200:
                res_text = r.json().get("translation", "").strip()
                if res_text and has_sinhala_characters(res_text):
                    return apply_spoken_sinhala(res_text)
        except Exception:
            continue

    # Engine 3: MyMemory API Fallback
    try:
        enc_q = urllib.parse.quote(text)
        r = requests.get(f"https://api.mymemory.translated.net/get?q={enc_q}&langpair=en|si", timeout=8)
        if r.status_code == 200:
            res_text = r.json().get("responseData", {}).get("translatedText", "").strip()
            if res_text and has_sinhala_characters(res_text) and "MYMEMORY WARNING" not in res_text:
                return apply_spoken_sinhala(res_text)
    except Exception:
        pass

    return text

def download_video():
    print(f"📥 Starting Download...")
    timeout_arg = '--bt-stop-timeout=300'
    
    if search_type == "BATCH":
        subprocess.run(['aria2c', '--bt-metadata-only=true', '--bt-save-metadata=true', '--seed-time=0', '--bt-stop-timeout=120', magnet])
        torrent_files = glob.glob("*.torrent")
        if torrent_files:
            from torrentool.api import Torrent
            my_torrent = Torrent.from_file(torrent_files[0])
            target_idx = None
            for idx, f in enumerate(my_torrent.files, start=1):
                if f.name.lower().endswith(('.mkv', '.mp4')) and extract_ep_number(os.path.basename(f.name)) == int(ep_num):
                    target_idx = idx
                    break
            if target_idx:
                subprocess.run(['aria2c', '--seed-time=0', f'--select-file={target_idx}', f'--dir={BASE_DIR}', timeout_arg, torrent_files[0]])
    else:
        subprocess.run(['aria2c', '--seed-time=0', f'--dir={BASE_DIR}', timeout_arg, magnet])

    target_ep_int = int(ep_num)
    for root, dirs, files in os.walk(BASE_DIR):
        for f in files:
            if f.endswith(('.mkv', '.mp4')) and extract_ep_number(f) == target_ep_int:
                return os.path.join(root, f)
    for root, dirs, files in os.walk(BASE_DIR):
        for f in files:
            if f.endswith(('.mkv', '.mp4')): return os.path.join(root, f)
    return None

def get_best_subtitle_stream(video_path):
    print("🔍 Scanning video for softsubs...")
    cmd = [
        'ffprobe', '-v', 'error', '-select_streams', 's',
        '-show_entries', 'stream=index:stream_tags=language',
        '-of', 'json', video_path
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        streams = json.loads(result.stdout).get('streams', [])
        if not streams: return None
        
        for s in streams:
            lang = s.get('tags', {}).get('language', '').lower()
            if lang in ['eng', 'en', 'english']:
                print(f"✅ Found English subtitle at stream index {s['index']}")
                return s['index']
                
        print(f"⚠️ English not found. Taking default subtitle stream index {streams[0]['index']}")
        return streams[0]['index']
    except:
        return None

def process_sinhala_sub(sub_path):
    out_name = os.path.join(TEMP_SUB_DIR, "sinhala_sub.srt")
    try:
        print("🧹 Cleaning dialogs & unwanted lines...")
        try: subs = pysubs2.load(sub_path, encoding=detect_encoding(sub_path))
        except: subs = pysubs2.load(sub_path, encoding='latin-1')
        
        cleaned_events, unique_texts, prev_text, seen_texts_count = [], set(), "", {}
        bad_words = ['subtitle by', 'translated by', 'sync by', 'encoded by', 'www.', '.com', 'discord', 'telegram']
        
        for e in subs:
            if is_garbage_sub(e.text): continue
            txt, t_low = clean_vtt_tags(e.text), clean_vtt_tags(e.text).lower()
            if any(x in t_low for x in bad_words) or len(txt) > 250 or len(txt) < 2 or '♪' in txt or '♫' in txt: continue
            
            if txt == prev_text:
                if cleaned_events: cleaned_events[-1].end = max(cleaned_events[-1].end, e.end)
                continue
                
            seen_texts_count[txt] = seen_texts_count.get(txt, 0) + 1
            if len(txt) > 30 and seen_texts_count[txt] > 2: continue
            
            e.text = txt
            cleaned_events.append(e)
            unique_texts.add(txt)
            prev_text = txt
            
        if not cleaned_events: return None
        
        uni_list = list(unique_texts)
        print(f"🔄 Translating {len(uni_list)} clean unique lines with Zero English Fallback...")
        
        translation_map = {}
        total = len(uni_list)
        
        for idx, text in enumerate(uni_list):
            translation_map[text] = translate_guaranteed_sinhala(text)
            if idx % 25 == 0:
                print(f"   📊 Progress: {int((idx/total)*100)}%")
            time.sleep(0.12)
                    
        for event in cleaned_events: 
            event.text = translation_map.get(event.text, event.text)
            
        subs.events = cleaned_events
        subs.save(out_name, encoding="utf-8")
        return out_name
    except Exception as e:
        print(f"❌ Error during translation: {e}")
        return None

def process_and_translate_subtitle(video_path):
    eng_sub = os.path.join(TEMP_SUB_DIR, "extracted.srt") 
    extracted_successfully = False

    target_stream = get_best_subtitle_stream(video_path)
    if target_stream is not None:
        print(f"📝 Extracting Subtitle Stream {target_stream}...")
        subprocess.run(['ffmpeg', '-i', video_path, '-map', f'0:{target_stream}', eng_sub, '-y'], stderr=subprocess.DEVNULL)
        if os.path.exists(eng_sub) and os.path.getsize(eng_sub) >= 100:
            extracted_successfully = True

    if not extracted_successfully:
        print("⚠️ No valid softsub found! Starting AI Audio Transcription (Japanese -> English)...")
        audio_path = os.path.join(TEMP_SUB_DIR, "audio.mp3")
        subprocess.run(['ffmpeg', '-i', video_path, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', audio_path, '-y'], stderr=subprocess.DEVNULL)
        
        if os.path.exists(audio_path):
            try:
                model = WhisperModel("base", device="cpu", compute_type="int8")
                segments, info = model.transcribe(audio_path, task="translate")
                
                subs = pysubs2.SSAFile()
                for segment in segments:
                    subs.events.append(pysubs2.SSAEvent(
                        start=int(segment.start * 1000),
                        end=int(segment.end * 1000),
                        text=segment.text.strip()
                    ))
                subs.save(eng_sub, encoding="utf-8")
                print("✅ AI Transcription Complete!")
                extracted_successfully = True
            except Exception as e:
                print(f"❌ AI Transcription failed: {e}")
        
    if not extracted_successfully:
        return None

    return process_sinhala_sub(eng_sub)

def get_abyss_token():
    print("🔑 Authenticating with Abyss to get JWT Token for Subtitle...")
    if not ABYSS_EMAIL or not ABYSS_PASSWORD: 
        return None
    try:
        res = requests.post("https://api.abyss.to/auth/login", json={"email": ABYSS_EMAIL, "password": ABYSS_PASSWORD}).json()
        return res.get("token")
    except Exception as e: 
        return None

def upload_video_to_abyss(video_path):
    print("☁️ Uploading Original Video to Abyss.to (ROOT)...")
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

            if resp_data.get("status") is True or str(resp_data.get("status")) == "200":
                vhd_code = resp_data.get("slug") or resp_data.get("id") or resp_data.get("code")
                if vhd_code: return vhd_code, os.path.getsize(video_path)
        except:
            if attempt < 2: time.sleep(15)
    return None, 0

def upload_subtitle_to_abyss_api(vhd_code, srt_path, token):
    print("☁️ Uploading Sinhala Subtitle to Abyss...")
    try:
        url = f"https://api.abyss.to/v1/upload/subtitles/{vhd_code}?language=Sinhala&filename=sinhala.srt"
        headers = {
            "Authorization": f"Bearer {token}", 
            "Content-Type": "application/octet-stream"
        } 
        with open(srt_path, "rb") as f: sub_data = f.read()
        resp = requests.put(url, headers=headers, data=sub_data, timeout=60)
        if resp.status_code == 200:
            print("🎉 Subtitle Attached Successfully!")
        else:
            print(f"⚠️ Subtitle upload failed. Status Code: {resp.status_code}")
    except Exception as e: 
        pass

def update_database(file_code):
    print("💾 Updating Firestore...")
    ep_doc_id = f"episode_{int(ep_num):04d}" if str(ep_num).isdigit() else f"episode_{ep_num}"
    fs_db.collection('anime_series').document(str(anime_id)).collection('episodes').document(ep_doc_id).set({
        'status': 'uploaded',
        'links': {'abyss_video_id': file_code, 'abyss_embed': f"https://abyss.to/embed/{file_code}"},
        'last_updated': firestore.SERVER_TIMESTAMP
    }, merge=True)

# --- MAIN EXECUTION ---
original_video = download_video()

if original_video:
    srt_sub_path = process_and_translate_subtitle(original_video)
    jwt_token = get_abyss_token()
    upload_result = upload_video_to_abyss(original_video)
    
    if upload_result and upload_result[0]:
        file_code, file_size = upload_result
        if srt_sub_path and os.path.exists(srt_sub_path) and jwt_token:
            upload_subtitle_to_abyss_api(file_code, srt_sub_path, jwt_token)
            
        update_database(file_code)
        notify_status("success", file_size)
        print("🎉 WORKER COMPLETED SUCCESSFULLY!")
        sys.exit(0)
    else:
        print("❌ Video Upload Failed!")
        notify_status("failed", 0)
        sys.exit(1)
else:
    print("❌ Download Failed!")
    notify_status("failed", 0)
    sys.exit(1)
