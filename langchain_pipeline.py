# -*- coding: UTF-8 -*-
"""
langchain_pipeline.py

Document loading, clause segmentation, and clause-level diffing utilities
for DocCompare. This module is purely additive — it does not touch the
fitz annotation pipeline in app.py.

The AI summary is handled by azure_summary.py (Azure OpenAI).
This module provides the underlying text/IR infrastructure used to
structure documents into clause-tagged blocks before diffing.

Jai Shree Krishna !!
Code by Vraj !!!
"""

import os
import re
from typing import List, Dict, Optional

from langchain_core.documents import Document
from langchain_community.document_loaders import PyMuPDFLoader
from langchain_community.document_loaders import Docx2txtLoader


# Hard cap on text stored per clause segment (protects against one giant
# "preamble" blob when clause markers are sparse or missing).
MAX_CHARS_PER_CLAUSE = 1200


def load_document_lc(file_path: str) -> List[Document]:
    """
    Load a PDF or DOCX file using the appropriate LangChain document loader.
    Returns a list of LangChain Document objects (one per page for PDF,
    one per paragraph block for DOCX).

    The fitz-based word-extraction for visual highlighting is kept separately
    in app.py — this loader is used only for the text/IR pipeline feeding the
    clause segmentation and diff steps.
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        loader = PyMuPDFLoader(file_path)
    elif ext in (".docx", ".doc"):
        loader = Docx2txtLoader(file_path)
    else:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
        return [Document(page_content=raw, metadata={"source": file_path})]

    return loader.load()


def merge_full_text(docs: List[Document]) -> str:
    """
    Merge LangChain Document pages into a single normalised string without
    truncating — clause segmentation needs the complete document text.
    """
    full_text = "\n".join(doc.page_content for doc in docs)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)
    full_text = re.sub(r" {2,}", " ", full_text)
    return full_text


# Clause marker patterns (shared with azure_summary.py's word-level detector).
_NUMERIC_RE = re.compile(r'^(\d{1,3}(?:\.\d{1,3}){1,5})\b')
_BRACKET_RE = re.compile(r'^\(([a-zA-Z]{1,6})\)')


def _detect_clause_marker(line: str) -> Optional[tuple]:
    m = _NUMERIC_RE.match(line)
    if m:
        return ('numeric', m.group(1))
    m = _BRACKET_RE.match(line)
    if m:
        return ('bracket', m.group(1))
    return None


def extract_clause_segments(text: str) -> List[Dict[str, str]]:
    """
    Walk the document text line-by-line and tag each line with the clause
    it belongs to based on leading clause markers:

        2.1.1   Clause 2.1.3 "Commercial Proposal" is revised...
        (k)     Attachment No. 10 is revised...  -> tagged "2.1.5(k)"

    Returns a list of {"clause": "<id>", "text": "<joined text>"} in
    document order. Text before the first marker is tagged "Preamble".
    """
    segments: List[Dict[str, str]] = []
    current_numeric: Optional[str] = None
    current_full = "Preamble"
    buf: List[str] = []

    def flush():
        if buf:
            joined = " ".join(buf).strip()
            if joined:
                segments.append({"clause": current_full, "text": joined})
            buf.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        marker = _detect_clause_marker(line)
        if marker:
            kind, val = marker
            flush()
            if kind == 'numeric':
                current_numeric = val
                current_full = val
            else:  # bracket — nest under the last numeric clause seen
                current_full = f"{current_numeric}({val})" if current_numeric else f"({val})"

        buf.append(line)

    flush()
    return segments


def _normalize_ws(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def diff_clauses(segments_orig: List[Dict], segments_mod: List[Dict]) -> List[Dict]:
    """
    Align clause segments by clause id (not by page/position) and classify
    each clause as added / removed / modified. Unchanged clauses are dropped
    so the LLM only sees what actually changed.

    Returns a list of:
        {"clause": "2.1.5(k)", "status": "modified",
         "original_text": "...", "modified_text": "..."}
    """
    orig_map: Dict[str, str] = {}
    orig_order: List[str] = []
    for seg in segments_orig:
        c = seg["clause"]
        if c not in orig_map:
            orig_order.append(c)
            orig_map[c] = seg["text"]
        else:
            orig_map[c] += " " + seg["text"]

    mod_map: Dict[str, str] = {}
    mod_order: List[str] = []
    for seg in segments_mod:
        c = seg["clause"]
        if c not in mod_map:
            mod_order.append(c)
            mod_map[c] = seg["text"]
        else:
            mod_map[c] += " " + seg["text"]

    # Preserve original document order; new clauses from the modified doc
    # are appended at the end in their own order.
    ordered_clauses = list(orig_order)
    for c in mod_order:
        if c not in orig_map:
            ordered_clauses.append(c)

    diffs: List[Dict] = []
    for clause in ordered_clauses:
        o_text = orig_map.get(clause)
        m_text = mod_map.get(clause)

        if o_text is not None and m_text is not None:
            if _normalize_ws(o_text) == _normalize_ws(m_text):
                continue  # unchanged — skip
            status = "modified"
        elif o_text is not None:
            status = "removed"
        else:
            status = "added"

        diffs.append({
            "clause":        clause,
            "status":        status,
            "original_text": (o_text or "").strip()[:MAX_CHARS_PER_CLAUSE],
            "modified_text": (m_text or "").strip()[:MAX_CHARS_PER_CLAUSE],
        })

    return diffs
