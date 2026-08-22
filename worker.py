import os
import sys
import json
import time
import requests
import subprocess
import glob
import re
import urllib.parse
import concurrent.futures
import mimetypes
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
ABYSS_API_KEY = "19136c9e1c8d2cac4e0e8b612008050a"
ABYSS_UPLOAD_URL = f"https://up.abyss.to/{ABYSS_API_KEY}"

payload = json.loads(os.environ.get("JOB_PAYLOAD", "{}"))
anime_id = payload.get("anilist_id")
ep_num = payload.get("episode")
magnet = payload.get("magnet")
job_type = payload.get("job_type")
search_type = payload.get("search_type")
anime_title = payload.get("title", "Unknown Anime")

print(f"🚀 [WORKER STARTED] Anime: {anime_title} | Ep: {ep_num}")

BASE_DIR = "downloads"
TEMP_SUB_DIR = f"temp_subs_ep_{ep_num}"
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TEMP_SUB_DIR, exist_ok=True)

def notify_status(status="failed", file_size=0):
    try:
        db.reference("worker_job_status").set({
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

# --- 2. EXTRACT EMBEDDED SUBTITLE & TRANSLATE (NO MUXING) ---
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
    
    # Save as SRT format
    sin_sub_srt = os.path.join(TEMP_SUB_DIR, "sinhala.srt")
    subs.save(sin_sub_srt, encoding="utf-8")
    print("✅ Sinhala Subtitle File Created Successfully!")
    return sin_sub_srt

# --- 3. UPLOAD RAW VIDEO TO ABYSS.TO ---
def upload_video_to_abyss(video_path):
    print("☁️ Uploading RAW Video to Abyss.to...")
    try:
        upload_filename = os.path.basename(video_path)
        mime_type = 'video/x-matroska' if upload_filename.endswith('.mkv') else 'video/mp4'

        fields = {
            'file': (upload_filename, open(video_path, 'rb'), mime_type)
        }

        multipart_data = MultipartEncoder(fields=fields)
        headers = {'Content-Type': multipart_data.content_type}
        
        up_resp = requests.post(ABYSS_UPLOAD_URL, data=multipart_data, headers=headers, timeout=1200).json()
        print(f"📥 Abyss.to Video Response: {up_resp}")
        
        if up_resp.get("status") is True or str(up_resp.get("status")) == "200":
            vhd_code = up_resp.get("slug") or up_resp.get("id") or up_resp.get("code")
            if vhd_code:
                file_size = os.path.getsize(video_path)
                print(f"✅ Video Uploaded Successfully! Slug: {vhd_code}")
                return vhd_code, file_size
    except Exception as e:
        print(f"⚠️ Abyss Video Upload Error: {e}")
    return None, 0

# --- 4. UPLOAD SUBTITLE DIRECTLY TO ABYSS API ---
def upload_subtitle_to_abyss(vhd_code, sub_path):
    print(f"☁️ Uploading Subtitle File directly to Abyss.to ({vhd_code})...")
    try:
        # Document එකේ විදිහටම Query Parameters සකස් කිරීම
        url = f"https://api.abyss.to/v1/upload/subtitles/{vhd_code}?language=Sinhala&filename=sinhala.srt"
        
        # Headers සකස් කිරීම
        headers = {
            "Authorization": f"Bearer {ABYSS_API_KEY}",
            "Content-Type": "application/octet-stream"
        }
        
        # Binary විදිහට Subtitle file එක කියවීම
        with open(sub_path, 'rb') as f:
            sub_data = f.read()
            
        # curl --data-binary එකට සමානව PUT Request එක යැවීම
        resp = requests.put(url, headers=headers, data=sub_data, timeout=60)
        
        print(f"📥 Abyss Subtitle API Response [{resp.status_code}]: {resp.text}")
        
        if resp.status_code == 200:
            print("✅ Subtitle Uploaded and Linked Successfully!")
        else:
            print(f"❌ Failed to upload subtitle: {resp.status_code}")
            
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
    translated_sub_path = process_and_translate_subtitle(original_video)
    
    upload_result = upload_video_to_abyss(original_video)
    
    if upload_result and upload_result[0]:
        file_code, file_size = upload_result
        
        # ඔයා දුන්න Document එකේ විදිහට Subtitle ෆයිල් එක කෙලින්ම යවමු
        if translated_sub_path and os.path.exists(translated_sub_path):
            upload_subtitle_to_abyss(file_code, translated_sub_path)
        
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
