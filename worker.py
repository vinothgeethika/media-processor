import os
import sys
import json
import time
import requests
import subprocess
import glob
import re
import concurrent.futures
import firebase_admin
from firebase_admin import credentials, firestore, db
import pysubs2
from deep_translator import GoogleTranslator
from requests_toolbelt.multipart.encoder import MultipartEncoder

# --- 🗣️ SPOKEN SINHALA DICTIONARY ---
try:
    from spoken_dict import SPOKEN_DICT
except ImportError:
    SPOKEN_DICT = {}

def apply_spoken_sinhala(text):
    if not text or not SPOKEN_DICT: 
        return text
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
    firebase_admin.initialize_app(cred, {
        'databaseURL': FIREBASE_DB_URL
    })
fs_db = firestore.client()

# --- ⚙️ ABYSS.TO API SETTINGS ---
ABYSS_API_KEY = os.environ.get("ABYSS_API_KEY", "")
ABYSS_EMAIL = os.environ.get("ABYSS_EMAIL", "")
ABYSS_PASSWORD = os.environ.get("ABYSS_PASSWORD", "")
ABYSS_UPLOAD_URL = f"https://up.abyss.to/{ABYSS_API_KEY}"

# Feedback Node එක වෙනස් කර ඇත (Bot 1 සඳහා)
RTDB_WORKER_FEEDBACK = "worker_job_status_short"

payload = json.loads(os.environ.get("JOB_PAYLOAD", "{}"))
anime_id = payload.get("anilist_id")
ep_num = payload.get("episode")
magnet = payload.get("magnet")
job_type = payload.get("job_type")
search_type = payload.get("search_type")
anime_title = payload.get("title", "Unknown Anime")

print(f"🚀 [WORKER STARTED] Anime: {anime_title} | Ep: {ep_num} | Mode: API SOFTSUB (BOT 1)")

BASE_DIR = "downloads"
TEMP_SUB_DIR = f"temp_subs_ep_{ep_num}"
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TEMP_SUB_DIR, exist_ok=True)

def notify_status(status="failed", file_size=0):
    try:
        db.reference(RTDB_WORKER_FEEDBACK).set({
            "status": status,
            "anilist_id": str(anime_id),
            "episode": int(ep_num),
            "file_size": file_size,
            "timestamp": time.time()
        })
    except Exception as e:
        print(f"⚠️ Failed to write RTDB feedback: {e}")

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

# --- 1. DOWNLOADING VIDEO (Aria2c) ---
def download_video():
    print(f"📥 Starting Download...")
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
                subprocess.run(['aria2c', '--seed-time=0', f'--select-file={target_idx}', f'--dir={BASE_DIR}', torrent_files[0]])
    else:
        subprocess.run(['aria2c', '--seed-time=0', f'--dir={BASE_DIR}', magnet])

    target_ep_int = int(ep_num)
    for root, dirs, files in os.walk(BASE_DIR):
        for f in files:
            if f.endswith(('.mkv', '.mp4')) and extract_ep_number(f) == target_ep_int:
                return os.path.join(root, f)
    for root, dirs, files in os.walk(BASE_DIR):
        for f in files:
            if f.endswith(('.mkv', '.mp4')):
                return os.path.join(root, f)
    return None

# --- 2. EXTRACT & TRANSLATE SUBTITLE (TO .SRT) ---
def clean_vtt_tags(text):
    if not text: return ""
    t = str(text)
    t = re.sub(r'\{.*?\}', '', t).replace('\\h', ' ').replace('\\N', '\n')
    return re.sub(r'<[^>]+>', '', t).strip()

def process_and_translate_subtitle(video_path):
    print("📝 Extracting Embedded Subtitle from Video...")
    eng_sub = os.path.join(TEMP_SUB_DIR, "extracted.srt") 
    
    subprocess.run(['ffmpeg', '-i', video_path, '-map', '0:s:0', eng_sub, '-y'], stderr=subprocess.DEVNULL)
    
    if not os.path.exists(eng_sub) or os.path.getsize(eng_sub) < 100:
        print("❌ Video has no embedded subtitle!")
        return None

    print("⚡ Translating Extracted Subtitle to Sinhala...")
    try: 
        subs = pysubs2.load(eng_sub)
    except: 
        return None

    unique_texts = list(set([clean_vtt_tags(e.text) for e in subs if e.text and len(clean_vtt_tags(e.text)) >= 2]))
    translation_map = {}
    
    def translate_single_line(text):
        translator = GoogleTranslator(source='auto', target='si')
        for attempt in range(5):
            try:
                res = translator.translate(text)
                if res and "Error 500" not in str(res):
                    return apply_spoken_sinhala(res)
            except: pass
            time.sleep(1 + attempt)
        return text

    def safe_translate_batch(batch_chunk):
        translator = GoogleTranslator(source='auto', target='si')
        batch_res = {}
        failed_lines = []
        try:
            res = translator.translate_batch(batch_chunk)
            for orig, trans in zip(batch_chunk, res):
                if "Error 500" in str(trans):
                    failed_lines.append(orig)
                else:
                    batch_res[orig] = apply_spoken_sinhala(trans)
        except:
            failed_lines = list(batch_chunk)
            
        for f_line in failed_lines:
            batch_res[f_line] = translate_single_line(f_line)
            
        return batch_res

    chunks = [unique_texts[i:i+20] for i in range(0, len(unique_texts), 20)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(safe_translate_batch, chunk) for chunk in chunks]
        for future in concurrent.futures.as_completed(futures):
            translation_map.update(future.result())

    for e in subs:
        if e.text:
            cl = clean_vtt_tags(e.text)
            e.text = str(translation_map.get(cl, cl))
            
    wm_text = "සිංහල උපසිරසි සමඟ Anime Movies/Series\nනැරඹීමට හා Download කිරීමට පිවිසෙන්න\n<font color=\"#1E90FF\">anishift.netlify.app</font>"
    
    start_wm = pysubs2.SSAEvent(start=5000, end=15000, text=wm_text)
    subs.insert(0, start_wm)
    
    if len(subs) > 1:
        last_time = max([e.end for e in subs if e.text != wm_text])
        end_wm = pysubs2.SSAEvent(start=last_time + 2000, end=last_time + 12000, text=wm_text)
        subs.append(end_wm)

    sin_sub_srt = os.path.join(TEMP_SUB_DIR, "sinhala_sub.srt")
    subs.save(sin_sub_srt, encoding="utf-8")
    print("✅ Sinhala .SRT Subtitle File Created Successfully!")
    return sin_sub_srt

# --- 3. UPLOAD RAW VIDEO TO ABYSS.TO ---
def upload_video_to_abyss(video_path):
    print("☁️ Uploading Original Video to Abyss.to...")
    upload_filename = os.path.basename(video_path)
    mime_type = 'video/x-matroska' if upload_filename.endswith('.mkv') else 'video/mp4'

    for attempt in range(3):
        try:
            fields = {'file': (upload_filename, open(video_path, 'rb'), mime_type)}
            multipart_data = MultipartEncoder(fields=fields)
            
            headers = {
                'Content-Type': multipart_data.content_type,
                'User-Agent': 'Mozilla/5.0'
            }

            up_resp = requests.post(ABYSS_UPLOAD_URL, data=multipart_data, headers=headers, timeout=1200)
            
            try:
                resp_data = up_resp.json()
            except json.JSONDecodeError:
                if attempt < 2: time.sleep(15)
                continue

            if resp_data.get("status") is True or str(resp_data.get("status")) == "200":
                vhd_code = resp_data.get("slug") or resp_data.get("id") or resp_data.get("code")
                if vhd_code:
                    file_size = os.path.getsize(video_path)
                    print(f"✅ Video Uploaded Successfully! Slug: {vhd_code}")
                    return vhd_code, file_size

        except Exception as e:
            if attempt < 2: time.sleep(15)
                
    return None, 0

# --- 4. UPLOAD SUBTITLE TO ABYSS API (JWT AUTH) ---
def get_abyss_token():
    print("🔑 Authenticating with Abyss to get JWT Token...")
    if not ABYSS_EMAIL or not ABYSS_PASSWORD:
        print("⚠️ ABYSS_EMAIL or ABYSS_PASSWORD not found in environment variables!")
        return None
        
    login_url = "https://api.abyss.to/auth/login"
    login_payload = {"email": ABYSS_EMAIL, "password": ABYSS_PASSWORD}
    
    try:
        login_resp = requests.post(login_url, json=login_payload).json()
        token = login_resp.get("token")
        if token:
            print("✅ JWT Token Retrieved Successfully!")
            return token
        else:
            print(f"❌ Login Failed: {login_resp}")
    except Exception as e:
        print(f"⚠️ Auth Error: {e}")
    return None

def upload_subtitle_to_abyss_api(vhd_code, srt_path, token):
    print(f"☁️ Uploading Sinhala Subtitle to {vhd_code}...")
    try:
        url = f"https://api.abyss.to/v1/upload/subtitles/{vhd_code}?language=Sinhala&filename=sinhala.srt"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "User-Agent": "Mozilla/5.0"
        }
        
        with open(srt_path, "rb") as f:
            sub_data = f.read()
            
        resp = requests.put(url, headers=headers, data=sub_data, timeout=60)
        
        if resp.status_code == 200:
            print("🎉 Subtitle Attached Successfully via Abyss API!")
        else:
            print(f"❌ Failed to attach subtitle. HTTP {resp.status_code}: {resp.text}")
            
    except Exception as e:
        print(f"⚠️ Subtitle API Upload Error: {e}")

# --- 5. UPDATE DATABASE ---
def update_database(file_code):
    print("💾 Updating Firestore...")
    ep_doc_id = f"episode_{int(ep_num):04d}" if str(ep_num).isdigit() else f"episode_{ep_num}"
    fs_db.collection('anime_series').document(str(anime_id)).collection('episodes').document(ep_doc_id).set({
        'status': 'uploaded',
        'links': {
            'abyss_video_id': file_code,
            'abyss_embed': f"https://abyss.to/embed/{file_code}"
        },
        'last_updated': firestore.SERVER_TIMESTAMP
    }, merge=True)

# --- MAIN EXECUTION ---
original_video = download_video()

if original_video:
    srt_sub_path = process_and_translate_subtitle(original_video)
    
    upload_result = upload_video_to_abyss(original_video)
    
    if upload_result and upload_result[0]:
        file_code, file_size = upload_result
        
        if srt_sub_path and os.path.exists(srt_sub_path):
            jwt_token = get_abyss_token()
            if jwt_token:
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
