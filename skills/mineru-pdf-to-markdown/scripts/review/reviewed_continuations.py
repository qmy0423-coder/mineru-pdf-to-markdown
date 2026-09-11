"""Detect layout-split paragraphs and optionally join them in the review page.

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


_HEADING_OR_ITEM = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)、]\s*|[一二三四五六七八九十百]+[、.]\s*|[（(]\d+[)）.]\s*)")
_CJK_START_CUES = re.compile(
    r"^(?:包括|其中|如|和|与|或|及|的|在|为|可|应|需|并|但|因此|该|其|状|据|明|现|经|对|由|是|有|患者|相关|根据|通过|如果|当|用于|采用|建议)"
)
_TERMINAL = set("。！？.!?；;。")


def _plain_boundary(text: str) -> str:
    """Use Markdown text only for boundary tests; never rewrite its content."""
    return re.sub(r"[`*_~]", "", text or "").strip()


def _source_boxes(structure: dict) -> dict[str, list[dict]]:
    boxes: dict[str, list[dict]] = {}
    for page in structure.get("pages", []):
        for box in page.get("boxes", []):
            if box.get("id"):
                boxes.setdefault(box["id"], []).append(box)
    return boxes


def _layout_support(left: dict, right: dict, boxes: dict[str, list[dict]]) -> bool:
    """Require a plausible reading-order transition, not mere text adjacency."""
    if right["page"] > left["page"]:
        return True
    if right["page"] < left["page"]:
        return False
    lb = boxes.get(left["id"], [{"bbox": left.get("bbox", [0, 0, 1000, 1000])}])
    rb = boxes.get(right["id"], [{"bbox": right.get("bbox", [0, 0, 1000, 1000])}])
    lx = max(item["bbox"][2] for item in lb)
    rx = min(item["bbox"][0] for item in rb)
    # Same-column split blocks have a small vertical gap; cross-column blocks
    # start to the right. This excludes arbitrary adjacent Markdown sections.
    return rx >= lx - 35 or any(item["bbox"][1] >= max(x["bbox"][3] for x in lb) - 18 for item in rb)


def _generic_joins(structure: dict) -> list[dict]:
    """Find heuristic layout splits using the updated source tool's policy.

    Latin boundaries, delimiters and cross-page cues are eligible for the
    optional automatic mode. Same-page cue-only relations stay candidates.
    These rules do not establish word boundaries or semantic correctness.
    """
    blocks = structure.get("blocks", [])
    boxes = _source_boxes(structure)
    joins: list[dict] = []
    candidates: list[dict] = []
    for index, left in enumerate(blocks[:-1]):
        if left["type"] not in {"text", "list", "ref_text"}:
            continue
        for right_index in range(index + 1, min(len(blocks), index + 5)):
            right = blocks[right_index]
            skipped = blocks[index + 1:right_index]
            if right["type"] not in {"text", "list", "ref_text"}:
                continue
            if any(not _floating_block(item) for item in skipped):
                continue
            if not _layout_support(left, right, boxes):
                continue
            tail = _plain_boundary(left["markdown"])
            head = _plain_boundary(right["markdown"])
            if not tail or not head or _HEADING_OR_ITEM.match(head):
                continue
            last, first = tail[-1], head[0]
            hard_word = bool(re.search(r"[A-Za-z0-9]$", tail) and re.match(r"[a-z0-9]", head))
            broken_delimiter = last in "([{《“‘/\\-" or (last.isalnum() and first in ")]】》”’")
            cue = bool(last not in _TERMINAL and (first.isascii() is False or _CJK_START_CUES.match(head)))
            if not (hard_word or broken_delimiter or cue):
                continue
            join_with = " " if hard_word and last.isalnum() and first.isalnum() else ""
            edge = {
                "left": left["id"], "right": right["id"], "joinWith": join_with,
                "reason": "generic layout continuation: token/delimiter/continuation cue",
                "leftTail": left["markdown"][-min(80, len(left["markdown"])):],
                "rightHead": right["markdown"][:min(100, len(right["markdown"]))],
                "skipIds": [item["id"] for item in skipped],
            }
            # Preserve the source tool's automatic-mode eligibility. Default
            # Skill use exposes both groups as unverified review candidates.
            if hard_word or broken_delimiter or right["page"] > left["page"]:
                joins.append(edge)
            else:
                candidates.append({**edge, "confidence": 0.72})
            break
    # Never branch, and prefer the first edge in document order.
    used_right: set[str] = set()
    filtered = []
    for edge in joins:
        if edge["left"] in {item["left"] for item in filtered} or edge["right"] in used_right:
            continue
        filtered.append(edge); used_right.add(edge["right"])
    structure["continuationCandidates"] = candidates
    return filtered


def _floating_block(block: dict) -> bool:
    if block["type"] in {"image", "table"}:
        return True
    text = re.sub(r"^#+\s*", "", block["markdown"]).strip()
    return bool(re.match(r"^[图表]\s*\d+", text))


def find_continuation_candidates(structure: dict) -> list[dict]:
    """Expose every generic suggestion without changing blocks or metadata.

    autoEligible describes the source tool's heuristic policy, not a verified
    semantic judgment. Codex must still review the original page and context.
    """
    probe = dict(structure)
    automatic = _generic_joins(probe)
    candidates = [{**edge, "autoEligible": True} for edge in automatic]
    candidates.extend({**edge, "autoEligible": False}
                      for edge in probe["continuationCandidates"])
    order = {block["id"]: index for index, block in enumerate(structure.get("blocks", []))}
    return sorted(candidates, key=lambda edge: (order[edge["left"]], order[edge["right"]]))


def apply_reviewed_continuations(structure: dict, profile_dir: Path | None = None,
                                 *, auto_continuations: bool = False) -> dict:
    """Preserve source text by default; apply explicitly selected merge modes.

    A matching external profile takes precedence over generic detection.
    Without one, generic joins require auto_continuations=True. Otherwise
    every detected relation is a candidate for Codex's semantic review.
    All merge modes retain exact original units for inverse verification.
    """
    result = copy.deepcopy(structure)
    source_hash = structure["metadata"]["sourceMarkdownHash"]
    profile_path = profile_dir / f"{source_hash}.json" if profile_dir is not None else None
    profile_is_reviewed = profile_path is not None and profile_path.is_file()
    if profile_is_reviewed:
        profile_bytes = profile_path.read_bytes()
        profile = json.loads(profile_bytes.decode("utf-8"))
        result["metadata"]["continuationMode"] = "reviewed"
    elif not auto_continuations:
        result["continuationCandidates"] = find_continuation_candidates(result)
        result["metadata"]["continuationMode"] = "candidates"
        result["metadata"]["stats"].update({
            "genericContinuationDetector": True,
            "continuationCandidateCount": len(result["continuationCandidates"]),
        })
        return result
    else:
        profile = {"sourceMarkdownHash": source_hash, "joins": _generic_joins(result)}
        result["continuationCandidates"] = [
            {**edge, "autoEligible": False} for edge in result["continuationCandidates"]
        ]
        result["metadata"]["continuationMode"] = "generic"
        result["metadata"]["stats"].update({
            "genericContinuationDetector": True,
            "continuationCandidateCount": len(result["continuationCandidates"]),
        })
        profile_bytes = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if not profile["joins"]:
            return result
    if profile.get("sourceMarkdownHash") != source_hash:
        raise ValueError("Continuation profile does not match the source Markdown")
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
            "id": f"{'reviewed' if profile_is_reviewed else 'generic'}-{merged['id']}",
            "kind": "text", "source": "layout_review" if profile_is_reviewed else "generic_layout",
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
    metadata["paragraphPolicyId"] = hashlib.sha256(
        (b"reviewed-paragraphs-v2-reviewed\0" if profile_is_reviewed else b"reviewed-paragraphs-v2-generic\0") + profile_bytes
    ).hexdigest()
    markdown = "".join(block["markdown"] + block["separatorAfter"] for block in paragraphs)
    stats = metadata["stats"]
    stats.update({
        "rawExtractionExactMatch": stats["sourceMarkdownExactMatch"],
        "sourceMarkdownExactMatch": hashlib.sha256(markdown.encode("utf-8")).hexdigest() == source_hash,
        "originalEditableBlocks": len(original), "editableBlocks": len(paragraphs),
        "markdownCharacters": len(markdown),
        "reviewedJoinBoundaries": len(edges) if profile_is_reviewed else 0,
        "reviewedParagraphGroups": len(merged_groups) if profile_is_reviewed else 0,
        "genericJoinBoundaries": 0 if profile_is_reviewed else len(edges),
        "genericParagraphGroups": 0 if profile_is_reviewed else len(merged_groups),
        "sourcePartsExactMatch": True,
        "blockTypes": dict(Counter(block["type"] for block in paragraphs)),
        "continuations": dict(Counter(group["kind"] for group in result["continuations"])),
        "genericContinuationDetector": not profile_is_reviewed,
        "continuationCandidateCount": len(result.get("continuationCandidates", [])),
    })
    return result
