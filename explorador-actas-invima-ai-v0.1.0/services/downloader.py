from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from services.manifest import is_allowed_url


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


def download_pdf(
    title: str,
    url: str,
    target_dir: Path,
    allowed_hosts: tuple[str, ...],
    max_bytes: int,
    timeout: int = 60,
) -> Path:
    if not is_allowed_url(url, allowed_hosts):
        raise ValueError(f"Dominio de documento no permitido: {url}")

    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_filename(title, url)
    if target_path.exists() and target_path.stat().st_size > 4:
        with target_path.open("rb") as handle:
            if handle.read(4) == b"%PDF":
                return target_path

    request = Request(
        url,
        headers={"User-Agent": "Explorador-Actas-INVIMA/1.0"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        announced_size = response.headers.get("Content-Length")
        if announced_size and int(announced_size) > max_bytes:
            raise ValueError("El PDF supera el tamaño máximo permitido")

        payload = response.read(max_bytes + 1)

    if len(payload) > max_bytes:
        raise ValueError("El PDF supera el tamaño máximo permitido")
    if not payload.startswith(b"%PDF"):
        raise ValueError("La respuesta no contiene un PDF válido")

    temporary_path = target_path.with_suffix(".pdf.part")
    temporary_path.write_bytes(payload)
    temporary_path.replace(target_path)
    return target_path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

