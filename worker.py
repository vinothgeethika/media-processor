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
import mimetypes
import firebase_admin
from firebase_admin import credentials, firestore, db
import pysubs2
from deep_translator import GoogleTranslator

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

def clean_vtt_tags(text):
    if not text: return ""
    t = str(text)
    t = re.sub(r'\{.*?\}', '', t).replace('\\h', ' ').replace('\\N', '\n')
    t = re.sub(r'<[^>]+>', '', t)
    return t.strip()

def is_garbage_sub(text):
    if not text: return True
    if re.search(r'\\pos\(|\\c&H|\\alpha|\\t\(|\\fad\(|\\an\d', str(text)): return True
    cl = clean_vtt_tags(text)
    if re.match(r'^m\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+(?:l|b|s|c|m)\s+', cl): return True
    return False

def is_error_text(t):
    if not t: return True
    t_str = str(t)
    if "Error 500" in t_str or "Server Error" in t_str or "<!DOCTYPE html>" in t_str or "That’s an error" in t_str:
        return True
    return False

def translate_single_line_guaranteed(text):
    translator = GoogleTranslator(source='auto', target='si')
    for attempt in range(5):
        try:
            res = translator.translate(text)
            if res and not is_error_text(res):
                return apply_spoken_sinhala(res)
        except: pass
        time.sleep(1 + attempt)
    return text

def process_subtitles_and_mux(video_path):
    print("📝 Checking for Subtitles...")
    eng_sub = "english.ass" 
    subprocess.run(['ffmpeg', '-i', video_path, '-map', '0:s:0', eng_sub, '-y'], stderr=subprocess.DEVNULL)
    
    valid_eng_sub = eng_sub if is_valid_subtitle(eng_sub) else search_subdl_for_episode(anime_title, ep_num)
    
    if not valid_eng_sub:
        print("❌ Could not find a valid subtitle. Uploading original video without Sinhala subs.")
        return video_path

    print("⚡ Translating Subtitles to Sinhala with Spoken Sinhala Dictionary...")
    try: 
        subs = pysubs2.load(valid_eng_sub)
    except: 
        return video_path

    unique_texts = list(set([clean_vtt_tags(e.text) for e in subs if e.text and not is_garbage_sub(e.text) and len(clean_vtt_tags(e.text)) >= 2]))
    translation_map = {}
    
    def safe_translate_batch(batch_chunk):
        translator = GoogleTranslator(source='auto', target='si')
        batch_res = {}
        failed_lines = []
        try:
            res = translator.translate_batch(batch_chunk)
            for orig, trans in zip(batch_chunk, res):
                if is_error_text(trans):
                    failed_lines.append(orig)
                else:
                    batch_res[orig] = apply_spoken_sinhala(trans)
        except:
            failed_lines = list(batch_chunk)
            
        for f_line in failed_lines:
            batch_res[f_line] = translate_single_line_guaranteed(f_line)
            
        return batch_res

    chunks = [unique_texts[i:i+20] for i in range(0, len(unique_texts), 20)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(safe_translate_batch, chunk) for chunk in chunks]
        for future in concurrent.futures.as_completed(futures):
            translation_map.update(future.result())

    for e in subs:
        raw_text = str(e.text) if e.text is not None else ""
        if is_garbage_sub(raw_text):
            e.text = ""
            continue
        clean_text = clean_vtt_tags(raw_text)
        if clean_text in translation_map:
            e.text = str(translation_map[clean_text])
        else:
            e.text = clean_text
    
    sin_sub = "sinhala.srt"
    subs.save(sin_sub, encoding="utf-8")
    print("✅ Sinhala Translation 100% Completed!")

    orig_filename = os.path.basename(video_path)
    base_name, _ = os.path.splitext(orig_filename)
    output_filename = f"{base_name}.mkv"
    muxed_video_path = os.path.join(OUTPUT_DIR, output_filename)

    print(f"🎬 Muxing: Adding ONLY Sinhala track [{output_filename}]...")

    cmd = [
        'ffmpeg', '-i', video_path, '-i', sin_sub,
        '-map', '0:v', '-map', '0:a', '-map', '1:s:0',
        '-c', 'copy', '-c:s', 'srt',
        '-metadata:s:s:0', 'language=sin',
        '-metadata:s:s:0', 'title=Sinhala',
        '-disposition:s:s:0', 'default',
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
        print(f"☁️ Uploading Video to StreamHG: {upload_filename}...")
        
        with open(final_video_path, 'rb') as f:
            data = {'key': STREAMHG_API_KEY}
            if folder_id and str(folder_id).isdigit() and int(folder_id) > 0:
                data['fld_id'] = int(folder_id)
                
            files = {
                'file': (upload_filename, f)
            }
            up_resp = requests.post(upload_url, data=data, files=files, timeout=600).json()
        
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
