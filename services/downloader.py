from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from services.manifest import is_allowed_url


@dataclass(frozen=True)
class DownloadedPdf:
    path: Path
    resolved_url: str


_CACHE_METADATA_VERSION = 1


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


def _normalized_source_url(url: str) -> str:
    """Normaliza solo las partes de una URL que no cambian el recurso."""

    parsed = urlparse(url)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            parsed.query,
            "",
        )
    )


def _legacy_safe_filename(title: str, url: str) -> str:
    """Reproduce el nombre usado hasta la versión 0.7 para migrar la caché."""

    parsed = urlparse(url)
    original = Path(parsed.path).name
    if original.lower().endswith(".pdf"):
        base = original
    else:
        clean_title = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_")
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        base = f"{clean_title or 'documento'}_{digest}.pdf"
    return base[:180]


def safe_filename(title: str, url: str) -> str:
    """Devuelve un nombre estable y único para la URL de origen.

    El título no forma parte de la identidad: puede corregirse en el catálogo sin
    descargar el mismo documento otra vez. El hash también evita que dos URL que
    terminan, por ejemplo, en ``acta.pdf`` compartan accidentalmente la caché.
    """

    del title  # se conserva en la firma por compatibilidad con los llamadores
    normalized_url = _normalized_source_url(url)
    parsed = urlparse(normalized_url)
    original = Path(parsed.path).name
    stem = Path(original).stem if original else "documento"
    clean_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:16]
    suffix = f"_{digest}.pdf"
    prefix = (clean_stem or "documento")[: 180 - len(suffix)]
    return f"{prefix}{suffix}"


def _metadata_path(pdf_path: Path) -> Path:
    return pdf_path.with_name(f"{pdf_path.name}.meta.json")


def _source_path(pdf_path: Path) -> Path:
    return pdf_path.with_suffix(f"{pdf_path.suffix}.url")


def _is_valid_pdf_file(path: Path, max_bytes: int) -> bool:
    try:
        size = path.stat().st_size
        if size <= 4 or size > max_bytes:
            return False
        with path.open("rb") as handle:
            return handle.read(4) == b"%PDF"
    except OSError:
        return False


def _write_cache_sidecars(
    pdf_path: Path,
    source_url: str,
    resolved_url: str,
) -> None:
    metadata = {
        "version": _CACHE_METADATA_VERSION,
        "source_url": source_url,
        "source_identity": _normalized_source_url(source_url),
        "resolved_url": resolved_url,
        "size": pdf_path.stat().st_size,
        "sha256": file_sha256(pdf_path),
    }
    metadata_path = _metadata_path(pdf_path)
    temporary_metadata_path = metadata_path.with_name(f"{metadata_path.name}.part")
    temporary_metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_metadata_path.replace(metadata_path)

    # Se mantiene el sidecar histórico para no romper herramientas auxiliares.
    _source_path(pdf_path).write_text(resolved_url, encoding="utf-8")


def _load_valid_cached_pdf(
    pdf_path: Path,
    source_url: str,
    allowed_hosts: tuple[str, ...],
    max_bytes: int,
) -> DownloadedPdf | None:
    if not _is_valid_pdf_file(pdf_path, max_bytes):
        return None

    try:
        metadata = json.loads(_metadata_path(pdf_path).read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            return None
        resolved_url = str(metadata["resolved_url"])
        if int(metadata.get("version", 0)) != _CACHE_METADATA_VERSION:
            return None
        if metadata.get("source_identity") != _normalized_source_url(source_url):
            return None
        if not is_allowed_url(resolved_url, allowed_hosts):
            return None
        if int(metadata.get("size", -1)) != pdf_path.stat().st_size:
            return None
        if metadata.get("sha256") != file_sha256(pdf_path):
            return None
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return DownloadedPdf(pdf_path, resolved_url)


def _legacy_name_contains_source_identity(path: Path, source_url: str) -> bool:
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
    return digest in path.name


def _migrate_legacy_cache(
    title: str,
    source_url: str,
    target_dir: Path,
    target_path: Path,
    allowed_hosts: tuple[str, ...],
    max_bytes: int,
) -> DownloadedPdf | None:
    legacy_path = target_dir / _legacy_safe_filename(title, source_url)
    if legacy_path == target_path or not _is_valid_pdf_file(legacy_path, max_bytes):
        return None

    legacy_source_path = _source_path(legacy_path)
    resolved_url = source_url
    if legacy_source_path.exists():
        try:
            resolved_url = legacy_source_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    # Los nombres antiguos de URL terminadas en .pdf no incluían identidad. Solo
    # son reutilizables si el sidecar demuestra que pertenecen a esta misma URL.
    identity_is_proven = _legacy_name_contains_source_identity(
        legacy_path,
        source_url,
    ) or _normalized_source_url(resolved_url) == _normalized_source_url(source_url)
    if not identity_is_proven or not is_allowed_url(resolved_url, allowed_hosts):
        return None

    legacy_path.replace(target_path)
    _write_cache_sidecars(target_path, source_url, resolved_url)
    if legacy_source_path != _source_path(target_path):
        legacy_source_path.unlink(missing_ok=True)
    return DownloadedPdf(target_path, resolved_url)


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
    cached = _load_valid_cached_pdf(
        target_path,
        url,
        allowed_hosts,
        max_bytes,
    )
    if cached is not None:
        return cached

    migrated = _migrate_legacy_cache(
        title,
        url,
        target_dir,
        target_path,
        allowed_hosts,
        max_bytes,
    )
    if migrated is not None:
        return migrated

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
    _write_cache_sidecars(target_path, url, resolved_url)
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
