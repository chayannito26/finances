import os
import json
from flask import Flask, jsonify, request, send_from_directory, render_template, abort
from werkzeug.utils import secure_filename
import time
# NEW: imports for push orchestration
import subprocess
import threading
from datetime import datetime

# --- Configuration ---
# This sets up the Flask application.
# 'templates' is where Flask will look for index.html.
# 'static_folder' is set to None because we will define a custom route for receipts.
app = Flask(__name__, template_folder='.', static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB upload limit

# Define the base directory for our data files (parent of current script’s folder).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.abspath(os.path.join(BASE_DIR, os.pardir))

INCOME_FILE = os.path.join(PARENT_DIR, 'income', 'revenues.json')
EXPENSES_FILE = os.path.join(PARENT_DIR, 'expenses', 'expenses.json')
RECEIPTS_DIR = os.path.join(PARENT_DIR, 'expenses', 'receipts')

# Ensure necessary directories and files exist on startup.
os.makedirs(os.path.dirname(INCOME_FILE), exist_ok=True)
os.makedirs(os.path.dirname(EXPENSES_FILE), exist_ok=True)
os.makedirs(RECEIPTS_DIR, exist_ok=True)

if not os.path.exists(INCOME_FILE):
    with open(INCOME_FILE, 'w') as f:
        json.dump([], f)
if not os.path.exists(EXPENSES_FILE):
    with open(EXPENSES_FILE, 'w') as f:
        json.dump([], f)

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.pdf'}

# NEW: repo paths for pushing
INCOME_REPO = os.path.join(PARENT_DIR, 'income')
EXPENSES_REPO = os.path.join(PARENT_DIR, 'expenses')

# NEW: serialize push operations
_push_lock = threading.Lock()

def _run_git(repo_path, args, timeout=180):
    """Run a git command and return (rc, out, err)."""
    try:
        cp = subprocess.run(
            ['git'] + list(args),
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return cp.returncode, cp.stdout, cp.stderr
    except Exception as e:
        return 1, '', f'Exception: {e}'

def _ensure_git_identity(repo_path, log_acc):
    """Ensure local git user.name/email exist to allow commits."""
    rc_e, out_e, _ = _run_git(repo_path, ['config', '--get', 'user.email'])
    rc_n, out_n, _ = _run_git(repo_path, ['config', '--get', 'user.name'])
    if (rc_e != 0 or not out_e.strip()):
        _run_git(repo_path, ['config', 'user.email', 'finance-bot@example.com'])
        log_acc.append('Configured user.email')
    if (rc_n != 0 or not out_n.strip()):
        _run_git(repo_path, ['config', 'user.name', 'Finance Bot'])
        log_acc.append('Configured user.name')

def _repo_ok(repo_path):
    return os.path.isdir(repo_path) and os.path.isdir(os.path.join(repo_path, '.git'))

def _git_available():
    try:
        cp = subprocess.run(['git', '--version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        return cp.returncode == 0
    except Exception:
        return False

def _has_local_changes(repo_path):
    """
    Fast local change detector. Returns (has_changes: bool, err: Optional[str]).
    Counts tracked/untracked (non-ignored) changes; ignores files in .gitignore.
    """
    rc, out, err = _run_git(repo_path, ['status', '--porcelain'])
    if rc != 0:
        return False, (err.strip() or out.strip() or 'git status failed')
    return bool(out.strip()), None

def _commit_and_push(repo_path, repo_name):
    """
    For a git repo, only push when local changes exist:
      - if no local changes: skip remote operations and return up-to-date
      - else: add -A, commit, pull --rebase, push
    Returns dict with status and logs.
    """
    result = {
        'name': repo_name,
        'changed': False,
        'committed': False,
        'pushed': False,
        'log': '',
        'error': None
    }
    logs = []
    if not _repo_ok(repo_path):
        result['error'] = f'Repository not found or not a git repo: {repo_path}'
        return result

    # Fast path: skip everything if nothing changed locally
    has_changes, status_err = _has_local_changes(repo_path)
    if status_err:
        result['error'] = status_err
        result['log'] = '\n'.join(logs)
        return result
    if not has_changes:
        logs.append('No local changes. Skipping pull/push.')
        result['log'] = '\n'.join(logs)
        return result

    result['changed'] = True

    # Configure identity only when we will commit
    _ensure_git_identity(repo_path, logs)

    # Quick remote check (needed only when we intend to push)
    rc, out, err = _run_git(repo_path, ['remote', 'get-url', 'origin'])
    if rc != 0:
        result['error'] = f'No remote "origin" configured.\n{err}'.strip()
        result['log'] = '\n'.join(logs)
        return result
    logs.append(f'origin: {out.strip()}')

    # Stage and commit
    rc, out, err = _run_git(repo_path, ['add', '-A'])
    logs.append(out + err)

    rc, _, _ = _run_git(repo_path, ['diff', '--cached', '--quiet'])
    staged_changes = (rc == 1)
    if not staged_changes:
        logs.append('No staged changes after add. Nothing to commit.')
        result['log'] = '\n'.join(logs)
        return result

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    message = f'Finance sync ({repo_name}) at {ts}'
    rc, out, err = _run_git(repo_path, ['commit', '-m', message])
    logs.append(out + err)
    if rc != 0:
        result['error'] = f'Commit failed.\n{out}\n{err}'.strip()
        result['log'] = '\n'.join(logs)
        return result
    result['committed'] = True

    # Rebase to remote (fetch is implicit in pull)
    rc, out, err = _run_git(repo_path, ['pull', '--rebase'])
    logs.append(out + err)
    if rc != 0:
        _run_git(repo_path, ['rebase', '--abort'])
        result['error'] = f'Pull --rebase failed.\n{out}\n{err}'.strip()
        result['log'] = '\n'.join(logs)
        return result

    # Push
    rc, out, err = _run_git(repo_path, ['push'])
    logs.append(out + err)
    if rc != 0:
        result['error'] = f'Push failed.\n{out}\n{err}'.strip()
        result['log'] = '\n'.join(logs)
        return result

    # Show final commit (for visibility)
    rc, out, err = _run_git(repo_path, ['rev-parse', 'HEAD'])
    if rc == 0:
        logs.append(f'HEAD: {out.strip()}')

    result['pushed'] = True
    result['log'] = '\n'.join(logs)
    return result

# --- Helper Functions for File I/O ---
# These functions handle reading from and writing to the JSON files safely.
def read_json_file(filepath):
    """Reads data from a JSON file."""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return []

def write_json_file(filepath, data):
    """Writes data to a JSON file."""
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)
    except IOError as e:
        print(f"Error writing to file {filepath}: {e}")

# NEW: simple extension checker used by upload/update routes (was missing)
def _allowed_extension(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTENSIONS

# NEW: helpers to sort entries by date (newest first)
def _parse_any_date_to_ts(value):
    """
    Try to parse a date/time value into a UNIX timestamp (seconds).
    Supports:
      - ISO 8601 strings (e.g., 2024-05-01 or 2024-05-01T12:34:56[Z])
      - Common date formats with/without time
      - Numeric timestamps (seconds or milliseconds)
    """
    if value is None:
        return None

    # Numeric timestamp handling
    try:
        num = float(value)
        # Heuristic: treat very large numbers as milliseconds
        if num > 10**12:
            return num / 1000.0
        return num
    except (TypeError, ValueError):
        pass

    if isinstance(value, str):
        s = value.strip()
        # ISO 8601
        try:
            iso = s.replace('Z', '+00:00')
            return datetime.fromisoformat(iso).timestamp()
        except Exception:
            pass

        # Common formats
        fmts = [
            '%Y-%m-%d',
            '%Y/%m/%d',
            '%d/%m/%Y',
            '%m/%d/%Y',
            '%Y-%m-%d %H:%M',
            '%Y-%m-%d %H:%M:%S',
            '%Y/%m/%d %H:%M',
            '%Y/%m/%d %H:%M:%S',
            '%d/%m/%Y %H:%M',
            '%d/%m/%Y %H:%M:%S',
            '%m/%d/%Y %H:%M',
            '%m/%d/%Y %H:%M:%S',
            '%d-%m-%Y',
            '%Y-%m-%d %H:%M:%S.%f',
        ]
        for fmt in fmts:
            try:
                return datetime.strptime(s, fmt).timestamp()
            except ValueError:
                continue

    return None

def _item_ts_for_sort(item):
    """Derive a timestamp from an item using date-like fields or id as fallback."""
    if not isinstance(item, dict):
        return 0.0

    for key in ('date', 'createdAt', 'created_at'):
        ts = _parse_any_date_to_ts(item.get(key))
        if ts is not None:
            return ts

    # Fallback to id if it looks like a timestamp
    ts = _parse_any_date_to_ts(item.get('id'))
    if ts is not None:
        return ts

    return 0.0

def _sort_by_date_desc(items):
    """Return items sorted from newest to oldest."""
    try:
        return sorted(items or [], key=_item_ts_for_sort, reverse=True)
    except Exception:
        return items or []

# --- Main Application Routes ---
@app.route('/')
def index():
    """Serves the main HTML page."""
    return render_template('index.html')

@app.route('/receipts/<path:filename>')
def get_receipt(filename):
    """Serves uploaded receipt files from the 'expenses/receipts' directory."""
    # Prevent path traversal by normalizing and ensuring inside RECEIPTS_DIR
    safe_name = os.path.basename(filename)
    # Strip any accidental query string fragments
    safe_name = safe_name.split('?', 1)[0]
    return send_from_directory(RECEIPTS_DIR, safe_name)

# Return JSON for 413 on API routes instead of HTML
from werkzeug.exceptions import RequestEntityTooLarge

@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'File too large. Max 10 MB.'}), 413
    return ('File too large. Max 10 MB.', 413)

# --- API Endpoints for Data Management ---

@app.route('/api/data', methods=['GET'])
def get_all_data():
    """API endpoint to fetch all income and expense data."""
    income_data = read_json_file(INCOME_FILE)
    expenses_data = read_json_file(EXPENSES_FILE)

    # NEW: sort by date (newest first)
    income_data = _sort_by_date_desc(income_data)
    expenses_data = _sort_by_date_desc(expenses_data)

    return jsonify({
        'revenues': income_data,
        'expenses': expenses_data
    })

# --- Income API ---
@app.route('/api/income', methods=['POST'])
def manage_income():
    """API endpoint to add or update an income entry."""
    data = request.get_json()
    if not data:
        abort(400, description="Invalid JSON payload")

    all_income = read_json_file(INCOME_FILE)
    item_id = data.get('id')

    # If an ID exists, it's an update; otherwise, it's a new entry.
    # Normalize IDs to integers for comparison to handle type mismatches
    if item_id is not None:
        try:
            item_id = int(item_id)
            data['id'] = item_id
        except (ValueError, TypeError):
            item_id = None
    
    if item_id and any(int(item.get('id', -1)) == item_id for item in all_income):
        # Update existing item
        all_income = [data if int(item.get('id', -1)) == item_id else item for item in all_income]
    else:
        # Add new item with a unique ID
        data['id'] = int(time.time() * 1000) # Generate a new timestamp-based ID
        all_income.append(data)

    write_json_file(INCOME_FILE, all_income)
    return jsonify(data), 200

@app.route('/api/income/<int:item_id>', methods=['DELETE'])
def delete_income(item_id):
    """API endpoint to delete an income entry."""
    all_income = read_json_file(INCOME_FILE)
    filtered_income = [item for item in all_income if item.get('id') != item_id]

    if len(all_income) == len(filtered_income):
        abort(404, description="Income item not found")

    write_json_file(INCOME_FILE, filtered_income)
    return jsonify({'success': True}), 200


# --- Expenses API ---
@app.route('/api/expenses', methods=['POST'])
def manage_expense():
    """API endpoint to add or update an expense entry."""
    data = request.get_json()
    if not data:
        abort(400, description="Invalid JSON payload")

    all_expenses = read_json_file(EXPENSES_FILE)
    item_id = data.get('id')

    # If an ID exists, it's an update; otherwise, it's a new entry.
    # Normalize IDs to integers for comparison to handle type mismatches
    if item_id is not None:
        try:
            item_id = int(item_id)
            data['id'] = item_id
        except (ValueError, TypeError):
            item_id = None
    
    if item_id and any(int(item.get('id', -1)) == item_id for item in all_expenses):
        # Update existing item
        all_expenses = [data if int(item.get('id', -1)) == item_id else item for item in all_expenses]
    else:
        # Add new item
        data['id'] = int(time.time() * 1000) # Generate a new timestamp-based ID
        all_expenses.append(data)

    write_json_file(EXPENSES_FILE, all_expenses)
    return jsonify(data), 200

@app.route('/api/expenses/<int:item_id>', methods=['DELETE'])
def delete_expense(item_id):
    """API endpoint to delete an expense entry."""
    all_expenses = read_json_file(EXPENSES_FILE)
    filtered_expenses = [item for item in all_expenses if item.get('id') != item_id]

    if len(all_expenses) == len(filtered_expenses):
        abort(404, description="Expense item not found")

    write_json_file(EXPENSES_FILE, filtered_expenses)
    return jsonify({'success': True}), 200


# --- Receipt Upload API ---
@app.route('/api/upload_receipt', methods=['POST'])
def upload_receipt():
    """Handles uploading of receipt files."""
    if 'receipt' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['receipt']
    base_name = (request.form.get('filename') or '').strip()
    description = (request.form.get('description') or '').strip()

    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if not base_name:
        return jsonify({'error': 'Filename is required'}), 400

    if not description:
        return jsonify({'error': 'Description is required'}), 400

    # Sanitize filename for storage (no extension here; we add it)
    safe_base = secure_filename(base_name)
    if not safe_base:
        return jsonify({'error': 'Invalid filename'}), 400

    # Validate extension/type
    original_ext = os.path.splitext(file.filename)[1].lower()
    if original_ext == '' or not _allowed_extension(file.filename):
        return jsonify({'error': 'Unsupported file type'}), 400

    # Ensure unique final filename to avoid overwriting: <safe_base>-<ts><ext>
    ts = int(time.time() * 1000)
    final_filename = f"{safe_base}-{ts}{original_ext}"
    save_path = os.path.join(RECEIPTS_DIR, final_filename)

    try:
        file.save(save_path)
    except Exception as e:
        return jsonify({'error': f'Failed to save file: {e}'}), 500

    # URL used by the frontend (relative so it works behind proxies too)
    url_path = f'./receipts/{final_filename}'
    return jsonify({
        'url': url_path,
        'filename': final_filename,
        'description': description
    }), 200

@app.route('/api/delete_receipt', methods=['POST'])
def delete_receipt():
    """Deletes a previously uploaded receipt file. Body: { filename } or { url }."""
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
    url = data.get('url')

    if not filename and url:
        filename = os.path.basename(url)

    if not filename:
        return jsonify({'error': 'filename or url is required'}), 400

    safe_name = os.path.basename(filename)
    target_path = os.path.join(RECEIPTS_DIR, safe_name)

    # Ensure target is within receipts dir
    if not os.path.abspath(target_path).startswith(os.path.abspath(RECEIPTS_DIR)):
        return jsonify({'error': 'Invalid filename'}), 400

    if os.path.exists(target_path):
        try:
            os.remove(target_path)
            return jsonify({'success': True}), 200
        except Exception as e:
            return jsonify({'error': f'Failed to delete: {e}'}), 500

    # Idempotent delete
    return jsonify({'success': True, 'message': 'File not found; treated as deleted'}), 200

@app.route('/api/update_receipt', methods=['POST'])
def update_receipt():
    """
    Update/rename/replace a receipt file.

    Form fields:
      - old_filename: existing filename (or basename extracted from URL), may include ?v=... (will be stripped)
      - new_base: new filename base (no extension)
      - description: new description string
      - receipt (optional): replacement file; if provided, will be saved (converted on client-side already if needed)
    Behavior:
      - If new_base + ext == old name, overwrite same file when a new file is uploaded.
      - If different name and a new file is uploaded, save new file and delete old.
      - If no new file, just rename (overwrite if target exists).
    """
    old_filename = (request.form.get('old_filename') or '').strip()
    new_base = (request.form.get('new_base') or '').strip()
    description = (request.form.get('description') or '').strip()

    if not old_filename or not new_base or not description:
        return jsonify({'error': 'old_filename, new_base and description are required'}), 400

    # Sanitize and normalize names
    old_filename = os.path.basename(old_filename).split('?', 1)[0]
    safe_new_base = secure_filename(new_base)
    if not safe_new_base:
        return jsonify({'error': 'Invalid new_base'}), 400

    old_path = os.path.join(RECEIPTS_DIR, old_filename)
    if not os.path.abspath(old_path).startswith(os.path.abspath(RECEIPTS_DIR)):
        return jsonify({'error': 'Invalid old_filename'}), 400

    # Determine extension
    file = request.files.get('receipt')
    if file:
        incoming_ext = os.path.splitext(file.filename)[1].lower()
        if not _allowed_extension(file.filename):
            return jsonify({'error': 'Unsupported file type'}), 400
        new_ext = incoming_ext
    else:
        # Keep old extension
        new_ext = os.path.splitext(old_filename)[1].lower()
        if new_ext not in ALLOWED_EXTENSIONS:
            return jsonify({'error': 'Unsupported file type'}), 400

    new_filename = f"{safe_new_base}{new_ext}"
    new_path = os.path.join(RECEIPTS_DIR, new_filename)
    if not os.path.abspath(new_path).startswith(os.path.abspath(RECEIPTS_DIR)):
        return jsonify({'error': 'Invalid target path'}), 400

    try:
        if file:
            # Save/overwrite new file content
            file.save(new_path)
            # If renamed, remove old file (best effort)
            if new_filename != old_filename and os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except Exception:
                    pass
        else:
            # No new file; just rename if different
            if new_filename != old_filename:
                if os.path.exists(old_path):
                    # Overwrite if target exists
                    os.replace(old_path, new_path)

        url_path = f'./receipts/{new_filename}'
        return jsonify({
            'url': url_path,
            'filename': new_filename,
            'description': description
        }), 200
    except Exception as e:
        return jsonify({'error': f'Failed to update receipt: {e}'}), 500

@app.route('/api/push', methods=['POST'])
def push_to_github():
    """
    Push ../income and ../expenses repositories.
    Only pushes repos with local changes; unchanged repos are reported as up-to-date.
    Serialized to avoid concurrent pushes. Returns detailed logs.
    To allow remote callers, set ALLOW_REMOTE_PUSH=1; otherwise restricted to localhost.
    """
    if os.environ.get('ALLOW_REMOTE_PUSH') != '1':
        if request.remote_addr not in ('127.0.0.1', '::1'):
            return jsonify({'error': 'Forbidden'}), 403

    if not _git_available():
        return jsonify({'error': 'git is not available on the server'}), 500

    # Try to acquire the lock without blocking
    if not _push_lock.acquire(blocking=False):
        return jsonify({'error': 'Another push is in progress'}), 429

    started = datetime.now()
    try:
        repos = []
        repos.append(_commit_and_push(INCOME_REPO, 'income'))
        repos.append(_commit_and_push(EXPENSES_REPO, 'expenses'))

        success = all(r.get('error') is None for r in repos)
        finished = datetime.now()
        return jsonify({
            'success': success,
            'started_at': started.strftime('%Y-%m-%d %H:%M:%S'),
            'finished_at': finished.strftime('%Y-%m-%d %H:%M:%S'),
            'duration_ms': int((finished - started).total_seconds() * 1000),
            'repos': repos
        }), 200 if success else 207
    finally:
        _push_lock.release()

# --- Main execution ---
if __name__ == '__main__':
    # Runs the Flask app. debug=True allows for auto-reloading on code changes.
    app.run(debug=True, port=5000)
