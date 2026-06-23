# -*- coding: UTF-8 -*-
"""
azure_summary.py

LangChain-based AI change-summary module using Azure OpenAI.

Environment variables (set in .env):
    AZURE_OPENAI_API_KEY        — your organisation subscription key
    AZURE_OPENAI_ENDPOINT       — e.g. https://digital-openaikey.openai.azure.com/
    AZURE_OPENAI_API_VERSION    — e.g. 2024-12-01-preview
    AZURE_OPENAI_DEPLOYMENT     — e.g. gpt-4.1-mini

Install:
    pip install langchain langchain-openai langchain-core openai python-dotenv
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from langchain_openai import AzureChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser


INSTRUCTIONS_PATH = os.path.join(os.path.dirname(__file__), "AGENT_INSTRUCTIONS.md")

_ENDPOINT   = lambda: os.environ.get("AZURE_OPENAI_ENDPOINT",    "")
_API_KEY    = lambda: os.environ.get("AZURE_OPENAI_API_KEY",     "")
_API_VER    = lambda: os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
_DEPLOYMENT = lambda: os.environ.get("AZURE_OPENAI_DEPLOYMENT",  "gpt-4.1-mini")

MAX_COMPLETION_TOKENS = int(os.environ.get("AZURE_MAX_TOKENS", "13107"))

CONTEXT_WORDS    = 8
BRIDGE_GAP_WORDS = 2
MAX_SPANS        = 400


# Clause marker patterns — must match those used in langchain_pipeline.py's
# extract_clause_segments() and app.py's LINE_TOLERANCE_Y so clause detection
# stays consistent across modules.
_NUMERIC_RE = re.compile(r'^(\d{1,3}(?:\.\d{1,3}){1,5})\b')
_BRACKET_RE = re.compile(r'^\(([a-zA-Z]{1,6})\)')
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
    Walks words_data (in page/reading order as produced by
    extract_words_with_styles) and tags every word dict IN PLACE with a
    "clause" key — the clause identifier it belongs to.

    Numeric markers (e.g. "2.1.3") reset the running clause; bracket markers
    (e.g. "(k)") nest under the last numeric clause seen as "2.1.5(k)".
    Words before the first marker are tagged "Preamble".
    """
    current_numeric = None
    current_clause  = "Preamble"
    prev_y_center   = None
    prev_page       = None

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


class AzureSummarizerError(Exception):
    pass


@dataclass
class ChangeSpan:
    kind: str                         # "added" | "removed"
    page: int                         # 1-indexed
    clause: str = "Preamble"
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
    Same-colour runs separated by <= bridge_gap unchanged words are merged
    so a lightly-edited sentence appears as a single change.

    Requires words_data to have been passed through tag_words_with_clause()
    so each word carries a "clause" key.
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
    Builds the JSON payload sent to the LLM.
    Called with words1/words2 already tagged by align_words_with_difflib.
    """
    tag_words_with_clause(words1)
    tag_words_with_clause(words2)

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
            "type":           "replaced",
            "clause":         r.clause or a.clause,
            "page":           r.page,
            "old_text":       r.text,
            "new_text":       a.text,
            "context_before": r.context_before,
            "context_after":  a.context_after,
        })
    for r in leftover_rem:
        changes.append({
            "type":           "removed",
            "clause":         r.clause,
            "page":           r.page,
            "text":           r.text,
            "context_before": r.context_before,
            "context_after":  r.context_after,
        })
    for a in leftover_add:
        changes.append({
            "type":           "added",
            "clause":         a.clause,
            "page":           a.page,
            "text":           a.text,
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


def _load_instructions(path=INSTRUCTIONS_PATH) -> str:
    p = Path(path)
    if not p.exists():
        raise AzureSummarizerError(
            f"AGENT_INSTRUCTIONS.md not found at: {path}\n"
            "Place it in the same folder as azure_summary.py."
        )
    return p.read_text(encoding="utf-8")


def _validate_config():
    """Checks all four required Azure env vars are present before building the chain."""
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
    Constructs the LCEL chain: ChatPromptTemplate | AzureChatOpenAI | StrOutputParser

    Parameters match the organisation's deployment settings exactly:
        api_version           = AZURE_OPENAI_API_VERSION  (from .env)
        azure_endpoint        = AZURE_OPENAI_ENDPOINT     (from .env)
        azure_deployment      = AZURE_OPENAI_DEPLOYMENT   (from .env)
        max_completion_tokens = AZURE_MAX_TOKENS          (default 13107)
        temperature / top_p / penalties = org defaults
    """
    _validate_config()
    system_prompt = _load_instructions()

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human",
         "Here is the structured diff payload for the two documents "
         "currently being compared. Produce the summary exactly as "
         "specified in your instructions.\n\n{payload}"),
    ])

    llm = AzureChatOpenAI(
        api_version           = _API_VER(),
        azure_endpoint        = _ENDPOINT(),
        api_key               = _API_KEY(),
        azure_deployment      = _DEPLOYMENT(),
        max_completion_tokens = MAX_COMPLETION_TOKENS,
        temperature           = 1.0,
        top_p                 = 1.0,
        frequency_penalty     = 0.0,
        presence_penalty      = 0.0,
    )

    return prompt | llm | StrOutputParser()


def generate_change_summary(words1, words2,
                             doc_a="Document A",
                             doc_b="Document B",
                             case_insensitive=None,
                             ignore_quotes=None,
                             ignore_ligatures=None) -> str:
    """
    Full pipeline: extract spans → build payload → invoke Azure LangChain chain.

    Called from the /api/summarize Flask route in app.py.
    Returns a markdown-formatted amendment summary string.
    Raises AzureSummarizerError on any failure so the route can return a
    clean JSON error instead of a 500 traceback.
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
