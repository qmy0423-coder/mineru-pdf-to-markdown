"""Lossless review mapping for MinerU's pipeline (CPU or GPU) output.

Use the installed official formatter, preserving each pipeline paragraph as
one editable block. Do not invent the VLM-specific continuation metadata.
"""
from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def load_pipeline_structure(folder: Path, middle: dict, source_bytes: bytes) -> dict:
    from mineru.backend.pipeline.pipeline_middle_json_mkcontent import (
        make_blocks_to_markdown, merge_para_with_text,
    )
    from mineru.utils.enum_class import MakeMode
    from extract_blocks import _normalize_bbox, _leaves, REFERENCE_TYPES

    document_name = next(folder.glob("*_middle.json")).name.removesuffix("_middle.json")
    source = source_bytes.decode("utf-8")
    blocks, pages = [], []
    for page in middle["pdf_info"]:
        idx, size = int(page["page_idx"]), page["page_size"]
        info = {"index": idx, "width": size[0], "height": size[1],
                "boxes": [], "referenceBlocks": []}
        pages.append(info)
        for parent in page.get("para_blocks") or []:
            rendered = make_blocks_to_markdown([copy.deepcopy(parent)], MakeMode.MM_MD, "images")
            if not rendered:
                continue
            if len(rendered) != 1:
                raise ValueError("Pipeline formatter returned unexpected block boundaries")
            block_id = f"block-{len(blocks) + 1:04d}"
            bbox = _normalize_bbox(parent["bbox"], size)
            blocks.append({"id": block_id, "page": idx, "type": parent["type"],
                           "bbox": bbox, "markdown": rendered[0],
                           "originalMarkdown": rendered[0], "separatorAfter": ""})
            info["boxes"].append({"id": block_id, "bbox": bbox,
                                  "type": parent["type"], "editable": True})
        for parent in page.get("discarded_blocks") or []:
            kind = REFERENCE_TYPES.get(parent["type"], parent["type"])
            bbox = _normalize_bbox(parent["bbox"], size)
            text = "\n\n".join(merge_para_with_text(copy.deepcopy(leaf))
                              for leaf in _leaves(parent) if leaf.get("lines"))
            ref_id = f"reference-{idx:04d}-{len(info['referenceBlocks']) + 1:04d}"
            info["referenceBlocks"].append({"id": ref_id, "page": idx, "type": kind,
                                            "bbox": bbox, "markdown": text,
                                            "export": False, "editable": False})
            info["boxes"].append({"id": ref_id, "bbox": bbox, "type": kind, "editable": False})
    for block in blocks[:-1]:
        block["separatorAfter"] = "\n\n"
    rebuilt = "".join(b["markdown"] + b["separatorAfter"] for b in blocks)
    if rebuilt != source:
        raise ValueError("Pipeline formatter differs from the original Markdown. Use the parsing environment/version; no output saved.")
    digest = hashlib.sha256(source_bytes).hexdigest()
    return {"metadata": {
        "schemaVersion": 1, "title": next((re.sub(r"^#+\s*", "", b["markdown"])
            for b in blocks if b["type"] == "title"), document_name),
        "documentName": document_name, "sourcePath": str(folder / f"{document_name}_origin.pdf"),
        "sourceMarkdownPath": str(folder / f"{document_name}.md"),
        "sourceMarkdownHash": digest,
        "documentId": hashlib.sha256((document_name + "\0" + digest).encode()).hexdigest(),
        "pageCount": len(pages), "createdAt": datetime.now(timezone.utc).isoformat(), "savedAt": 0,
        "coordinateSystem": "normalized-1000", "stats": {
            "editableBlocks": len(blocks), "markdownCharacters": len(source),
            "sourceMarkdownExactMatch": True, "blockTypes": dict(Counter(b["type"] for b in blocks)),
            "referenceBlocks": sum(len(p["referenceBlocks"]) for p in pages),
            "mappingBackend": "pipeline", "continuations": {},
        }}, "blocks": blocks, "pages": pages, "continuations": []}
