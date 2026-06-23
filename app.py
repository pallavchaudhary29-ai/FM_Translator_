import os
import sys
import uuid
import time
import html
import threading
import requests
import re
from flask import Flask, request, jsonify, render_template, send_file, abort
import fitz  # PyMuPDF
from deep_translator import GoogleTranslator

app = Flask(__name__)

# Configure upload limit to 100MB
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'

# Ensure directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# Global status tracking dict and lock
tasks = {}
tasks_lock = threading.Lock()

def update_task(task_id, **kwargs):
    with tasks_lock:
        if task_id not in tasks:
            tasks[task_id] = {}
        tasks[task_id].update(kwargs)

def get_task(task_id):
    with tasks_lock:
        return tasks.get(task_id)

def download_font_if_needed():
    """Downloads Noto Sans Devanagari font for Hindi rendering."""
    font_dir = os.path.join(app.static_folder or 'static', 'fonts')
    os.makedirs(font_dir, exist_ok=True)
    font_path = os.path.join(font_dir, 'NotoSansDevanagari-Regular.ttf')
    
    if not os.path.exists(font_path):
        print("Downloading Noto Sans Devanagari font...")
        url = "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansDevanagari/NotoSansDevanagari-Regular.ttf"
        try:
            r = requests.get(url, stream=True, timeout=30)
            r.raise_for_status()
            with open(font_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            print("Font downloaded successfully to", font_path)
        except Exception as e:
            print(f"Error downloading font: {e}")
            # If download fails, fitz will fall back to using standard sans-serif
            # or auto-fallback to NOTO library internally if internet is available.

def cleanup_temp_files():
    """Removes uploads and outputs older than 30 minutes."""
    now = time.time()
    for folder in [app.config['UPLOAD_FOLDER'], app.config['OUTPUT_FOLDER']]:
        if os.path.exists(folder):
            for filename in os.listdir(folder):
                filepath = os.path.join(folder, filename)
                try:
                    if os.path.getmtime(filepath) < now - 1800:
                        os.remove(filepath)
                        print(f"Cleaned up old temporary file: {filepath}")
                except Exception as e:
                    print(f"Error cleaning file {filepath}: {e}")

def estimate_alignment(block, block_bbox):
    """Infers the block text alignment based on horizontal bounds of its lines."""
    lines = block.get("lines", [])
    if not lines or len(lines) == 1:
        return 0  # Default to Left
    
    block_width = block_bbox[2] - block_bbox[0]
    if block_width <= 0:
        return 0
        
    start_diffs = []
    end_diffs = []
    
    for line in lines:
        line_bbox = line["bbox"]
        dx0 = line_bbox[0] - block_bbox[0]
        dx1 = block_bbox[2] - line_bbox[2]
        start_diffs.append(dx0)
        end_diffs.append(dx1)
        
    avg_start_diff = sum(start_diffs) / len(start_diffs)
    avg_end_diff = sum(end_diffs) / len(end_diffs)
    
    # Calculate variances to determine alignment
    start_var = sum((d - avg_start_diff)**2 for d in start_diffs) / len(start_diffs)
    end_var = sum((d - avg_end_diff)**2 for d in end_diffs) / len(end_diffs)
    
    if start_var < 1.0 and avg_start_diff < 5.0:
        return 0  # Left aligned
    elif end_var < 1.0 and avg_end_diff < 5.0:
        return 2  # Right aligned
    elif abs(avg_start_diff - avg_end_diff) < 5.0:
        return 1  # Center aligned
    
    return 0  # Fallback to Left

def clean_text_for_translation(text):
    """Cleans up internal newlines, tabs, and duplicate spaces for accurate translation."""
    if not text:
        return ""
    # Replace layout spacing and breaks with a single space
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def is_inside_table(bbox, tables):
    """Checks if the bounding box center lies within any detected table bounding box."""
    for table in tables:
        t_bbox = table.bbox
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        if t_bbox[0] <= cx <= t_bbox[2] and t_bbox[1] <= cy <= t_bbox[3]:
            return True
    return False

def get_dominant_style_in_bbox(bbox, text_dict):
    """Finds spans overlapping with a bounding box and returns their dominant size and color."""
    spans = []
    x0, y0, x1, y1 = bbox
    for block in text_dict.get("blocks", []):
        if block.get("type") == 0:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    span_bbox = span.get("bbox")
                    cx = (span_bbox[0] + span_bbox[2]) / 2
                    cy = (span_bbox[1] + span_bbox[3]) / 2
                    if x0 <= cx <= x1 and y0 <= cy <= y1:
                        spans.append(span)
                        
    if not spans:
        return 10, (0, 0, 0)
        
    style_weights = {}
    for span in spans:
        text_len = len(span.get("text", ""))
        size = round(span.get("size", 10), 1)
        color = span.get("color", 0)
        style_key = (size, color)
        style_weights[style_key] = style_weights.get(style_key, 0) + text_len
        
    if not style_weights:
        return 10, (0, 0, 0)
        
    dominant_style = max(style_weights, key=style_weights.get)
    dominant_size, dominant_color_int = dominant_style
    
    r = (dominant_color_int >> 16) & 255
    g = (dominant_color_int >> 8) & 255
    b = dominant_color_int & 255
    dominant_color_rgb = (r / 255.0, g / 255.0, b / 255.0)
    
    return dominant_size, dominant_color_rgb

def get_dominant_style(block):
    """Determines dominant font size and color weighted by text length."""
    spans = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            spans.append(span)
            
    if not spans:
        return 12, (0, 0, 0)
        
    style_weights = {}
    for span in spans:
        text_len = len(span.get("text", ""))
        size = round(span.get("size", 12), 1)
        color = span.get("color", 0)
        
        style_key = (size, color)
        style_weights[style_key] = style_weights.get(style_key, 0) + text_len
        
    if not style_weights:
        return 12, (0, 0, 0)
        
    dominant_style = max(style_weights, key=style_weights.get)
    dominant_size, dominant_color_int = dominant_style
    
    # Convert integer color (RRGGBB) to RGB float tuple
    r = (dominant_color_int >> 16) & 255
    g = (dominant_color_int >> 8) & 255
    b = dominant_color_int & 255
    dominant_color_rgb = (r / 255.0, g / 255.0, b / 255.0)
    
    return dominant_size, dominant_color_rgb

def estimate_line_height(block, dominant_size):
    """Estimates line spacing ratio based on line separation distance."""
    lines = block.get("lines", [])
    if len(lines) < 2 or dominant_size <= 0:
        return "1.2"
        
    heights = []
    for i in range(len(lines) - 1):
        y0_current = lines[i]["bbox"][1]
        y0_next = lines[i+1]["bbox"][1]
        diff = y0_next - y0_current
        if diff > 0:
            heights.append(diff)
            
    if not heights:
        return "1.2"
        
    avg_diff = sum(heights) / len(heights)
    line_height_multiplier = avg_diff / dominant_size
    
    if line_height_multiplier < 0.8:
        line_height_multiplier = 0.8
    elif line_height_multiplier > 2.5:
        line_height_multiplier = 1.2
        
    return f"{line_height_multiplier:.2f}"

def translate_blocks_batched(block_items, translator):
    """Combines text blocks into large batches to translate efficiently and prevent rate limits."""
    texts = [b["text"] for b in block_items]
    if not texts:
        return []
        
    delimiter = "\n[===]\n"
    chunks = []
    current_chunk = []
    current_length = 0
    
    for text in texts:
        if len(text) > 4000:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_length = 0
            chunks.append([text])
        elif current_length + len(text) + len(delimiter) > 4000:
            chunks.append(current_chunk)
            current_chunk = [text]
            current_length = len(text)
        else:
            current_chunk.append(text)
            current_length += len(text) + len(delimiter)
            
    if current_chunk:
        chunks.append(current_chunk)
        
    translated_texts = []
    for chunk in chunks:
        combined_text = delimiter.join(chunk)
        try:
            translated_combined = translator.translate(combined_text)
            # Split with robust regex to allow optional whitespace inside/around the brackets
            split_trans = re.split(r'\s*\[\s*===\s*\]\s*', translated_combined)
            split_trans = [t.strip() for t in split_trans]
            
            if len(split_trans) == len(chunk):
                translated_texts.extend(split_trans)
                continue
            else:
                print(f"Batch length mismatch: expected {len(chunk)}, got {len(split_trans)}")
        except Exception as e:
            print(f"Batch translation failed: {e}. Falling back to individual translation.")
            
        # Fallback: translate individually
        for item in chunk:
            if not item.strip():
                translated_texts.append("")
            else:
                try:
                    translated_texts.append(translator.translate(item))
                except Exception as ex:
                    print(f"Individual translation failed: {ex}")
                    translated_texts.append(item)  # Keep original text on failure
                    
    return translated_texts

def translate_pdf_task(task_id, input_path, output_path, original_filename):
    """Background thread target to perform the translation."""
    try:
        update_task(task_id, status='processing', progress=5, message='Opening PDF...')
        doc = fitz.open(input_path)
        total_pages = len(doc)
        
        translator = GoogleTranslator(source='en', target='hi')
        
        # Load local font file directory as an Archive
        font_dir = os.path.join(app.static_folder or 'static', 'fonts')
        archive = fitz.Archive(font_dir)
        
        update_task(task_id, status='processing', progress=10, message='Processing pages...')
        
        for page_idx in range(total_pages):
            page = doc[page_idx]
            
            # Update progress
            progress_pct = int(10 + (page_idx / total_pages) * 80)
            update_task(
                task_id, 
                status='processing', 
                progress=progress_pct, 
                message=f'Translating page {page_idx + 1} of {total_pages}...'
            )
            
            text_dict = page.get_text("dict")
            
            # 1. Find tables on the page
            tables = page.find_tables()
            table_list = tables.tables if tables else []
            
            # Track all page elements to translate in one batch
            page_elements = []
            
            # 2. Extract table cells
            for table in table_list:
                tab_text = table.extract()
                for r_idx, row in enumerate(table.rows):
                    for c_idx, cell in enumerate(row.cells):
                        if not cell:
                            continue
                        cell_bbox = cell
                        cell_raw_text = tab_text[r_idx][c_idx] if r_idx < len(tab_text) and c_idx < len(tab_text[r_idx]) else ""
                        cleaned_cell_text = clean_text_for_translation(cell_raw_text)
                        
                        if cleaned_cell_text:
                            size, color = get_dominant_style_in_bbox(cell_bbox, text_dict)
                            page_elements.append({
                                "type": "cell",
                                "text": cleaned_cell_text,
                                "bbox": cell_bbox,
                                "size": size,
                                "color": color,
                                "alignment": 0,  # Left align table cells by default
                                "line_height": "1.2"
                            })
            
            # 3. Extract general text blocks (excluding table content)
            blocks = text_dict.get("blocks", [])
            for block in blocks:
                if block.get("type") == 0:  # Text block
                    bbox = block.get("bbox")
                    if is_inside_table(bbox, table_list):
                        continue
                        
                    block_text = ""
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            block_text += span.get("text", "")
                        block_text += " "  # Space-join instead of newline to preserve layout/grammar
                        
                    cleaned_block_text = clean_text_for_translation(block_text)
                    if cleaned_block_text:
                        size, color = get_dominant_style(block)
                        alignment = estimate_alignment(block, bbox)
                        line_height = estimate_line_height(block, size)
                        
                        page_elements.append({
                            "type": "block",
                            "text": cleaned_block_text,
                            "bbox": bbox,
                            "size": size,
                            "color": color,
                            "alignment": alignment,
                            "line_height": line_height
                        })
            
            if not page_elements:
                continue
                
            # 4. Batch translate all extracted items for this page
            translated_texts = translate_blocks_batched(page_elements, translator)
            
            # 5. Add redaction annotations to erase old text
            for item in page_elements:
                bbox = item["bbox"]
                page.add_redact_annot(bbox, fill=(1, 1, 1))
                
            page.apply_redactions()
            
            # 6. Insert translated Hindi text boxes
            for idx, item in enumerate(page_elements):
                bbox = item["bbox"]
                trans_text = translated_texts[idx]
                
                if not trans_text.strip():
                    continue
                    
                dominant_size = item["size"]
                dominant_color_rgb = item["color"]
                alignment = item["alignment"]
                line_height = item["line_height"]
                
                align_map = {0: "left", 1: "center", 2: "right", 3: "justify"}
                alignment_css = align_map.get(alignment, "left")
                
                escaped_text = html.escape(trans_text).replace("\n", "<br>")
                html_content = f"<div>{escaped_text}</div>"
                
                r, g, b = dominant_color_rgb
                css = f"""
                @font-face {{
                    font-family: 'NotoSansDevanagari';
                    src: url('NotoSansDevanagari-Regular.ttf');
                }}
                div {{
                    font-family: 'NotoSansDevanagari', sans-serif;
                    font-size: {dominant_size}pt;
                    color: rgb({int(r*255)}, {int(g*255)}, {int(b*255)});
                    text-align: {alignment_css};
                    line-height: {line_height};
                    margin: 0;
                    padding: 0;
                }}
                """
                
                try:
                    page.insert_htmlbox(bbox, html_content, css=css, archive=archive)
                except Exception as ex:
                    print(f"Error inserting HTML textbox on page {page_idx+1}: {ex}")
                    # Fallback to standard textbox
                    try:
                        page.insert_textbox(
                            bbox, 
                            trans_text, 
                            fontname="helv", 
                            fontsize=dominant_size, 
                            color=dominant_color_rgb, 
                            align=alignment
                        )
                    except Exception:
                        pass
                        
        update_task(task_id, status='processing', progress=95, message='Saving translated PDF...')
        doc.save(output_path)
        doc.close()
        
        base_name, ext = os.path.splitext(original_filename)
        download_name = f"{base_name}_hindi{ext}"
        
        update_task(
            task_id, 
            status='completed', 
            progress=100, 
            message='Translation complete!', 
            download_filename=download_name
        )
        
    except Exception as e:
        print(f"Error in translate_pdf_task for {task_id}: {e}")
        update_task(task_id, status='error', error=str(e), message='Translation failed.')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    cleanup_temp_files()  # Trigger routine cleanup on upload
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file part in request'}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
        
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Only PDF files are allowed'}), 400
        
    # Generate unique Task ID
    task_id = uuid.uuid4().hex
    input_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{task_id}_input.pdf")
    output_path = os.path.join(app.config['OUTPUT_FOLDER'], f"{task_id}_output.pdf")
    
    try:
        file.save(input_path)
    except Exception as e:
        return jsonify({'error': f'Failed to save uploaded file: {e}'}), 500
        
    # Register task status
    update_task(task_id, status='queued', progress=0, message='Initializing...', error=None, download_filename=None)
    
    # Run the processing asynchronously
    threading.Thread(
        target=translate_pdf_task,
        args=(task_id, input_path, output_path, file.filename),
        daemon=True
    ).start()
    
    return jsonify({'task_id': task_id})

@app.route('/status/<task_id>', methods=['GET'])
def get_status(task_id):
    task = get_task(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(task)

@app.route('/download/<task_id>', methods=['GET'])
def download_file(task_id):
    task = get_task(task_id)
    if not task or task.get('status') != 'completed':
        abort(404, description="File not ready or task failed")
        
    output_path = os.path.join(app.config['OUTPUT_FOLDER'], f"{task_id}_output.pdf")
    if not os.path.exists(output_path):
        abort(404, description="Translated file not found on server")
        
    return send_file(
        output_path, 
        as_attachment=True, 
        download_name=task.get('download_filename', 'translated.pdf')
    )

# Download the Devanagari font when starting the app
with app.app_context():
    download_font_if_needed()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
