"""Makes documents fit the portals' upload rules (file type and size)."""
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

IMAGE_TYPES = {".jpg", ".jpeg", ".png"}


def _to_image(path):
    if path.suffix.lower() == ".pdf":
        return pdfium.PdfDocument(str(path))[0].render(scale=2).to_pil()
    return Image.open(path)


def prepare_upload(src, out_dir, max_kb=490, allow_pdf=True, pdf_only=False, min_width=300):
    """Return a path that meets the limits: the original file if it already does, else a compressed
    JPEG copy (or, with pdf_only, a compressed single-page PDF)."""
    src = Path(src)
    suffix = src.suffix.lower()
    allowed = {".pdf"} if pdf_only else IMAGE_TYPES | ({".pdf"} if allow_pdf else set())
    if suffix in allowed and src.stat().st_size <= max_kb * 1024:
        return str(src)

    img = _to_image(src).convert("RGB")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}.{'pdf' if pdf_only else 'jpg'}"
    quality = 85
    while True:
        if pdf_only:
            img.save(out, "PDF", quality=quality, resolution=100)
        else:
            img.save(out, "JPEG", quality=quality, optimize=True)
        if out.stat().st_size <= max_kb * 1024:
            return str(out)
        if quality > 50:
            quality -= 10
        else:
            img = img.resize((int(img.width * 0.8), int(img.height * 0.8)))
            if img.width < min_width:
                raise ValueError(f"Could not shrink {src.name} under {max_kb} KB")
