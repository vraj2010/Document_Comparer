# -*- coding: UTF-8 -*-

from dotenv import load_dotenv
load_dotenv()

import difflib
import os
import re
import uuid
import base64
import threading
from io import BytesIO
from collections import defaultdict

import fitz  # PyMuPDF
from PIL import Image
from flask import Flask, render_template, request, jsonify

from azure_summary import generate_change_summary, AzureSummarizerError

try:
    import win32com.client
    import pythoncom
    from ctypes import windll, wintypes
    on_windows = 1
except Exception:
    windll = None
    wintypes = None
    on_windows = 0

app = Flask(__name__)

BASE_DIR     = os.path.dirname(__file__)
TEMP_PDF_DIR = os.path.join(BASE_DIR, "temp_pdfs")
os.makedirs(TEMP_PDF_DIR, exist_ok=True)

# In-memory diff cache — stores aligned word lists between /api/compare and /api/summarize.
# Thread-safe via a lock. For multi-worker production, swap for Redis.
_diff_cache      : dict = {}
_diff_cache_lock = threading.Lock()


def _cache_put(comparison_id: str, words1: list, words2: list,
               name_a: str, name_b: str,
               case_insensitive: bool, ignore_quotes: bool,
               ignore_ligatures: bool) -> None:
    with _diff_cache_lock:
        _diff_cache[comparison_id] = {
            "words1":           words1,
            "words2":           words2,
            "name_a":           name_a,
            "name_b":           name_b,
            "case_insensitive": case_insensitive,
            "ignore_quotes":    ignore_quotes,
            "ignore_ligatures": ignore_ligatures,
        }


def _cache_get(comparison_id: str):
    """Returns cached dict and removes the entry (one-time use)."""
    with _diff_cache_lock:
        return _diff_cache.pop(comparison_id, None)


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
        output_pdf_path = os.path.join(
            TEMP_PDF_DIR, f"{base_name}_temp_{os.urandom(4).hex()}.pdf"
        )

    wdFormatPDF          = 17
    wdRevisionsViewFinal = 0
    word_app = None

    try:
        pythoncom.CoInitialize()
        word_app = win32com.client.DispatchEx("Word.Application")
        word_app.Visible       = False
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
            except Exception:
                pass
        pythoncom.CoUninitialize()


def detect_header_footer_bounds(pdf_document,
                                 top_zone_frac=0.15,
                                 bottom_zone_frac=0.15,
                                 repeat_ratio_threshold=0.6,
                                 fallback_top_frac=0.08,
                                 fallback_bottom_frac=0.08):
    """
    Detects per-page header/footer y-bounds using a hybrid strategy:

      1. Inspect text blocks inside the top/bottom *_zone_frac band of each page.
      2. Normalize block text (digits -> '#', whitespace collapsed) so page
         numbers and dates don't break matching across pages.
      3. If a normalized text recurs on >= repeat_ratio_threshold of pages
         (min 2), treat the furthest extent of any matching occurrence as the
         real header/footer boundary, stored as a fraction of page height so
         it applies sanely when page sizes vary within the document.
      4. Fall back to fixed margin bands when nothing repeats enough
         (single-page docs, or documents with no repeating header/footer).

    Returns:
        dict {page_num: (header_max_y, footer_min_y)}
        Words whose y1 <= header_max_y or y0 >= footer_min_y are header/footer.
    """
    page_count = pdf_document.page_count

    header_candidates = defaultdict(list)
    footer_candidates = defaultdict(list)

    def normalize(text):
        text = re.sub(r'\d+', '#', text)
        text = re.sub(r'\s+', ' ', text).strip().lower()
        return text

    page_heights = []

    for page_num in range(page_count):
        page = pdf_document.load_page(page_num)
        page.remove_rotation()
        page_height = page.rect.height
        page_heights.append(page_height)

        top_limit    = page_height * top_zone_frac
        bottom_limit = page_height * (1 - bottom_zone_frac)

        blocks = page.get_text("dict").get("blocks", [])
        for block in blocks:
            if block.get("type") != 0:   # text blocks only
                continue
            bbox = block.get("bbox")
            if not bbox:
                continue
            x0, y0, x1, y1 = bbox

            block_text = "".join(
                span.get("text", "")
                for line in block.get("lines", [])
                for span in line.get("spans", [])
            ).strip()
            if not block_text:
                continue

            norm = normalize(block_text)
            if not norm:
                continue

            if y1 <= top_limit:
                header_candidates[norm].append((page_num, y1, page_height))
            elif y0 >= bottom_limit:
                footer_candidates[norm].append((page_num, y0, page_height))

    min_pages_required = max(2, int(round(page_count * repeat_ratio_threshold)))

    def resolve_bound_fraction(candidates, want_max):
        """
        Returns the y-fraction of the candidate group with the most page hits
        (provided it clears min_pages_required), or None if nothing qualifies.
        """
        best_norm, best_hits = None, []
        for norm, hits in candidates.items():
            if len(hits) >= min_pages_required and len(hits) > len(best_hits):
                best_norm, best_hits = norm, hits

        if not best_hits:
            return None

        fracs = [(y / h) for (_, y, h) in best_hits]
        return max(fracs) if want_max else min(fracs)

    header_frac = resolve_bound_fraction(header_candidates, want_max=True)
    footer_frac = resolve_bound_fraction(footer_candidates, want_max=False)

    bounds = {}
    for page_num in range(page_count):
        page_height = page_heights[page_num]
        h_frac = header_frac if header_frac is not None else fallback_top_frac
        f_frac = footer_frac if footer_frac is not None else (1 - fallback_bottom_frac)
        bounds[page_num] = (page_height * h_frac, page_height * f_frac)

    return bounds


def extract_words_with_styles(pdf_document, ignore_ligatures=True,
                               header_footer_bounds=None):
    all_words_data = []
    LINE_TOLERANCE_Y = 3

    for page_num, page in enumerate(pdf_document):
        page.remove_rotation()

        header_max_y, footer_min_y = (None, None)
        if header_footer_bounds is not None:
            header_max_y, footer_min_y = header_footer_bounds.get(page_num, (None, None))

        if ignore_ligatures:
            words_data = page.get_text("words", flags=0)
        else:
            words_data = page.get_text("words")

        top_left_in_block = dict()
        grouped_lines     = []

        for word_info in words_data:
            x0, y0, x1, y1, word_text, block_no, _, _ = word_info[:8]
            word_center_y = (y0 + y1) / 2

            # Skip blank / lone-punctuation tokens to avoid false-positive diffs.
            _stripped = word_text.strip()
            if not _stripped:
                continue
            if re.fullmatch(r'[.,:;!?\u2026\u2022]+', _stripped):
                continue

            # Skip words that fall inside the detected header/footer zone.
            if header_max_y is not None and y1 <= header_max_y:
                continue
            if footer_min_y is not None and y0 >= footer_min_y:
                continue

            added_to_existing_line = False

            if block_no not in top_left_in_block:
                top_left_in_block[block_no] = x0, y0
            else:
                if (y0 < top_left_in_block[block_no][1] or
                        (y0 == top_left_in_block[block_no][1] and
                         x0 < top_left_in_block[block_no][0])):
                    top_left_in_block[block_no] = x0, y0

            for line_group in grouped_lines:
                if (abs(line_group['y_center'] - word_center_y) < LINE_TOLERANCE_Y
                        and line_group['block_no'] == block_no):
                    line_group['words'].append(word_info)
                    line_group['y_center'] = (
                        sum((w[1] + w[3]) / 2 for w in line_group['words'])
                        / len(line_group['words'])
                    )
                    added_to_existing_line = True
                    break

            if not added_to_existing_line:
                grouped_lines.append({
                    'y_center': word_center_y,
                    'words':    [word_info],
                    'block_no': block_no,
                })

        grouped_lines.sort(key=lambda lg: (
            top_left_in_block[lg['block_no']][1],
            top_left_in_block[lg['block_no']][0],
            lg['y_center'],
        ))

        for line_group in grouped_lines:
            line_group['words'].sort(key=lambda w: w[0])
            for word_info in line_group['words']:
                x0, y0, x1, y1, word_text, _, _, _ = word_info[:8]
                all_words_data.append({
                    "text":            word_text,
                    "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "page_num":        page_num,
                    "unique_id":       None,
                    "highlight_color": None,
                })

    return all_words_data


def helper_case_quotes(words_data1, words_data2, case_insensitive, ignore_quotes):
    a_compare = [w["text"] for w in words_data1]
    b_compare = [w["text"] for w in words_data2]

    if case_insensitive:
        a_compare = [w.lower() for w in a_compare]
        b_compare = [w.lower() for w in b_compare]

    if ignore_quotes:
        def nq(w):
            return (w.replace("\u2018", "'").replace("\u2019", "'")
                     .replace("\u02bc", "'").replace("\u201c", '"')
                     .replace("\u201d", '"'))
        a_compare = [nq(w) for w in a_compare]
        b_compare = [nq(w) for w in b_compare]

    # Strip trailing punctuation to avoid false-positive diffs on sentence ends.
    a_compare = [w.strip() for w in a_compare]
    b_compare = [w.strip() for w in b_compare]
    a_compare = [re.sub(r'[.,:;!?]+$', '', w) for w in a_compare]
    b_compare = [re.sub(r'[.,:;!?]+$', '', w) for w in b_compare]

    return a_compare, b_compare


def align_words_with_difflib(words_data1, words_data2,
                              case_insensitive, ignore_quotes,
                              highlight_scanned=False):
    """
    Align two word lists using difflib.
    highlight_scanned=True tags equal (unchanged) words as "yellow" so every
    successfully scanned token receives an annotation for OCR coverage review.
    """
    a_compare, b_compare = helper_case_quotes(
        words_data1, words_data2, case_insensitive, ignore_quotes
    )
    s = difflib.SequenceMatcher(None, a_compare, b_compare)

    common_word_id_counter = 0
    idx1_current           = 0
    idx2_current           = 0

    for tag, i1, i2, j1, j2 in s.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                common_id = f"common-word-{common_word_id_counter}"
                words_data1[idx1_current + k]["unique_id"]       = common_id
                words_data2[idx2_current + k]["unique_id"]       = common_id
                scan_color = "yellow" if highlight_scanned else None
                words_data1[idx1_current + k]["highlight_color"] = scan_color
                words_data2[idx2_current + k]["highlight_color"] = scan_color
                common_word_id_counter += 1
            idx1_current += (i2 - i1)
            idx2_current += (j2 - j1)

        elif tag == 'delete':
            for k in range(i2 - i1):
                words_data1[idx1_current + k]["unique_id"]       = None
                words_data1[idx1_current + k]["highlight_color"] = "red"
            idx1_current += (i2 - i1)

        elif tag == 'insert':
            for k in range(j2 - j1):
                words_data2[idx2_current + k]["unique_id"]       = None
                words_data2[idx2_current + k]["highlight_color"] = "green"
            idx2_current += (j2 - j1)

        elif tag == 'replace':
            for k in range(i2 - i1):
                words_data1[idx1_current + k]["unique_id"]       = None
                words_data1[idx1_current + k]["highlight_color"] = "red"
            for k in range(j2 - j1):
                words_data2[idx2_current + k]["unique_id"]       = None
                words_data2[idx2_current + k]["highlight_color"] = "green"
            idx1_current += (i2 - i1)
            idx2_current += (j2 - j1)

    return words_data1, words_data2


# Yellow is intentionally lighter than red/green so it doesn't obscure change marks.
_COLOR_OPACITY = {
    "red":    0.30,
    "green":  0.30,
    "yellow": 0.18,
    "blue":   0.25,
}


def apply_annotations_to_pdf_pages(pdf_document, words_data, dark_mode=False):
    if not pdf_document or pdf_document.is_closed:
        return 0, 0

    words_by_page = defaultdict(list)
    ins, dels     = 0, 0

    for word in words_data:
        if word["highlight_color"]:
            words_by_page[word["page_num"]].append(word)
            if word["highlight_color"] == "green":
                ins  += 1
            elif word["highlight_color"] == "red":
                dels += 1

    for page_num in range(pdf_document.page_count):
        page = pdf_document.load_page(page_num)

        for annot in list(page.annots()):
            if (annot.type[0] == fitz.PDF_ANNOT_HIGHLIGHT
                    and annot.info.get("title") == "PDFComparer"):
                page.delete_annot(annot)

        page_words = words_by_page[page_num]
        if not page_words:
            continue

        highlights_by_color = defaultdict(list)
        for word in page_words:
            rect = fitz.Rect(word["x0"], word["y0"], word["x1"], word["y1"])
            highlights_by_color[word["highlight_color"]].append(rect)

        # Draw yellow first so red/green change marks render on top.
        color_order = ["yellow", "blue", "red", "green"]
        ordered_items = sorted(
            highlights_by_color.items(),
            key=lambda kv: color_order.index(kv[0]) if kv[0] in color_order else 99
        )

        for color, rects_to_merge in ordered_items:
            if not rects_to_merge:
                continue

            merged_rects        = []
            current_merged_rect = rects_to_merge[0]

            for i in range(1, len(rects_to_merge)):
                nr = rects_to_merge[i]
                if (abs(current_merged_rect.y0 - nr.y0) < 10
                        and abs(current_merged_rect.y1 - nr.y1) < 10
                        and nr.x0 <= current_merged_rect.x1 + 10):
                    current_merged_rect = current_merged_rect | nr
                else:
                    merged_rects.append(current_merged_rect)
                    current_merged_rect = nr

            merged_rects.append(current_merged_rect)

            rgb = (0.0, 0.0, 0.0)
            if   color == "red":    rgb = (1.0, 0.0,  0.0)
            elif color == "green":  rgb = (0.0, 1.0,  0.0)
            elif color == "yellow": rgb = (1.0, 0.95, 0.0)
            elif color == "blue":   rgb = (0.0, 0.5,  1.0)

            opacity = _COLOR_OPACITY.get(color, 0.30)

            for rect in merged_rects:
                annot = page.add_highlight_annot(rect)
                annot.set_colors(stroke=rgb)
                if dark_mode:
                    annot.set_blendmode("Exclusion")
                    annot.set_opacity(1)
                else:
                    annot.set_blendmode("Multiply")
                    annot.set_opacity(opacity)
                annot.set_info(title="PDFComparer")
                annot.update()

    return ins, dels


def get_pdf_images_and_layout(pdf_path, zoom=1.0):
    images_b64   = []
    page_heights = []
    doc = fitz.open(pdf_path)
    for page_num in range(doc.page_count):
        page = doc.load_page(page_num)
        page_heights.append(page.rect.height)
        pix      = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img      = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        images_b64.append(base64.b64encode(buffered.getvalue()).decode())
    doc.close()
    return images_b64, page_heights


def calculate_absolute_y(word, page_heights, PAGE_PADDING=10):
    page_num = word["page_num"]
    y_start  = sum(page_heights[:page_num]) + (PAGE_PADDING * page_num)
    return y_start + word["y0"]


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/compare', methods=['POST'])
def compare():
    try:
        if 'original' not in request.files or 'modified' not in request.files:
            return jsonify({"error": "Both files are required"}), 400

        orig_file = request.files['original']
        mod_file  = request.files['modified']

        case_insensitive     = request.form.get('caseInsensitive',    'true')  == 'true'
        ignore_quotes        = request.form.get('ignoreQuotes',       'true')  == 'true'
        ignore_ligatures     = request.form.get('ignoreLigatures',    'true')  == 'true'
        dark_mode            = request.form.get('darkMode',           'false') == 'true'
        highlight_scanned    = request.form.get('highlightScanned',   'false') == 'true'
        ignore_header_footer = request.form.get('ignoreHeaderFooter', 'true')  == 'true'

        orig_path = os.path.join(
            TEMP_PDF_DIR, f"orig_{uuid.uuid4().hex}_{orig_file.filename}"
        )
        mod_path = os.path.join(
            TEMP_PDF_DIR, f"mod_{uuid.uuid4().hex}_{mod_file.filename}"
        )
        orig_file.save(orig_path)
        mod_file.save(mod_path)

        ext1 = os.path.splitext(orig_path)[1].lower()
        if ext1 in ['.doc', '.docx', '.rtf', '.txt']:
            orig_path = convert_word_to_pdf_no_markup(orig_path)

        ext2 = os.path.splitext(mod_path)[1].lower()
        if ext2 in ['.doc', '.docx', '.rtf', '.txt']:
            mod_path = convert_word_to_pdf_no_markup(mod_path)

        doc1 = fitz.open(orig_path)
        doc2 = fitz.open(mod_path)

        # Detect header/footer bounds independently per document since each file
        # may use different templates or margin sizes.
        bounds1 = detect_header_footer_bounds(doc1) if ignore_header_footer else None
        bounds2 = detect_header_footer_bounds(doc2) if ignore_header_footer else None

        words1 = extract_words_with_styles(doc1, ignore_ligatures, header_footer_bounds=bounds1)
        words2 = extract_words_with_styles(doc2, ignore_ligatures, header_footer_bounds=bounds2)

        words1, words2 = align_words_with_difflib(
            words1, words2, case_insensitive, ignore_quotes,
            highlight_scanned=highlight_scanned,
        )

        comparison_id = uuid.uuid4().hex
        _cache_put(
            comparison_id,
            words1, words2,
            orig_file.filename, mod_file.filename,
            case_insensitive, ignore_quotes, ignore_ligatures,
        )

        out1_path = os.path.join(TEMP_PDF_DIR, f"out1_{uuid.uuid4().hex}.pdf")
        out2_path = os.path.join(TEMP_PDF_DIR, f"out2_{uuid.uuid4().hex}.pdf")

        _, dels = apply_annotations_to_pdf_pages(doc1, words1, dark_mode)
        ins, _  = apply_annotations_to_pdf_pages(doc2, words2, dark_mode)

        doc1.save(out1_path)
        doc2.save(out2_path)

        images1_b64, page_heights_1 = get_pdf_images_and_layout(out1_path)
        images2_b64, page_heights_2 = get_pdf_images_and_layout(out2_path)

        words2_dict      = {w["unique_id"]: w for w in words2 if w["unique_id"]}
        common_words_map = []
        for w1 in words1:
            if w1["unique_id"] and w1["unique_id"] in words2_dict:
                w2 = words2_dict[w1["unique_id"]]
                common_words_map.append({
                    "left_y":  calculate_absolute_y(w1, page_heights_1),
                    "right_y": calculate_absolute_y(w2, page_heights_2),
                })

        changes = []
        for w1 in words1:
            if w1["highlight_color"] and w1["highlight_color"] != "yellow":
                changes.append({"pane": "left",
                                 "y": calculate_absolute_y(w1, page_heights_1)})
        for w2 in words2:
            if w2["highlight_color"] and w2["highlight_color"] != "yellow":
                changes.append({"pane": "right",
                                 "y": calculate_absolute_y(w2, page_heights_2)})
        changes.sort(key=lambda x: x["y"])

        scanned_words = (
            sum(1 for w in words1 if w["highlight_color"] == "yellow")
            if highlight_scanned else 0
        )

        doc1.close()
        doc2.close()

        return jsonify({
            "images1":          images1_b64,
            "images2":          images2_b64,
            "common_words_map": common_words_map,
            "changes":          changes,
            "insertions":       ins,
            "deletions":        dels,
            "scanned_words":    scanned_words,
            "comparison_id":    comparison_id,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/summarize', methods=['POST'])
def summarize():
    """
    Called by the frontend after /api/compare has rendered the diff images.

    Request JSON:  { "comparison_id": "<hex>" }
    Response JSON: { "ai_summary": "<markdown>", "ai_summary_error": null }
                or { "ai_summary": null, "ai_summary_error": "<message>" }
    """
    body          = request.get_json(silent=True) or {}
    comparison_id = body.get("comparison_id")

    if not comparison_id:
        return jsonify({"error": "comparison_id is required"}), 400

    cached = _cache_get(comparison_id)
    if cached is None:
        return jsonify({
            "ai_summary":       None,
            "ai_summary_error": (
                "Comparison session not found. "
                "It may have already been summarized or the server restarted. "
                "Run the comparison again."
            ),
        }), 404

    try:
        summary = generate_change_summary(
            cached["words1"],
            cached["words2"],
            doc_a            = cached["name_a"],
            doc_b            = cached["name_b"],
            case_insensitive = cached["case_insensitive"],
            ignore_quotes    = cached["ignore_quotes"],
            ignore_ligatures = cached["ignore_ligatures"],
        )
        return jsonify({"ai_summary": summary, "ai_summary_error": None})

    except AzureSummarizerError as e:
        return jsonify({"ai_summary": None, "ai_summary_error": str(e)})

    except Exception as e:
        return jsonify({
            "ai_summary":       None,
            "ai_summary_error": f"Unexpected error: {e}",
        })


if __name__ == '__main__':
    app.run(debug=True, port=5000)
