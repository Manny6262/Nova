"""Extract text from a PDF for use as an attachment. Pure-Python (pypdf)."""

import io
from pypdf import PdfReader

MAX_PDF_BYTES = 20 * 1024 * 1024   # 20 MB
MAX_PAGES = 50                     # cap runaway PDFs
MAX_CHARS = 40000                  # cap text handed to the model


def extract(raw_bytes):
    """Return (text, note). Raises ValueError on bad/oversized input."""
    if len(raw_bytes) > MAX_PDF_BYTES:
        raise ValueError("PDF is larger than 20 MB.")

    reader = PdfReader(io.BytesIO(raw_bytes))

    if reader.is_encrypted:
        # try an empty password (common for "protected but no password" PDFs)
        try:
            reader.decrypt("")
        except Exception:
            raise ValueError("PDF is password-protected.")

    pages = reader.pages[:MAX_PAGES]
    chunks = []
    for i, page in enumerate(pages, 1):
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            chunks.append("")

    text = "\n\n".join(c.strip() for c in chunks if c.strip())

    if not text.strip():
        raise ValueError("No selectable text found (looks scanned/image-only).")

    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS] + "\n… [truncated]"

    note = f"{len(pages)} page(s)"
    if len(reader.pages) > MAX_PAGES:
        note += f" of {len(reader.pages)} (capped)"
    return text, note
