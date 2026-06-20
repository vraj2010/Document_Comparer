# -*- coding: UTF-8 -*-
"""
groq_summary.py

AI change-summary module for the Document_Comparer Flask app (app.py).

This module is import-only: it has no Flask dependency itself, so it can be
unit-tested standalone. app.py imports generate_change_summary() and calls
it inside the existing /api/compare route, right after
align_words_with_difflib() has tagged words1/words2 -- see the
INTEGRATION section at the bottom of this file for the exact patch.

Pipeline:
    words1, words2 (tagged by align_words_with_difflib in app.py)
        -> extract_change_spans()   groups consecutive red/green words into
                                     contiguous, human-readable spans with
                                     surrounding context (per document)
        -> build_diff_payload()     merges both documents' spans into the
                                     JSON contract described in
                                     AGENT_INSTRUCTIONS.md, pairing
                                     delete+insert spans into "replaced"
        -> summarize_with_groq()    sends payload + system prompt
                                     (AGENT_INSTRUCTIONS.md) to Groq,
                                     returns markdown summary text

Requires:
    pip install groq

Environment:
    GROQ_API_KEY must be set (e.g. in a .env file loaded by app.py, or the
    shell environment).

Note on this app's diff backend: app.py only uses align_words_with_difflib
(no git-diff backend, no move detection), so highlight_color is always one
of {"red", "green", None} here -- never "blue". This module reflects that;
it does not handle a "moved" change type.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INSTRUCTIONS_PATH = os.path.join(os.path.dirname(__file__), "AGENT_INSTRUCTIONS.md")

DEFAULT_MODEL = "llama-3.3-70b-versatile"

# Words of context pulled on each side of a change span.
CONTEXT_WORDS = 8

# Unchanged-word runs of this length or shorter, sitting BETWEEN two
# same-color highlighted runs, get bridged into a single span instead of
# being reported as separate tiny changes.
BRIDGE_GAP_WORDS = 2

# Hard cap on spans sent to the model, to bound token usage on very large
# diffs (e.g. a wholesale template swap). Anything beyond this is still
# reflected in `stats`, just not itemized in `changes`.
MAX_SPANS_TO_MODEL = 400


# ---------------------------------------------------------------------------
# Step 1: Extract contiguous change spans from one document's words list
# ---------------------------------------------------------------------------

@dataclass
class ChangeSpan:
    kind: str                  # "added" | "removed"
    page: int                  # 1-indexed for human readability
    words: list = field(default_factory=list)
    context_before: str = ""
    context_after: str = ""

    @property
    def text(self) -> str:
        return " ".join(w["text"] for w in self.words)


def _word_sort_key(w):
    # Reading order: page, then top-to-bottom, then left-to-right.
    return (w["page_num"], round(w["y0"], 1), w["x0"])


def _plain_text_context(sorted_words, around_index, direction, max_words):
    """
    Walks outward from around_index in `direction` (-1 or +1) over a
    page-sorted word list, collecting up to max_words of unchanged
    (highlight_color is None) words for context. Stops at a page boundary.
    """
    collected = []
    page_num = sorted_words[around_index]["page_num"]
    i = around_index + direction
    while 0 <= i < len(sorted_words) and len(collected) < max_words:
        w = sorted_words[i]
        if w["page_num"] != page_num:
            break
        collected.append(w["text"])
        i += direction
    if direction == -1:
        collected.reverse()
    return " ".join(collected)


def extract_change_spans(words_data, bridge_gap=BRIDGE_GAP_WORDS):
    """
    Groups consecutive highlighted words (highlight_color in {"red","green"})
    in a single document's word list into contiguous ChangeSpan objects. A
    short run of unchanged words (<= bridge_gap) sandwiched BETWEEN two
    same-color highlighted runs is folded into one span. Unchanged words
    are never appended to a span unless another same-color run actually
    follows -- trailing plain text after the last highlighted run is left
    alone.

    Call separately on words1 (-> "removed" spans) and words2
    (-> "added" spans); this app's diff never tags "blue"/moved words.

    Returns a list of ChangeSpan in reading order.
    """
    sorted_words = sorted(words_data, key=_word_sort_key)
    position_by_id = {id(w): i for i, w in enumerate(sorted_words)}
    color_to_kind = {"red": "removed", "green": "added"}

    raw_runs = []
    gaps_after_run = []

    current_color = None
    current_words = []
    pending_gap = []

    for w in sorted_words:
        color = w.get("highlight_color")
        if color in color_to_kind:
            if current_color is None:
                current_color = color
                current_words = [w]
                pending_gap = []
            elif color == current_color and pending_gap == []:
                current_words.append(w)
            else:
                raw_runs.append({"color": current_color, "words": current_words})
                gaps_after_run.append(pending_gap)
                current_color = color
                current_words = [w]
                pending_gap = []
        else:
            if current_color is not None:
                pending_gap.append(w)

    if current_words:
        raw_runs.append({"color": current_color, "words": current_words})
        gaps_after_run.append(pending_gap)

    # Merge raw_runs[i] with raw_runs[i+1] when same color and bridgeable gap.
    merged_runs = []
    i = 0
    while i < len(raw_runs):
        color = raw_runs[i]["color"]
        words = list(raw_runs[i]["words"])
        j = i
        while (j + 1 < len(raw_runs)
               and raw_runs[j + 1]["color"] == color
               and 0 < len(gaps_after_run[j]) <= bridge_gap):
            words.extend(gaps_after_run[j])
            words.extend(raw_runs[j + 1]["words"])
            j += 1
        merged_runs.append({"color": color, "words": words})
        i = j + 1

    spans = []
    for run in merged_runs:
        first_idx = position_by_id[id(run["words"][0])]
        last_idx = position_by_id[id(run["words"][-1])]
        spans.append(ChangeSpan(
            kind=color_to_kind[run["color"]],
            page=run["words"][0]["page_num"] + 1,
            words=run["words"],
            context_before=_plain_text_context(sorted_words, first_idx, -1, CONTEXT_WORDS),
            context_after=_plain_text_context(sorted_words, last_idx, 1, CONTEXT_WORDS),
        ))
    return spans


# ---------------------------------------------------------------------------
# Step 2: Pair removed+added spans into "replaced", build the JSON payload
# ---------------------------------------------------------------------------

def _pair_replacements(removed_spans, added_spans):
    """
    Pairs a 'removed' span with the 'added' span that is its best same-page
    boundary match (the unchanged text immediately before and/or after a
    true in-place replacement is identical in both documents, so the
    context strings should agree on at least one side). Picks the
    highest-scoring available match per removed span rather than the first
    candidate, to stay correct when several same-page replacements share
    similar surrounding text.
    """
    used_added = set()
    replacements = []
    leftover_removed = []

    def match_score(removed, added):
        r_before = (removed.context_before or "").split()
        a_before = (added.context_before or "").split()
        r_after = (removed.context_after or "").split()
        a_after = (added.context_after or "").split()
        score = 0
        for n in (3, 2, 1):
            if r_before[-n:] and r_before[-n:] == a_before[-n:]:
                score = max(score, n)
                break
        for n in (3, 2, 1):
            if r_after[:n] and r_after[:n] == a_after[:n]:
                score = max(score, n)
                break
        return score

    for r in removed_spans:
        best_idx, best_score = None, 0
        for idx, a in enumerate(added_spans):
            if idx in used_added or a.page != r.page:
                continue
            score = match_score(r, a)
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx is not None:
            used_added.add(best_idx)
            replacements.append((r, added_spans[best_idx]))
        else:
            leftover_removed.append(r)

    leftover_added = [a for i, a in enumerate(added_spans) if i not in used_added]
    return replacements, leftover_removed, leftover_added


def build_diff_payload(words1, words2,
                        document_a_name="Document A",
                        document_b_name="Document B",
                        case_insensitive=None,
                        ignore_quotes=None,
                        ignore_ligatures=None):
    """
    Builds the JSON-serializable payload contract described in
    AGENT_INSTRUCTIONS.md from the two tagged word lists app.py already has
    in memory inside the /api/compare handler (words1, words2 -- the same
    variables passed into apply_annotations_to_pdf_pages()).
    """
    removed_spans = [s for s in extract_change_spans(words1) if s.kind == "removed"]
    added_spans = [s for s in extract_change_spans(words2) if s.kind == "added"]

    replacements, leftover_removed, leftover_added = _pair_replacements(removed_spans, added_spans)

    changes = []
    for r, a in replacements:
        changes.append({
            "type": "replaced",
            "page": r.page,
            "old_text": r.text,
            "new_text": a.text,
            "context_before": r.context_before,
            "context_after": a.context_after,
        })
    for r in leftover_removed:
        changes.append({
            "type": "removed",
            "page": r.page,
            "text": r.text,
            "context_before": r.context_before,
            "context_after": r.context_after,
        })
    for a in leftover_added:
        changes.append({
            "type": "added",
            "page": a.page,
            "text": a.text,
            "context_before": a.context_before,
            "context_after": a.context_after,
        })

    changes.sort(key=lambda c: c["page"])

    truncated = len(changes) > MAX_SPANS_TO_MODEL
    if truncated:
        changes = changes[:MAX_SPANS_TO_MODEL]

    added_words = sum(len(s.words) for s in added_spans)
    removed_words = sum(len(s.words) for s in removed_spans)

    return {
        "document_a_name": document_a_name,
        "document_b_name": document_b_name,
        "stats": {
            "total_words_a": len(words1),
            "total_words_b": len(words2),
            "added_words": added_words,
            "removed_words": removed_words,
            "change_spans_included": len(changes),
            "change_spans_truncated": truncated,
        },
        "normalization": {
            "case_insensitive": bool(case_insensitive) if case_insensitive is not None else None,
            "ignore_quotes": bool(ignore_quotes) if ignore_quotes is not None else None,
            "ignore_ligatures": bool(ignore_ligatures) if ignore_ligatures is not None else None,
        },
        "changes": changes,
    }


# ---------------------------------------------------------------------------
# Step 3: Call Groq
# ---------------------------------------------------------------------------

class GroqSummarizerError(Exception):
    pass


def _load_system_prompt(instructions_path=INSTRUCTIONS_PATH):
    path = Path(instructions_path)
    if not path.exists():
        raise GroqSummarizerError(
            f"Agent instructions file not found at: {instructions_path}\n"
            "Make sure AGENT_INSTRUCTIONS.md sits next to groq_summary.py."
        )
    return path.read_text(encoding="utf-8")


def summarize_with_groq(payload: dict,
                         api_key: Optional[str] = None,
                         model: str = DEFAULT_MODEL,
                         instructions_path: str = INSTRUCTIONS_PATH,
                         temperature: float = 0.2,
                         max_tokens: int = 2000) -> str:
    """
    Sends the diff payload to Groq with the system prompt loaded from
    AGENT_INSTRUCTIONS.md. Raises GroqSummarizerError on any failure
    (missing key, missing package, API error, empty response) so the Flask
    route can turn it into a clean JSON error instead of a 500 traceback.
    """
    if not GROQ_AVAILABLE:
        raise GroqSummarizerError(
            "The 'groq' package is not installed. Run: pip install groq"
        )

    resolved_key = api_key or os.environ.get("GROQ_API_KEY")
    if not resolved_key:
        raise GroqSummarizerError(
            "No Groq API key found. Set the GROQ_API_KEY environment variable."
        )

    if not payload.get("changes"):
        normalized = any(v for v in payload.get("normalization", {}).values())
        return ("## Overview\n\nNo content differences were detected between the two documents"
                + (" (after normalization)." if normalized else "."))

    system_prompt = _load_system_prompt(instructions_path)
    user_content = (
        "Here is the structured diff payload for the two documents currently "
        "being compared. Produce the summary exactly as specified in your "
        "instructions.\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    )

    client = Groq(api_key=resolved_key)
    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as e:
        raise GroqSummarizerError(f"Groq API request failed: {e}") from e

    if not completion.choices:
        raise GroqSummarizerError("Groq API returned no choices in the response.")

    summary = completion.choices[0].message.content
    if not summary or not summary.strip():
        raise GroqSummarizerError("Groq API returned an empty summary.")

    return summary.strip()


# ---------------------------------------------------------------------------
# Convenience one-shot entry point used by the Flask route
# ---------------------------------------------------------------------------

def generate_change_summary(words1, words2,
                             document_a_name="Document A",
                             document_b_name="Document B",
                             case_insensitive=None,
                             ignore_quotes=None,
                             ignore_ligatures=None,
                             api_key=None,
                             model=DEFAULT_MODEL) -> str:
    """
    Single call doing extraction + payload building + Groq call. This is
    the function app.py should call from inside the /api/compare (or a
    dedicated /api/summarize) route handler.
    """
    payload = build_diff_payload(
        words1, words2,
        document_a_name=document_a_name,
        document_b_name=document_b_name,
        case_insensitive=case_insensitive,
        ignore_quotes=ignore_quotes,
        ignore_ligatures=ignore_ligatures,
    )
    return summarize_with_groq(payload, api_key=api_key, model=model)


# ---------------------------------------------------------------------------
# INTEGRATION -- apply these changes to app.py
# ---------------------------------------------------------------------------
#
# Two integration options are shown. Option B (separate endpoint) is
# recommended: it lets the frontend render the highlighted PDFs
# immediately (fast, local) and fetch the AI summary afterward in a second
# request (slower, network-bound), instead of making every comparison wait
# on Groq even when the user doesn't care about the summary.
#
# ---------------------------------------------------------------------------
# 0) requirements.txt -- add this line:
#
#       groq
#
#    And set the API key before running, e.g. (Windows, current shell):
#       set GROQ_API_KEY=your-key-here
#    or create a .env file and load it at the top of app.py with
#    python-dotenv (add "python-dotenv" to requirements.txt too):
#       from dotenv import load_dotenv
#       load_dotenv()
#
# ---------------------------------------------------------------------------
# OPTION A -- inline in the existing /api/compare response
# ---------------------------------------------------------------------------
#
# 1) Add near the top of app.py, with the other imports:
#
#       from groq_summary import generate_change_summary, GroqSummarizerError
#
# 2) Inside compare(), right after this existing line:
#
#       words1, words2 = align_words_with_difflib(words1, words2, case_insensitive, ignore_quotes)
#
#    add:
#
#       ai_summary = None
#       ai_summary_error = None
#       try:
#           ai_summary = generate_change_summary(
#               words1, words2,
#               document_a_name=orig_file.filename,
#               document_b_name=mod_file.filename,
#               case_insensitive=case_insensitive,
#               ignore_quotes=ignore_quotes,
#               ignore_ligatures=ignore_ligatures,
#           )
#       except GroqSummarizerError as e:
#           ai_summary_error = str(e)
#
# 3) Add these two keys to the existing return jsonify({...}) payload:
#
#       "ai_summary": ai_summary,
#       "ai_summary_error": ai_summary_error,
#
# This is the simplest change (one route, one response) but means every
# comparison waits for the Groq round-trip before the PDFs render.
#
# ---------------------------------------------------------------------------
# OPTION B (recommended) -- separate /api/summarize endpoint
# ---------------------------------------------------------------------------
#
# 1) Same import as Option A step 1.
#
# 2) Inside compare(), keep words1/words2 around for reuse instead of only
#    using them locally. The simplest approach without adding real server
#    -side session/cache state is to recompute them in the new route from
#    the same two uploaded files, OR (better) stash them in a short-lived
#    in-memory cache keyed by a comparison id returned from /api/compare.
#    Minimal version using an in-memory cache:
#
#       # near the top of app.py, with other globals
#       _comparison_cache = {}   # comparison_id -> (words1, words2, names)
#
#    Inside compare(), right after words1, words2 = align_words_with_difflib(...):
#
#       comparison_id = uuid.uuid4().hex
#       _comparison_cache[comparison_id] = {
#           "words1": words1,
#           "words2": words2,
#           "name_a": orig_file.filename,
#           "name_b": mod_file.filename,
#           "case_insensitive": case_insensitive,
#           "ignore_quotes": ignore_quotes,
#           "ignore_ligatures": ignore_ligatures,
#       }
#
#    Add "comparison_id": comparison_id to the existing jsonify({...}) response.
#
# 3) Add a new route, anywhere after the existing /api/compare route:
#
#       @app.route('/api/summarize', methods=['POST'])
#       def summarize():
#           try:
#               comparison_id = request.json.get('comparison_id')
#               cached = _comparison_cache.get(comparison_id)
#               if not cached:
#                   return jsonify({"error": "Unknown or expired comparison_id. "
#                                             "Run /api/compare again."}), 400
#               summary = generate_change_summary(
#                   cached["words1"], cached["words2"],
#                   document_a_name=cached["name_a"],
#                   document_b_name=cached["name_b"],
#                   case_insensitive=cached["case_insensitive"],
#                   ignore_quotes=cached["ignore_quotes"],
#                   ignore_ligatures=cached["ignore_ligatures"],
#               )
#               return jsonify({"ai_summary": summary})
#           except GroqSummarizerError as e:
#               return jsonify({"error": str(e)}), 502
#           except Exception as e:
#               return jsonify({"error": str(e)}), 500
#
#    Note: an in-memory dict is fine for a single-process dev server
#    (Flask's `debug=True` / `flask run`) but will not survive a worker
#    restart or work correctly behind multiple gunicorn workers. For
#    production, swap _comparison_cache for Redis, a database row, or
#    simply re-pass words1/words2 from the frontend instead of caching
#    server-side (the frontend already receives `changes`, so the
#    documents' filenames + a re-upload-free summarize call is the
#    simplest robust option if you don't want server-side session state).
#
# 4) Frontend (templates/index.html or its linked script.js) -- after the
#    existing fetch('/api/compare') call succeeds and you have its JSON
#    response `data`:
#
#       const summaryDiv = document.getElementById('ai-summary');
#       summaryDiv.textContent = "Generating AI summary...";
#       fetch('/api/summarize', {
#           method: 'POST',
#           headers: { 'Content-Type': 'application/json' },
#           body: JSON.stringify({ comparison_id: data.comparison_id })
#       })
#       .then(r => r.json())
#       .then(result => {
#           summaryDiv.innerHTML = result.ai_summary
#               ? marked.parse(result.ai_summary)   // optional: render markdown
#               : ('Could not generate summary: ' + (result.error || 'unknown error'));
#       })
#       .catch(err => { summaryDiv.textContent = 'Error: ' + err; });
#
#    Add a container for it in templates/index.html, e.g.:
#       <div id="ai-summary" class="ai-summary-panel"></div>
#
#    (Optional) include the `marked` library via CDN to render the
#    markdown headings/bullets as HTML instead of showing raw markdown text:
#       <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
#
# Either option reuses words1/words2 that app.py already computed for
# rendering the highlighted PDFs -- no re-diffing, no extra PDF parsing,
# and the summary always reflects exactly the same
# case_insensitive/ignore_quotes/ignore_ligatures settings the user
# selected for that comparison.
