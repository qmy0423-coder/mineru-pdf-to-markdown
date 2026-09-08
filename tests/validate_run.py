"""Validate a real parse + HTML + Codex review packet run, without bundled PDFs.

Run in the MinerU Python environment:
python tests/validate_run.py --input <parse-dir> --html <review.html> --packets <packet-dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

SKILL = Path(__file__).resolve().parents[1] / "skills" / "mineru-pdf-to-markdown"
sys.path.insert(0, str(SKILL / "scripts" / "review"))
from build_review import build
from extract_blocks import load_document_structure


def validate(input_dir: Path, html_path: Path, packet_dir: Path) -> dict:
    source_name = next(input_dir.glob("*_middle.json")).name.removesuffix("_middle.json")
    source_bytes = (input_dir / f"{source_name}.md").read_bytes()
    source = source_bytes.decode("utf-8")
    html = html_path.read_text(encoding="utf-8")
    match = re.search(r'<script id="document-data" type="application/json">(.*?)</script>', html, re.S)
    assert match, "HTML has no embedded document data"
    data = json.loads(match[1])
    rebuilt = "".join(block["markdown"] + block["separatorAfter"] for block in data["blocks"])
    assert rebuilt.encode("utf-8") == source_bytes, "Initial HTML must preserve Markdown bytes"
    assert data["sourceMarkdownHash"] == hashlib.sha256(source_bytes).hexdigest()
    assert not data.get("paragraphMerges"), "Unknown documents must not use historical profiles"
    assert len(data["pages"]) == data["pageCount"]
    assert all(page["image"].startswith("data:image/jpeg;base64,") for page in data["pages"])
    assert not re.search(r'<script[^>]+src=|<link[^>]+href=', html), "Viewer must embed runtime assets"
    for ref in re.findall(r'!\[[^\]]*\]\((images/[^)]+)\)', source):
        assert ref in data["assets"], f"Image missing from HTML: {ref}"
    manifest = json.loads((packet_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_sha256"] == data["sourceMarkdownHash"]
    expected_start = 1
    for chunk in manifest["chunks"]:
        assert chunk["line_start"] == expected_start, "Review coverage has a gap or duplicate primary range"
        assert (packet_dir / chunk["file"]).is_file()
        expected_start = chunk["line_end"] + 1
    assert expected_start - 1 == len(source.split("\n"))
    block_records = [json.loads(line) for line in (packet_dir / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(block_records) == len(data["blocks"])
    for record in block_records:
        assert source[record["char_start"]:record["char_end_exclusive"]] == record["markdown"]
        assert record["line_start"] == 1 + source[:record["char_start"]].count("\n")
        assert all(1 <= p <= data["pageCount"] for p in record["pdf_pages_1based"])
    try:
        build(input_dir, input_dir / "must-not-write.html", 1, False)
        raise AssertionError("Output inside source directory was accepted")
    except ValueError:
        assert not (input_dir / "must-not-write.html").exists()
    try:
        build(input_dir, html_path, 1, False)
        raise AssertionError("Existing HTML was overwritten without --force")
    except FileExistsError:
        pass
    with tempfile.TemporaryDirectory(prefix="mineru-negative-test-") as temporary:
        copied = Path(temporary) / "raw"
        shutil.copytree(input_dir, copied)
        md = copied / f"{source_name}.md"
        md.write_bytes(source_bytes + b"\nchanged outside MinerU")
        try:
            load_document_structure(copied)
            raise AssertionError("Changed source Markdown was accepted")
        except ValueError:
            pass
    result = {"pages": data["pageCount"], "blocks": len(data["blocks"]),
              "images": len(data["assets"]), "chunks": len(manifest["chunks"]),
              "roundtrip": "exact", "source_protection": "passed", "packet_coverage": "complete"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--html", required=True, type=Path)
    parser.add_argument("--packets", required=True, type=Path)
    args = parser.parse_args()
    validate(args.input.resolve(), args.html.resolve(), args.packets.resolve())
