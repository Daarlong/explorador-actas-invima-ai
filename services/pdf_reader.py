from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path


LOGGER = logging.getLogger(__name__)
NATIVE_TEXT_EXTRACTOR_VERSION = "pymupdf-text-v1"
OCR_TEXT_EXTRACTOR_VERSION = "tesseract-ocr-v1"
_PLAUSIBLE_PUNCTUATION = frozenset(".,;:!?¿¡%()[]{}-_/+°º'\"=&@#$*<>|")


def estimate_text_quality(text: str) -> float:
    """Estima legibilidad técnica sin intentar juzgar el contenido.

    La métrica es determinística y combina un 80 % de caracteres plausibles
    (alfanuméricos o puntuación habitual) con un 20 % de longitud útil hasta
    120 caracteres. Penaliza explícitamente el carácter de reemplazo Unicode,
    frecuente cuando la codificación del PDF está dañada. No pretende sustituir
    una confianza de OCR provista por el motor.
    """

    useful = [character for character in str(text) if not character.isspace()]
    if not useful:
        return 0.0
    plausible = sum(
        character.isalnum() or character in _PLAUSIBLE_PUNCTUATION
        for character in useful
    )
    plausible_ratio = plausible / len(useful)
    length_factor = min(1.0, len(useful) / 120)
    replacement_ratio = useful.count("�") / len(useful)
    replacement_penalty = max(0.0, 1.0 - min(1.0, replacement_ratio * 5))
    score = (0.8 * plausible_ratio + 0.2 * length_factor) * replacement_penalty
    return round(max(0.0, min(1.0, score)), 3)


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _ocr_page(
    page,
    page_number: int,
    working_directory: Path,
    *,
    languages: str,
    dpi: int,
    timeout_seconds: int,
) -> str:
    """Renderiza una página y la procesa con Tesseract sin usar shell."""
    with tempfile.TemporaryDirectory(
        prefix=f"acta-ocr-{page_number}-",
        dir=working_directory,
    ) as temporary_directory:
        image_path = Path(temporary_directory) / "page.png"
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        pixmap.save(str(image_path))
        process = subprocess.run(
            [
                "tesseract",
                str(image_path),
                "stdout",
                "-l",
                languages,
                "--dpi",
                str(dpi),
                "--psm",
                "3",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
        if process.returncode:
            detail = " ".join(process.stderr.split())[:300]
            raise RuntimeError(
                f"Tesseract terminó con código {process.returncode}: {detail}"
            )
        return process.stdout.strip()


def extract_pdf_pages(
    pdf_path: Path,
    *,
    ocr_enabled: bool = False,
    ocr_languages: str = "spa+eng",
    ocr_dpi: int = 180,
    ocr_timeout_seconds: int = 120,
    ocr_min_chars: int = 20,
) -> tuple[list[dict], list[int]]:
    """Devuelve el inventario completo de páginas físicas del PDF.

    Cada página produce una fila, incluso cuando no contiene texto o falla el
    OCR. En esos casos ``text`` es ``None`` y ``extraction_error`` explica por
    qué la página no es consultable. ``possible_scans`` se conserva por
    compatibilidad y enumera precisamente esas páginas sin texto recuperado.
    """
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError("PyMuPDF no está instalado") from exc

    pages: list[dict] = []
    possible_scans: list[int] = []
    can_use_ocr = ocr_enabled and tesseract_available()

    with pymupdf.open(pdf_path) as document:
        for page_number, page in enumerate(document, start=1):
            errors: list[str] = []
            try:
                text = page.get_text("text").strip()
            except Exception as exc:  # PyMuPDF expone errores heterogéneos
                text = ""
                errors.append(
                    "Extracción nativa falló: "
                    f"{type(exc).__name__}: {str(exc).strip()[:500]}"
                )
            ocr_used = False
            if not text and can_use_ocr:
                try:
                    ocr_text = _ocr_page(
                        page,
                        page_number,
                        pdf_path.parent,
                        languages=ocr_languages,
                        dpi=ocr_dpi,
                        timeout_seconds=ocr_timeout_seconds,
                    )
                    if len(ocr_text) >= ocr_min_chars:
                        text = ocr_text
                        ocr_used = True
                    else:
                        errors.append(
                            "OCR produjo texto insuficiente "
                            f"({len(ocr_text)} caracteres; mínimo {ocr_min_chars})"
                        )
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    detail = f"{type(exc).__name__}: {str(exc).strip()[:500]}"
                    errors.append(f"OCR falló: {detail}")
                    LOGGER.warning(
                        "OCR falló en %s, página %s: %s",
                        pdf_path.name,
                        page_number,
                        exc,
                    )
            elif not text and ocr_enabled:
                errors.append("OCR no disponible: Tesseract no está instalado")
            elif not text:
                errors.append("Página sin texto nativo; OCR no ejecutado")
            if text:
                text_source = "ocr" if ocr_used else "native_pdf"
                pages.append(
                    {
                        "page": page_number,
                        "text": text,
                        "ocr_used": ocr_used,
                        "text_source": text_source,
                        "text_quality": estimate_text_quality(text),
                        "text_extractor_version": (
                            OCR_TEXT_EXTRACTOR_VERSION
                            if ocr_used
                            else NATIVE_TEXT_EXTRACTOR_VERSION
                        ),
                        "extraction_error": "; ".join(errors) or None,
                    }
                )
            else:
                possible_scans.append(page_number)
                extractor_version = NATIVE_TEXT_EXTRACTOR_VERSION
                if ocr_enabled:
                    extractor_version += f"+{OCR_TEXT_EXTRACTOR_VERSION}"
                pages.append(
                    {
                        "page": page_number,
                        "text": None,
                        "ocr_used": False,
                        "text_source": None,
                        "text_quality": 0.0,
                        "extraction_error": "; ".join(errors),
                        "text_extractor_version": extractor_version,
                    }
                )

    return pages, possible_scans
