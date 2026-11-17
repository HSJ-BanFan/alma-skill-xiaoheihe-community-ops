from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))

from postspec import prepare_post, preview_post
from xhh_sdk.exceptions import XhhConfigError
from xhh_sdk.payload import Post

try:
    import markdown_it  # noqa: F401
    MARKDOWN_INSTALLED = True
except ImportError:
    MARKDOWN_INSTALLED = False

requires_markdown = unittest.skipUnless(MARKDOWN_INSTALLED, "markdown-it-py is not installed")


def html_of(post: Post) -> str:
    return json.loads(post.build(protocol="web")["text"])[0]["text"]


class LegacyParityTest(unittest.TestCase):
    def test_text_and_html_payloads_match_direct_post_build(self):
        specs = [
            {"title": "标题", "content": "第一行\n\n第二行 <b>x</b>", "content_format": "text",
             "hashtags": ["话题", "#话题"], "topic_ids": ["7214", 9], "original": False},
            {"title": "标题", "content": "<p>已有 HTML</p>", "post_format": "article", "content_format": "html",
             "hashtags": [], "topic_ids": ["7214"], "original": True},
            {"title": "文章", "content": "正文", "content_format": "text",
             "cover_url": "https://imgheybox.max-c.com/cover.png", "post_type": "3",
             "visibility": "1", "original": False},
        ]
        for spec in specs:
            for draft in (True, False):
                with self.subTest(spec=spec, draft=draft):
                    expected = Post(**{**spec, "draft": draft}).build(protocol="web")
                    actual = prepare_post(copy.deepcopy(spec), draft=draft).build(protocol="web")
                    self.assertEqual(list(actual), list(expected))
                    self.assertEqual(json.dumps(actual, ensure_ascii=False),
                                     json.dumps(expected, ensure_ascii=False))

    def test_prepare_and_preview_do_not_mutate_the_source(self):
        spec = {"title": "t", "content": "# h", "content_format": "text", "images": [],
                "hashtags": ["a"], "topic_ids": ["1"]}
        before = copy.deepcopy(spec)
        prepare_post(spec, draft=False)
        preview_post(spec, public=True)
        self.assertEqual(spec, before)

    def test_unknown_fields_formats_and_types_fail_as_config_errors(self):
        cases = [
            {"content": "x", "content_format": "md"},
            {"content": "x", "content_format": None},
            {"content": "x", "post_type": 1},
            {"content": "x", "post_type": "2"},
            {"content": "x", "hashtags": "not-a-list"},
            {"content": "x", "images": "https://example.com/a.png"},
            {"content": "x", "images": None},
            {"content": "x", "unknown-field": 1},
            {"content": 5},
            {"content": "x", "original": "false"},
            {"content": "x", "original": 1},
        ]
        for spec in cases:
            with self.subTest(spec=spec):
                with self.assertRaises(XhhConfigError):
                    prepare_post(spec, draft=True)


class TypeAndMediaGateTest(unittest.TestCase):
    def test_cover_preserves_format_and_requires_https(self):
        base = {"content": "正文", "cover_url": "https://imgheybox.max-c.com/cover.png"}
        for spec in ({**base, "post_type": "3", "cover_url": "http://x/y.png"},
                     {"content": "正文", "cover_url": "https:///no-host"}):
            with self.subTest(spec=spec):
                with self.assertRaises(XhhConfigError):
                    prepare_post(spec, draft=True)
        post = prepare_post({**base, "post_type": "3", "visibility": "1"}, draft=True)
        payload = post.build(protocol="web")
        self.assertEqual((payload["thumb"], payload["post_type"], payload["view_limit"]),
                         ("https://imgheybox.max-c.com/cover.png", "1", "1"))
        self.assertEqual(payload["link_tag"], "11")
        preview = preview_post(base)
        self.assertEqual(preview["spec"]["post_format"], "image_text")
        self.assertEqual(preview["payload"]["link_tag"], "28")
        self.assertEqual(preview["payload"]["thumb"], base["cover_url"])
        self.assertEqual(preview["media"]["images"], [])

    def test_visibility_uses_current_choices_in_both_formats(self):
        for spec in ({"content": "x", "visibility": "0"},
                     {"content": "x", "post_type": "1", "visibility": "4"},
                     {"content": "x", "post_type": "3", "visibility": 1},
                     {"content": "x", "post_type": "3", "visibility": "unknown"}):
            with self.subTest(spec=spec):
                with self.assertRaises(XhhConfigError):
                    prepare_post(spec, draft=True)
        for kind in ("article", "image_text"):
            for value in ("1", "2", "3"):
                self.assertEqual(prepare_post({"content": "x", "post_format": kind, "visibility": value},
                                              draft=True).build(protocol="web")["view_limit"], value)

    def test_gallery_images_require_absolute_https(self):
        for url in ("relative.png", "http://example.com/a.png", "data:image/png;base64,aaaa",
                    " https://example.com/a.png", "https:///missing-host", "https://@",
                    "https://example.com:not-a-port/a.png"):
            with self.subTest(url=url):
                with self.assertRaises(XhhConfigError):
                    prepare_post({"content": "x", "images": [url]}, draft=True)

    def test_media_summary_comes_from_the_payload(self):
        spec = {"content": "<p>literal &lt;img src=\"https://fake/x.png\"&gt;</p>"
                           "<p><img src=\"https://real/a.png\" alt=\"a\"></p>",
                "post_format": "article", "content_format": "html",
                "images": ["https://real/trailing.png", "https://real/trailing.png"]}
        preview = preview_post(spec)
        self.assertEqual([(item["placement"], item["url"]) for item in preview["media"]["images"]],
                         [("inline", "https://real/a.png"),
                          ("inline", "https://real/trailing.png"),
                          ("inline", "https://real/trailing.png")])
        self.assertEqual([item["ordinal"] for item in preview["media"]["images"]], [1, 2, 3])
        self.assertEqual(preview["media"]["image_count"], 3)

    def test_html_article_rejects_different_display_and_original_image_urls(self):
        spec = {"content": ('<img src="https://example.com/display.png" '
                            'data-original="https://example.com/original.png">'),
                "post_format": "article", "content_format": "html"}
        with self.assertRaises(XhhConfigError):
            preview_post(spec)

    def test_text_format_never_counts_img_markup_as_media(self):
        preview = preview_post({"content": "<img src=\"https://real/a.png\">", "content_format": "text"})
        self.assertEqual(preview["media"]["images"], [])
        self.assertIn("<img", html_of(prepare_post({"content": "<img src=\"https://real/a.png\">",
                                                       "content_format": "text"}, draft=True)))


@requires_markdown
class MarkdownConversionTest(unittest.TestCase):
    def test_markdown_is_reserved_for_article_in_both_protocols(self):
        spec = {"content": "**body**", "post_format": "image_text", "content_format": "markdown"}
        for protocol in ("web", "app"):
            with self.subTest(protocol=protocol), self.assertRaises(XhhConfigError):
                prepare_post(spec, draft=True, protocol=protocol)

    def test_preview_spec_is_normalized_and_can_be_published_without_markdown(self):
        preview = preview_post({"content": "## Heading\n\n**bold**", "post_format": "article", "content_format": "markdown",
                                "original": False})
        self.assertEqual(preview["spec"]["content_format"], "html")
        self.assertIn("<h2>Heading</h2>", preview["spec"]["content"])
        self.assertEqual(Post(**preview["spec"]).build(protocol="web"), preview["payload"])

    def test_headings_keep_authored_levels(self):
        html = html_of(prepare_post({"content": "# one\n\n### three\n\n###### six",
                                     "post_format": "article", "content_format": "markdown"}, draft=True))
        self.assertIn("<h1>one</h1>", html)
        self.assertIn("<h3>three</h3>", html)
        self.assertIn("<h6>six</h6>", html)
        self.assertNotIn("<h2>", html)

    def test_emphasis_link_quote_list_and_code_whitespace(self):
        source = ("**bold** and [link](https://example.com) and <b>raw</b>\n\n"
                  "> quote\n\n- a\n- b\n\n"
                  "```python\n  x = 1\n    y = 2\n\nprint(\"<tag>\")\n```")
        html = html_of(prepare_post({"content": source, "post_format": "article", "content_format": "markdown"}, draft=True))
        for fragment in ("<strong>bold</strong>", '<a href="https://example.com">link</a>',
                         "&lt;b&gt;raw&lt;/b&gt;", "<blockquote>", "<ul>", "<li>a</li>"):
            self.assertIn(fragment, html)
        self.assertIn("  x = 1\n    y = 2\n\nprint(&quot;&lt;tag&gt;&quot;)\n", html)
        self.assertIn("class=\"language-python\"", html)

    def test_tables_and_strikethrough_are_parsed_by_the_default_preset(self):
        html = html_of(prepare_post(
            {"content": "| a | b |\n| - | - |\n| 1 | 2 |\n\n~~gone~~", "post_format": "article", "content_format": "markdown"},
            draft=True))
        self.assertIn("<table>", html)
        self.assertIn("<td>2</td>", html)
        self.assertIn("<s>gone</s>", html)

    def test_raw_html_is_escaped(self):
        html = html_of(prepare_post({"content": "<script>alert(1)</script>", "post_format": "article", "content_format": "markdown"},
                                    draft=True))
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_inline_images_keep_order_repetition_and_following_text(self):
        source = ("![first](https://img.example/a.png \"T\") middle "
                  "![again](https://img.example/a.png) after")
        spec = {"content": source, "post_format": "article", "content_format": "markdown", "images": []}
        preview = preview_post(spec)
        html = json.loads(preview["payload"]["text"])[0]["text"]
        self.assertEqual(html.count("<img"), 2)
        self.assertIn('data-original="https://img.example/a.png"', html)
        self.assertIn('data-type="image"', html)
        self.assertNotIn('src="https://img.example/a.png"', html)
        self.assertIn('title="T"', html)
        self.assertLess(html.index("first"), html.index("middle"))
        self.assertLess(html.index("middle"), html.index("again"))
        self.assertIn("after", html)
        self.assertEqual(prepare_post(spec, draft=True).images, [])
        self.assertEqual([item["url"] for item in preview["media"]["images"]],
                         ["https://img.example/a.png", "https://img.example/a.png"])

    def test_inline_and_trailing_images_stay_separate(self):
        spec = {"title": "图片", "content": "before\n\n![inline](https://img.example/inline.png)\n\nafter",
                "post_format": "article", "content_format": "markdown", "images": ["https://img.example/trailing.png"]}
        preview = preview_post(spec)
        self.assertEqual([(item["placement"], item["url"]) for item in preview["media"]["images"]],
                         [("inline", "https://img.example/inline.png"),
                          ("inline", "https://img.example/trailing.png")])
        self.assertEqual(prepare_post(spec, draft=True).images, ["https://img.example/trailing.png"])

    def test_markdown_images_require_absolute_https(self):
        for source in ("![x](relative.png)", "![x](http://example.com/a.png)",
                       "![x](data:image/png;base64,aaaa)", "![x](//example.com/a.png)"):
            with self.subTest(source=source):
                with self.assertRaises(XhhConfigError):
                    prepare_post({"content": source, "post_format": "article", "content_format": "markdown"}, draft=True)

    def test_empty_markdown_is_rejected(self):
        for content in ("", "   \n\t"):
            with self.subTest(content=content):
                with self.assertRaises(XhhConfigError):
                    prepare_post({"content": content, "post_format": "article", "content_format": "markdown"}, draft=True)

    def test_image_only_markdown_is_rejected(self):
        for content in ("![only](https://img.example/a.png)",
                        "  ![again](https://img.example/a.png)  "):
            with self.subTest(content=content), self.assertRaises(XhhConfigError):
                prepare_post({"content": content, "post_format": "article", "content_format": "markdown"}, draft=True)

    def test_crlf_and_bom_do_not_change_the_rendered_bytes(self):
        plain = prepare_post({"content": "# h\n\ntext", "post_format": "article", "content_format": "markdown"}, draft=True).build(protocol="web")
        varied = prepare_post({"content": "﻿# h\r\n\r\ntext\r\n", "post_format": "article", "content_format": "markdown"},
                              draft=True).build(protocol="web")
        self.assertEqual(varied["text"], plain["text"])


class PreviewShapeTest(unittest.TestCase):
    def test_preview_public_flag_only_changes_draft(self):
        spec = {"content": "正文", "content_format": "text"}
        draft = preview_post(spec)
        public = preview_post(spec, public=True)
        self.assertEqual(draft["spec"]["draft"], True)
        self.assertEqual(public["spec"]["draft"], False)
        self.assertEqual(draft["payload"]["draft"], "1")
        self.assertEqual(public["payload"]["draft"], "0")
        self.assertEqual({key: value for key, value in draft["payload"].items() if key != "draft"},
                         {key: value for key, value in public["payload"].items() if key != "draft"})
        self.assertEqual(set(draft), {"spec", "payload", "media"})


class CurrentCreatorContractTest(unittest.TestCase):
    def test_format_and_plan_are_independent(self):
        for kind, tag, block in (("image_text", "27", "text"), ("article", "11", "html")):
            for plan, mode in ((None, "1"), ("current_metadata_key", "2")):
                payload = prepare_post({"content": "body", "post_format": kind, "post_plan": plan,
                                        "topic_ids": ["7214"]}, draft=True).build(protocol="web")
                self.assertEqual((payload["link_tag"], payload["post_type"]), (tag, mode))
                self.assertEqual(json.loads(payload["text"])[0]["type"], block)
                self.assertEqual((payload["edit"], payload["draft"], payload["view_limit"], payload["schedule_ts"]),
                                 ("0", "1", "1", "0"))
                self.assertEqual(payload["hashtags"], "[]")

    def test_image_text_uses_editor_dynamic_section_only_without_topics(self):
        fallback = preview_post({"content": "body", "post_format": "image_text"})["payload"]
        self.assertEqual((fallback["topic_ids"], fallback["link_tag"]), ("58144", "28"))
        selected = preview_post({"content": "body", "post_format": "image_text",
                                 "topic_ids": ["7214", "9"]})["payload"]
        self.assertEqual((selected["topic_ids"], selected["link_tag"]), ("7214,9", "27"))
        article = preview_post({"content": "body", "post_format": "article"})["payload"]
        self.assertNotIn("topic_ids", article)
        self.assertEqual(article["link_tag"], "11")

    def test_authorship_permission_and_ai_declaration_are_independent(self):
        payload = prepare_post({"content": "x", "original": False}, draft=True).build(protocol="web")
        self.assertNotIn("original", payload)
        self.assertNotIn("declaration", payload)
        payload = prepare_post({"content": "x", "authorship": "original", "original": False,
                                "declaration": 2, "extra_declaration": 1}, draft=True).build(protocol="web")
        self.assertEqual((payload["original"], payload["declaration"], payload["extra_declaration"]), ("1", "2", "1"))
        payload = prepare_post({"content": "x", "authorship": "repost", "declaration": 1,
                                "source": "https://example.com/source"}, draft=True).build(protocol="web")
        self.assertEqual((payload["original"], payload["declaration"], payload["source"]),
                         ("0", "1", "https://example.com/source"))

    def test_invalid_authorship_and_permission_choices_are_refused(self):
        for values in ({"authorship": "guess"}, {"declaration": "2"}, {"declaration": True},
                       {"declaration": 3}, {"source": ""}, {"post_format": "video"}):
            with self.subTest(values=values), self.assertRaises(XhhConfigError):
                prepare_post({"content": "x", **values}, draft=True)

    def test_article_image_metadata_does_not_duplicate_visible_images(self):
        preview = preview_post({"content": '<p>before</p><img src="https://example.com/a.png" width="640">',
                                "content_format": "html", "post_format": "article"})
        blocks = json.loads(preview["payload"]["text"])
        self.assertEqual(blocks[1], {"type": "img", "url": "https://example.com/a.png", "width": "640", "height": ""})
        self.assertEqual(preview["media"]["image_count"], 1)

    @requires_markdown
    def test_upload_dimensions_reach_markdown_and_wire_metadata(self):
        info = {"url": "https://example.com/a.png", "width": 640, "height": 360}
        preview = preview_post({"content": "before\n\n![a](https://example.com/a.png)\n\nafter",
                                "post_format": "article", "content_format": "markdown", "image_info": [info]})
        blocks = json.loads(preview["payload"]["text"])
        self.assertIn('data-width="640"', blocks[0]["text"])
        self.assertIn('data-height="360"', blocks[0]["text"])
        self.assertEqual((blocks[1]["width"], blocks[1]["height"]), ("640", "360"))
        self.assertEqual((preview["media"]["images"][0]["width"], preview["media"]["images"][0]["height"]), ("640", "360"))

    def test_image_info_rejects_invalid_dimensions_and_conflicts(self):
        valid = {"url": "https://example.com/a.png", "width": 640, "height": 360}
        for value in (None, "metadata", [{**valid, "width": True}], [{**valid, "height": 0}],
                      [{**valid, "other": 1}], [valid, {**valid, "width": 128}]):
            with self.subTest(value=value), self.assertRaises(XhhConfigError):
                prepare_post({"content": "x", "image_info": value}, draft=True)


class ProtocolWireTest(unittest.TestCase):
    SPAN = ('<span data-emoji="cube_笑cry" '
            'class="hb-emoji hb-emoji-cube hb-emoji-cube_32"></span>')

    def test_raw_sdk_defaults_preserve_legacy_html_and_app_payload(self):
        post = Post(content="<p>legacy</p>", original=False)
        self.assertEqual(post.content_format, "html")
        payload = post.build()
        self.assertEqual((payload["link_tag"], payload["post_type"]), ("1", "1"))
        self.assertEqual(json.loads(payload["text"]), [{"type": "html", "text": "<p>legacy</p>"}])
        self.assertNotIn("edit", payload)
        self.assertNotIn("view_limit", payload)

    def test_app_uses_the_legacy_contract_not_the_creator_contract(self):
        payload = prepare_post({"content": "第一行\n\n第二行", "content_format": "text",
                                "hashtags": ["话题", "#话题"], "topic_ids": ["7214"],
                                "post_plan": "metadata_key", "extra_declaration": 2},
                               draft=True, protocol="app").build(protocol="app")
        self.assertEqual((payload["link_tag"], payload["post_type"], payload["draft"]),
                         ("1", "1", "1"))
        self.assertEqual((payload["original"], payload["declaration"]), ("1", "原创首发"))
        self.assertEqual(payload["hashtags"], '["#话题"]')
        self.assertEqual(payload["topic_ids"], "7214")
        self.assertEqual((payload["post_plan"], payload["extra_declaration"]), ("metadata_key", "2"))
        for absent in ("edit", "schedule_ts", "view_limit"):
            self.assertNotIn(absent, payload)

    def test_app_post_type_comes_from_format_and_plan_is_never_implied(self):
        for plan in (None, "metadata_key"):
            payload = prepare_post({"content": "body", "post_format": "article", "post_plan": plan},
                                   draft=True, protocol="app").build(protocol="app")
            self.assertEqual((payload["post_type"], payload["link_tag"]), ("3", "1"))
        payload = prepare_post({"content": "body", "post_format": "image_text", "post_plan": "metadata_key"},
                               draft=True, protocol="app").build(protocol="app")
        self.assertEqual(payload["post_type"], "1")

    def test_app_original_false_omits_the_legacy_declaration(self):
        payload = prepare_post({"content": "x", "original": False}, draft=True,
                               protocol="app").build(protocol="app")
        self.assertNotIn("original", payload)
        self.assertNotIn("declaration", payload)

    def test_app_preserves_structured_html_and_native_spans(self):
        post = prepare_post({"content": "<p>before " + self.SPAN + " after</p>", "content_format": "html",
                             "post_format": "image_text"}, draft=True, protocol="app")
        payload = post.build(protocol="app")
        self.assertEqual(json.loads(payload["text"]),
                         [{"type": "html", "text": "<p>before " + self.SPAN + " after</p>"}])
        self.assertEqual(payload["desc"], "before [cube_笑cry] after")

    def test_app_legacy_image_only_post_keeps_just_the_img_block(self):
        payload = Post(content="", images=["https://cdn.max-c.com/fixture.png"],
                       original=False).build(protocol="app")
        self.assertEqual(json.loads(payload["text"]),
                         [{"type": "img", "url": "https://cdn.max-c.com/fixture.png"}])
        self.assertEqual((payload["words_count"], payload["post_type"], payload["link_tag"]),
                         ("0", "1", "1"))

    def test_app_gallery_order_and_dimensions_are_never_invented(self):
        spec = {"content": "<p>body</p>", "content_format": "html", "post_format": "article",
                "images": ["https://i.example/a.png", "https://i.example/b.png"],
                "image_info": [{"url": "https://i.example/a.png", "width": 640, "height": 360}]}
        blocks = json.loads(prepare_post(spec, draft=True, protocol="app").build(protocol="app")["text"])
        self.assertEqual([block["type"] for block in blocks], ["html", "img", "img"])
        self.assertEqual(blocks[1], {"type": "img", "url": "https://i.example/a.png",
                                     "width": "640", "height": "360"})
        self.assertEqual(blocks[2], {"type": "img", "url": "https://i.example/b.png"})

    def test_app_refuses_creator_only_authorship_declaration_and_source(self):
        for values in ({"authorship": "original"}, {"authorship": "repost"},
                       {"declaration": 1, "authorship": "original"},
                       {"source": "https://example.com/source"}):
            with self.subTest(values=values), self.assertRaises(XhhConfigError):
                prepare_post({"content": "x", **values}, draft=True, protocol="app")

    def test_app_cover_and_visibility_rules(self):
        cover = "https://imgheybox.max-c.com/cover.png"
        with self.assertRaises(XhhConfigError):
            prepare_post({"content": "x", "cover_url": cover}, draft=True, protocol="app")
        article = prepare_post({"content": "x", "post_format": "article", "cover_url": cover,
                                "visibility": "0"}, draft=True, protocol="app").build(protocol="app")
        self.assertEqual((article["thumb"], article["post_type"], article["view_limit"]), (cover, "3", "0"))
        for value in ("1", "2", "3"):
            payload = prepare_post({"content": "x", "post_format": "article", "visibility": value},
                                   draft=True, protocol="app").build(protocol="app")
            self.assertEqual(payload["view_limit"], value)
        with self.assertRaises(XhhConfigError):
            prepare_post({"content": "x", "visibility": "1"}, draft=True, protocol="app")
        with self.assertRaises(XhhConfigError):
            prepare_post({"content": "x", "post_format": "article", "visibility": "4"},
                         draft=True, protocol="app")
        with self.assertRaises(XhhConfigError):
            prepare_post({"content": "x", "visibility": "0"}, draft=True, protocol="web")

    def test_app_keeps_html_image_text_while_web_still_requires_text(self):
        spec = {"content": "<p>html body</p>", "content_format": "html", "post_format": "image_text"}
        payload = prepare_post(spec, draft=True, protocol="app").build(protocol="app")
        self.assertEqual(json.loads(payload["text"]), [{"type": "html", "text": "<p>html body</p>"}])
        with self.assertRaises(XhhConfigError):
            prepare_post(spec, draft=True, protocol="web")

    def test_app_gallery_images_stay_visible_beside_inline_html(self):
        spec = {"content": '<p>before</p><img src="https://i.example/a.png">',
                "content_format": "html", "post_format": "article",
                "images": ["https://i.example/g.png"]}
        app = preview_post(spec, protocol="app")["media"]["images"]
        self.assertEqual([(item["placement"], item["url"]) for item in app],
                         [("inline", "https://i.example/a.png"), ("trailing", "https://i.example/g.png")])
        web = preview_post(spec)["media"]["images"]
        self.assertEqual([(item["placement"], item["url"]) for item in web],
                         [("inline", "https://i.example/a.png"), ("inline", "https://i.example/g.png")])

    def test_preview_media_trailing_images_carry_real_dimensions(self):
        preview = preview_post({"content": "body", "post_format": "article",
                                "images": ["https://i.example/a.png"],
                                "image_info": [{"url": "https://i.example/a.png", "width": 12, "height": 8}]},
                               protocol="app")
        self.assertEqual([(item["placement"], item["width"], item["height"])
                          for item in preview["media"]["images"]],
                         [("trailing", "12", "8")])

    def test_prepare_preview_and_build_agree_within_each_protocol(self):
        spec = {"title": "标题", "content": "正文 <b>x</b>", "content_format": "text",
                "post_format": "article", "hashtags": ["话题"], "topic_ids": ["7214"],
                "images": ["https://i.example/a.png"], "original": False}
        for protocol in ("web", "app"):
            with self.subTest(protocol=protocol):
                preview = preview_post(copy.deepcopy(spec), public=True, protocol=protocol)
                prepared = prepare_post(copy.deepcopy(spec), draft=False, protocol=protocol)
                self.assertEqual(prepared.build(protocol=protocol), preview["payload"])
                self.assertEqual(preview["spec"]["draft"], False)

    def test_protocol_defaults_to_web_and_rejects_unknown_values(self):
        self.assertEqual(prepare_post({"content": "x"}, draft=True).build(protocol="web")["link_tag"], "28")
        with self.assertRaises(XhhConfigError):
            prepare_post({"content": "x"}, draft=True, protocol="desktop")
        with self.assertRaises(XhhConfigError):
            preview_post({"content": "x"}, protocol="desktop")


if __name__ == "__main__":
    unittest.main()
