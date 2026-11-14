import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from documents import document, summary, floors, link_id
from xhh_sdk.exceptions import XhhConfigError


class DocumentTests(unittest.TestCase):
    def test_empty_article_never_uses_excerpt_as_body(self):
        post = document({"linkid": 123, "is_article": 1, "description": "a summary", "text": ""}, {})
        self.assertEqual(post["body"]["status"], "missing")
        self.assertEqual(post["body"]["text"], "")
        self.assertEqual(post["excerpt"], "a summary")
        self.assertNotEqual(post["body"]["completeness"], "complete")

    def test_ordered_mixed_body_and_unknown_blocks(self):
        post = document({"id": 123, "text": [{"type": "text", "text": "before"},
                         {"type": "img", "url": "https://imgheybox.max-c.com/p.png", "width": "12"},
                         {"type": "html", "text": '<p>after <a href="https://example.com">source</a></p>'},
                         {"type": "future"}]}, {})
        self.assertEqual([block["type"] for block in post["body"]["blocks"]], ["text", "image", "text", "link", "unknown"])
        self.assertIn("unsupported_body_block", post["body"]["gaps"])

    def test_invalid_body_json_is_not_published_as_text(self):
        post = document({"id": 123, "text": '[{"broken"'}, {})
        self.assertEqual(post["body"]["status"], "invalid")
        self.assertEqual(post["body"]["text"], "")

    def test_source_full_flag_and_partial_flag(self):
        self.assertEqual(document({"id": 123, "text": "body", "is_full_text": 1}, {})["body"]["completeness"], "complete")
        self.assertEqual(document({"id": 123, "text": "body", "is_summary": 1}, {})["body"]["completeness"], "partial")
        self.assertEqual(document({"id": 123, "text": "body"}, {})["body"]["completeness"], "unknown")

    def test_charge_and_unknown_counts_are_distinct(self):
        post = summary({"linkid": 123, "up": 0, "link_award_num": 7})
        self.assertEqual(post["statistics"]["charges"], 7)
        self.assertIsNone(post["statistics"]["comments"])

    def test_precise_relations_and_untruncated_comment(self):
        nodes = floors([{"comment": [{"commentid": 9, "text": "root"},
                        {"commentid": 10, "replyid": 9, "text": "long" * 1000}]}], "123")
        self.assertEqual((nodes[1]["root_id"], nodes[1]["reply_to_id"]), ("9", "9"))
        self.assertEqual(len(nodes[1]["text"]), 4000)

    def test_urls_resolve_without_following_foreign_hosts(self):
        self.assertEqual(link_id("https://www.xiaoheihe.cn/app/bbs/link/123"), "123")
        self.assertEqual(link_id("heybox://link?link_id=123"), "123")
        with self.assertRaises(XhhConfigError):
            link_id("https://example.com/app/bbs/link/123")


if __name__ == "__main__":
    unittest.main()
