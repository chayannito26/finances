import os
import json
from flask import Flask, jsonify, request, send_from_directory, render_template, abort
from werkzeug.utils import secure_filename
import time

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

def _allowed_extension(filename):
    return os.path.splitext(filename.lower())[1] in ALLOWED_EXTENSIONS

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
    return send_from_directory(RECEIPTS_DIR, safe_name)


# --- API Endpoints for Data Management ---

@app.route('/api/data', methods=['GET'])
def get_all_data():
    """API endpoint to fetch all income and expense data."""
    income_data = read_json_file(INCOME_FILE)
    expenses_data = read_json_file(EXPENSES_FILE)
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
    if item_id and any(item['id'] == item_id for item in all_income):
        # Update existing item
        all_income = [data if item['id'] == item_id else item for item in all_income]
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
    if item_id and any(item['id'] == item_id for item in all_expenses):
        # Update existing item
        all_expenses = [data if item['id'] == item_id else item for item in all_expenses]
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


# --- Main execution ---
if __name__ == '__main__':
    # Runs the Flask app. debug=True allows for auto-reloading on code changes.
    app.run(debug=True, port=5000)
