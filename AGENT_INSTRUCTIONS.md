# Agent Instructions

These instructions are loaded automatically by `langchain_pipeline.py` to control the TONE and STYLE of the summary.

## CRITICAL RULE 1 — Single Section Output

Output ONLY the `##Clause-by-Clause Changes` section. Do NOT output an Overview, Impact Assessment, preamble, introduction, or conclusion of any kind.

## CRITICAL RULE 2 — NO MARKDOWN BOLDING

Do NOT use asterisks (`**`) or underscores (`__`) anywhere in the summary text. The system UI does not support markdown rendering. Output plain, clean text only.

## CRITICAL RULE 3 — Bullet Points, Clause No. First

Under the `## Clause-by-Clause Changes` section, you must list EVERY single clause from the provided JSON.

- Use a bullet point (`-`) for every clause. One bullet per clause.
- Each bullet MUST start with the clause's number/identifier exactly as given in the source JSON (e.g., "Clause 4.2", "Section 3.1(b)", "Attachment No. 10", "Article 87"), followed by a colon, BEFORE describing what changed. Never describe a change without naming its clause number first.
- After the colon, write a short, direct description (1-2 sentences max) of what changed in that clause.
- Keep sentences as short and direct as humanly possible.
- If multiple things changed in one clause, combine them quickly using commas under the same bullet. (e.g., "- Attachment No. 10: Renamed to Authorised Signatory, and Attachment No. 14 is renamed to Bank Account Details.")

## Domain Context

These documents are tender/bid packages (ITB sections).
Always quote exactly: "Attachment No. 10", "Appendix G", "Article 87 of Law 49/2016".
Never invent numbers, dates, or clause identifiers.

Make sure that you specify clause no of the changes instead of page no.
