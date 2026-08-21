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
from firebase_admin import credentials, firestore, db
import pysubs2
from deep_translator import GoogleTranslator

# --- ⚙️ SETUP FIREBASE ---
cred = credentials.Certificate("serviceAccountKey.json")
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "https://your-rtdb-default.firebaseio.com")

firebase_admin.initialize_app(cred, {
    'databaseURL': FIREBASE_DB_URL
})
fs_db = firestore.client()

# --- ⚙️ ENVIRONMENT VARIABLES & PAYLOAD ---
STREAMHG_API_KEY = os.environ.get("STREAMHG_API_KEY")
STREAMHG_BASE_URL = "https://streamhgapi.com/api"
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
OUTPUT_DIR = "output_muxed"
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TEMP_SUB_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

def notify_failure(reason="failed"):
    try:
        db.reference("worker_job_status").set({
            "status": "failed",
            "anilist_id": str(anime_id),
            "episode": ep_num,
            "reason": reason,
            "timestamp": time.time()
        })
        if job_key:
            db.reference(f"sever_3_job/{job_key}").update({"status": "worker_failed"})
    except Exception as e:
        print(f"⚠️ Failed to write RTDB feedback: {e}")

def notify_success():
    try:
        db.reference("worker_job_status").set({
            "status": "success",
            "anilist_id": str(anime_id),
            "episode": ep_num,
            "timestamp": time.time()
        })
        if job_key:
            db.reference(f"sever_3_job/{job_key}").update({"status": "completed"})
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
    if job_type == "backlog" and search_type == "BATCH":
        print("📦 Processing BATCH Magnet...")
        for old_t in glob.glob("*.torrent"):
            try: os.remove(old_t)
            except: pass
            
        subprocess.run(['aria2c', '--bt-metadata-only=true', '--bt-save-metadata=true', '--seed-time=0', '--bt-stop-timeout=120', magnet])
        torrent_files = glob.glob("*.torrent")
        
        if torrent_files:
            torrent_file = torrent_files[0]
            from torrentool.api import Torrent
            my_torrent = Torrent.from_file(torrent_file)
            target_idx = None
            for idx, f in enumerate(my_torrent.files, start=1):
                if any(f.name.lower().endswith(ext) for ext in ['.mkv', '.mp4']):
                    if extract_ep_number(os.path.basename(f.name)) == int(ep_num):
                        target_idx = idx
                        break
            if target_idx:
                subprocess.run(['aria2c', '--seed-time=0', f'--select-file={target_idx}', f'--dir={BASE_DIR}', torrent_file])
            else:
                print("❌ Episode not found inside batch!")
                return None
        else:
            print("❌ Failed to grab torrent metadata!")
            return None
    else:
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
        return len(subs) >= 20
    except:
        return False

def clean_pure_title(title):
    t = re.sub(r'(?i)(season|part|cour|ep|episode|s\d+|e\d+).*', '', title)
    return re.sub(r'\s+', ' ', t).strip()

def search_subdl_for_episode(title, target_ep):
    if not SUBDL_API_KEY: return None
    pure_title = clean_pure_title(title)
    api_url = f"https://api.subdl.com/api/v1/subtitles?api_key={SUBDL_API_KEY}&film_name={urllib.parse.quote(pure_title)}&type=tv&languages=EN&unpack=1"
    
    try:
        resp = requests.get(api_url, timeout=20).json()
        if not resp.get('status') or not resp.get('subtitles'): return None
        target_ep_int = int(target_ep)
        best_sub_url = None
        
        for sub in resp.get('subtitles', []):
            if sub.get('episode') == target_ep_int:
                if sub.get('unpack_files'):
                    for u_file in sub['unpack_files']:
                        if u_file.get('episode') == target_ep_int:
                            best_sub_url = u_file.get('url')
                            break
                if not best_sub_url: best_sub_url = sub.get('url')
                break
            elif sub.get('full_season') or (sub.get('episode_from') and sub.get('episode_end') and sub['episode_from'] <= target_ep_int <= sub['episode_end']):
                if sub.get('unpack_files'):
                    for u_file in sub['unpack_files']:
                        if u_file.get('episode') == target_ep_int:
                            best_sub_url = u_file.get('url')
                            break
                if best_sub_url: break

        if best_sub_url:
            dl_url = "https://dl.subdl.com" + best_sub_url
            sub_data = requests.get(dl_url, timeout=30).content
            if sub_data[:4] == b'PK\x03\x04': 
                zip_path = os.path.join(TEMP_SUB_DIR, "subdl.zip")
                with open(zip_path, 'wb') as f: f.write(sub_data)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref: zip_ref.extractall(TEMP_SUB_DIR)
                for root, dirs, files in os.walk(TEMP_SUB_DIR):
                    for f in files:
                        if f.endswith(('.srt', '.vtt', '.ass')):
                            sub_path = os.path.join(root, f)
                            if is_valid_subtitle(sub_path): return sub_path
            else:
                sub_path = os.path.join(TEMP_SUB_DIR, "subdl_extracted.srt")
                with open(sub_path, 'wb') as f: f.write(sub_data)
                if is_valid_subtitle(sub_path): return sub_path
    except: pass
    return None

def clean_tags(text):
    return re.sub(r'\{.*?\}|<[^>]+>', '', text).strip()

def process_subtitles_and_mux(video_path):
    print("📝 Checking for Subtitles...")
    eng_sub = "english.ass" 
    subprocess.run(['ffmpeg', '-i', video_path, '-map', '0:s:0', eng_sub, '-y'], stderr=subprocess.DEVNULL)
    
    valid_eng_sub = eng_sub if is_valid_subtitle(eng_sub) else search_subdl_for_episode(anime_title, ep_num)
    
    if not valid_eng_sub:
        print("❌ Could not find a valid subtitle. Uploading original video without Sinhala subs.")
        return video_path

    print("⚡ Translating Subtitles to Sinhala...")
    try: 
        subs = pysubs2.load(valid_eng_sub)
    except: 
        return video_path

    unique_texts = list(set([clean_tags(e.text) for e in subs if clean_tags(e.text)]))
    translation_map = {}
    
    def translate_chunk(chunk):
        translator = GoogleTranslator(source='auto', target='si')
        for _ in range(3):
            try:
                translated = translator.translate_batch(chunk)
                return {orig: trans for orig, trans in zip(chunk, translated)}
            except: time.sleep(1)
        return {orig: orig for orig in chunk}

    chunks = [unique_texts[i:i+40] for i in range(0, len(unique_texts), 40)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        for future in concurrent.futures.as_completed([executor.submit(translate_chunk, chunk) for chunk in chunks]):
            translation_map.update(future.result())

    for e in subs:
        clean_text = clean_tags(e.text)
        e.text = translation_map.get(clean_text, e.text)
    
    sin_sub = "sinhala.srt"
    subs.save(sin_sub)
    print("✅ Sinhala Translation Complete!")

    # Original Filename එක ආරක්ෂා කරගනිමින් output path එක සැකසීම
    original_filename = os.path.basename(video_path)
    base_name, _ = os.path.splitext(original_filename)
    output_filename = f"{base_name}.mkv"
    muxed_video_path = os.path.join(OUTPUT_DIR, output_filename)

    print(f"🎬 Muxing: Adding Sinhala track & keeping original filename [{output_filename}]...")
    probe_cmd = ['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries', 'stream=index', '-of', 'csv=p=0', video_path]
    probe_res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    existing_sub_count = len(probe_res.stdout.strip().splitlines()) if probe_res.stdout.strip() else 0

    cmd = [
        'ffmpeg', '-i', video_path, '-i', sin_sub,
        '-map', '0:v', '-map', '0:a', '-map', '0:s?', '-map', '1:s:0',
        '-c', 'copy', '-c:s', 'srt',
        f'-metadata:s:s:{existing_sub_count}', 'language=sin',
        f'-metadata:s:s:{existing_sub_count}', 'title=Sinhala',
        f'-disposition:s:{existing_sub_count}', 'default',
        muxed_video_path, '-y'
    ]
    
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    if os.path.exists(muxed_video_path):
        print(f"✅ Subtitle Muxing Complete! Output: {output_filename}")
        return muxed_video_path
    return video_path

# --- 3. UPLOAD TO STREAMHG ---
def upload_to_streamhg(final_video_path):
    print("☁️ Getting Upload Server...")
    try:
        srv_resp = requests.get(f"{STREAMHG_BASE_URL}/upload/server?key={STREAMHG_API_KEY}", timeout=15).json()
        if srv_resp.get("status") != 200: 
            print(f"❌ Server fetch failed: {srv_resp}")
            return None
        
        upload_url = srv_resp["result"]
        upload_filename = os.path.basename(final_video_path)
        print(f"☁️ Uploading Video as Original Name: {upload_filename}...")
        
        with open(final_video_path, 'rb') as f:
            data = {'key': STREAMHG_API_KEY, 'fld_id': folder_id} if folder_id else {'key': STREAMHG_API_KEY}
            up_resp = requests.post(upload_url, data=data, files={'file': (upload_filename, f)}, timeout=600).json()
        
        print(f"📥 StreamHG Response: {up_resp}")
        if up_resp.get("status") == 200:
            files_list = up_resp.get("files", [])
            if files_list and isinstance(files_list, list):
                vhd_code = files_list[0].get("filecode")
                if vhd_code:
                    print(f"✅ Video Uploaded Successfully! FileCode: {vhd_code}")
                    return vhd_code
            elif "result" in up_resp and isinstance(up_resp["result"], dict):
                vhd_code = up_resp["result"].get("filecode")
                if vhd_code:
                    print(f"✅ Video Uploaded Successfully! FileCode: {vhd_code}")
                    return vhd_code
    except Exception as e:
        print(f"⚠️ Upload Error: {e}")
    return None

# --- 4. UPDATE DATABASE ---
def update_database(file_code):
    print("💾 Updating Firestore...")
    ep_doc_id = f"episode_{int(ep_num):04d}" if str(ep_num).isdigit() else f"episode_{ep_num}"
    fs_db.collection('anime_series').document(str(anime_id)).collection('episodes').document(ep_doc_id).set({
        'status': 'uploaded',
        'links': {
            'streamhg_video_id': file_code,
            'streamhg_embed': f"https://streamhg.com/e/{file_code}"
        },
        'last_updated': firestore.SERVER_TIMESTAMP
    }, merge=True)

# --- MAIN EXECUTION ---
original_video = download_video()

if original_video:
    final_video = process_subtitles_and_mux(original_video)
    file_code = upload_to_streamhg(final_video)
    
    if file_code:
        update_database(file_code)
        notify_success()
        print("🎉 WORKER COMPLETED SUCCESSFULLY!")
        if os.path.exists(TEMP_SUB_DIR): shutil.rmtree(TEMP_SUB_DIR)
        if os.path.exists(OUTPUT_DIR): shutil.rmtree(OUTPUT_DIR)
        sys.exit(0)
    else:
        print("❌ Upload Failed!")
        notify_failure("upload_failed")
        sys.exit(1)
else:
    print("❌ Download Failed!")
    notify_failure("download_failed")
    sys.exit(1)
