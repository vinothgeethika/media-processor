import os
import sys
import json
import time
import requests
import subprocess
import glob
import re
import zipfile
import shutil
import urllib.parse
import concurrent.futures
import firebase_admin
from firebase_admin import credentials, firestore
import pysubs2
from deep_translator import GoogleTranslator

# --- SETUP FIREBASE ---
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- ENVIRONMENT VARIABLES ---
VIDEHIDE_API_KEY = os.environ.get("VIDEHIDE_API_KEY")
VIDEHIDE_BASE_URL = "https://earnvidsapi.com/api"
SUBDL_API_KEY = os.environ.get("SUBDL_API_KEY", "") 
payload = json.loads(os.environ.get("JOB_PAYLOAD", "{}"))

anime_id = payload.get("anilist_id")
ep_num = payload.get("episode")
magnet = payload.get("magnet")
folder_id = payload.get("folder_id")
job_type = payload.get("job_type")
search_type = payload.get("search_type")
job_key = payload.get("job_key")
anime_title = payload.get("title", "Unknown Anime")

print(f"🚀 [WORKER STARTED] Anime: {anime_title} | Ep: {ep_num} | Job: {job_type}")

BASE_DIR = "downloads"
TEMP_SUB_DIR = "temp_subs"
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TEMP_SUB_DIR, exist_ok=True)

def extract_ep_number(filename):
    clean = re.sub(r'\[.*?\]|\(.*?\)', ' ', filename.lower())
    clean = re.sub(r'\b(1080p|720p|480p|x264|x265|hevc|10bit|8bit)\b', ' ', clean)
    
    m = re.search(r'[sS]\d+[eE]0*(\d+)', clean)
    if m: return int(m.group(1))
    m = re.search(r'\b(?:ep|episode)\.?\s?0*(\d+)\b', clean)
    if m: return int(m.group(1))
    m = re.search(r'\s-\s0*(\d+)(?:v\d)?\b', clean)
    if m: return int(m.group(1))
    return None

# --- 1. DOWNLOADING VIDEO (Aria2c) ---
def download_video():
    print(f"📥 Starting Download...")
    if job_type == "backlog" and search_type == "BATCH":
        print("📦 Processing BATCH Magnet...")
        for old_t in glob.glob("*.torrent"):
            os.remove(old_t)
            
        subprocess.run(['aria2c', '--bt-metadata-only=true', '--bt-save-metadata=true', '--seed-time=0', '--bt-stop-timeout=120', magnet])
        torrent_files = glob.glob("*.torrent")
        
        if torrent_files:
            torrent_file = torrent_files[0]
            print(f"✅ Metadata saved as: {torrent_file}")
            
            from torrentool.api import Torrent
            my_torrent = Torrent.from_file(torrent_file)
            target_idx = None
            
            for idx, f in enumerate(my_torrent.files, start=1):
                if any(f.name.lower().endswith(ext) for ext in ['.mkv', '.mp4']):
                    found_ep = extract_ep_number(os.path.basename(f.name))
                    if found_ep == int(ep_num):
                        target_idx = idx
                        break
            
            if target_idx:
                print(f"⏳ Downloading specific episode (Index {target_idx}) from batch...")
                subprocess.run(['aria2c', '--seed-time=0', f'--select-file={target_idx}', f'--dir={BASE_DIR}', torrent_file])
            else:
                print("❌ Episode not found inside the batch torrent!")
                return None
        else:
            print("❌ Failed to download torrent metadata!")
            return None
    else:
        print("🎬 Processing Single Episode Magnet...")
        subprocess.run(['aria2c', '--seed-time=0', f'--dir={BASE_DIR}', magnet])

    for root, dirs, files in os.walk(BASE_DIR):
        for f in files:
            if f.endswith(('.mkv', '.mp4')):
                return os.path.join(root, f)
    return None

# --- 2. SUBTITLE HANDLING & SUBDL API ---
def is_valid_subtitle(file_path):
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 100:
        return False
    try:
        subs = pysubs2.load(file_path)
        if len(subs) < 25: # පේළි ගාණ 25ට අඩු නම් (උදා: සින්දු කෑලි විතරක් නම්) ප්‍රතික්ෂේප කරයි
            return False
        return True
    except:
        return False

def clean_pure_title(title):
    # 'Season X', 'Part Y', 'Ep Z' වැනි සියලු දේ ඉවත් කර නියම නම පමණක් ලබා ගනී
    t = re.sub(r'(?i)(season|part|cour|ep|episode|s\d+|e\d+).*', '', title)
    return re.sub(r'\s+', ' ', t).strip()

def search_subdl_for_episode(title, target_ep):
    if not SUBDL_API_KEY:
        print("⚠️ SubDL API Key is not set!")
        return None
        
    pure_title = clean_pure_title(title)
    print(f"🔍 Searching SubDL API for: '{pure_title}' Ep {target_ep}...")
    
    # Docs වලට අනුව හරියටම නම පමණක් (film_name) සහ unpack=1 භාවිතා කිරීම[cite: 6]
    api_url = f"https://api.subdl.com/api/v1/subtitles?api_key={SUBDL_API_KEY}&film_name={urllib.parse.quote(pure_title)}&type=tv&languages=EN&unpack=1"
    
    try:
        resp = requests.get(api_url, timeout=20).json()
        if not resp.get('status') or not resp.get('subtitles'):
            print("❌ SubDL: No subtitles found for this title.")
            return None
            
        subtitles = resp.get('subtitles', [])
        target_ep_int = int(target_ep)
        best_sub_url = None
        
        # අදාළ Episode එකට ගැළපෙන සබ් එක තෝරා ගැනීම[cite: 6]
        for sub in subtitles:
            # කෙලින්ම Episode Number එක සමාන නම්
            if sub.get('episode') == target_ep_int:
                if sub.get('unpack_files'):
                    for u_file in sub['unpack_files']:
                        if u_file.get('episode') == target_ep_int:
                            best_sub_url = u_file.get('url')
                            break
                if not best_sub_url:
                    best_sub_url = sub.get('url')
                break
                
            # Season Pack එකක් නම් සහ අපේ Episode එක ඒ සීමාවේ තියෙනවා නම්
            elif sub.get('full_season') or (sub.get('episode_from') and sub.get('episode_end') and sub['episode_from'] <= target_ep_int <= sub['episode_end']):
                if sub.get('unpack_files'):
                    for u_file in sub['unpack_files']:
                        if u_file.get('episode') == target_ep_int:
                            best_sub_url = u_file.get('url')
                            break
                if best_sub_url:
                    break

        if best_sub_url:
            dl_url = "https://dl.subdl.com" + best_sub_url
            print(f"📥 Downloading SubDL subtitle from: {dl_url}")
            sub_data = requests.get(dl_url, timeout=30).content
            
            # Zip එකක්ද, සාමාන්‍ය .srt එකක්ද කියා පරීක්ෂා කිරීම
            if sub_data[:4] == b'PK\x03\x04': 
                zip_path = os.path.join(TEMP_SUB_DIR, "subdl.zip")
                with open(zip_path, 'wb') as f:
                    f.write(sub_data)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(TEMP_SUB_DIR)
                
                for root, dirs, files in os.walk(TEMP_SUB_DIR):
                    for f in files:
                        if f.endswith(('.srt', '.vtt', '.ass')):
                            sub_path = os.path.join(root, f)
                            if is_valid_subtitle(sub_path):
                                return sub_path
            else:
                sub_path = os.path.join(TEMP_SUB_DIR, "subdl_extracted.srt")
                with open(sub_path, 'wb') as f:
                    f.write(sub_data)
                if is_valid_subtitle(sub_path):
                    return sub_path
                    
        print("❌ SubDL: Episode not found in the results.")
    except Exception as e:
        print(f"⚠️ SubDL Error: {e}")
        
    return None

def clean_tags(text):
    return re.sub(r'\{.*?\}|<[^>]+>', '', text).strip()

def process_subtitles(video_path):
    print("📝 Extracting Subtitles from Video...")
    eng_sub = "english.ass" # FFmpeg සමහරවිට ass ලෙස දෙන බැවින් මෙලෙස සේව් කිරීම ආරක්ෂිතයි
    
    # Video එකෙන් සබ් එක ගැනීම (Errors ආවොත් Crash නොවී ඉස්සරහට යයි)
    subprocess.run(['ffmpeg', '-i', video_path, '-map', '0:s:0', eng_sub, '-y'], stderr=subprocess.DEVNULL)
    
    valid_eng_sub = None
    
    if is_valid_subtitle(eng_sub):
        print("✅ Embedded Subtitle is VALID.")
        valid_eng_sub = eng_sub
    else:
        print("⚠️ Embedded Subtitle is MISSING or BROKEN. Falling back to SubDL...")
        subdl_file = search_subdl_for_episode(anime_title, ep_num)
        if subdl_file:
            valid_eng_sub = subdl_file
        else:
            print("❌ Could not find a valid subtitle anywhere!")
            return None

    print("⚡ Fast Translating Subtitles...")
    try:
        subs = pysubs2.load(valid_eng_sub)
    except:
        print("❌ Failed to parse subtitle file!")
        return None

    unique_texts = list(set([clean_tags(e.text) for e in subs if clean_tags(e.text)]))
    translation_map = {}
    
    def translate_chunk(chunk):
        translator = GoogleTranslator(source='auto', target='si')
        for _ in range(3):
            try:
                translated = translator.translate_batch(chunk)
                return {orig: trans for orig, trans in zip(chunk, translated)}
            except:
                time.sleep(1)
        return {orig: orig for orig in chunk}

    chunks = [unique_texts[i:i+40] for i in range(0, len(unique_texts), 40)]
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        futures = [executor.submit(translate_chunk, chunk) for chunk in chunks]
        for future in concurrent.futures.as_completed(futures):
            translation_map.update(future.result())

    for e in subs:
        clean_text = clean_tags(e.text)
        e.text = translation_map.get(clean_text, e.text)
    
    sin_sub = "sinhala.vtt"
    subs.save(sin_sub)
    print("✅ Translation Complete!")
    return sin_sub

# --- 3. UPLOAD TO VIDEHIDE ---
def upload_to_videhide(video_path, sub_path):
    print("☁️ Getting Upload Server...")
    srv_resp = requests.get(f"{VIDEHIDE_BASE_URL}/upload/server?key={VIDEHIDE_API_KEY}").json()
    if srv_resp.get("status") != 200:
        return None
    upload_url = srv_resp["result"]

    print("☁️ Uploading Video...")
    with open(video_path, 'rb') as f:
        data = {'key': VIDEHIDE_API_KEY, 'fld_id': folder_id} if folder_id else {'key': VIDEHIDE_API_KEY}
        files = {'file': (os.path.basename(video_path), f)}
        up_resp = requests.post(upload_url, data=data, files=files).json()
    
    if up_resp.get("status") != 200 or not up_resp.get("files"):
        return None
    
    vhd_code = up_resp["files"][0]["filecode"]
    print(f"✅ Video Uploaded! FileCode: {vhd_code}")

    if sub_path:
        print("☁️ Uploading Subtitle...")
        with open(sub_path, 'rb') as sf:
            sub_data = {'key': VIDEHIDE_API_KEY, 'file_code': vhd_code, 'sub_lang': 'sin'}
            requests.post(f"{VIDEHIDE_BASE_URL}/upload/sub", data=sub_data, files={'sub_file': ('1.vtt', sf)})

    return vhd_code

# --- 4. UPDATE DATABASE ---
def update_database(vhd_code):
    print("💾 Updating Firestore...")
    ep_doc_id = f"episode_{int(ep_num):04d}" if str(ep_num).isdigit() else f"episode_{ep_num}"
    doc_ref = db.collection('anime_series').document(str(anime_id)).collection('episodes').document(ep_doc_id)
    
    doc_ref.set({
        'status': 'uploaded',
        'links': {
            'vhd_video_id': vhd_code,
            'vhd_stream': f"https://s1.xvs.tt/hls/{vhd_code}/master.m3u8"
        },
        'last_updated': firestore.SERVER_TIMESTAMP
    }, merge=True)

    if job_type in ["custom_job", "server_3_manual"] and job_key:
        print("💾 Updating RTDB sever_3_job status...")
        rtdb_ref = firebase_admin.db.reference(f"sever_3_job/{job_key}")
        rtdb_ref.update({"status": "completed"})

# --- MAIN EXECUTION ---
video_file = download_video()

if video_file:
    subtitle_file = process_subtitles(video_file)
    vhd_filecode = upload_to_videhide(video_file, subtitle_file)
    
    if vhd_filecode:
        update_database(vhd_filecode)
        print("🎉 WORKER COMPLETED SUCCESSFULLY!")
        
        if os.path.exists(TEMP_SUB_DIR):
            shutil.rmtree(TEMP_SUB_DIR)
        sys.exit(0)
    else:
        print("❌ Upload Failed!")
        if job_key: firebase_admin.db.reference(f"sever_3_job/{job_key}").update({"status": "worker_failed"})
        sys.exit(1)
else:
    print("❌ Download Failed!")
    if job_key: firebase_admin.db.reference(f"sever_3_job/{job_key}").update({"status": "worker_failed"})
    sys.exit(1)
