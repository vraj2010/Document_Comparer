# -*- coding: UTF-8 -*-
import difflib
import os
import pathlib
import re
import sys
import uuid
import base64
from io import BytesIO
from collections import defaultdict

import fitz  # PyMuPDF
from PIL import Image
from flask import Flask, render_template, request, jsonify

# We keep pywin32 for local execution if requested
try:
    import win32com.client
    import pythoncom
    from ctypes import windll, wintypes
    on_windows = 1
except:
    windll = None
    wintypes = None
    on_windows = 0

app = Flask(__name__)
BASE_DIR = os.path.dirname(__file__)
TEMP_PDF_DIR = os.path.join(BASE_DIR, "temp_pdfs")
os.makedirs(TEMP_PDF_DIR, exist_ok=True)

# --- Backend Logic ---

def convert_word_to_pdf_no_markup(input_file_path, output_pdf_path=None):
    if not on_windows:
        raise Exception("Word conversion only works on Windows with pywin32.")

    input_file_path = input_file_path.replace("/", "\\")
    if output_pdf_path:
        output_pdf_path = output_pdf_path.replace("/", "\\")

    if not os.path.exists(input_file_path):
        return None

    if output_pdf_path is None:
        base_name = os.path.splitext(os.path.basename(input_file_path))[0]
        output_pdf_path = os.path.join(TEMP_PDF_DIR, f"{base_name}_temp_{os.urandom(4).hex()}.pdf")

    wdFormatPDF = 17
    wdRevisionsViewFinal = 0
    word_app = None
    doc = None

    try:
        pythoncom.CoInitialize()
        word_app = win32com.client.DispatchEx("Word.Application")
        word_app.Visible = False
        word_app.DisplayAlerts = False
        doc = word_app.Documents.Open(str(input_file_path))

        if hasattr(word_app.Options, 'WarnBeforeSavingPrintingSendingMarkup'):
            word_app.Options.WarnBeforeSavingPrintingSendingMarkup = False
        if doc.ActiveWindow:
            doc.ActiveWindow.View.RevisionsView = wdRevisionsViewFinal
        if hasattr(doc, 'ShowRevisions'):
            doc.ShowRevisions = False
        if hasattr(word_app.Options, 'PrintRevisions'):
            word_app.Options.PrintRevisions = False
        if hasattr(word_app.Options, 'PrintComments'):
            word_app.Options.PrintComments = False
        if hasattr(word_app.Options, 'PrintHiddenText'):
            word_app.Options.PrintHiddenText = False
        if hasattr(word_app.Options, 'PrintDrawingObjects'):
            word_app.Options.PrintDrawingObjects = True

        doc.SaveAs(str(output_pdf_path), FileFormat=wdFormatPDF)
        doc.Close(SaveChanges=False)
        return output_pdf_path
    except Exception as e:
        raise Exception(f"Word Conversion Error: {e}")
    finally:
        if word_app:
            try:
                word_app.Quit(SaveChanges=0)
            except:
                pass
        pythoncom.CoUninitialize()

def extract_words_with_styles(pdf_document, ignore_ligatures=True):
    all_words_data = []
    LINE_TOLERANCE_Y = 3

    for page_num, page in enumerate(pdf_document):
        page.remove_rotation()
        if ignore_ligatures:
            words_data = page.get_text("words", flags=0)
        else:
            words_data = page.get_text("words")
            
        top_left_in_block = dict()
        grouped_lines = []
        
        for word_info in words_data:
            x0, y0, x1, y1, word_text, block_no, _, _ = word_info[:8]
            word_center_y = (y0 + y1) / 2
            added_to_existing_line = False

            if block_no not in top_left_in_block:
                top_left_in_block[block_no] = x0, y0
            else:
                if y0 < top_left_in_block[block_no][1] or (y0 == top_left_in_block[block_no][1] and x0 < top_left_in_block[block_no][0]):
                    top_left_in_block[block_no] = x0, y0

            for line_group in grouped_lines:
                if abs(line_group['y_center'] - word_center_y) < LINE_TOLERANCE_Y and line_group['block_no'] == block_no:
                    line_group['words'].append(word_info)
                    line_group['y_center'] = sum((w[1] + w[3]) / 2 for w in line_group['words']) / len(line_group['words'])
                    added_to_existing_line = True
                    break

            if not added_to_existing_line:
                grouped_lines.append({
                    'y_center': word_center_y,
                    'words': [word_info],
                    'block_no': block_no
                })

        grouped_lines.sort(key=lambda lg: (top_left_in_block[lg['block_no']][1], top_left_in_block[lg['block_no']][0], lg['y_center']))

        for line_group in grouped_lines:
            line_group['words'].sort(key=lambda w: w[0])
            for word_info in line_group['words']:
                x0, y0, x1, y1, word_text, _, _, _ = word_info[:8]
                all_words_data.append({
                    "text": word_text,
                    "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "page_num": page_num,
                    "unique_id": None,
                    "highlight_color": None
                })
    return all_words_data

def helper_case_quotes(words_data1, words_data2, case_insensitive, ignore_quotes):
    a_compare = [word_info["text"] for word_info in words_data1]
    b_compare = [word_info["text"] for word_info in words_data2]
    if case_insensitive:
        a_compare = [word.lower() for word in a_compare]
        b_compare = [word.lower() for word in b_compare]
    if ignore_quotes:
        a_compare = [word.replace("‘", "'").replace("’", "'").replace("ʼ", "'").replace('“', '"').replace('”', '"') for word in a_compare]
        b_compare = [word.replace("‘", "'").replace("’", "'").replace("ʼ", "'").replace('“', '"').replace('”', '"') for word in b_compare]
    return a_compare, b_compare

def align_words_with_difflib(words_data1, words_data2, case_insensitive, ignore_quotes):
    a_compare, b_compare = helper_case_quotes(words_data1, words_data2, case_insensitive, ignore_quotes)
    s = difflib.SequenceMatcher(None, a_compare, b_compare)
    common_word_id_counter = 0
    idx1_current = 0
    idx2_current = 0

    for tag, i1, i2, j1, j2 in s.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                common_id = f"common-word-{common_word_id_counter}"
                words_data1[idx1_current + k]["unique_id"] = common_id
                words_data2[idx2_current + k]["unique_id"] = common_id
                words_data1[idx1_current + k]["highlight_color"] = None
                words_data2[idx2_current + k]["highlight_color"] = None
                common_word_id_counter += 1
            idx1_current += (i2 - i1)
            idx2_current += (j2 - j1)
        elif tag == 'delete': 
            for k in range(i2 - i1):
                words_data1[idx1_current + k]["unique_id"] = None
                words_data1[idx1_current + k]["highlight_color"] = "red"
            idx1_current += (i2 - i1)
        elif tag == 'insert': 
            for k in range(j2 - j1):
                words_data2[idx2_current + k]["unique_id"] = None
                words_data2[idx2_current + k]["highlight_color"] = "green"
            idx2_current += (j2 - j1)
        elif tag == 'replace': 
            for k in range(i2 - i1):
                words_data1[idx1_current + k]["unique_id"] = None
                words_data1[idx1_current + k]["highlight_color"] = "red"
            for k in range(j2 - j1):
                words_data2[idx2_current + k]["unique_id"] = None
                words_data2[idx2_current + k]["highlight_color"] = "green"
            idx1_current += (i2 - i1)
            idx2_current += (j2 - j1)
    return words_data1, words_data2

def apply_annotations_to_pdf_pages(pdf_document, words_data, dark_mode=False):
    if not pdf_document or pdf_document.is_closed:
        return 0, 0
        
    words_by_page = defaultdict(list)
    ins, dels = 0, 0
    for word in words_data:
        if word["highlight_color"]:
            words_by_page[word["page_num"]].append(word)
            if word["highlight_color"] == "green": ins += 1
            elif word["highlight_color"] == "red": dels += 1

    for page_num in range(pdf_document.page_count):
        page = pdf_document.load_page(page_num)
        annotations_to_delete = [
            annot for annot in page.annots()
            if annot.type[0] == fitz.PDF_ANNOT_HIGHLIGHT and annot.info.get("title") == "PDFComparer"
        ]
        for annot in annotations_to_delete:
            page.delete_annot(annot)

        page_words = words_by_page[page_num]
        if not page_words:
            continue

        highlights_by_color = defaultdict(list)
        for word in page_words:
            rect = fitz.Rect(word["x0"], word["y0"], word["x1"], word["y1"])
            highlights_by_color[word["highlight_color"]].append(rect)

        for color, rects_to_merge in highlights_by_color.items():
            if not rects_to_merge: continue
            merged_rects = []
            current_merged_rect = rects_to_merge[0]
            for i in range(1, len(rects_to_merge)):
                next_rect = rects_to_merge[i]
                y_tolerance = 10 
                x_tolerance = 10 
                if (abs(current_merged_rect.y0 - next_rect.y0) < y_tolerance and
                    abs(current_merged_rect.y1 - next_rect.y1) < y_tolerance and
                    next_rect.x0 <= current_merged_rect.x1 + x_tolerance): 
                    current_merged_rect = current_merged_rect | next_rect 
                else:
                    merged_rects.append(current_merged_rect)
                    current_merged_rect = next_rect
            merged_rects.append(current_merged_rect) 

            highlight_color_rgb_float = (0.0, 0.0, 0.0)
            if color == "red":
                highlight_color_rgb_float = (1.0, 0.0, 0.0)
            elif color == "green":
                highlight_color_rgb_float = (0.0, 1.0, 0.0)
            elif color == "blue": 
                highlight_color_rgb_float = (0.0, 0.5, 1.0)

            for merged_rect in merged_rects:
                annot = page.add_highlight_annot(merged_rect)
                annot.set_colors(stroke=highlight_color_rgb_float)
                
                if dark_mode:
                    annot.set_blendmode("Exclusion")
                    annot.set_opacity(1)
                else:
                    annot.set_blendmode("Multiply")
                    annot.set_opacity(0.3)
                    
                annot.set_info(title="PDFComparer")
                annot.update()
                
    return ins, dels

def get_pdf_images_and_layout(pdf_path, zoom=1.0):
    images_b64 = []
    page_heights = []
    doc = fitz.open(pdf_path)
    for page_num in range(doc.page_count):
        page = doc.load_page(page_num)
        page_heights.append(page.rect.height)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        images_b64.append(img_str)
    doc.close()
    return images_b64, page_heights

def calculate_absolute_y(word, page_heights, PAGE_PADDING=10):
    page_num = word["page_num"]
    y_start = sum(page_heights[:page_num]) + (PAGE_PADDING * page_num)
    return y_start + word["y0"]

# --- Flask Routes ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/compare', methods=['POST'])
def compare():
    try:
        if 'original' not in request.files or 'modified' not in request.files:
            return jsonify({"error": "Both files are required"}), 400

        orig_file = request.files['original']
        mod_file = request.files['modified']
        
        case_insensitive = request.form.get('caseInsensitive', 'true') == 'true'
        ignore_quotes = request.form.get('ignoreQuotes', 'true') == 'true'
        ignore_ligatures = request.form.get('ignoreLigatures', 'true') == 'true'
        dark_mode = request.form.get('darkMode', 'false') == 'true'

        orig_path = os.path.join(TEMP_PDF_DIR, f"orig_{uuid.uuid4().hex}_{orig_file.filename}")
        mod_path = os.path.join(TEMP_PDF_DIR, f"mod_{uuid.uuid4().hex}_{mod_file.filename}")
        
        orig_file.save(orig_path)
        mod_file.save(mod_path)
        
        # Word Conversion Check
        ext1 = os.path.splitext(orig_path)[1].lower()
        if ext1 in ['.doc', '.docx', '.rtf', '.txt']:
            orig_path = convert_word_to_pdf_no_markup(orig_path)
            
        ext2 = os.path.splitext(mod_path)[1].lower()
        if ext2 in ['.doc', '.docx', '.rtf', '.txt']:
            mod_path = convert_word_to_pdf_no_markup(mod_path)

        doc1 = fitz.open(orig_path)
        doc2 = fitz.open(mod_path)
        
        words1 = extract_words_with_styles(doc1, ignore_ligatures)
        words2 = extract_words_with_styles(doc2, ignore_ligatures)
        
        words1, words2 = align_words_with_difflib(words1, words2, case_insensitive, ignore_quotes)
        
        out1_path = os.path.join(TEMP_PDF_DIR, f"out1_{uuid.uuid4().hex}.pdf")
        out2_path = os.path.join(TEMP_PDF_DIR, f"out2_{uuid.uuid4().hex}.pdf")
        
        _, dels = apply_annotations_to_pdf_pages(doc1, words1, dark_mode)
        ins, _ = apply_annotations_to_pdf_pages(doc2, words2, dark_mode)
        
        doc1.save(out1_path)
        doc2.save(out2_path)
        
        images1_b64, page_heights_1 = get_pdf_images_and_layout(out1_path, zoom=1.0)
        images2_b64, page_heights_2 = get_pdf_images_and_layout(out2_path, zoom=1.0)
        
        common_words_map = []
        words2_dict = {w["unique_id"]: w for w in words2 if w["unique_id"] is not None}
        for w1 in words1:
            if w1["unique_id"] is not None and w1["unique_id"] in words2_dict:
                w2 = words2_dict[w1["unique_id"]]
                y1_abs = calculate_absolute_y(w1, page_heights_1)
                y2_abs = calculate_absolute_y(w2, page_heights_2)
                common_words_map.append({"left_y": y1_abs, "right_y": y2_abs})
        
        changes = []
        for w1 in words1:
            if w1["highlight_color"] is not None:
                changes.append({"pane": "left", "y": calculate_absolute_y(w1, page_heights_1)})
        for w2 in words2:
            if w2["highlight_color"] is not None:
                changes.append({"pane": "right", "y": calculate_absolute_y(w2, page_heights_2)})
        
        changes.sort(key=lambda x: x["y"])
        
        doc1.close()
        doc2.close()
        
        return jsonify({
            "images1": images1_b64,
            "images2": images2_b64,
            "common_words_map": common_words_map,
            "changes": changes,
            "insertions": ins,
            "deletions": dels
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)
