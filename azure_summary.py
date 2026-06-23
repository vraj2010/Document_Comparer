# -*- coding: UTF-8 -*-
"""
azure_summary.py

LangChain-based AI change-summary module using the organisation's
Azure OpenAI deployment.

Mirrors the exact client construction from the org's sample code:

    client = AzureOpenAI(
        api_version   = "2024-12-01-preview",
        azure_endpoint= "https://digital-openaikey.openai.azure.com/",
        api_key       = subscription_key,
    )
    response = client.chat.completions.create(
        model                = deployment,   # "gpt-4.1-mini"
        max_completion_tokens= 13107,
        temperature          = 1.0,
        top_p                = 1.0,
        frequency_penalty    = 0.0,
        presence_penalty     = 0.0,
        messages             = [...]
    )

This module wraps those exact settings inside a LangChain
AzureChatOpenAI + LCEL chain so the rest of the application
(app.py routes, caching, error handling) stays unchanged.

Files required in the same folder as this file:
    AGENT_INSTRUCTIONS.md   — the system prompt / agent instructions

Environment variables (set in .env — never commit that file):
    AZURE_OPENAI_API_KEY        your organisation subscription key
    AZURE_OPENAI_ENDPOINT       https://digital-openaikey.openai.azure.com/
    AZURE_OPENAI_API_VERSION    2024-12-01-preview
    AZURE_OPENAI_DEPLOYMENT     gpt-4.1-mini

Install:
    pip install langchain langchain-openai langchain-core openai python-dotenv
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Iterator

from langchain_openai import AzureChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser


# ── Configuration ─────────────────────────────────────────────────────────────

INSTRUCTIONS_PATH = os.path.join(os.path.dirname(__file__), "AGENT_INSTRUCTIONS.md")

# Read from environment; these are set in .env and loaded by app.py
# via load_dotenv() at startup.
_ENDPOINT   = lambda: os.environ.get("AZURE_OPENAI_ENDPOINT",    "")
_API_KEY    = lambda: os.environ.get("AZURE_OPENAI_API_KEY",     "")
_API_VER    = lambda: os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
_DEPLOYMENT = lambda: os.environ.get("AZURE_OPENAI_DEPLOYMENT",  "gpt-4.1-mini")

# Matches the org sample exactly: max_completion_tokens=13107
# Raise to 32000 or 65536 if you need longer amendment registers.
MAX_COMPLETION_TOKENS = int(os.environ.get("AZURE_MAX_TOKENS", "13107"))

CONTEXT_WORDS    = 8
BRIDGE_GAP_WORDS = 2
MAX_SPANS        = 400


# ── Clause marker detection (mirrors langchain_pipeline.py's patterns) ───────
# These are the SAME marker patterns used by langchain_pipeline.py's
# extract_clause_segments(). Duplicated here (not imported) so this
# Azure-only module doesn't pull in langchain_groq / document loaders it
# has no other use for. Keep in sync manually if those patterns change.
#
# Multi-level numeric clause markers at the START of a word: "2.1", "2.1.1", "5.13"
_NUMERIC_RE = re.compile(r'^(\d{1,3}(?:\.\d{1,3}){1,5})\b')

# Bracketed sub-item markers at the START of a word: "(a)", "(k)", "(vii)"
_BRACKET_RE = re.compile(r'^\(([a-zA-Z]{1,6})\)')

# Matches LINE_TOLERANCE_Y used in app.py's extract_words_with_styles, so
# "same line" is detected consistently between the two modules.
_CLAUSE_LINE_TOLERANCE_Y = 3


def _detect_clause_marker(word_text):
    m = _NUMERIC_RE.match(word_text)
    if m:
        return ('numeric', m.group(1))
    m = _BRACKET_RE.match(word_text)
    if m:
        return ('bracket', m.group(1))
    return None


def tag_words_with_clause(words_data):
    """
    Walks words_data (already in page/reading order, exactly as produced by
    app.py's extract_words_with_styles) and tags every word dict IN PLACE
    with a "clause" key — the clause identifier it belongs to.

    Uses the same numeric ("2.1.3") / bracket ("(k)") marker convention as
    langchain_pipeline.py's extract_clause_segments(), but detects markers
    at the WORD level (this module only has word dicts, not clean page
    text): a word counts as "start of a new line" when its vertical center
    differs from the immediately preceding word's by more than
    _CLAUSE_LINE_TOLERANCE_Y, or it's the first word on a new page. If that
    line-starting word matches a clause marker, the running "current
    clause" updates (numeric markers reset the base clause; bracket
    markers nest under the last numeric clause seen, e.g. "2.1.5(k)").
    Every word — including non-marker words — inherits whatever clause was
    last established. Text before the first marker is tagged "Preamble",
    matching langchain_pipeline.py's convention.

    Mutates words_data in place and also returns it for convenience.
    """
    current_numeric = None
    current_clause  = "Preamble"
    prev_y_center    = None
    prev_page        = None

    for w in words_data:
        y_center = (w["y0"] + w["y1"]) / 2
        is_line_start = (
            prev_page is None
            or w["page_num"] != prev_page
            or prev_y_center is None
            or abs(y_center - prev_y_center) > _CLAUSE_LINE_TOLERANCE_Y
        )

        if is_line_start:
            marker = _detect_clause_marker(w["text"].strip())
            if marker:
                kind, val = marker
                if kind == "numeric":
                    current_numeric = val
                    current_clause  = val
                else:
                    current_clause = f"{current_numeric}({val})" if current_numeric else f"({val})"

        w["clause"] = current_clause
        prev_y_center = y_center
        prev_page     = w["page_num"]

    return words_data
# ─────────────────────────────────────────────────────────────────────────────


# ── Custom exception ──────────────────────────────────────────────────────────

class AzureSummarizerError(Exception):
    pass


# ── Step 1: extract contiguous change spans from words_data ───────────────────

@dataclass
class ChangeSpan:
    kind: str                         # "added" | "removed"
    page: int                         # 1-indexed for humans
    clause: str = "Preamble"          # clause id this span belongs to (see tag_words_with_clause)
    words: list = field(default_factory=list)
    context_before: str = ""
    context_after:  str = ""

    @property
    def text(self) -> str:
        return " ".join(w["text"] for w in self.words)


def _sort_key(w):
    return (w["page_num"], round(w["y0"], 1), w["x0"])


def _context(sorted_words, around_idx, direction, max_words):
    collected = []
    page = sorted_words[around_idx]["page_num"]
    i = around_idx + direction
    while 0 <= i < len(sorted_words) and len(collected) < max_words:
        if sorted_words[i]["page_num"] != page:
            break
        collected.append(sorted_words[i]["text"])
        i += direction
    if direction == -1:
        collected.reverse()
    return " ".join(collected)


def extract_change_spans(words_data, bridge_gap=BRIDGE_GAP_WORDS):
    """
    Groups consecutive red/green highlighted words into ChangeSpan objects.
    Same-colour runs separated by <= bridge_gap unchanged words are folded
    into one span so a lightly-edited sentence appears as a single change.

    Assumes words_data has already been passed through
    tag_words_with_clause() (done in build_diff_payload), so each word
    dict carries a "clause" key.
    """
    sorted_words  = sorted(words_data, key=_sort_key)
    pos_by_id     = {id(w): i for i, w in enumerate(sorted_words)}
    color_to_kind = {"red": "removed", "green": "added"}

    raw_runs    = []
    gaps_after  = []
    cur_color   = None
    cur_words   = []
    pending_gap = []

    for w in sorted_words:
        color = w.get("highlight_color")
        if color in color_to_kind:
            if cur_color is None:
                cur_color, cur_words, pending_gap = color, [w], []
            elif color == cur_color and not pending_gap:
                cur_words.append(w)
            else:
                raw_runs.append({"color": cur_color, "words": cur_words})
                gaps_after.append(pending_gap)
                cur_color, cur_words, pending_gap = color, [w], []
        else:
            if cur_color is not None:
                pending_gap.append(w)

    if cur_words:
        raw_runs.append({"color": cur_color, "words": cur_words})
        gaps_after.append(pending_gap)

    merged = []
    i = 0
    while i < len(raw_runs):
        color = raw_runs[i]["color"]
        words = list(raw_runs[i]["words"])
        j = i
        while (j + 1 < len(raw_runs)
               and raw_runs[j + 1]["color"] == color
               and 0 < len(gaps_after[j]) <= bridge_gap):
            words.extend(gaps_after[j])
            words.extend(raw_runs[j + 1]["words"])
            j += 1
        merged.append({"color": color, "words": words})
        i = j + 1

    spans = []
    for run in merged:
        fi = pos_by_id[id(run["words"][0])]
        li = pos_by_id[id(run["words"][-1])]
        spans.append(ChangeSpan(
            kind           = color_to_kind[run["color"]],
            page           = run["words"][0]["page_num"] + 1,
            clause         = run["words"][0].get("clause", "Preamble"),
            words          = run["words"],
            context_before = _context(sorted_words, fi, -1, CONTEXT_WORDS),
            context_after  = _context(sorted_words, li,  1, CONTEXT_WORDS),
        ))
    return spans


# ── Step 2: pair removed+added into "replaced", build JSON payload ────────────

def _match_score(r, a):
    rb = (r.context_before or "").split()
    ab = (a.context_before or "").split()
    ra = (r.context_after  or "").split()
    aa = (a.context_after  or "").split()
    score = 0
    for n in (3, 2, 1):
        if rb[-n:] and rb[-n:] == ab[-n:]:
            score = max(score, n)
            break
    for n in (3, 2, 1):
        if ra[:n] and ra[:n] == aa[:n]:
            score = max(score, n)
            break
    return score


def build_diff_payload(words1, words2,
                       doc_a="Document A",
                       doc_b="Document B",
                       case_insensitive=None,
                       ignore_quotes=None,
                       ignore_ligatures=None):
    """
    Builds the JSON payload that will be sent to the LLM.
    Called with the words1/words2 already tagged by align_words_with_difflib.
    """
    # ── NEW: tag every word with the clause it belongs to, BEFORE spans
    #    are built, so each ChangeSpan inherits a real clause id instead
    #    of only ever having a page number. ──────────────────────────────
    tag_words_with_clause(words1)
    tag_words_with_clause(words2)
    # ─────────────────────────────────────────────────────────────────────

    removed = [s for s in extract_change_spans(words1) if s.kind == "removed"]
    added   = [s for s in extract_change_spans(words2) if s.kind == "added"]

    used_added   = set()
    replacements = []
    leftover_rem = []

    for r in removed:
        best_idx, best_sc = None, 0
        for idx, a in enumerate(added):
            if idx in used_added or a.page != r.page:
                continue
            sc = _match_score(r, a)
            if sc > best_sc:
                best_idx, best_sc = idx, sc
        if best_idx is not None:
            used_added.add(best_idx)
            replacements.append((r, added[best_idx]))
        else:
            leftover_rem.append(r)

    leftover_add = [a for i, a in enumerate(added) if i not in used_added]

    changes = []
    for r, a in replacements:
        changes.append({
            "type": "replaced",
            "clause": r.clause or a.clause,
            "page": r.page,
            "old_text": r.text,
            "new_text": a.text,
            "context_before": r.context_before,
            "context_after":  a.context_after,
        })
    for r in leftover_rem:
        changes.append({
            "type": "removed",
            "clause": r.clause,
            "page": r.page,
            "text": r.text,
            "context_before": r.context_before,
            "context_after":  r.context_after,
        })
    for a in leftover_add:
        changes.append({
            "type": "added",
            "clause": a.clause,
            "page": a.page,
            "text": a.text,
            "context_before": a.context_before,
            "context_after":  a.context_after,
        })

    changes.sort(key=lambda c: c["page"])
    truncated = len(changes) > MAX_SPANS
    if truncated:
        changes = changes[:MAX_SPANS]

    return {
        "document_a_name": doc_a,
        "document_b_name": doc_b,
        "stats": {
            "total_words_a":          len(words1),
            "total_words_b":          len(words2),
            "added_words":            sum(len(s.words) for s in added),
            "removed_words":          sum(len(s.words) for s in removed),
            "change_spans_included":  len(changes),
            "change_spans_truncated": truncated,
        },
        "normalization": {
            "case_insensitive": bool(case_insensitive) if case_insensitive is not None else None,
            "ignore_quotes":    bool(ignore_quotes)    if ignore_quotes    is not None else None,
            "ignore_ligatures": bool(ignore_ligatures) if ignore_ligatures is not None else None,
        },
        "changes": changes,
    }


# ── Step 3: build the LangChain chain using AzureChatOpenAI ──────────────────

def _load_instructions(path=INSTRUCTIONS_PATH) -> str:
    p = Path(path)
    if not p.exists():
        raise AzureSummarizerError(
            f"AGENT_INSTRUCTIONS.md not found at: {path}\n"
            "Place it in the same folder as azure_summary.py."
        )
    return p.read_text(encoding="utf-8")


def _validate_config():
    """Checks all four required Azure env vars are present before building."""
    missing = []
    if not _API_KEY():    missing.append("AZURE_OPENAI_API_KEY")
    if not _ENDPOINT():   missing.append("AZURE_OPENAI_ENDPOINT")
    if not _API_VER():    missing.append("AZURE_OPENAI_API_VERSION")
    if not _DEPLOYMENT(): missing.append("AZURE_OPENAI_DEPLOYMENT")
    if missing:
        raise AzureSummarizerError(
            "Missing Azure OpenAI configuration: " + ", ".join(missing) + ".\n"
            "Add them to your .env file and make sure load_dotenv() runs at "
            "the top of app.py before any imports read os.environ."
        )


def _build_chain():
    """
    Constructs the LCEL chain:
        ChatPromptTemplate | AzureChatOpenAI | StrOutputParser

    AzureChatOpenAI parameters match the org sample exactly:
        api_version          = "2024-12-01-preview"
        azure_endpoint       = "https://digital-openaikey.openai.azure.com/"
        api_key              = subscription_key
        azure_deployment     = "gpt-4.1-mini"   (the deployment name)
        max_completion_tokens= 13107
        temperature          = 1.0
        top_p                = 1.0
        frequency_penalty    = 0.0
        presence_penalty     = 0.0

    To swap to a different deployment later, just change
    AZURE_OPENAI_DEPLOYMENT in .env — no code change needed.
    """
    _validate_config()
    system_prompt = _load_instructions()

    # 1. Prompt — system carries AGENT_INSTRUCTIONS.md,
    #             human carries the JSON diff payload
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human",
         "Here is the structured diff payload for the two documents "
         "currently being compared. Produce the summary exactly as "
         "specified in your instructions.\n\n{payload}"),
    ])

    # 2. LLM — AzureChatOpenAI with the org's exact parameters
    llm = AzureChatOpenAI(
        api_version          = _API_VER(),
        azure_endpoint       = _ENDPOINT(),
        api_key              = _API_KEY(),
        azure_deployment     = _DEPLOYMENT(),
        max_completion_tokens= MAX_COMPLETION_TOKENS,
        temperature          = 1.0,
        top_p                = 1.0,
        frequency_penalty    = 0.0,
        presence_penalty     = 0.0,
    )

    # 3. Parser — extracts .content from AIMessage, returns plain str
    parser = StrOutputParser()

    # 4. LCEL chain: prompt | llm | parser
    return prompt | llm | parser


# ── Step 4: public API called by app.py ───────────────────────────────────────

def generate_change_summary(words1, words2,
                             doc_a="Document A",
                             doc_b="Document B",
                             case_insensitive=None,
                             ignore_quotes=None,
                             ignore_ligatures=None) -> str:
    """
    Full pipeline:
        extract spans → build payload → invoke LangChain Azure chain

    Called from the /api/summarize Flask route in app.py.
    Returns a markdown-formatted amendment summary string.
    Raises AzureSummarizerError on any failure so the route can return
    a clean JSON error instead of a 500 traceback.
    """
    payload = build_diff_payload(
        words1, words2,
        doc_a=doc_a, doc_b=doc_b,
        case_insensitive=case_insensitive,
        ignore_quotes=ignore_quotes,
        ignore_ligatures=ignore_ligatures,
    )

    if not payload["changes"]:
        normalized = any(v for v in payload.get("normalization", {}).values())
        return (
            "## Overview\n\n"
            "No content differences were detected between the two documents"
            + (" (after normalization)." if normalized else ".")
        )

    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        chain   = _build_chain()
        summary = chain.invoke({"payload": payload_json})
    except AzureSummarizerError:
        raise
    except Exception as e:
        raise AzureSummarizerError(
            f"Azure OpenAI chain failed: {e}\n"
            "Check your AZURE_OPENAI_* environment variables and that the "
            "deployment name matches what exists in Azure AI Foundry."
        ) from e

    if not summary or not summary.strip():
        raise AzureSummarizerError(
            "Azure OpenAI returned an empty response. "
            "The deployment may be throttled or the prompt exceeded "
            "max_completion_tokens."
        )

    return summary.strip()