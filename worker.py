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
ABYSS_API_KEY = "19136c9e1c8d2cac4e0e8b612008050a"
ABYSS_UPLOAD_URL = f"https://up.abyss.to/{ABYSS_API_KEY}"

payload = json.loads(os.environ.get("JOB_PAYLOAD", "{}"))
anime_id = payload.get("anilist_id")
ep_num = payload.get("episode")
magnet = payload.get("magnet")
job_type = payload.get("job_type")
search_type = payload.get("search_type")
anime_title = payload.get("title", "Unknown Anime")

print(f"🚀 [WORKER STARTED] Anime: {anime_title} | Ep: {ep_num} | Mode: HARDSUB")

BASE_DIR = "downloads"
TEMP_SUB_DIR = f"temp_subs_ep_{ep_num}"
OUTPUT_DIR = "output_hardsub"
FONT_DIR = "fonts"
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TEMP_SUB_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(FONT_DIR, exist_ok=True)

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

# --- 2. DOWNLOAD SINHALA FONT ---
def download_sinhala_font():
    font_path = os.path.join(FONT_DIR, "NotoSansSinhala-Regular.ttf")
    if not os.path.exists(font_path):
        print("🔤 Downloading Sinhala Font for Hardsubbing...")
        font_url = "https://raw.githubusercontent.com/googlefonts/noto-fonts/main/hinted/ttf/NotoSansSinhala/NotoSansSinhala-Regular.ttf"
        try:
            r = requests.get(font_url, timeout=15)
            if r.status_code == 200:
                with open(font_path, "wb") as f:
                    f.write(r.content)
                print("✅ Font downloaded successfully!")
        except Exception as e:
            print(f"⚠️ Font Download Error: {e}")
    return font_path

# --- 3. EXTRACT, TRANSLATE & CREATE .ASS FILE ---
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
    
    # 🎨 ASS Format Styling (For Hardsub)
    style = pysubs2.SSAStyle()
    style.fontname = "Noto Sans Sinhala"
    style.fontsize = 22
    style.primarycolor = pysubs2.Color(255, 255, 255) # White Text
    style.outlinecolor = pysubs2.Color(0, 0, 0)       # Black Outline
    style.borderstyle = 1
    style.outline = 1.5
    style.shadow = 0.5
    style.bold = True
    subs.styles["Default"] = style
    
    # FFmpeg Subtitles filter is sensitive to spaces and brackets in paths
    # So we save it as a simple filename in the root directory for rendering
    sin_sub_ass = "hardsub_temp.ass"
    subs.save(sin_sub_ass, encoding="utf-8")
    print("✅ Sinhala .ASS Subtitle File Created for Hardsubbing!")
    return sin_sub_ass

# --- 4. HARDSUB (BURN-IN) PROCESS ---
def burn_subtitles_to_video(video_path, sub_ass_path):
    print("🔥 Starting Hardsub Re-encoding Process (This may take 10-15 mins)...")
    
    output_filename = f"{os.path.splitext(os.path.basename(video_path))[0]}_Hardsubbed.mkv"
    hardsubbed_video_path = os.path.join(OUTPUT_DIR, output_filename)
    
    # Using 'fast' preset for speed, crf 24 to maintain good quality with small size
    cmd = [
        'ffmpeg', '-i', video_path, 
        '-vf', f"subtitles={sub_ass_path}:fontsdir={FONT_DIR}", 
        '-c:v', 'libx264', 
        '-preset', 'fast', 
        '-crf', '24', 
        '-c:a', 'copy', 
        hardsubbed_video_path, '-y'
    ]
    
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    if os.path.exists(hardsubbed_video_path) and os.path.getsize(hardsubbed_video_path) > 1000000:
        print(f"✅ Hardsubbing Complete! Output: {output_filename}")
        return hardsubbed_video_path
    else:
        print("❌ Hardsubbing Failed! Uploading original video instead.")
        return video_path

# --- 5. UPLOAD VIDEO TO ABYSS.TO ---
def upload_video_to_abyss(video_path):
    print("☁️ Uploading Video to Abyss.to...")
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

# --- 6. UPDATE DATABASE ---
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
download_sinhala_font()
original_video = download_video()

if original_video:
    # 1. Subtitle Extract & Translate
    ass_sub_path = process_and_translate_subtitle(original_video)
    
    # 2. Hardsub (Burn-in) process
    final_video = original_video
    if ass_sub_path and os.path.exists(ass_sub_path):
        final_video = burn_subtitles_to_video(original_video, ass_sub_path)
    
    # 3. Upload the final Hardsubbed video
    upload_result = upload_video_to_abyss(final_video)
    
    if upload_result and upload_result[0]:
        file_code, file_size = upload_result
        
        update_database(file_code)
        notify_status("success", file_size)
        print("🎉 WORKER COMPLETED SUCCESSFULLY!")
        
        # Cleanup
        if os.path.exists("hardsub_temp.ass"): os.remove("hardsub_temp.ass")
        sys.exit(0)
    else:
        print("❌ Video Upload Failed!")
        notify_status("failed", 0)
        sys.exit(1)
else:
    print("❌ Download Failed!")
    notify_status("failed", 0)
    sys.exit(1)
