import os
import json
import hashlib
import uuid
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, send_from_directory
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)

# --- Read secrets from environment variables ---
# Use the fixed secret key from the .env file for stable sessions
app.secret_key = os.environ.get('SECRET_KEY')
ADMIN_PIN = os.environ.get('ADMIN_PIN') # Get the admin PIN

if not app.secret_key or not ADMIN_PIN:
    raise ValueError("SECRET_KEY and ADMIN_PIN must be set as environment variables.")

CONFIG_DIR = 'config'
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
UPLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

timer_data = {
    "1": {"id": "1", "label": "Timer 1", "enabled": False, "end_time_utc_iso": None, "paused_time_remaining_seconds": None, "is_running": False, "initial_duration_seconds": 0, "logo_filename": None},
    "2": {"id": "2", "label": "Timer 2", "enabled": False, "end_time_utc_iso": None, "paused_time_remaining_seconds": None, "is_running": False, "initial_duration_seconds": 0, "logo_filename": None}
}

# --- Config Loading/Saving ---
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"logos": []}
    with open(CONFIG_FILE, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"logos": []} # Return default if file is empty or corrupted

def save_config(data_to_save):
    # Ensure the config directory exists before trying to write to it
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(data_to_save, f, indent=4)

config = load_config()

# --- Authentication (Simplified for Docker/env vars) ---
def check_pin(submitted_pin):
    # Direct comparison with the PIN from the environment variable
    return submitted_pin == ADMIN_PIN

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        pin = request.form.get('pin')
        if check_pin(pin):
            session['admin_logged_in'] = True
            next_url = request.args.get('next')
            return redirect(next_url or url_for('admin_dashboard'))
        else:
            return render_template('admin_login.html', error="Invalid PIN")
    return render_template('admin_login.html')

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function
@app.route('/')
def viewer():
    return render_template('viewer.html')
@app.route('/admin')
@login_required
def admin_dashboard():
    current_config = load_config()
    return render_template('admin_dashboard.html', timers_status=timer_data, logos=current_config.get('logos', []))
@app.route('/api/timer_status', methods=['GET'])
def get_timer_status_api():
    response = {}
    now_utc = datetime.utcnow()
    for timer_id, data in timer_data.items():
        if not data["enabled"]:
            response[timer_id] = {"time_remaining_seconds": 0, "is_running": False, "times_up": False, "enabled": False, "logo_filename": data["logo_filename"]}
            continue
        time_remaining = 0
        times_up = False
        current_is_running = data["is_running"]
        if data["is_running"] and data["end_time_utc_iso"]:
            end_time = datetime.fromisoformat(data["end_time_utc_iso"])
            remaining_delta = end_time - now_utc
            time_remaining = max(0, int(remaining_delta.total_seconds()))
            if time_remaining == 0:
                times_up = True
        elif not data["is_running"] and data["paused_time_remaining_seconds"] is not None:
            time_remaining = data["paused_time_remaining_seconds"]
            if time_remaining == 0:
                 times_up = True
        elif not data["is_running"] and data["initial_duration_seconds"] > 0 and data["end_time_utc_iso"] is None:
             time_remaining = data["initial_duration_seconds"]
        response[timer_id] = {
            "time_remaining_seconds": time_remaining,
            "is_running": current_is_running,
            "times_up": times_up,
            "enabled": data["enabled"],
            "logo_filename": data["logo_filename"]
        }
    return jsonify(response)
@app.route('/api/control_timer/<timer_id>', methods=['POST'])
@login_required
def control_timer_api(timer_id):
    if timer_id not in timer_data:
        return jsonify({"error": "Invalid timer ID"}), 400
    try:
        payload = request.get_json()
        if not payload or 'action' not in payload:
            return jsonify({"error": "Missing action in payload"}), 400
    except Exception as e:
        return jsonify({"error": f"Invalid JSON payload: {str(e)}"}), 400
    action = payload.get('action')
    td = timer_data[timer_id]
    if action == 'toggle_enable':
        td["enabled"] = payload.get('enabled', False)
        if not td["enabled"]:
            td.update({"end_time_utc_iso": None, "paused_time_remaining_seconds": 0, "is_running": False, "initial_duration_seconds": 0, "logo_filename": None})
    elif action == 'set_time':
        hours = int(payload.get('hours', 0))
        minutes = int(payload.get('minutes', 0))
        seconds = int(payload.get('seconds', 0))
        total_seconds = hours * 3600 + minutes * 60 + seconds
        td["initial_duration_seconds"] = total_seconds
        td["paused_time_remaining_seconds"] = total_seconds
        td["is_running"] = False
        td["end_time_utc_iso"] = None
    elif action == 'start':
        if td["enabled"]:
            duration_to_start_seconds = td["paused_time_remaining_seconds"] if td["paused_time_remaining_seconds"] is not None else td["initial_duration_seconds"]
            if duration_to_start_seconds > 0:
                td["end_time_utc_iso"] = (datetime.utcnow() + timedelta(seconds=duration_to_start_seconds)).isoformat()
                td["is_running"] = True
                td["paused_time_remaining_seconds"] = None
    elif action == 'pause':
        if td["is_running"] and td["end_time_utc_iso"]:
            end_time = datetime.fromisoformat(td["end_time_utc_iso"])
            remaining_delta = end_time - datetime.utcnow()
            td["paused_time_remaining_seconds"] = max(0, int(remaining_delta.total_seconds()))
            td["is_running"] = False
    elif action == 'resume':
        if not td["is_running"] and td["paused_time_remaining_seconds"] is not None and td["paused_time_remaining_seconds"] > 0:
            td["end_time_utc_iso"] = (datetime.utcnow() + timedelta(seconds=td["paused_time_remaining_seconds"])).isoformat()
            td["is_running"] = True
            td["paused_time_remaining_seconds"] = None
    elif action == 'reset':
        td["paused_time_remaining_seconds"] = td["initial_duration_seconds"]
        td["is_running"] = False
        td["end_time_utc_iso"] = None
    elif action == 'set_logo':
        td["logo_filename"] = payload.get('logo_filename')
    app.logger.info(f"Timer {timer_id} action {action}. New state: {td}")
    return jsonify({"message": f"Timer {timer_id} action {action} processed", "newState": td})
@app.route('/api/upload_logo', methods=['POST'])
@login_required
def upload_logo_api():
    if 'logo_file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['logo_file']
    common_name = request.form.get('common_name', '').strip()
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if not common_name:
        return jsonify({"error": "Common name for logo is required"}), 400
    if file:
        filename_base = "".join(c if c.isalnum() or c in [' ', '_', '-'] else '_' for c in common_name).rstrip()
        filename_base = filename_base.replace(' ', '_')
        original_extension = os.path.splitext(file.filename)[1]
        if not original_extension: original_extension = ".png"
        abs_upload_folder = os.path.join(app.root_path, UPLOAD_FOLDER)
        if not os.path.exists(abs_upload_folder):
            os.makedirs(abs_upload_folder)
        unique_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}_{filename_base}{original_extension}"
        filepath = os.path.join(abs_upload_folder, unique_filename)
        try:
            file.save(filepath)
            app.logger.info(f"Logo saved: {filepath}")
            current_config = load_config()
            if 'logos' not in current_config or not isinstance(current_config['logos'], list):
                current_config['logos'] = []
            current_config['logos'].append({"name": common_name, "filename": unique_filename})
            save_config(current_config)
            return jsonify({"message": "Logo uploaded successfully", "logo": {"name": common_name, "filename": unique_filename}})
        except Exception as e:
            app.logger.error(f"Error saving logo: {e}")
            return jsonify({"error": f"Could not save logo: {str(e)}"}), 500
    return jsonify({"error": "Upload failed, file or common name issue."}), 400
@app.route('/api/get_logos', methods=['GET'])
@login_required
def get_logos_api():
    current_config = load_config()
    return jsonify(current_config.get('logos', []))
@app.route('/api/delete_logo/<filename>', methods=['DELETE'])
@login_required
def delete_logo_api(filename):
    current_config = load_config()
    logos = current_config.get('logos', [])
    if '..' in filename or filename.startswith('/'):
        return jsonify({"error": "Invalid filename"}), 400
    logo_to_delete = next((logo for logo in logos if logo["filename"] == filename), None)
    if not logo_to_delete:
        return jsonify({"error": "Logo not found in config"}), 404
    current_config['logos'] = [logo for logo in logos if logo["filename"] != filename]
    save_config(current_config)
    try:
        filepath = os.path.join(app.root_path, UPLOAD_FOLDER, filename)
        if os.path.exists(filepath):
            os.remove(filepath)
            app.logger.info(f"Deleted logo file: {filepath}")
        else:
            app.logger.warning(f"Logo file not found for deletion: {filepath}")
    except Exception as e:
        app.logger.error(f"Error deleting logo file {filename}: {e}")
        return jsonify({"warning": f"Logo removed from list, but file deletion failed: {str(e)}"}), 500
    return jsonify({"message": f"Logo '{logo_to_delete['name']}' deleted successfully."})
