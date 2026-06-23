# DocuDiff — AI-Powered Document Diff

A Flask web app that performs word-level diff annotation on PDF and Word documents, with an Azure OpenAI–powered change summary.

## Features

- Side-by-side PDF viewer with red/green word-level highlights (deleted / inserted)
- Sync scroll and sync zoom across both panels
- Change navigation (Prev / Next buttons)
- Optional yellow "Highlight Scanned Text" overlay for OCR coverage verification
- Header/footer auto-detection and exclusion from diff
- AI Summary modal powered by Azure OpenAI (via LangChain)
- Supports PDF, DOCX, DOC, RTF, TXT input (Word formats require Windows + pywin32)

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```env
AZURE_OPENAI_API_KEY=<your key>
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
AZURE_OPENAI_API_VERSION=2024-12-01-preview
AZURE_OPENAI_DEPLOYMENT=gpt-4.1-mini
```

## Running

```bash
python app.py
```

Open `http://localhost:5000` in your browser.

## Project Structure

```
app.py                  — Flask server, PDF extraction, diff annotation, routes
azure_summary.py        — Azure OpenAI LangChain chain for AI change summaries
langchain_pipeline.py   — Document loading and clause segmentation utilities
AGENT_INSTRUCTIONS.md   — System prompt for the AI summarizer (hot-reloaded)
templates/index.html    — Single-page UI (dark theme, inline JS/CSS)
requirements.txt        — Python dependencies
temp_pdfs/              — Temporary working directory (auto-created, gitignored)
```

## Notes

- The `.env` file is gitignored — never commit it.
- `temp_pdfs/` is gitignored — annotated PDFs are written here during processing.
- Word-to-PDF conversion (`convert_word_to_pdf_no_markup`) requires Windows and pywin32.
- The AI Summary feature requires valid Azure OpenAI credentials in `.env`.
- `AGENT_INSTRUCTIONS.md` is loaded on every summary request — edit it to adjust
  AI tone or domain rules without restarting the server.
