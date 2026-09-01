from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from services.manifest import is_allowed_url


@dataclass(frozen=True)
class DownloadedPdf:
    path: Path
    resolved_url: str


class _PdfLinkParser(HTMLParser):
    """Extrae enlaces candidatos de las fichas de la Biblioteca de INVIMA."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self.links.append(str(attributes["href"]))
        if tag in {"iframe", "embed"} and attributes.get("src"):
            self.links.append(str(attributes["src"]))


def safe_filename(title: str, url: str) -> str:
    parsed = urlparse(url)
    original = Path(parsed.path).name
    if original.lower().endswith(".pdf"):
        base = original
    else:
        clean_title = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_")
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        base = f"{clean_title or 'documento'}_{digest}.pdf"
    return base[:180]


def _read_url(
    url: str,
    max_bytes: int,
    timeout: int,
    allowed_hosts: tuple[str, ...],
) -> tuple[bytes, str, str]:
    for attempt in range(3):
        request = Request(
            url,
            headers={
                "User-Agent": "Explorador-Actas-INVIMA/1.1",
                "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.1",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                resolved_url = (
                    str(response.geturl())
                    if hasattr(response, "geturl")
                    else url
                )
                if not is_allowed_url(resolved_url, allowed_hosts):
                    raise ValueError(
                        f"La descarga redirigió a un dominio no permitido: {resolved_url}"
                    )
                announced_size = response.headers.get("Content-Length")
                if announced_size and int(announced_size) > max_bytes:
                    raise ValueError("La respuesta supera el tamaño máximo permitido")
                content_type = str(response.headers.get("Content-Type", "")).lower()
                payload = response.read(max_bytes + 1)
            break
        except (HTTPError, URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2**attempt)

    if len(payload) > max_bytes:
        raise ValueError("La respuesta supera el tamaño máximo permitido")
    return payload, content_type, resolved_url


def _pdf_links_from_html(
    payload: bytes,
    page_url: str,
    allowed_hosts: tuple[str, ...],
) -> list[str]:
    parser = _PdfLinkParser()
    parser.feed(payload.decode("utf-8", errors="ignore"))

    candidates: list[str] = []
    for raw_link in parser.links:
        candidate = urljoin(page_url, raw_link)
        path = urlparse(candidate).path.lower()
        if not (
            "/biblioteca/download/" in path
            or "/biblioteca/preview/" in path
            or path.endswith(".pdf")
        ):
            continue
        if is_allowed_url(candidate, allowed_hosts) and candidate not in candidates:
            candidates.append(candidate)

    candidates.sort(
        key=lambda value: (
            0 if "/biblioteca/download/" in urlparse(value).path.lower() else 1,
            0 if urlparse(value).path.lower().endswith(".pdf") else 1,
        )
    )
    return candidates


def download_pdf_resource(
    title: str,
    url: str,
    target_dir: Path,
    allowed_hosts: tuple[str, ...],
    max_bytes: int,
    timeout: int = 60,
) -> DownloadedPdf:
    if not is_allowed_url(url, allowed_hosts):
        raise ValueError(f"Dominio de documento no permitido: {url}")

    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_filename(title, url)
    source_path = target_path.with_suffix(f"{target_path.suffix}.url")
    if target_path.exists() and target_path.stat().st_size > 4:
        with target_path.open("rb") as handle:
            if handle.read(4) == b"%PDF":
                resolved_url = (
                    source_path.read_text(encoding="utf-8").strip()
                    if source_path.exists()
                    else url
                )
                return DownloadedPdf(target_path, resolved_url)

    payload, content_type, resolved_url = _read_url(
        url,
        max_bytes,
        timeout,
        allowed_hosts,
    )

    if not payload.startswith(b"%PDF"):
        looks_like_html = "text/html" in content_type or b"<html" in payload[:2048].lower()
        if not looks_like_html:
            raise ValueError("La respuesta no contiene un PDF ni una ficha HTML válida")

        candidates = _pdf_links_from_html(payload, url, allowed_hosts)
        if not candidates:
            raise ValueError("La ficha del documento no contiene un enlace PDF permitido")

        last_error: Exception | None = None
        for candidate in candidates[:5]:
            try:
                candidate_payload, _, candidate_url = _read_url(
                    candidate,
                    max_bytes,
                    timeout,
                    allowed_hosts,
                )
                if candidate_payload.startswith(b"%PDF"):
                    payload = candidate_payload
                    resolved_url = candidate_url
                    break
            except Exception as exc:  # prueba el siguiente enlace de la ficha
                last_error = exc
        else:
            detail = f": {last_error}" if last_error else ""
            raise ValueError(f"No fue posible descargar el PDF enlazado{detail}")

    if len(payload) > max_bytes:
        raise ValueError("El PDF supera el tamaño máximo permitido")

    temporary_path = target_path.with_suffix(".pdf.part")
    temporary_path.write_bytes(payload)
    temporary_path.replace(target_path)
    source_path.write_text(resolved_url, encoding="utf-8")
    return DownloadedPdf(target_path, resolved_url)


def download_pdf(
    title: str,
    url: str,
    target_dir: Path,
    allowed_hosts: tuple[str, ...],
    max_bytes: int,
    timeout: int = 60,
) -> Path:
    return download_pdf_resource(
        title,
        url,
        target_dir,
        allowed_hosts,
        max_bytes,
        timeout,
    ).path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
