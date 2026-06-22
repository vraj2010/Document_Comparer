# -*- coding: UTF-8 -*-
# langchain_pipeline.py
# LangChain-based document loading, text extraction, and AI diff summary pipeline
# for DocCompare — additive module, does NOT touch existing fitz annotation logic.
#
# Jai Shree Krishna !!
# Code by Vraj !!!

import os
import difflib
from typing import List, Tuple

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
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama3-8b-8192")

# Max characters sent to the LLM per document side (keeps token cost low).
# Increase if you have a paid Groq tier.
MAX_CHARS_PER_DOC = 6000


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  DOCUMENT LOADING  (replaces raw fitz text-only extraction)
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
        # Fallback: read as plain text
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
        return [Document(page_content=raw, metadata={"source": file_path})]

    return loader.load()


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  CANONICAL IR  (Intermediate Representation — uniform text from any format)
# ═══════════════════════════════════════════════════════════════════════════════

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=50,
    separators=["\n\n", "\n", " ", ""],
)


def docs_to_canonical_text(docs: List[Document], max_chars: int = MAX_CHARS_PER_DOC) -> str:
    """
    Merge LangChain Document pages into a single normalised string.
    Strips excessive whitespace and truncates to max_chars so the LLM
    prompt stays within the context window.
    """
    full_text = "\n\n".join(doc.page_content for doc in docs)
    # Normalise whitespace
    import re
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)
    full_text = re.sub(r" {2,}", " ", full_text)
    return full_text[:max_chars]


def get_text_chunks(docs: List[Document]) -> List[Document]:
    """
    Split loaded documents into overlapping chunks suitable for
    semantic processing or future vector-store ingestion.
    """
    return _splitter.split_documents(docs)


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  DIFF SUMMARY CHAIN  (LCEL pipeline)
# ═══════════════════════════════════════════════════════════════════════════════

_SUMMARY_PROMPT = ChatPromptTemplate.from_template(
    """You are an expert document analyst specialising in contract and technical document review.

You are given two versions of a document — the ORIGINAL and the MODIFIED version.
Your task is to produce a structured, actionable diff summary.

━━━━━━━━━━  ORIGINAL DOCUMENT (truncated)  ━━━━━━━━━━
{text_original}

━━━━━━━━━━  MODIFIED DOCUMENT (truncated)  ━━━━━━━━━━
{text_modified}

━━━━━━━━━━  INSTRUCTIONS  ━━━━━━━━━━
Provide your response EXACTLY in the following markdown format:

## 📝 Overview
<2-3 sentence high-level summary of what changed>

## ➕ Additions
- <bullet per significant addition>

## ➖ Deletions
- <bullet per significant deletion>

## 🔄 Modifications
- <bullet per significant modification>

## ⚠️ Impact Assessment
<Brief note on overall risk / importance of the changes>

Be concise. Focus on semantically meaningful changes, not trivial whitespace."""
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
    chain = _SUMMARY_PROMPT | llm | StrOutputParser()
    return chain


# Singleton chain — built once per process.
_diff_chain = build_diff_summary_chain()


def generate_diff_summary(text_original: str, text_modified: str) -> str:
    """
    Public API called from app.py.
    Runs the LCEL chain and returns a markdown-formatted diff summary string.
    Falls back to a plain difflib stat block if Groq is unavailable.
    """
    if _diff_chain is None:
        return _fallback_diff_summary(text_original, text_modified)

    try:
        result = _diff_chain.invoke({
            "text_original": text_original[:MAX_CHARS_PER_DOC],
            "text_modified":  text_modified[:MAX_CHARS_PER_DOC],
        })
        return result
    except Exception as e:
        return f"⚠️ LLM summary unavailable: {e}\n\n" + _fallback_diff_summary(text_original, text_modified)


def _fallback_diff_summary(text1: str, text2: str) -> str:
    """
    Pure-Python fallback when Groq is not configured.
    Uses difflib to produce a word-count diff stat.
    """
    lines1 = text1.splitlines()
    lines2 = text2.splitlines()
    differ  = difflib.Differ()
    delta   = list(differ.compare(lines1, lines2))

    added   = sum(1 for l in delta if l.startswith("+ "))
    removed = sum(1 for l in delta if l.startswith("- "))

    return (
        f"## 📝 Overview\n"
        f"Fallback diff (Groq API key not set).\n\n"
        f"## ➕ Additions\n- {added} line(s) added\n\n"
        f"## ➖ Deletions\n- {removed} line(s) removed\n"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  CONVENIENCE WRAPPER  — one call from app.py
# ═══════════════════════════════════════════════════════════════════════════════

def run_langchain_analysis(path_original: str, path_modified: str) -> dict:
    """
    Full LangChain pipeline:
      1. Load both documents via LangChain loaders
      2. Convert to canonical IR text
      3. Generate LLM diff summary via LCEL chain
      4. Return a dict ready to be merged into the /api/compare JSON response

    This function is purely ADDITIVE — it does not replace or touch the
    existing fitz word-extraction / annotation pipeline in app.py.
    """
    docs1 = load_document_lc(path_original)
    docs2 = load_document_lc(path_modified)

    text1 = docs_to_canonical_text(docs1)
    text2 = docs_to_canonical_text(docs2)

    summary = generate_diff_summary(text1, text2)

    return {
        "lc_summary":        summary,
        "lc_original_chars": len(text1),
        "lc_modified_chars": len(text2),
        "lc_original_pages": len(docs1),
        "lc_modified_pages": len(docs2),
    }