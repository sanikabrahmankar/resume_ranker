import io
import logging
import os

from pypdf import PdfReader

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = int(os.getenv("OCR_MIN_CHARS", "100"))  # below this -> treat as scanned
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "5"))
OCR_DPI = int(os.getenv("OCR_DPI", "200"))
POPPLER_PATH = os.getenv("POPPLER_PATH") or None  # Windows: ...\poppler\Library\bin

_WIN_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSERACT_CMD = os.getenv("TESSERACT_CMD") or (
    _WIN_TESSERACT if os.name == "nt" and os.path.exists(_WIN_TESSERACT) else None
)


class PDFExtractionError(Exception):
    """Raised with a message that is safe to show to API clients."""


def _extract_native(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted and not reader.decrypt(""):
            raise PDFExtractionError("PDF is password-protected")
        parts = []
        for page in reader.pages:
            try:
                t = page.extract_text()
            except Exception:
                log.warning("pypdf failed on a page", exc_info=True)
                continue
            if t:
                parts.append(t)
        return " ".join(parts).strip()
    except PDFExtractionError:
        raise
    except Exception as exc:
        raise PDFExtractionError("File is not a readable PDF") from exc


def _extract_ocr(pdf_bytes: bytes) -> str:
    # Imported lazily so the app still boots if OCR deps are missing.
    import pytesseract
    from pdf2image import convert_from_bytes

    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

    images = convert_from_bytes(
        pdf_bytes,
        dpi=OCR_DPI,
        first_page=1,
        last_page=OCR_MAX_PAGES,
        poppler_path=POPPLER_PATH,
    )
    return " ".join(pytesseract.image_to_string(img) for img in images).strip()


def extract_text_from_pdf(pdf_bytes: bytes):
    """Returns (text, method) where method is 'text' or 'ocr'. Operates fully in memory."""
    text = _extract_native(pdf_bytes)
    if len(text) >= MIN_TEXT_CHARS:
        return text, "text"

    try:
        ocr_text = _extract_ocr(pdf_bytes)
    except Exception as exc:
        log.warning("OCR fallback failed: %r", exc)
        if text:
            return text, "text"
        raise PDFExtractionError(
            "No selectable text and OCR is unavailable (check Tesseract/Poppler setup)"
        ) from exc

    if len(ocr_text) > len(text):
        return ocr_text, "ocr"
    if text:
        return text, "text"
    raise PDFExtractionError("No text could be extracted from this PDF")