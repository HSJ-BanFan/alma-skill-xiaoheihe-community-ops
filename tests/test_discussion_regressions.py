import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import test_tools
import xhh
from browser import Browser
from documents import comment, summary


class DiscussionFixture(test_tools.PlatformFixture):
    def __init__(self):
        super().__init__()
        self.inbox_result = None
        self.required_sort = None
        self.required_limit = None
        self.article = False
        self.created = None
        self.show_created = True
        self.sub_pages = None
        self.root_page = None

    def __call__(self, request, timeout=None):
        data = json.loads(super().__call__(request, timeout).read())
        if data.get("status") != "ok":
            return test_tools.Response(data)
        path = urlsplit(request.full_url).path
        query = parse_qs(urlsplit(request.full_url).query)
        if path == "/bbs/app/user/message" and self.inbox_result is not None:
            data["result"] = self.inbox_result
        elif path == "/bbs/app/link/tree":
            result = data["result"]
            nodes = result["comments"][0]["comment"]
            nodes[0].update(child_num=3 if self.created else 2, has_more=True)
            nodes[1]["replyid"] = "601"
            page = int(query.get("page", ["1"])[0])
            result["total_page"] = max(1, page, self.root_page or 1)
            if self.root_page is not None and page != self.root_page:
                result["comments"] = []
            if "limit" in query and (
                self.required_sort is not None and query.get("sort_filter") != [self.required_sort]
                or self.required_limit is not None and query["limit"] != [str(self.required_limit)]
            ):
                result["comments"] = []
            if self.article:
                link = result["link"]
                link.update(is_article=1, description="fixture excerpt")
                if query.get("x_app") == ["heybox_website"]:
                    link["text"] = [
                        {"type": "text", "text": "fixture article body"},
                        {"type": "img", "url": "https://imgheybox.max-c.com/fixture.png"},
                    ]
                else:
                    link.pop("text", None)
        elif path == "/bbs/app/comment/sub/comments":
            result = data["result"]
            if self.sub_pages is not None:
                selected = self.sub_pages.get(query.get("lastval", [None])[0], {"comments": [], "has_more": False})
                result = {**selected, "comments": [dict(node) for node in selected["comments"]]}
                data["result"] = result
            else:
                result["comments"][0]["replyid"] = "602"
            if self.created and self.show_created:
                result["comments"].append(self.created)
            if self.sub_pages is None and "lastval" in query and query["lastval"] != ["selected"]:
                result["comments"] = []
        elif path == "/bbs/app/comment/create" and data.get("commentid"):
            payload = parse_qs(request.data.decode())
            self.created = {
                "commentid": str(data["commentid"]),
                "userid": self.identity,
                "replyid": payload.get("reply_id", [None])[0],
                "text": payload["text"][0],
            }
        elif path == "/bbs/app/profile/user/link/list":
            data["result"] = {"user": {"id": "303", "username": "fixture author"}, "links": []}
        elif path in ("/bbs/app/profile/follower/list", "/bbs/app/profile/following/list"):
            data["result"] = {"users": [{"id": "404", "username": "fixture relation"}], "has_more": False}
        return test_tools.Response(data)


class DiscussionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.config = self.folder / "fixture.json"
        self.config.write_text(json.dumps({
            "pkey": "fixture-secret-only", "heybox_id": "101", "protocol_mode": "web",
        }), encoding="utf-8")
        self.text = self.folder / "reply.txt"
        self.text.write_text("fixture reply", encoding="utf-8")
        self.fixture = DiscussionFixture()

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with patch("urllib.request.urlopen", self.fixture), patch(
            "paging.read_http", side_effect=lambda request, timeout, **kwargs: self.fixture(request, timeout).read(),
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = xhh.main(["--config", str(self.config), "--data-dir", str(self.folder / "state"), *arguments])
            except SystemExit as error:
                return error.code, {"error": {"code": "argument_error", "message": errors.getvalue()}}
        return code, json.loads(output.getvalue())

    def reply(self, target="603", operation="reply-event", *extra):
        return self.run_cli("reply", "501", "601", target, "--text-file", str(self.text),
                            "--operation-id", operation, "--confirm", *extra)

    def assert_success(self, result):
        code, value = result
        self.assertEqual(code, 0, json.dumps(value, ensure_ascii=False))
        return value

    def sends(self):
        return [call for call in self.fixture.calls if call[1] == "POST"]

    def incoming(self):
        return {"comment_a_id": "603", "comment_b_id": "602", "root_comment_id": "601",
                "linkid": "501", "comment_a_text": "incoming text" * 100, "comment_b_text": "prior text" * 100}

    def test_entries_preserve_text_relations_metadata_and_next_offset(self):
        message = self.incoming()
        self.fixture.inbox_result = {"entries": [message], "message_unread_num": {"comment": 1, "award": 0}}
        value = self.assert_success(self.run_cli("inbox", "--offset", "20", "--limit", "5"))
        self.assertEqual(value["data"]["entries"], [message])
        self.assertEqual(value["data"]["message_unread_num"], {"comment": 1, "award": 0})
        self.assertEqual(value["coverage"]["next_offset"], 21)
        self.assertFalse(value["coverage"]["full_history"])

    def test_empty_entries_is_an_observed_empty_page(self):
        self.fixture.inbox_result = {"entries": [], "message_unread_num": {"comment": 0}}
        value = self.assert_success(self.run_cli("inbox", "--offset", "7"))
        self.assertEqual(value["data"]["entries"], [])
        self.assertEqual(value["coverage"]["next_offset"], 7)

    def test_malformed_entries_does_not_fall_back_to_legacy_messages(self):
        self.fixture.inbox_result = {"entries": None, "messages": []}
        code, value = self.run_cli("inbox")
        self.assertEqual(code, 1, value)
        self.assertEqual(value["error"]["code"], "invalid_inbox_response")

    def test_observed_first_subpage_target_can_be_replied_to(self):
        thread = self.assert_success(self.run_cli("thread", "501", "601", "--around", "603", "--pages", "2"))
        self.assertTrue(thread["data"]["target_observed"])
        self.assertTrue(thread["data"]["ancestry_complete"])
        value = self.assert_success(self.reply())
        self.assertEqual(value["data"]["receipt"]["reply_id"], "603")
        payload = self.sends()[0][2]
        self.assertEqual((payload["root_id"], payload["reply_id"]), (["601"], ["603"]))

    def test_invalid_root_target_and_explicit_cursor_never_send(self):
        for root, target, extra in (("999", "603", []), ("601", "999", []),
                                    ("601", "603", ["--cursor", "wrong-page"])):
            with self.subTest(root=root, target=target, cursor=extra):
                code, value = self.run_cli("reply", "501", root, target, "--text-file", str(self.text),
                                           "--operation-id", "invalid-" + root + target, "--confirm", *extra)
                self.assertEqual(code, 1, value)
        self.assertEqual(self.sends(), [])

    def test_reply_accepts_absolute_page_above_one_hundred(self):
        self.assert_success(self.reply("602", "page-101", "--page", "101"))
        pages = [query["page"] for path, method, payload, query in self.fixture.calls
                 if path == "/bbs/app/link/tree" and "limit" in query]
        self.assertIn(["101"], pages)

    def test_reply_reuses_read_sort_and_page_size(self):
        self.fixture.required_sort, self.fixture.required_limit = "time_desc", 5
        self.assert_success(self.reply("602", "sorted-page", "--page", "2", "--sort", "time_desc", "--limit", "5"))
        queries = [query for path, method, payload, query in self.fixture.calls
                   if path == "/bbs/app/link/tree" and "limit" in query]
        self.assertEqual((queries[-1]["page"], queries[-1]["limit"], queries[-1]["sort_filter"]),
                         (["2"], ["5"], ["time_desc"]))

    def test_parent_entity_ids_are_not_author_uids(self):
        for record in (summary({"id": "501"}), comment({"id": "603"}, "501", "601")):
            with self.subTest(entity=record["id"]):
                self.assertIsNone(record["author"]["uid"])

    def test_explicit_and_nested_author_identity_is_preserved(self):
        self.assertEqual(summary({"id": "501", "userid": "202"})["author"]["uid"], "202")
        self.assertEqual(summary({"id": "501", "user": {"id": "303", "username": "fixture"}})["author"]["uid"], "303")

    def test_public_user_profile_accepts_its_user_id_field(self):
        value = self.assert_success(self.run_cli("user", "303"))
        self.assertEqual((value["data"]["uid"], value["data"]["name"]), ("303", "fixture author"))

    def test_public_user_relations_accept_their_user_id_fields(self):
        for view in ("followers", "following"):
            with self.subTest(view=view):
                value = self.assert_success(self.run_cli("user", "303", "--view", view))
                self.assertEqual(value["data"]["items"][0]["uid"], "404")

    def test_inline_reply_location_matches_the_observed_root_page(self):
        self.fixture.root_page = 3
        value = self.assert_success(self.run_cli("thread", "501", "601", "--around", "602", "--page", "1",
                                                "--pages", "3", "--limit", "7", "--sort", "time_aes"))
        self.assertEqual(value["data"].get("reply_location"),
                         {"page": 3, "limit": 7, "sort": "time_aes", "cursor": None})
        pages = [query["page"] for path, method, payload, query in self.fixture.calls
                 if path == "/bbs/app/link/tree" and "limit" in query]
        self.assertEqual(pages, [["1"], ["2"], ["3"]])
        self.assertEqual(str(value["data"]["reply_location"]["page"]), pages[-1][0])

    def test_reply_location_keeps_target_page_instead_of_final_cursor(self):
        self.fixture.root_page = 2
        self.fixture.sub_pages = {
            None: {"comments": [{"commentid": "603", "replyid": "602", "text": "first"}],
                   "has_more": True, "lastval": "second"},
            "second": {"comments": [{"commentid": "606", "replyid": "603", "text": "target"}],
                       "has_more": True, "lastval": "third"},
            "third": {"comments": [{"commentid": "609", "replyid": "606", "text": "last"}], "has_more": False},
        }
        value = self.assert_success(self.run_cli("thread", "501", "601", "--around", "606", "--page", "2",
                                                "--pages", "4", "--limit", "5", "--sort", "time_desc"))
        location = value["data"].get("reply_location")
        self.assertEqual(location, {"page": 2, "limit": 5, "sort": "time_desc", "cursor": "second"})
        root_queries = [query for path, method, payload, query in self.fixture.calls
                        if path == "/bbs/app/link/tree" and "limit" in query]
        self.assertEqual([query["page"] for query in root_queries], [["2"]])
        self.assertEqual(str(location["page"]), root_queries[0]["page"][0])
        self.assert_success(self.reply("606", "located-target", "--page", str(location["page"]), "--limit",
                                       str(location["limit"]), "--sort", location["sort"], "--cursor", location["cursor"]))
        self.assertEqual(self.sends()[0][2]["reply_id"], ["606"])

    def test_thread_resume_preserves_previously_observed_reply_location(self):
        self.fixture.sub_pages = {
            None: {"comments": [{"commentid": "603", "replyid": "602", "text": "target"}],
                   "has_more": True, "lastval": "selected"},
            "selected": {"comments": [{"commentid": "606", "replyid": "603", "text": "later"}], "has_more": False},
        }
        first = self.assert_success(self.run_cli("thread", "501", "601", "--around", "603", "--pages", "3",
                                                "--requests", "3", "--snapshot", "discussion"))
        self.assertTrue(first["data"]["target_observed"])
        self.assertEqual(first["data"].get("reply_location"),
                         {"page": 1, "limit": 50, "sort": "hot", "cursor": None})
        resumed = self.assert_success(self.run_cli("snapshot", "resume", "discussion"))
        self.assertTrue(resumed["data"]["target_observed"])
        self.assertFalse(resumed["data"]["target_observed_this_read"])
        self.assertEqual(resumed["data"]["reply_location"], first["data"]["reply_location"])

    def test_read_reply_and_readback_loop_reuses_message_operation_id(self):
        self.fixture.article = True
        post = self.assert_success(self.run_cli("post", "501", "--reader", "website"))
        self.assertEqual(post["data"]["body"]["status"], "available")
        self.assertIn("fixture article body", post["data"]["body"]["text"])
        self.assertEqual(len(post["data"]["media"]), 1)
        self.fixture.inbox_result = {"entries": [self.incoming()]}
        for _ in range(2):
            inbox = self.assert_success(self.run_cli("inbox"))
            incoming = inbox["data"]["entries"][0]
            thread = self.assert_success(self.run_cli("thread", incoming["linkid"], incoming["root_comment_id"],
                                                      "--around", incoming["comment_a_id"], "--pages", "2"))
            self.assertTrue(thread["data"]["ancestry_complete"])
            receipt = self.assert_success(self.reply(incoming["comment_a_id"], "incoming-" + incoming["comment_a_id"]))
            reply_id = receipt["data"]["receipt"]["comment_id"]
            visible = self.assert_success(self.run_cli("thread", "501", "601", "--around", reply_id, "--pages", "2"))
            self.assertTrue(visible["data"]["target_observed"])
            node = next(node for node in visible["data"]["items"] if node["id"] == reply_id)
            self.assertEqual((node["reply_to_id"], node["author"]["uid"]), ("603", "101"))
        self.assertEqual(len(self.sends()), 1)

    def test_web_default_reader_returns_article_body_in_one_request(self):
        self.fixture.article = True
        post = self.assert_success(self.run_cli("post", "501", "--requests", "1"))
        self.assertEqual(post["data"]["body"]["status"], "available")
        self.assertIn("fixture article body", post["data"]["body"]["text"])
        self.assertEqual(post["data"]["source"]["reader"], "website")
        self.assertEqual(len(post["data"]["media"]), 1)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_explicit_native_without_app_session_never_uses_web_session(self):
        self.fixture.article = True
        code, value = self.run_cli("post", "501", "--reader", "native")
        self.assertEqual(code, 1)
        self.assertEqual(value["error"]["code"], "app_session_missing")
        self.assertEqual(self.fixture.calls, [])

    def test_app_default_reader_uses_the_native_session(self):
        calls = []
        config = SimpleNamespace(
            protocol_mode="app",
            heybox_id="101",
            timeout=5,
            web_origin="https://www.xiaoheihe.cn",
            user_agent_web="fixture",
        )
        def native(route, query=None):
            calls.append((route, query))
            return {"status": "ok", "result": {"link": {"id": "501", "text": "native body"},
                                               "comments": [], "total_page": 1}}
        client = SimpleNamespace(config=config, transport=SimpleNamespace(signed_request=native))
        data, scope = Browser(client).execute(xhh.parser().parse_args(["post", "501", "--requests", "1"]))
        self.assertEqual(data["body"]["text"], "native body")
        self.assertEqual(data["source"]["protocol"], "app")
        self.assertEqual(len(calls), 1)

    def test_missing_public_readback_does_not_cause_another_send(self):
        self.fixture.show_created = False
        receipt = self.assert_success(self.reply())
        reply_id = receipt["data"]["receipt"]["comment_id"]
        visible = self.assert_success(self.run_cli("thread", "501", "601", "--around", reply_id, "--pages", "2"))
        self.assertFalse(visible["data"]["target_observed"])
        replay = self.assert_success(self.reply())
        self.assertTrue(replay["data"]["replayed"])
        self.assertEqual(len(self.sends()), 1)

    def test_offset_pages_with_new_arrivals_can_be_deduplicated_by_message_identity(self):
        seen, observed = set(), []
        pages = ((0, ["603", "604"]), (2, ["604", "606"]), (0, ["605", "603"]))
        for offset, cids in pages:
            messages = [{**self.incoming(), "comment_a_id": cid, "message_id": "event-" + cid} for cid in cids]
            self.fixture.inbox_result = {"entries": messages}
            inbox = self.assert_success(self.run_cli("inbox", "--offset", str(offset), "--limit", "2"))
            self.assertEqual(inbox["coverage"]["next_offset"], offset + 2)
            for message in inbox["data"]["entries"]:
                key = message["message_id"]
                if key not in seen:
                    seen.add(key)
                    observed.append(message["comment_a_id"])
        self.assertEqual(observed, ["603", "604", "606", "605"])


if __name__ == "__main__":
    unittest.main()
