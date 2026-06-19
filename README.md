# Document Comparer

A Python desktop application built with `tkinter` for comparing PDF and Word documents. It visualizes differences between documents, handles clipboard pasting, and even uses `git diff` or `difflib` algorithms to highlight insertions, deletions, and moved text.

## Features

- **Document Comparison:** Visualizes the differences between two PDF documents.
- **Word to PDF Conversion:** Native conversion of Word documents to PDF without markup, leveraging Windows COM (`pywin32`).
- **Clipboard Support:** Paste HTML or plain text from the clipboard and automatically convert it to a PDF for comparison.
- **Diff Algorithms:** Utilizes `difflib` as a fallback or `git diff` (if available) for identifying differences between text fragments.
- **Drag and Drop:** Easily drag and drop PDF files into the application using `tkinterdnd2`.
- **Light/Dark Mode:** Toggle PDF highlights between Multiply and Exclusion blend modes.

## Installation

Ensure you have Python installed. You can set up the environment and install dependencies by running:

```cmd
python -m venv myenv
myenv\Scripts\activate
pip install -r requirements.txt
python myenv\Scripts\pywin32_postinstall.py -install
```

### Dependencies

- `PyMuPDF` (`fitz`): For reading, writing, and highlighting PDFs.
- `klembord`: For fetching rich text (HTML) from the clipboard.
- `Pillow`: For image handling.
- `tkinterdnd2`: For drag-and-drop support.
- `pywin32`: For Word to PDF conversion (requires Microsoft Word on Windows).
- `PyAutoGUI`: For the clickless panning feature.

## Usage

Run the main application script:

```cmd
python app.py
```

> **Note:** The `app.py` file currently contains an incomplete script. The original text provided was truncated. Please update `app.py` with the complete script to run the application successfully.

## Building (Optional)

You can build a standalone executable using `pyinstaller` on Windows:

```cmd
ren app.py app.pyw
pyinstaller --noconfirm app.pyw
```
