import json
import unittest

from test_reader_followup import CommandTests
from paging import decode_resume
from observations import Store


class DiscoveryShapeTests(CommandTests):
    @staticmethod
    def story(identity):
        return {
            "card_id_info": {"item_id": identity},
            "card_type": "video",
            "h_src": "tracking-source",
            "feedback_info": {"tracking": "feedback"},
            "link_card_info": {
                "title": "<b>Story title</b>",
                "description": "<p>Story description</p>",
                "user": {"userid": "202", "username": "author", "avatar": "https://img.example/avatar.png"},
                "comment_num": 4, "link_award_num": 2, "favour_count": 3, "click": 10,
                "cover": "https://img.example/cover.png",
                "imgs": [{"url": "https://img.example/image.png", "width": 100}],
                "video_info": {"url": "https://video.example/story.mp4", "duration": 12,
                               "width": 640, "height": 360, "h_src": "video-tracking"},
                "feedback_info": {"nested": "tracking"},
            },
        }

    def test_story_cards_are_clean_post_like_items(self):
        code, value, _ = self.run_cli(["stories"], lambda *args: {
            "result": {"list": [self.story("123")], "has_more": False},
        })
        self.assertEqual(code, 0, value)
        item = value["data"]["items"][0]
        self.assertEqual(item["id"], "123")
        self.assertEqual(item["url"], "https://www.xiaoheihe.cn/app/bbs/link/123")
        self.assertEqual(item["title"], "Story title")
        self.assertEqual(item["excerpt"], "Story description")
        self.assertEqual(item["description"], "Story description")
        self.assertEqual(item["author"]["uid"], "202")
        self.assertEqual(item["author"]["name"], "author")
        self.assertEqual(item["statistics"], {"comments": 4, "charges": 2, "favourites": 3, "views": 10})
        self.assertEqual(item["cover_url"], "https://img.example/cover.png")
        self.assertEqual(item["media"][0]["url"], "https://img.example/image.png")
        self.assertEqual(item["video"]["url"], "https://video.example/story.mp4")
        self.assertEqual(item["video"]["duration"], 12)
        self.assertTrue(item["has_video"])
        self.assertEqual(item["type"], "story")
        self.assertEqual(item["provenance"]["identity"], "card_id_info.item_id")
        self.assertNotIn("h_src", json.dumps(value["data"]))
        self.assertNotIn("feedback_info", json.dumps(value["data"]))

    def test_story_limit_replays_same_page_then_completes(self):
        def handler(path, query):
            self.assertEqual(path, "/bbs/app/story_mode/list")
            return {"result": {"list": [self.story(str(index)) for index in range(101, 111)],
                               "has_more": False}}

        arguments = ["stories", "--limit", "5"]
        code, first, clients = self.run_cli(arguments, handler)
        self.assertEqual(code, 0, first)
        self.assertTrue(first["ok"])
        self.assertEqual([item["id"] for item in first["data"]["items"]], [str(index) for index in range(101, 106)])
        self.assertEqual(first["coverage"]["status"], "partial")
        self.assertEqual(first["coverage"]["stop_reason"], "item_budget")
        self.assertEqual(first["coverage"]["returned_total"], 10)
        token = decode_resume(first["coverage"]["resume"], "stories", "101")
        self.assertEqual(token["position"], 0)
        self.assertEqual(token["item_window"], {"position": 0, "consumed": ["post:" + str(index) for index in range(101, 106)]})
        self.assertIsNone(first["error"])
        self.assertEqual(clients[0].calls[0][1]["offset"], "0")
        code, second, clients = self.run_cli(arguments + ["--resume", first["coverage"]["resume"]], handler)
        self.assertEqual(code, 0, second)
        self.assertEqual([item["id"] for item in second["data"]["items"]], [str(index) for index in range(106, 111)])
        self.assertEqual(second["coverage"]["status"], "complete")
        self.assertIsNone(second["coverage"]["resume"])
        self.assertEqual(clients[0].calls[0][1]["offset"], "0")

    def test_story_nested_stats_and_video_url_use_card_identity(self):
        record = self.story("123")
        card = record["link_card_info"]
        card["linkid"] = "999"
        card["stat"] = {key: card.pop(key) for key in ("comment_num", "link_award_num", "favour_count", "click")}
        card["stat"]["comment_num"] = 0
        card["user_info"] = card.pop("user")
        card["video_info"]["video_url"] = card["video_info"].pop("url")
        code, value, _ = self.run_cli(["stories"], lambda *args: {
            "result": {"list": [record], "has_more": False},
        })
        self.assertEqual(code, 0, value)
        item = value["data"]["items"][0]
        self.assertEqual(item["id"], "123")
        self.assertEqual(item["statistics"]["comments"], 0)
        self.assertEqual(item["statistics"]["views"], 10)
        self.assertEqual(item["author"]["uid"], "202")
        self.assertEqual(item["video"]["url"], "https://video.example/story.mp4")

    def test_story_limit_without_completion_marker_still_replays_same_page(self):
        code, value, _ = self.run_cli(["stories", "--limit", "5"], lambda *args: {
            "result": {"list": [self.story(str(index)) for index in range(101, 111)]},
        })
        self.assertEqual(code, 0, value)
        self.assertEqual(value["coverage"]["status"], "partial")
        self.assertEqual(len(value["data"]["items"]), 5)
        token = decode_resume(value["coverage"]["resume"], "stories", "101")
        self.assertEqual(token["position"], 0)
        self.assertEqual(token["item_window"]["position"], 0)
        self.assertIsNone(value["error"])

    def test_story_without_card_identity_is_excluded_with_a_gap(self):
        missing = self.story("123")
        missing["card_id_info"] = {}
        missing["link_card_info"]["linkid"] = "999"
        code, value, _ = self.run_cli(["stories"], lambda *args: {
            "result": {"list": [missing, self.story("124")], "has_more": False},
        })
        self.assertEqual(code, 0, value)
        self.assertEqual([item["id"] for item in value["data"]["items"]], ["124"])
        self.assertEqual(value["coverage"]["invalid"], 1)
        self.assertEqual(value["coverage"]["status"], "partial")
        self.assertIn("invalid_story_records", value["coverage"]["gaps"])

    def test_banner_groups_preserve_both_placements_for_same_community(self):
        code, value, _ = self.run_cli(["banners", "--snapshot", "placements"], lambda *args: {
            "result": {"topic_banner": {
                "top_topics": [{"topic_id": "7214", "topic_name": "community"}],
                "subscribed_topics": [{"topic_id": "7214", "name": "community"}],
            }},
        })
        self.assertEqual(code, 0, value)
        self.assertEqual(value["coverage"]["status"], "complete")
        self.assertEqual([item["placement"] for item in value["data"]["items"]], ["top", "subscribed"])
        for item in value["data"]["items"]:
            self.assertEqual(item["community"], {"id": "7214", "name": "community"})
        self.assertEqual([item["id"] for item in Store(self.root).get("101", "placements")["data"]["items"]],
                         ["top:7214", "subscribed:7214"])

    def test_empty_banner_groups_are_successful(self):
        for top, subscribed in (([], []), ([{"topic_id": "1", "name": "top"}], []),
                                ([], [{"topic_id": "2", "name": "subscribed"}])):
            with self.subTest(top=top, subscribed=subscribed):
                code, value, _ = self.run_cli(["banners"], lambda *args: {
                    "result": {"topic_banner": {"top_topics": top, "subscribed_topics": subscribed}},
                })
                self.assertEqual(code, 0, value)
                self.assertEqual(len(value["data"]["items"]), len(top) + len(subscribed))

    def test_empty_recommendations_have_explicit_gap(self):
        code, value, _ = self.run_cli(["recommended-users"], lambda *args: {
            "result": {"recommend_list": [], "rec_users": [], "recommend_nums": 0},
        })
        self.assertEqual(code, 0, value)
        self.assertTrue(value["ok"])
        self.assertEqual(value["data"]["items"], [])
        self.assertEqual(value["coverage"]["status"], "complete")
        self.assertIn("recommendations_empty", value["coverage"]["gaps"])
        self.assertIsNone(value["coverage"]["resume"])
        self.assertIsNone(value["error"])

    def test_recommendation_alternatives_normalize_author_shape(self):
        for key in ("recommend_list", "rec_users"):
            with self.subTest(key=key):
                payload = {"recommend_list": [], "rec_users": [], "has_more": False,
                           key: [{"user": {"userid": "202", "username": "author", "avatar": "https://img.example/avatar.png"}}]}
                code, value, _ = self.run_cli(["recommended-users"], lambda *args: {"result": payload})
                self.assertEqual(code, 0, value)
                self.assertEqual(value["data"]["items"], [{"uid": "202", "name": "author", "avatar_url": "https://img.example/avatar.png"}])

    def test_mixed_search_records_keep_valid_items_and_report_gaps(self):
        cases = (
            (["search", "query", "--community", "7214", "--min-comments", "2"], "links",
             [{"linkid": "123", "comment_num": 3}, {"title": "missing ID"}, {"linkid": "124", "comment_num": 0}], "id", "123", 1, 2),
            (["search", "query", "--type", "game"], "games",
             [{"game": {"appid": "123", "name": "game"}}, {"game": {"name": "missing ID"}}], "id", "123", 1, 1),
            (["games", "search", "query"], "games",
             [{"appid": "123", "name": "game"}, {"name": "missing ID"}], "id", "123", 1, 1),
            (["search", "query", "--type", "user"], "users",
             [{"user": {"userid": "123", "username": "author"}}, {"user": {"username": "missing ID"}}], "uid", "123", 1, 1),
        )
        for arguments, key, records, identity_key, identity, invalid, excluded in cases:
            with self.subTest(arguments=arguments):
                def handler(path, query):
                    if "--community" in arguments:
                        self.assertEqual(query["topic_id"], "7214")
                    return {"result": {key: records, "has_more": False}}

                code, value, _ = self.run_cli(arguments, handler)
                self.assertEqual(code, 0, value)
                self.assertTrue(value["ok"])
                self.assertEqual([item[identity_key] for item in value["data"]["items"]], [identity])
                self.assertEqual(value["coverage"]["status"], "partial")
                self.assertEqual(value["coverage"]["invalid"], invalid)
                self.assertEqual(value["coverage"]["excluded"], excluded)
                self.assertTrue(value["coverage"]["gaps"])
                self.assertIsNone(value["coverage"]["resume"])
                self.assertIsNone(value["error"])

    def test_invalid_counts_and_gaps_survive_later_clean_search_pages(self):
        def handler(path, query):
            if query["offset"] == "0":
                return {"result": {"search_result": [{"linkid": "123"}, {"title": "missing ID"}], "has_more": True}}
            self.assertEqual(query["offset"], "2")
            return {"result": {"search_result": [{"linkid": "124"}], "has_more": False}}

        code, value, _ = self.run_cli(["search", "query", "--community", "7214", "--pages", "2"], handler)
        self.assertEqual(code, 0, value)
        self.assertEqual([item["id"] for item in value["data"]["items"]], ["123", "124"])
        self.assertEqual(value["coverage"]["status"], "partial")
        self.assertEqual(value["coverage"]["invalid"], 1)
        self.assertEqual(value["coverage"]["excluded"], 1)
        self.assertEqual(value["coverage"]["returned_total"], 3)
        self.assertIn("invalid_link_records", value["coverage"]["gaps"])

    def test_all_invalid_user_search_is_partial_empty(self):
        code, value, _ = self.run_cli(["search", "query", "--type", "user"], lambda *args: {
            "result": {"search_result": [{"user": {"username": "missing"}}], "has_more": False},
        })
        self.assertEqual(code, 0, value)
        self.assertEqual(value["data"]["items"], [])
        self.assertEqual(value["coverage"]["status"], "partial")
        self.assertEqual(value["coverage"]["invalid"], 1)

    def test_legacy_discovery_shapes_still_normalize(self):
        cases = (
            (["stories"], {"links": [{"linkid": "123", "title": "legacy"}], "has_more": False}, "id"),
            (["recommended-users"], {"users": [{"userid": "123", "username": "legacy"}], "has_more": False}, "uid"),
            (["search", "query", "--type", "game"], {"search_result": [{"game": {"appid": "123", "name": "legacy"}}], "has_more": False}, "id"),
        )
        for arguments, result, identity_key in cases:
            with self.subTest(arguments=arguments):
                code, value, _ = self.run_cli(arguments, lambda *args: {"result": result})
                self.assertEqual(code, 0, value)
                self.assertEqual(value["data"]["items"][0][identity_key], "123")


if __name__ == "__main__":
    unittest.main()
