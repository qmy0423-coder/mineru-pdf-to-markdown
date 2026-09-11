"""Build a portable, offline MinerU PDF / editable Markdown review page.

Run with the MinerU environment's Python. Parsing outputs are read-only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import mimetypes
from pathlib import Path
import re
import tarfile
import tempfile
import urllib.request


CODE_DIR = Path(__file__).resolve().parent
PACKAGES = {"markdown-it": "14.1.0", "katex": "0.16.22"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_snapshot(directory: Path) -> dict[str, str]:
    return {
        file.relative_to(directory).as_posix(): sha256(file.read_bytes())
        for file in sorted(directory.rglob("*")) if file.is_file()
    }


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "MinerU-Local-Review/1.0"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def download_vendor() -> None:
    """Fetch pinned official npm packages and validate npm's integrity digest."""
    vendor = CODE_DIR / "vendor"
    for name, version in PACKAGES.items():
        destination = vendor / name
        manifest = destination / "vendor-manifest.json"
        if manifest.exists():
            info = json.loads(manifest.read_text(encoding="utf-8"))
            if info.get("version") == version and all(
                (destination / key).is_file()
                and sha256((destination / key).read_bytes()) == digest
                for key, digest in info["files"].items()
            ):
                print(f"Vendor ready: {name} {version}", flush=True)
                continue
        print(f"Downloading official npm package: {name} {version}", flush=True)
        metadata = json.loads(fetch_bytes(f"https://registry.npmjs.org/{name}/{version}"))
        url = metadata["dist"]["tarball"]
        if not url.startswith(f"https://registry.npmjs.org/{name}/-/"):
            raise ValueError(f"Unexpected npm tarball URL: {url}")
        archive = fetch_bytes(url)
        algorithm, expected = metadata["dist"]["integrity"].split("-", 1)
        actual = base64.b64encode(hashlib.new(algorithm, archive).digest()).decode()
        if actual != expected:
            raise ValueError(f"Integrity mismatch for {name} {version}")
        selected: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                relative = member.name.removeprefix("package/")
                wanted = relative in {"LICENSE", "LICENSE.txt", "dist/markdown-it.min.js",
                                      "dist/katex.min.js", "dist/katex.min.css"}
                wanted |= relative.startswith("dist/fonts/") and relative.endswith(".woff2")
                if not wanted or ".." in Path(relative).parts:
                    continue
                stream = tar.extractfile(member)
                assert stream is not None
                content = stream.read()
                file = destination / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(content)
                selected[relative] = sha256(content)
        manifest.write_text(json.dumps({"package": name, "version": version,
            "source": url, "integrity": metadata["dist"]["integrity"], "files": selected},
            ensure_ascii=False, indent=2), encoding="utf-8")


def vendor_contents() -> dict[str, str]:
    vendor = CODE_DIR / "vendor"
    required = [vendor / "markdown-it/dist/markdown-it.min.js",
                vendor / "katex/dist/katex.min.js", vendor / "katex/dist/katex.min.css"]
    if any(not file.is_file() for file in required):
        raise FileNotFoundError("First run needs --download-vendor. Later builds work offline.")
    for name in PACKAGES:
        base = vendor / name
        manifest = json.loads((base / "vendor-manifest.json").read_text(encoding="utf-8"))
        for relative, digest in manifest["files"].items():
            if sha256((base / relative).read_bytes()) != digest:
                raise ValueError(f"Vendor file was modified: {relative}")
    css = required[2].read_text(encoding="utf-8")

    def embed_font(match: re.Match) -> str:
        source = match.group(0)
        woff2 = re.search(r'url\(([^)]+\.woff2)\)', source)
        if not woff2:
            raise ValueError("KaTeX font has no WOFF2 source")
        relative = woff2.group(1).strip("\"'")
        file = required[2].parent / relative
        data = base64.b64encode(file.read_bytes()).decode("ascii")
        return f'src:url(data:font/woff2;base64,{data}) format("woff2");'

    css = re.sub(r"src:[^;]+;", embed_font, css)
    return {"MARKDOWN_IT_JS": required[0].read_text(encoding="utf-8"),
            "KATEX_JS": required[1].read_text(encoding="utf-8"), "KATEX_CSS": css}


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def render_pages(data: dict, pdf_path: Path, scale: float, cache_root: Path) -> None:
    import pypdfium2 as pdfium

    digest = sha256(pdf_path.read_bytes())
    cache = cache_root / f"{digest[:20]}-s{scale:g}"
    cache.mkdir(parents=True, exist_ok=True)
    document = pdfium.PdfDocument(pdf_path)
    try:
        if len(document) != data["pageCount"]:
            raise ValueError("PDF page count differs from MinerU data")
        for info in data["pages"]:
            idx = info["index"]
            cached = cache / f"page-{idx + 1:04d}.jpg"
            page = document[idx]
            try:
                width, height = page.get_size()
                if abs(width - info["width"]) > 2 or abs(height - info["height"]) > 2:
                    raise ValueError(f"Page {idx + 1} coordinate sizes do not match")
                if not cached.exists():
                    bitmap = page.render(scale=scale)
                    image = bitmap.to_pil()
                    rgb = image.convert("RGB")
                    try:
                        rgb.save(cached, format="JPEG", quality=88, optimize=True)
                    finally:
                        rgb.close()
                        image.close()
                        bitmap.close()
                info["image"] = data_uri(cached.read_bytes(), "image/jpeg")
            finally:
                page.close()
            if (idx + 1) % 10 == 0 or idx + 1 == data["pageCount"]:
                print(f"Original pages prepared: {idx + 1}/{data['pageCount']}", flush=True)
    finally:
        document.close()


def embed_assets(data: dict, input_dir: Path) -> None:
    data["assets"] = {}
    images = input_dir / "images"
    if images.is_dir():
        for file in sorted(images.rglob("*")):
            if file.is_file():
                relative = file.relative_to(input_dir).as_posix()
                mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                if not mime.startswith("image/"):
                    continue
                data["assets"][relative] = data_uri(file.read_bytes(), mime)
    # Reference cards are display-only, but their local image paths still need
    # the same offline asset validation as editable body content.
    markdown = "".join(block["markdown"] + block["separatorAfter"] for block in data["blocks"])
    markdown += "\n" + "\n".join(
        reference["markdown"]
        for page in data["pages"] for reference in page.get("referenceBlocks", [])
    )
    references = re.findall(r'!\[[^\]]*\]\((images/[^)]+)\)', markdown)
    references += re.findall(r'src=[\"\'](images/[^\"\']+)', markdown)
    for reference in references:
        if reference not in data["assets"]:
            raise FileNotFoundError(f"Missing Markdown image: {reference}")
    data["stats"]["embeddedImages"] = len(data["assets"])


def safe_json(value: object) -> str:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def build(input_dir: Path, output: Path, scale: float, replace: bool,
          cache_dir: Path | None = None, profile_dir: Path | None = None,
          auto_continuations: bool = False) -> dict:
    from extract_blocks import load_document_structure
    from reviewed_continuations import apply_reviewed_continuations

    input_dir = input_dir.resolve()
    output = output.resolve()
    if input_dir == output.parent or input_dir in output.parents:
        raise ValueError("Write the review HTML outside the original MinerU output directory")
    if not 1 <= scale <= 4:
        raise ValueError("scale must be between 1 and 4")
    if cache_dir is not None:
        cache_dir = cache_dir.resolve()
        if cache_dir == input_dir or input_dir in cache_dir.parents:
            raise ValueError("Write the page cache outside the original MinerU output directory")
    if output.exists() and not replace:
        raise FileExistsError(f"Output already exists. Use --force to rebuild: {output}")
    before = source_snapshot(input_dir)
    structure = load_document_structure(input_dir)
    structure = apply_reviewed_continuations(
        structure, profile_dir.resolve() if profile_dir is not None else None,
        auto_continuations=auto_continuations,
    )
    data = {**structure["metadata"], "blocks": structure["blocks"], "pages": structure["pages"],
            "continuations": structure.get("continuations", []),
            "continuationCandidates": structure.get("continuationCandidates", []),
            "paragraphMerges": structure.get("paragraphMerges", []),
            "sourceBlockOrder": structure.get("sourceBlockOrder", [])}
    if cache_dir is None:
        with tempfile.TemporaryDirectory(prefix="mineru-review-") as temporary:
            render_pages(data, Path(data["sourcePath"]), scale, Path(temporary))
    else:
        render_pages(data, Path(data["sourcePath"]), scale, cache_dir)
    embed_assets(data, input_dir)
    substitutions = vendor_contents()
    substitutions.update({"DOCUMENT_DATA": safe_json(data),
                          "VIEWER_CSS": (CODE_DIR / "viewer.css").read_text(encoding="utf-8"),
                          "VIEWER_JS": (CODE_DIR / "viewer.js").read_text(encoding="utf-8")})
    template = (CODE_DIR / "viewer_template.html").read_text(encoding="utf-8")
    html = re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: substitutions[match.group(1)], template)
    if before != source_snapshot(input_dir):
        raise RuntimeError("Source files changed during build")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8", newline="\n")
    print(f"Created: {output}", flush=True)
    print(f"HTML bytes: {output.stat().st_size:,}", flush=True)
    print(f"Source files unchanged: {len(before)}; editable blocks: {len(data['blocks'])}", flush=True)
    print(json.dumps(data["stats"], ensure_ascii=False), flush=True)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Directory containing one MinerU *_middle.json and matching .md / _origin.pdf")
    parser.add_argument("--output", type=Path, help="Standalone review HTML destination")
    parser.add_argument("--scale", type=float, default=2.0, help="Original page raster scale (72 dpi units)")
    parser.add_argument("--force", action="store_true", help="Replace an existing generated review HTML")
    parser.add_argument("--cache-dir", type=Path, help="Optional reusable page cache outside the input directory; default is a temporary cache")
    parser.add_argument("--profile-dir", type=Path, help="Explicitly apply reviewed paragraph profiles from this external directory")
    parser.add_argument("--auto-continuations", action="store_true", help="Apply generic heuristic joins when no matching external profile exists; default only records candidates")
    parser.add_argument("--download-vendor", action="store_true", help="Fetch pinned browser libraries once")
    args = parser.parse_args()
    if not 1 <= args.scale <= 4:
        parser.error("--scale must be between 1 and 4")
    if args.download_vendor:
        download_vendor()
    output = args.output or (args.input.parent / "校对.html")
    build(args.input, output, args.scale, args.force, args.cache_dir, args.profile_dir,
          args.auto_continuations)


if __name__ == "__main__":
    main()
