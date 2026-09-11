"""Exercise continuation modes and lossless provenance with synthetic text.

Run with Python: python -m unittest discover -s tests -p test_continuations.py
No MinerU installation or document-specific profile is needed.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

REVIEW = Path(__file__).resolve().parents[1] / "skills/mineru-pdf-to-markdown/scripts/review"
sys.path.insert(0, str(REVIEW))
from reviewed_continuations import apply_reviewed_continuations, find_continuation_candidates


def document(rows: list[tuple[str, str, int]]) -> dict:
    pages = [{"index": page, "boxes": []} for page in range(max(row[2] for row in rows) + 1)]
    blocks = []
    for index, (text, kind, page) in enumerate(rows):
        key = f"block-{index + 1:04d}"
        bbox = [50, 100 + index * 80, 450, 150 + index * 80]
        blocks.append({"id": key, "type": kind, "page": page, "bbox": bbox,
                       "markdown": text, "originalMarkdown": text, "separatorAfter": "\n\n"})
        pages[page]["boxes"].append({"id": key, "bbox": bbox, "editable": True, "type": kind})
    text = markdown(blocks)
    return {"metadata": {"sourceMarkdownHash": hashlib.sha256(text.encode()).hexdigest(),
                         "stats": {"sourceMarkdownExactMatch": True}},
            "blocks": blocks, "pages": pages, "continuations": []}


def markdown(blocks: list[dict]) -> str:
    return "".join(block["markdown"] + block["separatorAfter"] for block in blocks)


def recover(result: dict) -> str:
    parts = {}
    for block in result["blocks"]:
        for part in block.get("originParts", [block]):
            if part["id"] in parts:
                raise AssertionError("Duplicate original block")
            parts[part["id"]] = part
    return markdown([parts[key] for key in result["sourceBlockOrder"]])


class ContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = document([("检测指标", "text", 0), ("包括甲和乙。", "text", 1)])

    def write_profile(self, directory: str, source: dict, **overrides) -> Path:
        left, right = source["blocks"][:2]
        profile = {"sourceMarkdownHash": source["metadata"]["sourceMarkdownHash"], "joins": [{
            "left": left["id"], "right": right["id"], "joinWith": "", "reason": "checked in fixture",
            "leftTail": left["markdown"], "rightHead": right["markdown"], "skipIds": [],
        }]}
        profile.update(overrides)
        destination = Path(directory) / f"{source['metadata']['sourceMarkdownHash']}.json"
        destination.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        return Path(directory)

    def test_default_preserves_text_boxes_and_input(self):
        snapshot = copy.deepcopy(self.source)
        result = apply_reviewed_continuations(self.source)
        self.assertEqual(self.source, snapshot)
        self.assertEqual(result["blocks"], snapshot["blocks"])
        self.assertEqual(result["pages"], snapshot["pages"])
        self.assertEqual(result["metadata"]["continuationMode"], "candidates")
        self.assertEqual(len(result["continuationCandidates"]), 1)
        self.assertTrue(result["continuationCandidates"][0]["autoEligible"])
        self.assertFalse(result.get("paragraphMerges"))

    def test_candidate_detection_does_not_mutate_structure(self):
        snapshot = copy.deepcopy(self.source)
        candidates = find_continuation_candidates(self.source)
        self.assertEqual(self.source, snapshot)
        self.assertEqual(candidates[0]["leftTail"], "检测指标")
        self.assertEqual(candidates[0]["rightHead"], "包括甲和乙。")

    def test_opt_in_cross_page_join_has_exact_provenance(self):
        snapshot = copy.deepcopy(self.source)
        result = apply_reviewed_continuations(self.source, auto_continuations=True)
        self.assertEqual(markdown(result["blocks"]), "检测指标包括甲和乙。\n\n")
        self.assertEqual(recover(result), markdown(self.source["blocks"]))
        self.assertEqual(self.source, snapshot)
        self.assertEqual(result["continuations"][0]["source"], "generic_layout")
        self.assertEqual(result["metadata"]["stats"]["genericJoinBoundaries"], 1)
        self.assertEqual(result["metadata"]["stats"]["reviewedJoinBoundaries"], 0)
        self.assertEqual(result["pages"][1]["boxes"][0]["id"], self.source["blocks"][0]["id"])
        self.assertEqual(result["pages"][1]["boxes"][0]["sourceBlockId"], self.source["blocks"][1]["id"])

    def test_same_page_ambiguous_cue_remains_candidate_in_auto_mode(self):
        source = document([("检测指标", "text", 0), ("包括甲和乙。", "text", 0)])
        result = apply_reviewed_continuations(source, auto_continuations=True)
        self.assertEqual(result["blocks"], source["blocks"])
        self.assertFalse(result["continuationCandidates"][0]["autoEligible"])
        self.assertEqual(result["metadata"]["stats"]["continuationCandidateCount"], 1)

    def test_floating_image_is_preserved_once_and_original_order_recovers(self):
        source = document([("检测指标", "text", 0), ("![](images/fixture.jpg)", "image", 0),
                           ("包括甲和乙。", "text", 1)])
        result = apply_reviewed_continuations(source, auto_continuations=True)
        self.assertEqual([block["type"] for block in result["blocks"]], ["text", "image"])
        self.assertEqual(markdown(result["blocks"]).count("images/fixture.jpg"), 1)
        self.assertEqual(result["paragraphMerges"][0]["joins"][0]["skipIds"], ["block-0002"])
        self.assertEqual(recover(result), markdown(source["blocks"]))

    def test_headings_and_new_list_items_block_a_join(self):
        with_heading = document([("检测指标", "text", 0), ("# 新章节", "title", 1),
                                 ("包括其他内容。", "text", 1)])
        list_item = document([("检测指标", "text", 0), ("1. 独立列表项", "list", 1)])
        self.assertEqual(find_continuation_candidates(with_heading), [])
        self.assertEqual(find_continuation_candidates(list_item), [])

    def test_explicit_profile_overrides_generic_join_separator(self):
        source = document([("inter", "text", 0), ("national", "text", 1)])
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.write_profile(temporary, source)
            result = apply_reviewed_continuations(source, directory, auto_continuations=True)
        self.assertEqual(markdown(result["blocks"]), "international\n\n")
        self.assertEqual(result["metadata"]["continuationMode"], "reviewed")
        self.assertEqual(result["metadata"]["stats"]["genericJoinBoundaries"], 0)
        self.assertEqual(recover(result), markdown(source["blocks"]))

    def test_empty_reviewed_profile_prevents_generic_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.write_profile(temporary, self.source, joins=[])
            result = apply_reviewed_continuations(self.source, directory, auto_continuations=True)
        self.assertEqual(result["blocks"], self.source["blocks"])
        self.assertFalse(result["paragraphMerges"])

    def test_missing_profile_requires_auto_flag_for_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            default = apply_reviewed_continuations(self.source, Path(temporary))
            automatic = apply_reviewed_continuations(self.source, Path(temporary), auto_continuations=True)
        self.assertEqual(default["blocks"], self.source["blocks"])
        self.assertEqual(len(automatic["blocks"]), 1)

    def test_mismatched_profile_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.write_profile(temporary, self.source, sourceMarkdownHash="wrong")
            with self.assertRaisesRegex(ValueError, "does not match"):
                apply_reviewed_continuations(self.source, directory, auto_continuations=True)

    def test_changed_profile_boundary_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.write_profile(temporary, self.source)
            path = next(directory.glob("*.json"))
            profile = json.loads(path.read_text(encoding="utf-8"))
            profile["joins"][0]["leftTail"] = "unrelated text"
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "tail changed"):
                apply_reviewed_continuations(self.source, directory)


if __name__ == "__main__":
    unittest.main()
