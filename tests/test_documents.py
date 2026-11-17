import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from documents import comment, document, summary, floors, link_id
from xhh_sdk.exceptions import XhhConfigError
from xhh_sdk.payload import Post


class DocumentTests(unittest.TestCase):
    def test_built_article_reads_back_inline_image_order_and_repetitions(self):
        url = "https://imgheybox.max-c.com/fixture.png"
        payload = Post(post_format="article").build(protocol="web", blocks=[
            {"type": "html", "text": "<p>before</p>"},
            {"type": "img", "url": url},
            {"type": "html", "text": "<p>middle</p>"},
            {"type": "img", "url": url},
            {"type": "html", "text": "<p>after</p>"},
        ])
        post = document({"id": 123, "link_tag": payload["link_tag"], "text": payload["text"]}, {})
        blocks = post["body"]["blocks"]
        self.assertEqual([block["type"] for block in blocks], ["text", "image", "text", "image", "text"])
        self.assertEqual([block["url"] for block in blocks if block["type"] == "image"], [url, url])
        self.assertEqual([block["text"].strip() for block in blocks if block["type"] == "text"],
                         ["before", "middle", "after"])

    def test_current_article_metadata_preserves_repeated_visible_images(self):
        url = "https://imgheybox.max-c.com/fixture.png"
        post = document({"id": 123, "link_tag": 11, "text": [
            {"type": "html", "text": f'<p>before</p><img src="{url}"><p>middle</p><img src="{url}"><p>after</p>'},
            {"type": "img", "url": url}, {"type": "img", "url": url}]}, {})
        self.assertEqual([block["url"] for block in post["body"]["blocks"] if block["type"] == "image"], [url, url])
        self.assertEqual(post["content_type"], "article")

    def test_plain_text_block_does_not_interpret_html_markup_as_images(self):
        text = '<img src="https://example.com/a.png"> literal'
        post = document({"id": 123, "link_tag": 27, "text": [{"type": "text", "text": text}]}, {})
        self.assertEqual(post["body"]["text"], text)
        self.assertEqual([block["type"] for block in post["body"]["blocks"]], ["text"])
        self.assertEqual(post["content_type"], "dynamic")

    def test_mixed_legacy_blocks_are_not_treated_as_an_article_metadata_tail(self):
        url = "https://example.com/a.png"
        post = document({"id": 123, "text": [{"type": "html", "text": f'<img src="{url}">'},
                                              {"type": "img", "url": url}, {"type": "html", "text": "after"}]}, {})
        self.assertEqual(sum(block["type"] == "image" for block in post["body"]["blocks"]), 2)

    def test_legacy_app_article_gallery_image_is_not_web_metadata(self):
        url = "https://example.com/a.png"
        post = document({"id": 123, "link_tag": 1, "text": [
            {"type": "html", "text": f'<img src="{url}">'},
            {"type": "img", "url": url}]}, {})
        self.assertEqual(sum(block["type"] == "image" for block in post["body"]["blocks"]), 2)

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
        self.assertEqual([block["type"] for block in post["body"]["blocks"]], ["text", "image", "text", "link", "text", "unknown"])
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

    def test_omitted_fields_are_unknown_while_explicit_empties_are_observed(self):
        omitted = summary({"linkid": 123})
        self.assertIsNone(omitted["title"])
        self.assertIsNone(omitted["excerpt"])
        self.assertIsNone(omitted["content_type"])
        self.assertIsNone(omitted["communities"])
        self.assertIsNone(omitted["hashtags"])
        explicit = summary({"linkid": 123, "title": "", "description": "", "topics": [], "hashtags": []})
        self.assertEqual(explicit["title"], "")
        self.assertEqual(explicit["excerpt"], "")
        self.assertEqual(explicit["communities"], [])
        self.assertEqual(explicit["hashtags"], [])
        self.assertIsNone(comment({"commentid": 10}, "123", "9")["text"])
        self.assertEqual(comment({"commentid": 10, "text": ""}, "123", "9")["text"], "")

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

    def test_literal_paths_are_text_not_host_file_requests(self):
        post = document({"id": "1", "text": [{"type": "text", "text": "/data/project is my example path"}]}, {})
        self.assertEqual(post["body"]["text"], "/data/project is my example path")

    def test_legacy_image_references_and_missing_metadata_are_preserved(self):
        post = document({"id": "1", "text": [{"type": "img", "url": "http://cdn.max-c.com/old.png"},
                        {"type": "img", "width": 32, "text": "/storage/example.png"}]}, {})
        self.assertEqual(post["media"][0]["url"], "http://cdn.max-c.com/old.png")
        self.assertEqual(post["body"]["blocks"][1]["data"]["width"], 32)
        self.assertIn("image_url_missing", post["body"]["gaps"])


if __name__ == "__main__":
    unittest.main()
