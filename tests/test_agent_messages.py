import contextlib
import copy
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(Path(__file__).resolve().parent)]
import xhh
from documents import document, comment
from messages import compose, MessageError
from operations import Journal
from test_tools import PlatformFixture, Response
from xhh_sdk.payload import Post
from xhh_sdk.transport import Transport
from sticker_store import register as register_sticker
from emoji_catalog import BUNDLED, save_catalog

try:
    from PIL import Image
except ImportError:
    Image = None


class MessagePlatform(PlatformFixture):
    def __init__(self):
        super().__init__()
        self.posts, self.nodes = {}, {}
        self.next_post, self.next_comment = 801, 701
        self.change_body, self.gap_body, self.wrong_author, self.wrong_reply = False, False, False, False

    def __call__(self, request, timeout=None):
        path = urlsplit(request.full_url).path
        if request.get_method() == "PUT":
            return super().__call__(request, timeout)
        payload = parse_qs(request.data.decode(), keep_blank_values=True) if request.data else {}
        query = parse_qs(urlsplit(request.full_url).query)
        if path == "/account/info":
            return Response({"status": "ok", "result": {"userid": "101"}})
        if path == "/bbs/app/api/link/post":
            self.calls.append((path, request.get_method(), payload, query))
            if self.fail_send:
                raise TimeoutError("fixture after dispatch")
            identifier = str(self.next_post)
            self.next_post += 1
            self.posts[identifier] = {"id": identifier, "userid": "101", "title": payload["title"][0],
                                      "link_tag": payload["link_tag"][0], "text": payload["text"][0],
                                      "is_full_text": 1}
            return Response({"status": "ok", "result": {"link_id": identifier}})
        if path == "/bbs/app/comment/create":
            self.calls.append((path, request.get_method(), payload, query))
            if self.fail_send:
                raise TimeoutError("fixture after dispatch")
            identifier = str(self.next_comment)
            self.next_comment += 1
            self.nodes[identifier] = {"commentid": identifier, "userid": "101", "text": payload["text"][0],
                                      "replyid": payload.get("reply_id", [None])[0],
                                      "root_id": payload.get("root_id", [identifier])[0],
                                      "imgs": payload.get("imgs", [""])[0] or []}
            return Response({"status": "ok", "commentid": identifier})
        if path == "/bbs/app/link/tree":
            self.calls.append((path, request.get_method(), payload, query))
            identifier = query["link_id"][0]
            link = dict(self.posts.get(identifier, {"id": identifier, "userid": "202", "text": "fixture root", "is_full_text": 1}))
            if self.change_body:
                link["text"] = "changed by fixture"
            if self.gap_body:
                link["text"] = json.dumps([{"type": "text", "text": "fixture"}, {"type": "future"}])
            floors = [{"comment": [{"commentid": "601", "userid": "202", "text": "root"},
                                     {"commentid": "602", "userid": "303", "text": "target", "replyid": "601"}]}]
            for node in self.nodes.values():
                node = dict(node)
                if self.wrong_author:
                    node["userid"] = "999"
                if self.wrong_reply and node["replyid"]:
                    node["replyid"] = "601"
                if node["root_id"] == node["commentid"]:
                    floors.append({"comment": [node]})
                else:
                    floors[0]["comment"].append(node)
            return Response({"status": "ok", "result": {"link": link, "comments": floors, "total_page": 1}})
        if path == "/bbs/app/comment/sub/comments":
            self.calls.append((path, request.get_method(), payload, query))
            nodes = [dict(node) for node in self.nodes.values() if node["root_id"] == query["root_comment_id"][0]]
            if self.wrong_reply:
                for node in nodes:
                    node["replyid"] = "601"
            return Response({"status": "ok", "result": {"comments": nodes, "has_more": False}})
        return super().__call__(request, timeout)


class AgentMessageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.data = self.folder / "data"
        self.config = self.folder / "fixture.json"
        self.config.write_text(json.dumps({"pkey": "fixture-secret-only", "heybox_id": "101", "protocol_mode": "web"}), encoding="utf-8")
        self.platform = MessagePlatform()
        self.platform.posts["501"] = {"id": "501", "userid": "101",
                                      "text": "fixture root", "is_full_text": 1}

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("urllib.request.urlopen", self.platform), patch("paging.read_http", side_effect=lambda request, timeout, **kwargs: self.platform(request, timeout).read()):
            code = xhh.main(["--config", str(self.config), "--data-dir", str(self.data), *args])
        return code, json.loads(output.getvalue())

    def prepare(self, kind="post", content=None, **extra):
        spec = {"kind": kind, "content": content or [{"type": "native_emoji", "id": "cube:32"}], **extra}
        if kind != "post":
            spec["target"] = {"link_id": "501", **({"root_id": "601", "reply_id": "602"} if kind == "reply" else {})}
        return compose(spec, self.data)

    def send(self, prepared, operation="fixture-message", *extra):
        return self.cli("send", "--prepared-json", json.dumps(prepared, ensure_ascii=False), "--operation-id", operation, "--confirm", *extra)

    def use_app_config(self):
        self.config.write_text(json.dumps({"pkey": "fixture-secret-only", "heybox_id": "101",
                                           "protocol_mode": "app"}), encoding="utf-8")

    def signed_send(self, prepared, records, operation="fixture-message"):
        def signed(transport, route, *, query=None, payload=None, extra_cookies=None):
            records.append((route, payload))
            if route == "/account/info":
                return {"status": "ok", "result": {"userid": "101"}}
            return {"status": "ok", "result": {"link_id": "801"}}
        output = io.StringIO()
        with patch.object(Transport, "signed_request", signed), \
             contextlib.redirect_stdout(output), \
             patch("urllib.request.urlopen", self.platform), \
             patch("paging.read_http", side_effect=lambda request, timeout, **kwargs: self.platform(request, timeout).read()):
            code = xhh.main(["--config", str(self.config), "--data-dir", str(self.data),
                             "send", "--prepared-json", json.dumps(prepared, ensure_ascii=False),
                             "--operation-id", operation, "--confirm"])
        return code, json.loads(output.getvalue())

    def test_offline_compose_and_native_span_reading_preserve_identity_and_position(self):
        prepared = self.prepare(content=[{"type": "text", "text": "before "}, {"type": "native_emoji", "id": "cube:32"}, {"type": "text", "text": " after"}])
        self.assertFalse(self.data.exists())
        markup = prepared["preview"]["blocks"][0]["text"]
        self.assertIn('data-emoji="cube_笑cry"', markup)
        body = document({"id": 1, "text": markup}, {})["body"]
        self.assertEqual(body["text"], "before [cube_笑cry] after")
        self.assertEqual([node["type"] for node in body["blocks"]], ["text", "native_emoji", "text"])
        self.assertEqual(comment({"commentid": 1, "text": markup}, "1", "1")["text"], body["text"])

    def test_native_only_post_sends_once_and_verifies_body_but_not_public_rendering(self):
        prepared = self.prepare()
        code, receipt = self.send(prepared)
        self.assertEqual(code, 0, receipt)
        self.assertTrue(receipt["data"]["receipt"]["draft"])
        code, replay = self.send(prepared)
        self.assertEqual(code, 0, replay)
        self.assertTrue(replay["data"]["replayed"])
        self.assertEqual(sum(call[0] == "/bbs/app/api/link/post" for call in self.platform.calls), 1)
        code, checked = self.cli("verify", "fixture-message")
        self.assertEqual(code, 0, checked)
        self.assertEqual(checked["data"]["status"], "matched", checked)
        self.assertEqual(checked["data"]["observed_emoji_ids"], ["cube:32"])
        self.assertEqual(checked["data"]["native_render"]["status"], "unknown")
        self.assertEqual(checked["data"]["public_visibility"], "not_checked")

    def test_prepared_change_or_protocol_mismatch_cannot_send(self):
        prepared = self.prepare()
        prepared["spec"]["content"][0]["id"] = "cube:43"
        code, value = self.send(prepared)
        self.assertEqual(code, 1)
        self.assertEqual(value["error"]["code"], "prepared_message_changed")
        app = compose({"kind": "post", "content": [{"type": "native_emoji", "id": "cube:32"}]}, self.data, protocol="app")
        code, value = self.send(app)
        self.assertEqual(value["error"]["code"], "prepared_protocol_mismatch")
        self.assertFalse(any(call[0] == "/bbs/app/api/link/post" for call in self.platform.calls))

    def test_unknown_emoji_and_unconfirmed_send_do_not_write(self):
        with self.assertRaises(MessageError):
            self.prepare(content=[{"type": "native_emoji", "id": "cube:999"}])
        code, value = self.cli("send", "--prepared-json", json.dumps(self.prepare()), "--operation-id", "missing-confirm")
        self.assertEqual(code, 1)
        self.assertEqual(value["error"]["code"], "confirmation_required")
        self.assertFalse(self.platform.calls)
        self.assertFalse(self.data.exists())

    def test_comment_and_reply_refuse_a_post_owned_by_another_account(self):
        self.platform.posts["501"]["userid"] = "202"
        for kind in ("comment", "reply"):
            with self.subTest(kind=kind):
                code, value = self.send(self.prepare(kind), "foreign-" + kind)
                self.assertEqual(code, 1)
                self.assertIn("configured account's own posts", value["error"]["message"])
        self.assertFalse(any(call[0] == "/bbs/app/comment/create"
                             for call in self.platform.calls))

    def test_uncertain_delivery_retains_context_and_never_resends(self):
        self.platform.fail_send = True
        prepared = self.prepare()
        self.assertEqual(self.send(prepared)[0], 1)
        operation = Journal(self.data).read("101", "fixture-message")
        self.assertEqual(operation["status"], "uncertain")
        self.assertEqual(operation["context"]["expected"]["emoji_ids"], ["cube:32"])
        self.assertEqual(self.send(prepared)[1]["error"]["code"], "operation_pending_or_uncertain")
        self.assertEqual(sum(call[0] == "/bbs/app/api/link/post" for call in self.platform.calls), 1)
        self.assertEqual(self.cli("verify", "fixture-message")[1]["data"]["status"], "unverified")

    def test_readback_change_and_gaps_are_not_a_pass(self):
        self.assertEqual(self.send(self.prepare())[0], 0)
        self.platform.change_body = True
        self.assertEqual(self.cli("verify", "fixture-message")[1]["data"]["status"], "mismatch")
        self.platform.change_body, self.platform.gap_body = False, True
        checked = self.cli("verify", "fixture-message")[1]["data"]
        self.assertEqual(checked["status"], "unverified")
        self.assertEqual(checked["reason"], "readback_body_has_gaps")

    def test_reply_has_exact_target_and_native_shortcode(self):
        code, value = self.send(self.prepare("reply"))
        self.assertEqual(code, 0, value)
        sent = next(call[2] for call in self.platform.calls if call[0] == "/bbs/app/comment/create")
        self.assertEqual((sent["root_id"], sent["reply_id"], sent["text"]), (["601"], ["602"], ["[cube_笑cry]"]))
        checked = self.cli("verify", "fixture-message")[1]["data"]
        self.assertEqual(checked["status"], "matched", checked)
        self.platform.wrong_reply = True
        self.assertEqual(self.cli("verify", "fixture-message")[1]["data"]["status"], "mismatch")

    def test_legacy_journal_migration_preserves_receipts(self):
        self.data.mkdir()
        connection = sqlite3.connect(self.data / "operations.sqlite3")
        try:
            connection.execute("CREATE TABLE operations (uid TEXT, operation_id TEXT, fingerprint TEXT, kind TEXT, status TEXT, receipt TEXT, updated_at REAL, PRIMARY KEY(uid,operation_id))")
            connection.execute("INSERT INTO operations VALUES ('101','old','hash','comment','returned','{}',1)")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(Journal(self.data).read("101", "old")["receipt"], {})
        self.assertEqual(self.cli("verify", "old")[1]["data"]["status"], "unverified")

    def test_sdk_native_span_count_and_image_only_payload(self):
        span = '<span data-emoji="cube_笑cry" class="hb-emoji hb-emoji-cube hb-emoji-cube_32"></span>'
        payload = Post(content=span, content_format="html", post_format="article", original=False).build()
        self.assertEqual(payload["desc"], "[cube_笑cry]")
        picture = Post(content="", content_format="text", images=["https://cdn.max-c.com/fixture.png"],
                       original=False).build(protocol="web")
        self.assertEqual(json.loads(picture["text"]), [{"type": "text", "text": ""},
                                                       {"type": "img", "url": "https://cdn.max-c.com/fixture.png"}])
        self.assertEqual(picture["words_count"], "0")

    def test_public_post_uses_the_explicit_mode_and_community(self):
        prepared = self.prepare(topic_ids=["111"])
        code, value = self.send(prepared, "public-native", "--public")
        self.assertEqual(code, 0, value)
        self.assertFalse(value["data"]["receipt"]["draft"])
        payload = next(call[2] for call in self.platform.calls if call[0] == "/bbs/app/api/link/post")
        self.assertEqual(payload["draft"], ["0"])
        self.assertEqual(payload["topic_ids"], ["111"])
        self.assertNotIn("original", payload)

    def test_wrong_readback_author_is_not_a_match(self):
        self.assertEqual(self.send(self.prepare("comment"))[0], 0)
        self.platform.wrong_author = True
        checked = self.cli("verify", "fixture-message")[1]["data"]
        self.assertEqual(checked["status"], "mismatch")
        self.assertEqual(checked["reason"], "readback_author_not_verified")

    @unittest.skipIf(Image is None, "Pillow is optional")
    def test_image_sticker_is_uploaded_once_and_preserves_mixed_post_order(self):
        image_file = self.folder / "sticker.png"
        with Image.new("RGB", (20, 10), "blue") as image:
            image.save(image_file)
        sticker = register_sticker(image_file, self.data, caption="synthetic")
        prepared = self.prepare(post_format="article", content=[{"type": "text", "text": "before "}, {"type": "native_emoji", "id": "cube:32"},
                                         {"type": "image_sticker", "id": sticker["id"]}, {"type": "text", "text": "after"}])
        code, value = self.send(prepared)
        self.assertEqual(code, 0, value)
        code, checked = self.cli("verify", "fixture-message")
        self.assertEqual(checked["data"]["status"], "matched", checked)
        self.assertTrue(checked["data"]["checks"]["content_order"])
        self.assertEqual(self.send(prepared)[0], 0)
        self.assertEqual(sum(call[1] == "PUT" for call in self.platform.calls), 1)
        stored = self.platform.posts["801"]
        blocks = json.loads(stored["text"])
        blocks.insert(0, blocks.pop(1))
        stored["text"] = json.dumps(blocks)
        checked = self.cli("verify", "fixture-message")[1]["data"]
        self.assertEqual(checked["status"], "mismatch")
        self.assertFalse(checked["checks"]["content_order"])

    @unittest.skipIf(Image is None, "Pillow is optional")
    def test_image_sticker_usage_rejects_reply_and_mid_comment_placement(self):
        image_file = self.folder / "sticker.png"
        with Image.new("RGB", (20, 10), "blue") as image:
            image.save(image_file)
        sticker = register_sticker(image_file, self.data)
        content = [{"type": "text", "text": "before"}, {"type": "image_sticker", "id": sticker["id"]}, {"type": "text", "text": "after"}]
        with self.assertRaises(MessageError):
            self.prepare("comment", content=content)
        with self.assertRaises(MessageError):
            self.prepare("reply", content=content)

    def test_body_only_text_projection_retains_native_shortcode(self):
        span = '<span data-emoji="cube_笑cry" class="hb-emoji hb-emoji-cube hb-emoji-cube_32"></span>'
        self.platform.posts["801"] = {"id": "801", "userid": "101", "text": span, "is_full_text": 1}
        code, value = self.cli("post", "801", "--section", "body", "--body-format", "text")
        self.assertEqual(code, 0, value)
        self.assertEqual(value["data"]["body"]["text"], "[cube_笑cry]")
        self.assertNotIn("blocks", value["data"]["body"])

    def test_verify_uses_the_explicit_updated_catalog_directory(self):
        snapshot = json.loads(BUNDLED.read_text(encoding="utf-8"))
        snapshot["catalog_version"] = "fixture-updated-catalog"
        item = next(item for item in snapshot["items"] if item["id"] == "cube:43")
        item["name"], item["shortcode"] = "更新叹气", "[cube_更新叹气]"
        save_catalog(snapshot, self.data)
        prepared = self.prepare(content=[{"type": "native_emoji", "id": "cube:43"}])
        self.assertEqual(self.send(prepared)[0], 0)
        checked = self.cli("verify", "fixture-message")[1]["data"]
        self.assertEqual(checked["status"], "matched", checked)

    def test_css_or_selected_material_change_invalidates_preparation(self):
        prepared = self.prepare(content=[{"type": "native_emoji", "id": "cube:43"}])
        snapshot = json.loads(BUNDLED.read_text(encoding="utf-8"))
        snapshot["source"]["css_sha256"] = "a" * 64
        save_catalog(snapshot, self.data)
        changed = self.prepare(content=[{"type": "native_emoji", "id": "cube:43"}])
        self.assertNotEqual(prepared["fingerprint"], changed["fingerprint"])
        code, result = self.send(prepared)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "prepared_message_changed")
        snapshot = json.loads(BUNDLED.read_text(encoding="utf-8"))
        first = next(item for item in snapshot["items"] if item["id"] == "cube:43")
        second = next(item for item in snapshot["items"] if item["id"] == "cube:44")
        first["sprite"], second["sprite"] = copy.deepcopy(second["sprite"]), copy.deepcopy(first["sprite"])
        save_catalog(snapshot, self.data)
        changed = self.prepare(content=[{"type": "native_emoji", "id": "cube:43"}])
        self.assertNotEqual(prepared["fingerprint"], changed["fingerprint"])

    def test_typed_reply_records_interaction_facts_and_digest_only_context(self):
        private_text = "fixture message body should not persist in interaction metadata"
        prepared = self.prepare("reply", content=[{"type": "text", "text": private_text}, {"type": "native_emoji", "id": "cube:32"}])
        code, result = self.send(prepared)
        self.assertEqual(code, 0, result)
        code, history = self.cli("interaction", "history", "501")
        self.assertEqual(code, 0, history)
        self.assertTrue(any(item.get("id") == "701" for item in history["data"]["items"]), history)
        operation = Journal(self.data).read("101", "fixture-message")
        self.assertEqual(operation["context"]["target"]["reply_id"], "602")
        self.assertEqual(operation["receipt"]["expected"]["emoji_ids"], ["cube:32"])
        connection = sqlite3.connect(self.data / "operations.sqlite3")
        try:
            self.assertEqual(len(connection.execute("PRAGMA table_info(operations)").fetchall()), 7)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertNotIn(private_text, "\n".join(connection.iterdump()))
            self.assertIsNotNone(connection.execute("SELECT submission_fingerprint FROM interaction_write_context WHERE operation_id='fixture-message'").fetchone()[0])
        finally:
            connection.close()

    def test_app_composed_post_sends_the_previewed_native_spans(self):
        self.use_app_config()
        prepared = compose({"kind": "post", "post_format": "image_text",
                            "content": [{"type": "text", "text": "before "},
                                        {"type": "native_emoji", "id": "cube:32"}]},
                           self.data, protocol="app")
        records = []
        code, result = self.signed_send(prepared, records)
        self.assertEqual(code, 0, result)
        route, wire = next((route, payload) for route, payload in records if route == "/bbs/app/api/link/post")
        blocks = json.loads(wire["text"])
        self.assertEqual(blocks, prepared["preview"]["blocks"])
        self.assertEqual(blocks[0]["type"], "html")
        self.assertIn('data-emoji="cube_笑cry"', blocks[0]["text"])
        self.assertEqual((wire["link_tag"], wire["post_type"]), ("1", "1"))
        for absent in ("edit", "schedule_ts", "view_limit"):
            self.assertNotIn(absent, wire)
        self.assertIn("/account/info", [route for route, _ in records])

    def test_app_article_visibility_cover_and_declaration_reach_the_legacy_wire(self):
        self.use_app_config()
        cover = "https://imgheybox.max-c.com/cover.png"
        prepared = compose({"kind": "post", "post_format": "article", "visibility": "0",
                            "cover_url": cover, "original": False,
                            "content": [{"type": "text", "text": "body"}]}, self.data, protocol="app")
        records = []
        code, result = self.signed_send(prepared, records)
        self.assertEqual(code, 0, result)
        wire = next(payload for route, payload in records if route == "/bbs/app/api/link/post")
        self.assertEqual((wire["link_tag"], wire["post_type"], wire["view_limit"], wire["thumb"]),
                         ("1", "3", "0", cover))
        self.assertNotIn("original", wire)
        self.assertNotIn("declaration", wire)

    def test_app_compose_refuses_creator_only_fields_and_dynamic_visibility(self):
        for values in ({"authorship": "original"}, {"declaration": 1, "authorship": "repost"},
                       {"source": "https://example.com/source"}, {"visibility": "1"}):
            with self.subTest(values=values), self.assertRaises(MessageError):
                compose({"kind": "post", "content": [{"type": "text", "text": "x"}], **values},
                        self.data, protocol="app")


    def test_post_readback_matches_the_expected_author_as_well_as_body(self):
        self.assertEqual(self.send(self.prepare())[0], 0)
        post = self.platform.posts["801"]
        post["userid"] = "999"
        checked = self.cli("verify", "fixture-message")[1]["data"]
        self.assertEqual(checked["status"], "mismatch")
        self.assertEqual(checked["reason"], "readback_author_not_verified")
        post.pop("userid")
        checked = self.cli("verify", "fixture-message")[1]["data"]
        self.assertEqual(checked["status"], "unverified")
        self.assertEqual(checked["reason"], "readback_author_not_observed")


if __name__ == "__main__":
    unittest.main()
