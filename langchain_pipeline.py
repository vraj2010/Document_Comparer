# -*- coding: UTF-8 -*-
# langchain_pipeline.py
# LangChain-based document loading, clause-level diffing, and AI diff summary
# pipeline for DocCompare — additive module, does NOT touch fitz annotation logic.
#
# Jai Shree Krishna !!
# Code by Vraj !!!

import os
import re
import json
import difflib
from typing import List, Dict, Optional, Tuple

# ── LangChain core ──────────────────────────────────────────────────────────
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.documents import Document

# ── LangChain community loaders ──────────────────────────────────────────────
from langchain_community.document_loaders import PyMuPDFLoader   # PDF loader
from langchain_community.document_loaders import Docx2txtLoader  # DOCX loader

# ── LangChain text splitter ──────────────────────────────────────────────────
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ── Groq LLM via LangChain ───────────────────────────────────────────────────
from langchain_groq import ChatGroq

# ── Config ───────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")   # set in .env or environment
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# Max chars sent to the LLM per document side when NO clause structure is
# detected at all (rare fallback path only).
MAX_CHARS_PER_DOC = 6000

# Hard cap on the serialized clause-diff JSON sent to the LLM (token cost guard).
MAX_DIFF_JSON_CHARS = 12000

# Hard cap on text stored per clause segment (protects against one giant
# "preamble" blob if clause markers are sparse/missing).
MAX_CHARS_PER_CLAUSE = 1200

# ── [NEW] AGENT_INSTRUCTIONS.md  ─────────────────────────────────────────────
# This file is OPTIONAL. If present, its contents are inserted directly into
# the LLM prompt every time a summary is generated — so you can change the
# model's tone, domain context, or extra rules just by editing this .md file,
# with NO code changes and NO Flask restart required.
#
# ⚠️ Filename must match EXACTLY (case-sensitive). Place it next to this file
# (i.e. in the same folder as langchain_pipeline.py / app.py).
BASE_DIR                   = os.path.dirname(os.path.abspath(__file__))
AGENT_INSTRUCTIONS_FILENAME = "AGENT_INSTRUCTIONS.md"
AGENT_INSTRUCTIONS_PATH     = os.path.join(BASE_DIR, AGENT_INSTRUCTIONS_FILENAME)

# Tiny in-memory cache keyed by the file's mtime, so we don't re-read the
# file from disk on every single request — only when it's actually changed.
_agent_instructions_cache = {"mtime": None, "content": ""}


def _get_agent_instructions() -> str:
    """
    Returns the current contents of AGENT_INSTRUCTIONS.md.

    - If the file does not exist, returns "" and the pipeline runs exactly
      as before (the extra instructions block is simply empty).
    - If the file exists, it is re-read automatically whenever its
      modification time changes, so edits take effect on the next request
      without restarting the Flask process.
    """
    try:
        mtime = os.path.getmtime(AGENT_INSTRUCTIONS_PATH)
    except FileNotFoundError:
        if _agent_instructions_cache["mtime"] is not None:
            # File existed before and was deleted — clear the cache.
            _agent_instructions_cache["mtime"]   = None
            _agent_instructions_cache["content"] = ""
        return ""

    if _agent_instructions_cache["mtime"] != mtime:
        try:
            with open(AGENT_INSTRUCTIONS_PATH, "r", encoding="utf-8") as f:
                _agent_instructions_cache["content"] = f.read().strip()
            _agent_instructions_cache["mtime"] = mtime
        except OSError:
            # Read failed (permissions, race condition, etc.) — fall back to
            # whatever was last successfully loaded rather than crashing.
            pass

    return _agent_instructions_cache["content"]


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  DOCUMENT LOADING  (unchanged — replaces raw fitz text-only extraction)
# ═══════════════════════════════════════════════════════════════════════════════

def load_document_lc(file_path: str) -> List[Document]:
    """
    Load a PDF or DOCX file using the appropriate LangChain document loader.
    Returns a list of LangChain Document objects (one per page for PDF,
    one per paragraph block for DOCX).

    The fitz-based word-extraction for visual highlighting is kept separately
    in app.py — this loader is used only for the text/IR pipeline feeding the
    LLM summarizer.
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


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  CANONICAL TEXT  (full, untruncated — needed so clause markers near the
#     end of long documents aren't cut off before segmentation runs)
# ═══════════════════════════════════════════════════════════════════════════════

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=50,
    separators=["\n\n", "\n", " ", ""],
)


def merge_full_text(docs: List[Document]) -> str:
    """
    Merge LangChain Document pages into a single normalised string,
    WITHOUT truncating — clause segmentation needs to see the whole doc.
    """
    full_text = "\n".join(doc.page_content for doc in docs)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)
    full_text = re.sub(r" {2,}", " ", full_text)
    return full_text


def docs_to_canonical_text(docs: List[Document], max_chars: int = MAX_CHARS_PER_DOC) -> str:
    """
    Legacy helper (kept for backward compatibility / other call sites).
    Same as merge_full_text but truncated to max_chars.
    """
    return merge_full_text(docs)[:max_chars]


def get_text_chunks(docs: List[Document]) -> List[Document]:
    """Split loaded documents into overlapping chunks (for future vector-store use)."""
    return _splitter.split_documents(docs)


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  CLAUSE SEGMENTATION  (turns raw text into clause-tagged blocks)
# ═══════════════════════════════════════════════════════════════════════════════

# Multi-level numeric clause markers at the START of a line: "2.1", "2.1.1", "5.13"
_NUMERIC_RE = re.compile(r'^(\d{1,3}(?:\.\d{1,3}){1,5})\b')

# Bracketed sub-item markers at the START of a line: "(a)", "(k)", "(vii)", "(IV)"
_BRACKET_RE = re.compile(r'^\(([a-zA-Z]{1,6})\)')


def _detect_clause_marker(line: str) -> Optional[Tuple[str, str]]:
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
    it belongs to, based on leading clause markers:

        2.1.1   Clause 2.1.3 "Commercial Proposal" is revised...
        ...wrapped continuation lines stay under 2.1.1...
        (k)     Attachment No. 10 is revised...   -> tagged "2.1.5(k)" if the
                                                      last numeric clause was 2.1.5

    Returns: [{"clause": "2.1.1", "text": "<joined text for that clause>"}, ...]
    in document order. Text with no marker yet is tagged "Preamble".
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


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  CLAUSE-LEVEL DIFF  (replaces page/position-based diffing for the AI summary)
# ═══════════════════════════════════════════════════════════════════════════════

def diff_clauses(segments_orig: List[Dict], segments_mod: List[Dict]) -> List[Dict]:
    """
    Align clause segments by clause id (NOT by page/position) and classify
    each clause as added / removed / modified. Unchanged clauses are dropped
    so the LLM only sees what actually matters.

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

    # Preserve original document order; append clauses that are NEW
    # (only exist in the modified doc) at the end, in their own order.
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


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  DIFF SUMMARY CHAIN  (LCEL pipeline) — clause-aware + AGENT_INSTRUCTIONS.md
# ═══════════════════════════════════════════════════════════════════════════════

_SUMMARY_PROMPT = ChatPromptTemplate.from_template(
    """You are an expert document analyst specialising in contract and tender document review.

{agent_instructions}

Below is a structured, clause-level diff between the ORIGINAL and MODIFIED version of a document.
Each entry has a "clause" identifier exactly as it appears in the document (e.g. "2.1.3", "5.16(k)"),
a "status" (added / removed / modified), and the relevant original/modified text for that clause.

━━━━━━━━━━  CLAUSE-LEVEL DIFF (JSON)  ━━━━━━━━━━
{clause_diff_json}

━━━━━━━━━━  OUTPUT FORMAT (do not deviate from this)  ━━━━━━━━━━
## 📝 Overview
<2-3 sentence high-level summary of what changed overall>

## 📋 Clause-by-Clause Changes
### Clause {{clause}} — {{Added | Removed | Modified}}
<1-2 sentence plain-English description of exactly what changed in this clause>

(Repeat the "### Clause ..." block once per entry in the JSON above, in the same order. Use the EXACT clause id from the JSON — do not renumber or invent clause numbers.)

## ⚠️ Impact Assessment
<Brief note on the overall risk / importance of the changes>

Be concise. If the JSON is empty, state in the Overview that no clause-level changes were detected and omit the Clause-by-Clause section.
Anything in the "AGENT_INSTRUCTIONS" block above may add tone, domain context, or extra rules — but it must never override the OUTPUT FORMAT above."""
)


def build_diff_summary_chain():
    """
    Build and return the LCEL diff-summary chain.
    Chain: ChatPromptTemplate → ChatGroq (Llama3) → StrOutputParser
    Returns None if GROQ_API_KEY is not set, so the rest of the app
    degrades gracefully.
    """
    if not GROQ_API_KEY:
        return None

    llm = ChatGroq(
        model=GROQ_MODEL,
        groq_api_key=GROQ_API_KEY,
        temperature=0.2,
        max_tokens=1024,
    )
    return _SUMMARY_PROMPT | llm | StrOutputParser()


# Singleton chain — built once per process.
_diff_chain = build_diff_summary_chain()


def generate_diff_summary(clause_diff: List[Dict]) -> str:
    """
    Public API called from run_langchain_analysis.
    Runs the LCEL chain over the clause-level diff (plus whatever is
    currently in AGENT_INSTRUCTIONS.md, if anything) and returns a
    markdown-formatted, clause-keyed summary string.
    Falls back to a plain clause-list block if Groq is unavailable.
    """
    if not clause_diff:
        return "## 📝 Overview\nNo clause-level differences were detected between the two documents.\n"

    if _diff_chain is None:
        return _fallback_diff_summary(clause_diff)

    try:
        diff_json_str = json.dumps(clause_diff, indent=2, ensure_ascii=False)
        if len(diff_json_str) > MAX_DIFF_JSON_CHARS:
            diff_json_str = diff_json_str[:MAX_DIFF_JSON_CHARS] + "\n... (truncated)"

        agent_instructions = _get_agent_instructions()
        instructions_block = (
            f"━━━━━━━━━━  AGENT_INSTRUCTIONS  ━━━━━━━━━━\n{agent_instructions}"
            if agent_instructions else ""
        )

        return _diff_chain.invoke({
            "clause_diff_json":   diff_json_str,
            "agent_instructions": instructions_block,
        })
    except Exception as e:
        return f"⚠️ LLM summary unavailable: {e}\n\n" + _fallback_diff_summary(clause_diff)


def _fallback_diff_summary(clause_diff: List[Dict]) -> str:
    """Pure-Python fallback when Groq is not configured."""
    lines = ["## 📝 Overview", "Fallback diff (Groq API key not set).\n", "## 📋 Clause-by-Clause Changes"]
    for entry in clause_diff:
        lines.append(f"### Clause {entry['clause']} — {entry['status'].capitalize()}")
        lines.append("(LLM summary unavailable — see clause text in raw diff JSON.)\n")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# 6.  CONVENIENCE WRAPPER  — one call from app.py
# ═══════════════════════════════════════════════════════════════════════════════

def run_langchain_analysis(path_original: str, path_modified: str) -> dict:
    """
    Full LangChain pipeline:
      1. Load both documents via LangChain loaders
      2. Segment each into clause-tagged blocks (2.1.1, 5.16(k), ...)
      3. Diff the two clause-segment sets by clause id (not page/position)
      4. Generate an LLM summary keyed by clause number, honoring whatever
         is currently in AGENT_INSTRUCTIONS.md (if the file exists)
      5. Return a dict ready to be merged into the API JSON response

    This function is purely ADDITIVE — it does not replace or touch the
    existing fitz word-extraction / annotation pipeline in app.py.
    """
    docs1 = load_document_lc(path_original)
    docs2 = load_document_lc(path_modified)

    full_text1 = merge_full_text(docs1)
    full_text2 = merge_full_text(docs2)

    segments1 = extract_clause_segments(full_text1)
    segments2 = extract_clause_segments(full_text2)

    clause_diff = diff_clauses(segments1, segments2)

    summary = generate_diff_summary(clause_diff)

    return {
        "lc_summary":        summary,
        "lc_clause_diff":    clause_diff,
        "lc_original_pages": len(docs1),
        "lc_modified_pages": len(docs2),
    }