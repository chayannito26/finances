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
    return render_template('finance.html')

@app.route('/receipts/<filename>')
def get_receipt(filename):
    """Serves uploaded receipt files from the 'expenses/receipts' directory."""
    return send_from_directory(RECEIPTS_DIR, filename)


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
    new_filename = request.form.get('filename')

    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if file and new_filename:
        # Sanitize the filename to prevent security issues
        filename = secure_filename(new_filename)
        # Add extension from original file
        extension = os.path.splitext(file.filename)[1]
        final_filename = f"{filename}{extension}"
        
        save_path = os.path.join(RECEIPTS_DIR, final_filename)
        file.save(save_path)
        
        # The URL path the browser will use to access the file
        url_path = f'./receipts/{final_filename}'
        
        return jsonify({'url': url_path, 'description': filename}), 200

    return jsonify({'error': 'File or filename missing'}), 400


# --- Main execution ---
if __name__ == '__main__':
    # Runs the Flask app. debug=True allows for auto-reloading on code changes.
    app.run(debug=True, port=5000)
