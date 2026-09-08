"""Extract lossless, page-aware editable Markdown blocks from MinerU output.

The original Markdown is the authority. MinerU's own formatter is used only to
identify its block boundaries; the concatenated result must match it exactly.
Bounding boxes use MinerU content-list coordinates (0..1000, top-left origin).
Page numbers and page indices are zero based throughout this module.
"""

from __future__ import annotations

import copy
import base64
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


DISCARDED_TYPES = {"header", "footer", "page_header", "page_footer", "page_number"}
REFERENCE_TYPES = {"page_header": "header", "page_footer": "footer"}


def _leaves(block: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if "lines" in block:
        yield block
    for child in block.get("blocks", []):
        yield from _leaves(child)


def _span_signature(line: dict[str, Any]) -> tuple:
    return (
        tuple(line.get("bbox", [])),
        tuple(
            (span.get("type"), span.get("content"), span.get("image_path"), span.get("html"))
            for span in line.get("spans", [])
        ),
    )


def _leaf_signature(block: dict[str, Any]) -> tuple:
    # cross_page is a boolean marker, not an actual page identifier. It must
    # be ignored when matching a merged child to its original source page.
    return (
        block.get("type"),
        tuple(block.get("bbox", [])),
        block.get("index"),
        tuple(_span_signature(line) for line in block.get("lines", [])),
    )


def _normalize_bbox(bbox: list, page_size: list) -> list[float]:
    width, height = page_size
    return [
        round(float(bbox[0]) / width * 1000, 4),
        round(float(bbox[1]) / height * 1000, 4),
        round(float(bbox[2]) / width * 1000, 4),
        round(float(bbox[3]) / height * 1000, 4),
    ]


def _reference_crop_markdown(source_pdf: Path, page_index: int, bbox: list, page_size: list) -> str:
    """Preserve a discarded graphic as an in-memory source-page crop.

    When MinerU omits a footer graphic, its source rectangle preserves it.
    No source PDF or image file is created or changed by this operation.
    """
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(str(source_pdf)) as document:
        page = document[page_index]
        try:
            width, height = page.get_size()
            source_width, source_height = page_size
            left = max(0.0, float(bbox[0]) / source_width * width)
            top = max(0.0, float(bbox[1]) / source_height * height)
            right = min(width, float(bbox[2]) / source_width * width)
            bottom = min(height, float(bbox[3]) / source_height * height)
            bitmap = page.render(scale=3, crop=(left, height - bottom, width - right, top))
            try:
                pil_image = bitmap.to_pil()
                buffer = io.BytesIO()
                try:
                    pil_image.save(buffer, format="PNG")
                finally:
                    pil_image.close()
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            finally:
                bitmap.close()
        finally:
            page.close()
    return f"![原文件页脚图像](data:image/png;base64,{encoded})"


def _replay_official_paragraph_merges(source_pages: list[dict]) -> tuple[list[dict], bool]:
    """Verify stored text/list merges by replaying MinerU's installed rules.

    A layout hint alone is not a confirmed continuation: the official routine
    may reject it. Only results reproduced by the same routine are eligible for
    continuation metadata. This in-memory replay never changes the stored JSON.
    """
    from mineru.backend.utils.para_block_utils import (
        build_para_blocks_from_preproc,
        cleanup_internal_para_block_metadata,
        merge_para_text_blocks,
    )

    replayed = copy.deepcopy(source_pages)
    build_para_blocks_from_preproc(replayed)
    merge_para_text_blocks(replayed)
    cleanup_internal_para_block_metadata(replayed)
    exact_match = True
    for saved_page, replayed_page in zip(source_pages, replayed):
        saved_blocks = saved_page.get("para_blocks", [])
        replayed_blocks = replayed_page.get("para_blocks", [])
        exact_match = exact_match and saved_blocks == replayed_blocks
        # Table merging and title leveling are later, separate MinerU stages.
        # A duplicated excluded header/footer also cannot establish a text/list
        # continuation, so match relevant roots by their original identity.
        def text_list_map(blocks: list[dict]) -> dict[tuple, dict]:
            return {
                (block["type"], block.get("index"), tuple(block["bbox"])): block
                for block in blocks
                if block.get("type") in {"text", "list"}
            }

        if text_list_map(saved_blocks) != text_list_map(replayed_blocks):
            raise ValueError(
                f"Official text/list merge replay differs from saved page {saved_page['page_idx'] + 1}"
            )
    return replayed, exact_match


def load_document_structure(input_dir: str | Path) -> dict[str, Any]:
    """Return metadata, blocks and page boxes without creating any files.

    Editable Markdown is held in ``blocks``. Joining ``markdown`` followed by
    ``separatorAfter`` for every block reproduces the source file exactly.
    Lists are split into their original child blocks so merged references retain
    their physical page associations. Other paragraphs/tables/images remain
    complete Markdown units. ``pages`` may contain several boxes with the same
    block ID for a paragraph that spans columns, or an image with a caption.
    ``pages[].referenceBlocks`` holds read-only headers, footers and printed
    page numbers. These records have ``export=False`` and never enter ``blocks``.
    ``continuations`` contains only relationships verified against MinerU's own
    paragraph merging rules. Reference-list flow is explicitly distinguished
    from continuation of a single text paragraph.
    """
    folder = Path(input_dir).expanduser().resolve(strict=True)
    middle_paths = sorted(folder.glob("*_middle.json"))
    if len(middle_paths) != 1:
        raise ValueError(f"Expected one *_middle.json in {folder}; found {len(middle_paths)}")
    middle_path = middle_paths[0]
    document_name = middle_path.name.removesuffix("_middle.json")
    markdown_path = folder / f"{document_name}.md"
    source_pdf = folder / f"{document_name}_origin.pdf"
    source_bytes = markdown_path.read_bytes()
    source_markdown = source_bytes.decode("utf-8")
    middle = json.loads(middle_path.read_text(encoding="utf-8"))
    source_pages = middle["pdf_info"]
    if middle.get("_backend") == "pipeline":
        from pipeline_adapter import load_pipeline_structure
        return load_pipeline_structure(folder, middle, source_bytes)
    if middle.get("_backend") not in {"vlm", "hybrid"}:
        raise ValueError(f"Unsupported MinerU backend: {middle.get('_backend')!r}")
    from mineru.backend.vlm.vlm_middle_json_mkcontent import (
        merge_para_with_text,
        mk_blocks_to_markdown,
    )
    from mineru.utils.enum_class import MakeMode

    replayed_pages, official_replay_exact_match = _replay_official_paragraph_merges(source_pages)

    pages = [
        {
            "index": int(page["page_idx"]),
            "width": page["page_size"][0],
            "height": page["page_size"][1],
            "boxes": [],
            "referenceBlocks": [],
        }
        for page in source_pages
    ]
    page_by_index = {page["index"]: page for page in pages}
    page_size_by_index = {page["page_idx"]: page["page_size"] for page in source_pages}

    leaf_locations: dict[tuple, list[tuple]] = defaultdict(list)
    line_locations: dict[tuple, list[tuple]] = defaultdict(list)
    leaf_source_parents: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    line_source_parents: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for page in source_pages:
        page_index = int(page["page_idx"])
        for parent_index, parent in enumerate(page.get("preproc_blocks", [])):
            for leaf in _leaves(parent):
                location = (page_index, leaf["bbox"], leaf["type"])
                leaf_locations[_leaf_signature(leaf)].append(location)
                leaf_source_parents[_leaf_signature(leaf)].append((page_index, parent_index))
                for line in leaf.get("lines", []):
                    line_locations[_span_signature(line)].append(
                        (page_index, line.get("bbox", leaf["bbox"]), leaf["type"])
                    )
                    line_source_parents[_span_signature(line)].append((page_index, parent_index))

    block_records: list[dict[str, Any]] = []
    parents: list[list[dict[str, Any]]] = []
    seen_boxes: set[tuple] = set()
    seen_references: set[tuple] = set()
    reference_counts: Counter = Counter()
    reference_crop_count = 0
    excluded_counts: Counter = Counter()
    merged_list_children = 0
    parent_types: Counter = Counter()
    extracted_parents: list[tuple[int, int, dict, list[dict]]] = []

    def add_box(block_id: str | None, page_index: int, bbox: list, kind: str, editable: bool) -> None:
        normalized = _normalize_bbox(bbox, page_size_by_index[page_index])
        signature = (block_id, page_index, tuple(normalized), kind, editable)
        if signature in seen_boxes:
            return
        seen_boxes.add(signature)
        page_by_index[page_index]["boxes"].append(
            {"id": block_id, "bbox": normalized, "type": kind, "editable": editable}
        )

    def add_reference(block: dict, page_index: int) -> None:
        nonlocal reference_crop_count
        source_kind = block.get("type", "discarded")
        kind = REFERENCE_TYPES.get(source_kind, source_kind)
        if kind not in {"header", "footer", "page_number"}:
            add_box(None, page_index, block["bbox"], source_kind, False)
            return

        leaves = list(_leaves(block))
        # Identical entries may appear in both discarded_blocks and para_blocks.
        # Ignore extraction indices and aliases while retaining distinct text at
        # the same coordinates.
        signature = (
            page_index,
            kind,
            tuple(block["bbox"]),
            tuple(tuple(_span_signature(line) for line in leaf.get("lines", [])) for leaf in leaves),
        )
        if signature in seen_references:
            return
        seen_references.add(signature)

        text_segments = []
        image_segments = []
        seen_image_paths = set()
        for leaf in leaves:
            if leaf.get("lines"):
                text = merge_para_with_text(
                    copy.deepcopy(leaf),
                    formula_enable=True,
                    img_buket_path="images",
                    escape_text_block_prefix=False,
                ).strip()
                if text:
                    text_segments.append(text)
            for line in leaf.get("lines", []):
                for span in line.get("spans", []):
                    path = span.get("image_path")
                    if path and path not in seen_image_paths:
                        seen_image_paths.add(path)
                        relative = str(path).replace("\\", "/")
                        if not relative.startswith(("images/", "data:", "http://", "https://")):
                            relative = f"images/{relative}"
                        image_segments.append(f"![]({relative})")
        text = "\n\n".join(text_segments)
        # An empty publisher-symbol box and an image-only QR-code description
        # need their source graphic as well as any text MinerU supplied.
        needs_crop = kind == "footer" and not image_segments and (
            not text.strip() or bool(re.search(r"\bQR\s*code\b|二维码", text, flags=re.IGNORECASE))
        )
        if needs_crop:
            image_segments.append(
                _reference_crop_markdown(source_pdf, page_index, block["bbox"], page_size_by_index[page_index])
            )
            reference_crop_count += 1
        markdown = "\n\n".join(image_segments + text_segments)
        reference_id = f"reference-{sum(reference_counts.values()) + 1:04d}"
        reference_counts[kind] += 1
        page_by_index[page_index]["referenceBlocks"].append(
            {
                "id": reference_id,
                "page": page_index,
                "type": kind,
                "bbox": _normalize_bbox(block["bbox"], page_size_by_index[page_index]),
                "markdown": markdown,
                "export": False,
                "editable": False,
            }
        )
        add_box(reference_id, page_index, block["bbox"], kind, False)

    def locate_leaf(leaf: dict, fallback_page: int) -> list[tuple]:
        candidates = leaf_locations.get(_leaf_signature(leaf), [])
        if len(candidates) == 1:
            return candidates
        on_original_page = [item for item in candidates if item[0] == fallback_page]
        if len(on_original_page) == 1:
            return on_original_page
        # Paragraphs may merge lines across columns. Their
        # complete paragraph no longer exists in preproc_blocks; individual
        # lines retain unique signatures and locate both source rectangles.
        line_matches = []
        for line in leaf.get("lines", []):
            matches = line_locations.get(_span_signature(line), [])
            if len(matches) == 1:
                line_matches.extend(matches)
            else:
                local = [item for item in matches if item[0] == fallback_page]
                if len(local) == 1:
                    line_matches.extend(local)
        if line_matches:
            return line_matches
        if candidates:
            raise ValueError("Ambiguous page mapping for Markdown block")
        return [(fallback_page, leaf["bbox"], leaf["type"])]

    def add_record(markdown: str, block: dict, fallback_page: int, kind: str) -> dict:
        locations = []
        for leaf in _leaves(block):
            if leaf.get("lines"):
                locations.extend(locate_leaf(leaf, fallback_page))
        if not locations:
            locations = [(fallback_page, block["bbox"], kind)]
        primary_page, primary_bbox, _ = locations[0]
        # A visual block remains one editable unit, including its original
        # captions and footnotes, with a box for each visible component.
        if kind not in {"list", "ref_text"}:
            primary_bbox = block["bbox"]
        block_id = f"block-{len(block_records) + 1:04d}"
        record = {
            "id": block_id,
            "page": primary_page,
            "type": kind,
            "bbox": _normalize_bbox(primary_bbox, page_size_by_index[primary_page]),
            "markdown": markdown,
            "originalMarkdown": markdown,
            "separatorAfter": "",
        }
        block_records.append(record)
        for page_index, bbox, location_type in locations:
            add_box(block_id, page_index, bbox, location_type, True)
        return record

    for page in source_pages:
        page_index = int(page["page_idx"])
        for discarded in page.get("discarded_blocks", []):
            kind = discarded.get("type", "discarded")
            excluded_counts[kind] += 1
            add_reference(discarded, page_index)

        for parent_index, block in enumerate(page.get("para_blocks", [])):
            kind = block["type"]
            if kind in DISCARDED_TYPES:
                # Current MinerU .md excludes these already. If another file
                # includes them, the exact-match guard below rejects it rather
                # than silently changing source text.
                excluded_counts[kind] += 1
                add_reference(block, page_index)
                continue
            rendered = mk_blocks_to_markdown(
                [copy.deepcopy(block)], MakeMode.MM_MD, True, True, "images"
            )
            if not rendered:
                continue
            if len(rendered) != 1:
                raise ValueError("MinerU produced unexpected multiple results for a block")
            parent_markdown = rendered[0]
            parent_types[kind] += 1
            if kind != "list":
                records = [add_record(parent_markdown, block, page_index, kind)]
                parents.append(records)
                extracted_parents.append((page_index, parent_index, block, records))
                continue

            children = block.get("blocks", [])
            child_texts = [
                merge_para_with_text(
                    copy.deepcopy(child),
                    formula_enable=True,
                    img_buket_path="images",
                    escape_text_block_prefix=False,
                )
                for child in children
            ]
            # Keep the list formatter's hard-line-break separators exactly.
            # The final strip operation affects only the outside of the list.
            if not children or any(not text.strip() for text in child_texts):
                raise ValueError("Cannot losslessly split a nonempty list with empty children")
            child_texts[0] = child_texts[0].lstrip()
            child_texts[-1] = child_texts[-1].rstrip()
            if "  \n".join(child_texts) != parent_markdown:
                raise ValueError("List child boundaries do not reproduce original Markdown")
            child_records = []
            for index, (child, text) in enumerate(zip(children, child_texts)):
                child_kind = "ref_text" if block.get("sub_type") == "ref_text" else "list"
                record = add_record(text, child, page_index, child_kind)
                if record["page"] != page_index:
                    merged_list_children += 1
                if index + 1 < len(children):
                    record["separatorAfter"] = "  \n"
                child_records.append(record)
            parents.append(child_records)
            extracted_parents.append((page_index, parent_index, block, child_records))

    for parent in parents[:-1]:
        parent[-1]["separatorAfter"] = "\n\n"

    rebuilt = "".join(block["markdown"] + block["separatorAfter"] for block in block_records)
    if rebuilt != source_markdown:
        common = min(len(rebuilt), len(source_markdown))
        mismatch = next((i for i in range(common) if rebuilt[i] != source_markdown[i]), common)
        raise ValueError(
            f"Extracted Markdown differs from source at character {mismatch}; "
            f"lengths {len(rebuilt)} / {len(source_markdown)}. No output should be saved."
        )

    for page in pages:
        if not any(box["editable"] for box in page["boxes"]):
            # Blank source pages are legitimate, but a page with original
            # body blocks must not disappear because of cross-page merging.
            source_page = next(item for item in source_pages if item["page_idx"] == page["index"])
            if source_page.get("preproc_blocks"):
                raise ValueError(f"Page {page['index'] + 1} has body content but no editable mapping")

    # Source locations are retained separately from editable blocks so adding
    # connection lines cannot change IDs, drafts, or exported Markdown bytes.
    source_page_by_index = {page["page_idx"]: page for page in source_pages}
    replayed_page_by_index = {page["page_idx"]: page for page in replayed_pages}
    continuations: list[dict[str, Any]] = []
    confirmed_text_origins: set[tuple[int, int]] = set()

    def append_continuation(kind: str, relation: str, segments: list[dict]) -> None:
        continuations.append(
            {
                "id": f"continuation-{len(continuations) + 1:04d}",
                "kind": kind,
                "source": "mineru",
                "relation": relation,
                "blockIds": list(dict.fromkeys(segment["blockId"] for segment in segments)),
                "segments": segments,
                "crossPage": len({segment["page"] for segment in segments}) > 1,
            }
        )

    def unique_origin(candidates: list[tuple[int, int]], context: str) -> tuple[int, int]:
        unique = list(dict.fromkeys(candidates))
        if len(unique) != 1:
            raise ValueError(f"Cannot uniquely locate the original source for {context}")
        return unique[0]

    def verify_consumed_origin(origin: tuple[int, int], kind: str) -> None:
        page_index, parent_index = origin
        original = source_page_by_index[page_index]["preproc_blocks"][parent_index]
        replayed = replayed_page_by_index[page_index]["para_blocks"][parent_index]
        if original.get("type") != kind or not replayed.get("lines_deleted"):
            raise ValueError("Continuation source was not consumed by the official merge routine")
        field = "blocks" if kind == "list" else "lines"
        if replayed.get(field):
            raise ValueError("Official continuation source still contains unmerged content")

    for page_index, parent_index, parent, records in extracted_parents:
        if parent["type"] == "text":
            origins = []
            for line in parent.get("lines", []):
                origin = unique_origin(
                    line_source_parents.get(_span_signature(line), []), "text paragraph line"
                )
                if not origins or origins[-1] != origin:
                    origins.append(origin)
            if len(origins) <= 1:
                continue
            if origins[0] != (page_index, parent_index):
                raise ValueError("Confirmed paragraph does not begin at its stored destination")
            for origin in origins[1:]:
                verify_consumed_origin(origin, "text")
                confirmed_text_origins.add(origin)
            block_id = records[0]["id"]
            segments = []
            for origin_page, origin_parent in origins:
                original = source_page_by_index[origin_page]["preproc_blocks"][origin_parent]
                segments.append(
                    {
                        "page": origin_page,
                        "bbox": _normalize_bbox(original["bbox"], page_size_by_index[origin_page]),
                        "blockId": block_id,
                    }
                )
            append_continuation("text", "paragraph", segments)
        elif parent["type"] == "list" and parent.get("sub_type") == "ref_text":
            previous_origin = None
            previous_record = None
            for child, record in zip(parent.get("blocks", []), records):
                origin = unique_origin(
                    leaf_source_parents.get(_leaf_signature(child), []), "reference-list child"
                )
                if previous_origin is not None and origin != previous_origin:
                    verify_consumed_origin(origin, "list")
                    # Only connect the last/first child around each original
                    # list-container boundary. These are list-flow links, not
                    # a claim that two bibliography entries are one sentence.
                    append_continuation(
                        "reference_list",
                        "list_flow",
                        [
                            {
                                "page": previous_record["page"],
                                "bbox": previous_record["bbox"],
                                "blockId": previous_record["id"],
                            },
                            {"page": record["page"], "bbox": record["bbox"], "blockId": record["id"]},
                        ],
                    )
                previous_origin = origin
                previous_record = record

    merge_hint_origins = {
        (page["page_idx"], index)
        for page in source_pages
        for index, block in enumerate(page.get("preproc_blocks", []))
        if block.get("type") == "text" and block.get("merge_prev")
    }

    markdown_hash = hashlib.sha256(source_bytes).hexdigest()
    title = next(
        (re.sub(r"^#+\s*", "", block["markdown"]).strip() for block in block_records if block["type"] == "title"),
        document_name,
    )
    return {
        "metadata": {
            "schemaVersion": 1,
            "title": title,
            "documentName": document_name,
            "sourcePath": str(source_pdf if source_pdf.is_file() else folder),
            "sourceMarkdownPath": str(markdown_path),
            "sourceMarkdownHash": markdown_hash,
            "documentId": hashlib.sha256((document_name + "\0" + markdown_hash).encode("utf-8")).hexdigest(),
            "pageCount": len(pages),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "savedAt": 0,
            "coordinateSystem": "normalized-1000",
            "stats": {
                "originalParentBlocks": len(parents),
                "editableBlocks": len(block_records),
                "markdownCharacters": len(source_markdown),
                "sourceMarkdownExactMatch": True,
                "parentTypes": dict(parent_types),
                "blockTypes": dict(Counter(block["type"] for block in block_records)),
                "excludedTypes": dict(excluded_counts),
                "relocatedListChildren": merged_list_children,
                "referenceBlocks": sum(reference_counts.values()),
                "referenceTypes": dict(reference_counts),
                "referenceImageCrops": reference_crop_count,
                "officialParagraphReplayExactMatch": official_replay_exact_match,
                "textMergeHintCandidates": len(merge_hint_origins),
                "confirmedTextMergeSources": len(confirmed_text_origins),
                "unconfirmedTextMergeHints": len(merge_hint_origins - confirmed_text_origins),
                "continuations": dict(Counter(item["kind"] for item in continuations)),
            },
        },
        "blocks": block_records,
        "pages": pages,
        "continuations": continuations,
    }
