"""
MLBB Highlight Uploader
TikTok auto-upload tool with scheduling and multi-account support.
"""

import os
import json
import time
import threading
import webbrowser
import urllib.parse
import secrets
import schedule
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

# ─── Configuration ────────────────────────────────────────────────────────────

CLIENT_KEY    = "YOUR_CLIENT_KEY"      # From TikTok Developer Portal
CLIENT_SECRET = "YOUR_CLIENT_SECRET"  # From TikTok Developer Portal

# Must match exactly what you registered in TikTok Developer Portal
REDIRECT_URI  = "https://fendnetwork.github.io/mlbb-uploader/callback.html"

LOCAL_PORT    = 8765
ACCOUNTS_FILE = "accounts.json"
QUEUE_FILE    = "queue.json"

TIKTOK_AUTH_URL  = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_USER_URL  = "https://open.tiktokapis.com/v2/user/info/"
TIKTOK_INIT_URL  = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_UPLOAD_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

# ─── Storage Helpers ───────────────────────────────────────────────────────────

def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_accounts(accounts):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(accounts, f, indent=2)

def load_queue():
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "r") as f:
            return json.load(f)
    return []

def save_queue(queue):
    with open(QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2)

# ─── OAuth Flow ────────────────────────────────────────────────────────────────

auth_result = {}  # Shared between HTTP handler and main thread

class CallbackHandler(BaseHTTPRequestHandler):
    """Catches the redirect from callback.html and extracts the auth code."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/callback":
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            if code:
                auth_result["code"] = code
                self._respond(200, "Auth code received! You can close this window.")
            else:
                error = params.get("error", ["unknown"])[0]
                auth_result["error"] = error
                self._respond(400, f"Error: {error}")
        else:
            self._respond(404, "Not found")

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"""
        <html><body style="font-family:sans-serif;background:#0d0d1a;color:#e0e0e0;
        display:flex;align-items:center;justify-content:center;height:100vh;margin:0;">
        <div style="text-align:center;">
          <h2 style="color:#a855f7;">MLBB Highlight Uploader</h2>
          <p>{body}</p>
        </div></body></html>
        """.encode())

    def log_message(self, format, *args):
        pass  # Suppress request logs


def start_local_server():
    server = HTTPServer(("localhost", LOCAL_PORT), CallbackHandler)
    server.timeout = 120
    server.handle_request()  # Handle one request then stop
    server.server_close()


def login_account():
    """Open TikTok OAuth and return account info dict, or None on failure."""
    state = secrets.token_urlsafe(16)
    auth_result.clear()

    params = {
        "client_key": CLIENT_KEY,
        "response_type": "code",
        "scope": "user.info.basic,video.publish",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    auth_url = TIKTOK_AUTH_URL + "?" + urllib.parse.urlencode(params)

    # Start local server in background
    server_thread = threading.Thread(target=start_local_server, daemon=True)
    server_thread.start()

    print(f"\nOpening TikTok login in browser...")
    webbrowser.open(auth_url)

    # Wait for callback (max 2 minutes)
    server_thread.join(timeout=120)

    if "error" in auth_result:
        print(f"Login failed: {auth_result['error']}")
        return None

    if "code" not in auth_result:
        print("Login timed out. Please try again.")
        return None

    # Exchange code for token
    token_resp = requests.post(TIKTOK_TOKEN_URL, data={
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "code": auth_result["code"],
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    })
    token_data = token_resp.json()

    if "access_token" not in token_data:
        print(f"Token exchange failed: {token_data}")
        return None

    access_token = token_data["access_token"]
    open_id      = token_data["open_id"]
    refresh_token = token_data.get("refresh_token", "")

    # Fetch display name
    user_resp = requests.get(
        TIKTOK_USER_URL,
        params={"fields": "open_id,display_name"},
        headers={"Authorization": f"Bearer {access_token}"}
    )
    user_data = user_resp.json().get("data", {}).get("user", {})
    display_name = user_data.get("display_name", open_id)

    account = {
        "open_id": open_id,
        "display_name": display_name,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + token_data.get("expires_in", 86400),
    }
    return account


def refresh_access_token(account):
    """Refresh an expired access token. Returns updated account or None."""
    resp = requests.post(TIKTOK_TOKEN_URL, data={
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": account["refresh_token"],
    })
    data = resp.json()
    if "access_token" not in data:
        return None
    account["access_token"] = data["access_token"]
    account["refresh_token"] = data.get("refresh_token", account["refresh_token"])
    account["expires_at"] = time.time() + data.get("expires_in", 86400)
    return account


def get_valid_token(account):
    """Return a valid access token, refreshing if needed."""
    if time.time() > account.get("expires_at", 0) - 300:
        print(f"  Refreshing token for @{account['display_name']}...")
        updated = refresh_access_token(account)
        if not updated:
            print("  Token refresh failed. Please re-login this account.")
            return None
        account.update(updated)
        accounts = load_accounts()
        accounts[account["open_id"]] = account
        save_accounts(accounts)
    return account["access_token"]


# ─── TikTok Upload ─────────────────────────────────────────────────────────────

def upload_video(account, video_path, title="", description=""):
    """Upload a video file to TikTok. Returns True on success."""

    if not os.path.exists(video_path):
        print(f"  File not found: {video_path}")
        return False

    token = get_valid_token(account)
    if not token:
        return False

    file_size = os.path.getsize(video_path)
    post_title = title or os.path.splitext(os.path.basename(video_path))[0]

    print(f"  Initializing upload for: {os.path.basename(video_path)}")

    # Step 1: Init upload
    init_resp = requests.post(
        TIKTOK_INIT_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={
            "post_info": {
                "title": post_title[:150],
                "privacy_level": "SELF_ONLY",  # Change to PUBLIC_TO_EVERYONE when ready
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,
                "total_chunk_count": 1,
            },
        },
    )

    init_data = init_resp.json()
    if init_data.get("error", {}).get("code", "ok") != "ok":
        print(f"  Init error: {init_data['error']['message']}")
        return False

    upload_url = init_data["data"]["upload_url"]
    publish_id = init_data["data"]["publish_id"]

    # Step 2: Upload file
    print(f"  Uploading file ({file_size / 1024 / 1024:.1f} MB)...")
    with open(video_path, "rb") as f:
        upload_resp = requests.put(
            upload_url,
            data=f,
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
                "Content-Length": str(file_size),
            },
        )

    if upload_resp.status_code not in (200, 201, 206):
        print(f"  Upload failed with status {upload_resp.status_code}")
        return False

    # Step 3: Poll status
    print(f"  Waiting for TikTok to process...")
    for _ in range(20):
        time.sleep(5)
        status_resp = requests.post(
            TIKTOK_UPLOAD_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={"publish_id": publish_id},
        )
        status_data = status_resp.json()
        status = status_data.get("data", {}).get("status", "")
        if status == "PUBLISH_COMPLETE":
            print(f"  Published successfully!")
            return True
        elif status in ("FAILED", "ERROR"):
            print(f"  Publish failed: {status_data.get('data', {}).get('fail_reason', 'unknown')}")
            return False

    print("  Upload timed out waiting for processing.")
    return False


# ─── Queue & Scheduler ─────────────────────────────────────────────────────────

def process_queue():
    """Process all due items in the upload queue."""
    queue = load_queue()
    accounts = load_accounts()
    now = datetime.now()
    remaining = []

    for item in queue:
        scheduled_at = item.get("scheduled_at")
        due = True
        if scheduled_at:
            try:
                due = datetime.fromisoformat(scheduled_at) <= now
            except ValueError:
                due = True

        if not due:
            remaining.append(item)
            continue

        open_id = item.get("open_id")
        account = accounts.get(open_id)
        if not account:
            print(f"[Queue] Account {open_id} not found, skipping.")
            continue

        print(f"\n[Queue] Uploading to @{account['display_name']}: {item['video_path']}")
        success = upload_video(
            account,
            item["video_path"],
            title=item.get("title", ""),
        )
        if not success and item.get("retry", 0) < 2:
            item["retry"] = item.get("retry", 0) + 1
            item["scheduled_at"] = None  # Retry immediately next cycle
            remaining.append(item)
            print(f"  Will retry (attempt {item['retry']}/2).")

    save_queue(remaining)


# ─── CLI Menu ─────────────────────────────────────────────────────────────────

def print_header():
    print("\n" + "="*52)
    print("   MLBB Highlight Uploader  |  by Fend Network")
    print("="*52)

def list_accounts(accounts):
    if not accounts:
        print("  No accounts connected.")
        return
    for i, (oid, acc) in enumerate(accounts.items(), 1):
        expires = datetime.fromtimestamp(acc.get("expires_at", 0)).strftime("%d %b %Y")
        print(f"  [{i}] @{acc['display_name']}  (token expires: {expires})")

def add_to_queue(accounts):
    if not accounts:
        print("No accounts connected. Please add an account first.")
        return

    print("\nSelect account:")
    items = list(accounts.items())
    for i, (_, acc) in enumerate(items, 1):
        print(f"  [{i}] @{acc['display_name']}")
    choice = input("Account number: ").strip()
    try:
        idx = int(choice) - 1
        open_id, account = items[idx]
    except (ValueError, IndexError):
        print("Invalid choice.")
        return

    video_path = input("Video file path: ").strip().strip('"')
    if not os.path.exists(video_path):
        print("File not found.")
        return

    title = input("Post title (leave blank for filename): ").strip()
    schedule_input = input("Schedule time (YYYY-MM-DD HH:MM, leave blank for now): ").strip()

    scheduled_at = None
    if schedule_input:
        try:
            scheduled_at = datetime.strptime(schedule_input, "%Y-%m-%d %H:%M").isoformat()
        except ValueError:
            print("Invalid date format, uploading now.")

    queue = load_queue()
    queue.append({
        "open_id": open_id,
        "video_path": video_path,
        "title": title,
        "scheduled_at": scheduled_at,
        "retry": 0,
    })
    save_queue(queue)
    if scheduled_at:
        print(f"Scheduled: {scheduled_at}")
    else:
        print("Added to queue (will upload on next run).")


def main():
    print_header()

    # Auto-run scheduler every minute in background
    schedule.every(1).minutes.do(process_queue)
    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(10)
    threading.Thread(target=run_scheduler, daemon=True).start()

    while True:
        accounts = load_accounts()
        queue = load_queue()

        print(f"\nAccounts: {len(accounts)}  |  Queue: {len(queue)} item(s)")
        print("\n  [1] Add TikTok account")
        print("  [2] List accounts")
        print("  [3] Add video to queue")
        print("  [4] View queue")
        print("  [5] Upload queue now")
        print("  [6] Remove account")
        print("  [0] Exit")

        choice = input("\nChoice: ").strip()

        if choice == "1":
            print("\nOpening TikTok login...")
            account = login_account()
            if account:
                accounts = load_accounts()
                accounts[account["open_id"]] = account
                save_accounts(accounts)
                print(f"Connected: @{account['display_name']}")

        elif choice == "2":
            print()
            list_accounts(load_accounts())

        elif choice == "3":
            add_to_queue(load_accounts())

        elif choice == "4":
            queue = load_queue()
            if not queue:
                print("Queue is empty.")
            else:
                accounts = load_accounts()
                for i, item in enumerate(queue, 1):
                    acc = accounts.get(item["open_id"], {})
                    name = acc.get("display_name", item["open_id"])
                    sched = item.get("scheduled_at") or "now"
                    print(f"  [{i}] @{name} | {os.path.basename(item['video_path'])} | {sched}")

        elif choice == "5":
            print("\nProcessing queue...")
            process_queue()
            print("Done.")

        elif choice == "6":
            accounts = load_accounts()
            if not accounts:
                print("No accounts to remove.")
                continue
            print()
            list_accounts(accounts)
            idx_input = input("Account number to remove: ").strip()
            try:
                idx = int(idx_input) - 1
                open_id = list(accounts.keys())[idx]
                name = accounts[open_id]["display_name"]
                del accounts[open_id]
                save_accounts(accounts)
                print(f"Removed @{name}")
            except (ValueError, IndexError):
                print("Invalid choice.")

        elif choice == "0":
            print("Bye!")
            break


if __name__ == "__main__":
    main()
