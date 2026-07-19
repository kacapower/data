import gradio as gr
import requests
import hashlib
import os
import time
import json
import threading
import datetime
from huggingface_hub import HfApi, login

# ================= CONFIGURATION =================
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")
DATASET_REPO = os.getenv("DATASET_REPO", "your-username/insta_archive")

TARGETS_FILE = "targets.txt"
HASH_STORE_FILE = "profile_hashes.json"
LOG_FILE = "app.log" # New file for live logs

if not APIFY_TOKEN or not HF_TOKEN:
    print("! WARNING: API tokens missing. This will fail.")
else:
    login(token=HF_TOKEN)

hf_api = HfApi()

# ================= LOGGING HELPER =================
def write_log(message):
    """Writes a timestamped message to the log file and prints to console."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_message = f"[{timestamp}] {message}\n"
    with open(LOG_FILE, "a") as f:
        f.write(full_message)
    print(full_message.strip())

# Initialize log file on startup
with open(LOG_FILE, "w") as f:
    f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] System initialized...\n")

# ================= HF PERSISTENCE HELPERS =================
def download_from_hf(filename):
    if not HF_TOKEN: return
    try:
        hf_api.hf_hub_download(repo_id=DATASET_REPO, filename=filename, local_dir=".", repo_type="dataset")
        write_log(f"Downloaded {filename} from HF.")
    except Exception:
        write_log(f"{filename} not found on HF. Starting fresh locally.")
        if filename == HASH_STORE_FILE and not os.path.exists(HASH_STORE_FILE):
            with open(HASH_STORE_FILE, "w") as f: json.dump({}, f)
        elif filename == TARGETS_FILE and not os.path.exists(TARGETS_FILE):
            with open(TARGETS_FILE, "w") as f: f.write("")

def upload_to_hf(local_path, path_in_repo):
    if not HF_TOKEN: return
    try:
        hf_api.upload_file(
            path_in_repo=path_in_repo,
            path=local_path,
            repo_id=DATASET_REPO,
            repo_type="dataset"
        )
    except Exception as e:
        write_log(f"Failed to upload {local_path} to HF: {str(e)}")

download_from_hf(TARGETS_FILE)
download_from_hf(HASH_STORE_FILE)

def get_file_hash(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return hashlib.md5(response.content).hexdigest()
    except Exception:
        return None

def load_hashes():
    if os.path.exists(HASH_STORE_FILE):
        with open(HASH_STORE_FILE, "r") as f:
            try: return json.load(f)
            except json.JSONDecodeError: return {}
    return {}

def save_hashes(hashes):
    with open(HASH_STORE_FILE, "w") as f:
        json.dump(hashes, f, indent=4)
    upload_to_hf(HASH_STORE_FILE, HASH_STORE_FILE)

# ================= CORE AUTOMATION =================
def run_automation_core(check_privacy=True, check_pic=True):
    download_from_hf(TARGETS_FILE)
    if not os.path.exists(TARGETS_FILE) or os.path.getsize(TARGETS_FILE) == 0:
        return "No targets configured."
        
    with open(TARGETS_FILE, "r") as f:
        usernames = [line.strip() for line in f.readlines() if line.strip()]
        
    logs = []
    hashes = load_hashes()
    
    for user in usernames:
        try:
            # 1. Fetch profile data
            run_url = f"https://api.apify.com/v2/acts/apify~instagram-profile-scraper/runs?token={APIFY_TOKEN}"
            run_resp = requests.post(run_url, json={"username": user}).json()
            
            if 'data' not in run_resp:
                logs.append(f"❌ {user}: API Error")
                continue
                
            run_id = run_resp['data']['id']
            
            # Polling
            timeout = time.time() + 60
            task_failed = False
            while True:
                if time.time() > timeout:
                    logs.append(f"⚠️ {user}: Timeout")
                    task_failed = True
                    break
                
                status_resp = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_TOKEN}").json()
                status = status_resp.get('data', {}).get('status')
                
                if status == 'SUCCEEDED': break
                elif status in ['FAILED', 'ABORTED', 'TIMED-OUT']:
                    logs.append(f"❌ {user}: Apify run failed.")
                    task_failed = True
                    break
                time.sleep(3)
                
            if task_failed: continue
                
            # Extract Data
            dataset_id = status_resp['data']['defaultDatasetId']
            data_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_TOKEN}"
            results = requests.get(data_url).json()
            
            if not results:
                logs.append(f"⚠️ {user}: No data.")
                continue
                
            data = results[0]
            profile_pic = data.get('profilePicUrl')
            is_private = data.get('isPrivate', True)
            
            # 2. Profile Pic Check (Runs every 2 hours)
            if check_pic:
                curr_hash = get_file_hash(profile_pic) if profile_pic else None
                prev_hash = hashes.get(user)
                
                if curr_hash and curr_hash != prev_hash:
                    img_data = requests.get(profile_pic).content
                    local_path = f"{user}_pp.jpg"
                    with open(local_path, "wb") as f: f.write(img_data)
                    
                    upload_to_hf(local_path, f"profile_pics/{user}.jpg")
                    if os.path.exists(local_path): os.remove(local_path)
                        
                    hashes[user] = curr_hash
                    logs.append(f"🔄 {user}: Profile pic updated.")
                else:
                    logs.append(f"✅ {user}: Pic unchanged.")
                    
            # 3. Privacy Check (Runs every 30 mins)
            if check_privacy:
                if not is_private:
                    logs.append(f"🔓 {user} is PUBLIC! Initiating full dump...")
                    dump_url = f"https://api.apify.com/v2/acts/apify~instagram-post-scraper/runs?token={APIFY_TOKEN}"
                    requests.post(dump_url, json={"usernames": [user]})
                else:
                    logs.append(f"🔒 {user} is still private.")
                
        except Exception as e:
            logs.append(f"💥 {user} Error: {str(e)}")
            
    if check_pic:
        save_hashes(hashes)
        
    return "\n".join(logs)

# ================= BACKGROUND SCHEDULER =================
last_privacy_check = 0
last_pic_check = 0

def background_scheduler():
    global last_privacy_check, last_pic_check
    write_log("⏳ Background scheduler started...")
    
    # Run immediately on startup
    last_privacy_check = time.time()
    last_pic_check = time.time()
    write_log("🚀 Running Initial Startup Sync...")
    initial_logs = run_automation_core(check_privacy=True, check_pic=True)
    write_log(initial_logs)
    
    while True:
        time.sleep(60) # Wake up every minute to check the time
        current_time = time.time()
        
        run_privacy = False
        run_pic = False
        
        if current_time - last_privacy_check >= 1800: # 30 mins
            run_privacy = True
        
        if current_time - last_pic_check >= 7200: # 2 hours
            run_pic = True
            
        if run_privacy or run_pic:
            write_log("🔄 Running Scheduled Check...")
            logs = run_automation_core(check_privacy=run_privacy, check_pic=run_pic)
            write_log(logs)
            
            if run_privacy: last_privacy_check = time.time()
            if run_pic: last_pic_check = time.time()

# Start the background timer in a separate thread
threading.Thread(target=background_scheduler, daemon=True).start()

# ================= GRADIO UI =================
def load_initial_targets():
    download_from_hf(TARGETS_FILE)
    if os.path.exists(TARGETS_FILE):
        with open(TARGETS_FILE, "r") as f: return f.read()
    return ""

def manual_sync():
    result = run_automation_core(check_privacy=True, check_pic=True)
    write_log(f"Manual Sync Result:\n{result}")
    return result

def read_live_logs():
    """Reads the log file to display in the UI."""
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            # Return the last 50 lines to keep the UI clean
            lines = f.readlines()
            return "".join(lines[-50:])
    return "Waiting for logs..."

with gr.Blocks(theme=gr.themes.Soft()) as demo:
    gr.Markdown("# XORTRON: Insta-State Sync")
    with gr.Row():
        with gr.Column():
            user_input = gr.Textbox(label="Target Usernames", placeholder="one per line", lines=5, value=load_initial_targets)
            save_btn = gr.Button("Save Targets", variant="primary")
            sync_btn = gr.Button("Force Manual Sync", variant="secondary")
            
        with gr.Column():
            # This textbox will automatically refresh
            live_log_output = gr.Textbox(label="Live Background Logs (Auto-updates)", lines=12, interactive=False)
            
    def save_targets(text):
        with open(TARGETS_FILE, "w") as f: f.write(text)
        upload_to_hf(TARGETS_FILE, TARGETS_FILE)
        write_log("Targets saved & synced to Hugging Face.")
        return "Targets saved & synced to Hugging Face."
        
    save_btn.click(save_targets, inputs=user_input, outputs=None)
    sync_btn.click(manual_sync, outputs=None)
    
    # Auto-refresh the log output every 5 seconds
    demo.load(read_live_logs, inputs=None, outputs=live_log_output)

# ================= RENDER DEPLOYMENT =================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port)
