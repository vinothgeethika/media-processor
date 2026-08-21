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
SUBDL_API_KEY = os.environ.get("SUBDL_API_KEY", "") # SubDL API Key එක
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

# --- 1. DOWNLOADING VIDEO (Aria2c) ---
def download_video():
    print(f"📥 Starting Download...")
    if job_type == "backlog" and search_type == "BATCH":
        print("📦 Processing BATCH Magnet...")
        torrent_file = "temp.torrent"
        subprocess.run(['aria2c', '--bt-metadata-only=true', '--bt-save-metadata=true', '-o', torrent_file, '--seed-time=0', '--bt-stop-timeout=120', magnet])
        
        if os.path.exists(torrent_file):
            from torrentool.api import Torrent
            my_torrent = Torrent.from_file(torrent_file)
            target_idx = None
            
            for idx, f in enumerate(my_torrent.files, start=1):
                if any(f.name.lower().endswith(ext) for ext in ['.mkv', '.mp4']):
                    match = re.search(r'[sS]\d+[eE]0*(\d+)', f.name) or re.search(r'\b(?:ep|episode)\.?\s?0*(\d+)\b', f.name.lower())
                    if match and int(match.group(1)) == int(ep_num):
                        target_idx = idx
                        break
            
            if target_idx:
                print(f"🎯 Found Episode {ep_num} at index {target_idx}. Downloading...")
                subprocess.run(['aria2c', '--seed-time=0', f'--select-file={target_idx}', f'--dir={BASE_DIR}', torrent_file])
            else:
                print("❌ Episode not found in batch!")
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
    """සබ් එකේ පේළි ගාණ බලලා ඒක හොඳ එකක්ද (Dialogs තියෙනවද) කියලා බලයි"""
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 500:
        return False
    try:
        subs = pysubs2.load(file_path)
        if len(subs) < 50: # පේළි 50කට වඩා අඩු නම් අවුල්
            return False
        return True
    except:
        return False

def clean_search_title(title):
    t = re.sub(r'(?:\b|_)(?:s|season|series)\s?\d+(?:\b|_)', ' ', title, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', t).strip()

def search_subdl_for_episode(title, target_ep):
    """SubDL API එකෙන් සබ් එක බාගෙන නිවැරදි Episode එක තෝරා ගනී"""
    if not SUBDL_API_KEY:
        print("⚠️ SubDL API Key is not set!")
        return None
        
    print(f"🔍 Searching SubDL for: {title} Ep {target_ep}...")
    query = clean_search_title(title)
    api_url = f"https://api.subdl.com/api/v1/subtitles?api_key={SUBDL_API_KEY}&query={query}&languages=EN"
    
    try:
        resp = requests.get(api_url, timeout=20).json()
        if not resp.get('status') or not resp.get('results'):
            return None
            
        # පළමු ප්‍රතිඵලය (වැඩිපුරම ඩවුන්ලෝඩ් කරපු) ගන්නවා
        best_result = resp['results'][0]
        dl_url = "https://dl.subdl.com" + best_result['url']
        zip_path = os.path.join(TEMP_SUB_DIR, "subdl.zip")
        
        print("📥 Downloading SubDL Archive...")
        zip_data = requests.get(dl_url, timeout=30).content
        with open(zip_path, 'wb') as f:
            f.write(zip_data)
            
        print("📦 Extracting and Searching inside ZIP...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(TEMP_SUB_DIR)
            
        # Extract කරපු ෆයිල් අස්සේ අදාළ Episode Number එක තියෙන SRT/VTT එක හොයනවා
        extracted_files = []
        for root, dirs, files in os.walk(TEMP_SUB_DIR):
            for f in files:
                if f.endswith(('.srt', '.vtt', '.ass')):
                    extracted_files.append(os.path.join(root, f))
        
        target_ep_int = int(target_ep)
        
        for sub_file in extracted_files:
            fname = os.path.basename(sub_file).lower()
            # Episode නම්බර් එක ෆයිල් නමේ තියෙනවද බලනවා (e.g. S01E05, - 05)
            match = re.search(r'[sS]\d+[eE]0*(\d+)', fname) or re.search(r'\b(?:ep|episode)\.?\s?0*(\d+)\b', fname) or re.search(r'\b0*(\d+)\b', fname)
            
            if match and int(match.group(1)) == target_ep_int:
                print(f"🎯 Found Matching SubDL file: {fname}")
                if is_valid_subtitle(sub_file):
                    return sub_file
                    
        # Episode එකක් විශේෂයෙන් හම්බවුණේ නැත්නම්, තියෙන එකම ෆයිල් එක ගන්නවා (Single file release)
        if len(extracted_files) == 1 and is_valid_subtitle(extracted_files[0]):
            print("🎯 Using the only valid subtitle found in the archive.")
            return extracted_files[0]
            
    except Exception as e:
        print(f"⚠️ SubDL Error: {e}")
        
    return None

def clean_tags(text):
    return re.sub(r'\{.*?\}|<[^>]+>', '', text).strip()

def process_subtitles(video_path):
    print("📝 Extracting Subtitles from Video...")
    eng_sub = "english.srt"
    subprocess.run(['ffmpeg', '-i', video_path, '-map', '0:s:0', eng_sub, '-y'], stderr=subprocess.DEVNULL)
    
    valid_eng_sub = None
    
    # 1. Video එකේ සබ් එක චෙක් කරනවා
    if is_valid_subtitle(eng_sub):
        print("✅ Embedded Subtitle is VALID.")
        valid_eng_sub = eng_sub
    else:
        # 2. අවුල් නම් හෝ නැත්නම් SubDL එකෙන් හොයනවා
        print("⚠️ Embedded Subtitle is MISSING or BROKEN. Falling back to SubDL...")
        subdl_file = search_subdl_for_episode(anime_title, ep_num)
        if subdl_file:
            valid_eng_sub = subdl_file
        else:
            print("❌ Could not find a valid subtitle anywhere!")
            return None

    print("⚡ Fast Translating Subtitles...")
    subs = pysubs2.load(valid_eng_sub)
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
    else:
        print("❌ Upload Failed!")
        if job_key: firebase_admin.db.reference(f"sever_3_job/{job_key}").update({"status": "worker_failed"})
else:
    print("❌ Download Failed!")
    if job_key: firebase_admin.db.reference(f"sever_3_job/{job_key}").update({"status": "worker_failed"})

# Clean up temporary zip folder
if os.path.exists(TEMP_SUB_DIR):
    shutil.rmtree(TEMP_SUB_DIR)