"""Join reviewed layout-split paragraphs without altering MinerU source files.

Profiles are bound to the source Markdown hash and exact boundary text. They
are intentionally not a claim that punctuation alone detects semantic joins.
All original editable units are retained in originParts for auditing/drafts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


def _floating_block(block: dict) -> bool:
    if block["type"] in {"image", "table"}:
        return True
    text = re.sub(r"^#+\s*", "", block["markdown"]).strip()
    return bool(re.match(r"^[图表]\s*\d+", text))


def apply_reviewed_continuations(structure: dict, profile_dir: Path) -> dict:
    """Return reviewed paragraphs and immutable provenance, or the input.

    Unknown documents keep official MinerU output. Each profile entry must
    identify an audited forward edge, with explicitly approved floating items
    in between. No blind sentence/uppercase-based rewriting is performed.
    """
    source_hash = structure["metadata"]["sourceMarkdownHash"]
    profile_path = profile_dir / f"{source_hash}.json"
    if not profile_path.is_file():
        return structure
    profile_bytes = profile_path.read_bytes()
    profile = json.loads(profile_bytes.decode("utf-8"))
    if profile.get("sourceMarkdownHash") != source_hash:
        raise ValueError("Continuation profile does not match the source Markdown")
    result = copy.deepcopy(structure)
    original = result["blocks"]
    by_id = {block["id"]: block for block in original}
    order = {block["id"]: index for index, block in enumerate(original)}
    edges, incoming = {}, {}
    for edge in profile.get("joins", []):
        left, right = edge["left"], edge["right"]
        if left not in by_id or right not in by_id or order[left] >= order[right]:
            raise ValueError(f"Invalid continuation direction: {left} -> {right}")
        if left in edges or right in incoming:
            raise ValueError("A paragraph continuation cannot branch")
        if edge.get("joinWith") not in {"", " "} or not edge.get("reason"):
            raise ValueError("A reviewed continuation needs an explicit join and reason")
        if not edge.get("leftTail") or not by_id[left]["markdown"].endswith(edge["leftTail"]):
            raise ValueError(f"Continuation tail changed: {left}")
        if not edge.get("rightHead") or not by_id[right]["markdown"].startswith(edge["rightHead"]):
            raise ValueError(f"Continuation head changed: {right}")
        if any(by_id[key]["type"] not in {"text", "list", "ref_text"} for key in (left, right)):
            raise ValueError("Only reviewed textual units can form a paragraph")
        skipped = original[order[left] + 1:order[right]]
        if [block["id"] for block in skipped] != edge.get("skipIds", []):
            raise ValueError("Continuation skips unreviewed content")
        if any(not _floating_block(block) for block in skipped):
            raise ValueError("Continuation must not cross a heading or unrelated paragraph")
        edges[left] = edge
        incoming[right] = left

    official = result.get("continuations", [])
    official_by_id = {
        group["blockIds"][0]: group for group in official
        if group["kind"] == "text" and len(group["blockIds"]) == 1
    }
    boxes_by_id = {}
    for page in result["pages"]:
        for box in page["boxes"]:
            if box.get("id") in by_id:
                boxes_by_id.setdefault(box["id"], []).append(
                    {"page": page["index"], "bbox": box["bbox"], "blockId": box["id"]}
                )
    aliases, paragraphs, merged_groups, merge_records = {}, [], [], []
    for block in original:
        if block["id"] in incoming:
            continue
        parts = [block]
        joins = []
        while parts[-1]["id"] in edges:
            edge = edges[parts[-1]["id"]]
            joins.append(edge)
            parts.append(by_id[edge["right"]])
        if len(parts) == 1:
            paragraphs.append(block)
            continue
        merged = copy.deepcopy(block)
        merged["originParts"] = copy.deepcopy(parts)
        merged["joinSeparators"] = [edge["joinWith"] for edge in joins]
        merged["markdown"] = parts[0]["markdown"] + "".join(
            edge["joinWith"] + part["markdown"] for edge, part in zip(joins, parts[1:])
        )
        merged["originalMarkdown"] = merged["markdown"]
        # Floating graphics move after the now-complete paragraph; do not
        # accidentally turn a following list/table into paragraph text.
        merged["separatorAfter"] = parts[-1]["separatorAfter"]
        if any(edge.get("skipIds") for edge in joins):
            merged["separatorAfter"] = "\n\n"
        merged["sourcePages"] = sorted({part["page"] for part in parts})
        segments = []
        for part in parts:
            aliases[part["id"]] = merged["id"]
            locations = official_by_id.get(part["id"], {}).get("segments", boxes_by_id.get(part["id"], []))
            if not locations:
                raise ValueError(f"Continuation has no source rectangle: {part['id']}")
            for location in locations:
                segment = copy.deepcopy(location)
                segment["sourceBlockId"] = part["id"]
                segment["blockId"] = merged["id"]
                if not segments or segment != segments[-1]:
                    segments.append(segment)
        merged_groups.append({
            "id": f"reviewed-{merged['id']}", "kind": "text", "source": "layout_review",
            "relation": "paragraph", "blockIds": [merged["id"]], "segments": segments,
            "crossPage": len({item["page"] for item in segments}) > 1,
        })
        merge_records.append({
            "blockId": merged["id"], "sourceBlockIds": [part["id"] for part in parts],
            "joins": copy.deepcopy(joins),
        })
        paragraphs.append(merged)

    # Retain the exact raw units in provenance, including their source order.
    # This permits an inverse audit even when floating figures move relative
    # to the resumed text, and migrates legacy edits of either fragment.
    recovered = {}
    for paragraph in paragraphs:
        for part in paragraph.get("originParts", [paragraph]):
            if part["id"] in recovered:
                raise ValueError("Continuation duplicated source content")
            recovered[part["id"]] = part
    if len(recovered) != len(original) or any(recovered[b["id"]] != b for b in original):
        raise ValueError("Continuation lost or changed original text units")

    result["blocks"] = paragraphs
    for page in result["pages"]:
        for box in page["boxes"]:
            if box.get("id") in aliases:
                box["sourceBlockId"] = box["id"]
                box["id"] = aliases[box["id"]]
    retained = []
    for group in official:
        if group["kind"] == "text" and any(key in aliases for key in group["blockIds"]):
            continue  # Its original regions are included in the larger group.
        group["blockIds"] = list(dict.fromkeys(aliases.get(key, key) for key in group["blockIds"]))
        for segment in group["segments"]:
            segment["blockId"] = aliases.get(segment["blockId"], segment["blockId"])
        retained.append(group)
    result["continuations"] = retained + merged_groups
    result["paragraphMerges"] = merge_records
    result["sourceBlockOrder"] = [block["id"] for block in original]
    metadata = result["metadata"]
    metadata["paragraphPolicyId"] = hashlib.sha256(b"reviewed-paragraphs-v1\0" + profile_bytes).hexdigest()
    markdown = "".join(block["markdown"] + block["separatorAfter"] for block in paragraphs)
    stats = metadata["stats"]
    stats.update({
        "rawExtractionExactMatch": stats["sourceMarkdownExactMatch"],
        "sourceMarkdownExactMatch": hashlib.sha256(markdown.encode("utf-8")).hexdigest() == source_hash,
        "originalEditableBlocks": len(original), "editableBlocks": len(paragraphs),
        "markdownCharacters": len(markdown), "reviewedJoinBoundaries": len(edges),
        "reviewedParagraphGroups": len(merged_groups), "sourcePartsExactMatch": True,
        "blockTypes": dict(Counter(block["type"] for block in paragraphs)),
        "continuations": dict(Counter(group["kind"] for group in result["continuations"])),
    })
    return result
