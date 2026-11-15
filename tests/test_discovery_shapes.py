import copy
import json
import unittest

from test_reader_followup import CommandTests
from browser import game_detail
from paging import decode_resume
from observations import Store


class DiscoveryShapeTests(CommandTests):
    @staticmethod
    def game_info():
        return {
            "steam_appid": 570, "name": "Dota 2",
            "appicon": "https://img.example/dota.png",
            "image": "https://img.example/dota-cover.png",
            "about_the_game": "<p>Game description</p>",
            "platform_infos": [{"key": "steam"}], "has_bbs": 1,
            "topic_detail": {
                "topic_id": "7214", "name": "Dota community",
                "small_pic_url": "https://img.example/topic.png",
                "game": {"app_id": [570]},
            },
            "morelike": [{"steam_appid": 730, "name": "Related game"}],
            "similar_games": [], "publisher_games": [],
            "menu_v2": [{"key": "store", "name": "Store", "type": "web",
                         "protocol": "heybox://store", "value": "steam",
                         "params": {"appid": "570"}}],
        }

    def test_game_info_detail_shape_uses_one_app_get(self):
        def handler(path, query):
            self.assertEqual(path, "/game/get_game_detail")
            self.assertEqual(query, {"appid": "570"})
            return {"result": self.game_info()}

        code, value, clients = self.run_cli(["games", "info", "570", "--requests", "1"], handler)
        self.assertEqual(code, 0, value)
        self.assertEqual(len(clients[0].calls), 1)
        self.assertEqual(value["coverage"]["budget"]["used_requests"], 1)
        self.assertEqual(value["coverage"]["status"], "complete")
        self.assertFalse(value["coverage"]["full_history"])
        self.assertIsNone(value["coverage"]["resume"])
        self.assertEqual(value["coverage"]["gaps"], [])
        data = value["data"]
        self.assertNotIn("items", data)
        self.assertEqual(data["game"]["id"], "570")
        self.assertEqual(data["game"]["steam_app_id"], "570")
        self.assertEqual(data["game"]["description"], "<p>Game description</p>")
        self.assertEqual(data["game"]["icon_url"], "https://img.example/dota.png")
        self.assertEqual(data["game"]["platforms"], ["steam"])
        self.assertEqual(data["game"]["navigation"]["info"],
                         {"command": "games info", "steam_app_id": "570"})
        self.assertEqual(data["game"]["navigation"]["similar_games"],
                         {"command": "games similar", "game_id": "570"})
        self.assertEqual(data["community"], {
            "id": "7214", "name": "Dota community",
            "icon_url": "https://img.example/topic.png", "forum_available": True,
            "game_ids": ["570"], "navigation": {"feed": {"command": "feed", "topic_id": "7214"}},
        })
        self.assertEqual(data["menus"], [{
            "key": "store", "title": "Store", "type": "web",
            "protocol": "heybox://store", "value": "steam", "parameters": {"appid": "570"},
        }])
        self.assertEqual(data["source"]["route"], "/game/get_game_detail")
        self.assertEqual(data["source"]["protocol"], "app")
        self.assertFalse(data["source"]["identity_verified"])
        self.assertNotIn("raw_pages", data)

    def test_game_info_feed_navigation_requires_positive_forum(self):
        for flag, expected in ((True, True), ("1", True), (False, False), ("0", False), (None, None)):
            with self.subTest(flag=flag):
                payload = self.game_info()
                payload["has_bbs"] = flag
                payload["topic_detail"]["has_bbs"] = 1
                payload["topic_detail"]["game"]["app_id"] = 570
                code, value, _ = self.run_cli(["games", "info", "570"], lambda *args: {"result": payload})
                self.assertEqual(code, 0, value)
                community = value["data"]["community"]
                self.assertIs(community["forum_available"], expected)
                feed = {"command": "feed", "topic_id": "7214"} if expected is True else None
                self.assertEqual(community["navigation"]["feed"], feed)
                self.assertEqual(value["data"]["game"]["navigation"]["feed"], feed)
                self.assertEqual(community["game_ids"], ["570"])

    def test_game_info_related_games_deduplicate_and_enrich_all_memberships(self):
        payload = self.game_info()
        payload["morelike"][0]["platform_infos"] = [{"key": "steam"}]
        payload["similar_games"] = [{"appid": "730", "appicon": "https://img.example/related.png"}]
        payload["publisher_games"] = [
            {"app_id": "730", "description": "Publisher metadata"},
            {"appid": "730", "score": 9}, {"appid": "440", "name": "Second game"},
        ]
        code, value, _ = self.run_cli(["games", "info", "570", "--limit", "1"], lambda *args: {"result": payload})
        self.assertEqual(code, 0, value)
        games = value["data"]["related_games"]
        self.assertEqual([item["id"] for item in games], ["730", "440"])
        self.assertEqual(games[0]["relations"], ["morelike", "similar_games", "publisher_games"])
        self.assertEqual(games[0]["name"], "Related game")
        self.assertEqual(games[0]["icon_url"], "https://img.example/related.png")
        self.assertEqual(games[0]["description"], "Publisher metadata")
        self.assertEqual(games[0]["score"], 9)
        self.assertEqual(games[0]["platforms"], ["steam"])
        self.assertEqual(value["data"]["relation_groups"]["publisher_games"],
                         {"status": "observed", "returned": 3, "valid": 3, "invalid": 0})

    def test_game_info_empty_missing_and_invalid_groups_are_distinct(self):
        payload = self.game_info()
        payload["morelike"] = []
        del payload["similar_games"]
        payload["publisher_games"] = {"unexpected": "shape"}
        code, value, _ = self.run_cli(["games", "info", "570"], lambda *args: {"result": payload})
        self.assertEqual(code, 0, value)
        groups = value["data"]["relation_groups"]
        self.assertEqual(groups["morelike"], {"status": "observed_empty", "returned": 0, "valid": 0, "invalid": 0})
        self.assertEqual(groups["similar_games"], {"status": "not_returned", "returned": None, "valid": None, "invalid": None})
        self.assertEqual(groups["publisher_games"], {"status": "observed_with_invalid", "returned": 0, "valid": 0, "invalid": 1})
        self.assertEqual(value["coverage"]["gaps"], ["similar_games_not_returned", "invalid_publisher_games_shape"])
        self.assertEqual(value["coverage"]["status"], "partial")
        self.assertEqual(value["coverage"]["invalid"], 1)
        self.assertEqual(value["coverage"]["excluded"], 1)
        self.assertIsNone(value["coverage"]["resume"])

    def test_game_info_malformed_related_records_preserve_valid_games(self):
        for key in ("morelike", "similar_games", "publisher_games"):
            with self.subTest(key=key):
                payload = self.game_info()
                payload[key] = [None, "invalid", {}, {"name": "missing ID"},
                                {"appid": "440", "platform_infos": 42}, {"appid": "730"}]
                code, value, _ = self.run_cli(["games", "info", "570"], lambda *args: {"result": payload})
                self.assertEqual(code, 0, value)
                self.assertEqual([item["id"] for item in value["data"]["related_games"]], ["730"])
                self.assertEqual(value["data"]["relation_groups"][key],
                                 {"status": "observed_with_invalid", "returned": 6, "valid": 1, "invalid": 5})
                self.assertEqual(value["coverage"]["invalid"], 5)
                self.assertEqual(value["coverage"]["excluded"], 5)
                self.assertEqual(value["coverage"]["status"], "partial")
                self.assertIn(f"invalid_{key}_records", value["coverage"]["gaps"])
                self.assertIsNone(value["error"])

    def test_game_info_missing_and_invalid_community_keep_detail(self):
        for topic in (None, {}, {"topic_id": "bad"}, {"topic_id": 0}, {"id": "7214"}, "bad"):
            with self.subTest(topic=topic):
                payload = self.game_info()
                payload["topic_detail"] = topic
                code, value, _ = self.run_cli(["games", "info", "570"], lambda *args: {"result": payload})
                self.assertEqual(code, 0, value)
                self.assertIsNone(value["data"]["community"])
                self.assertIsNone(value["data"]["game"]["navigation"]["feed"])
                self.assertIn("invalid_community", value["coverage"]["gaps"])
                self.assertEqual(value["coverage"]["status"], "partial")
        payload = self.game_info()
        del payload["topic_detail"]
        code, value, _ = self.run_cli(["games", "info", "570"], lambda *args: {"result": payload})
        self.assertEqual(code, 0, value)
        self.assertIsNone(value["data"]["community"])
        self.assertIn("community_not_returned", value["coverage"]["gaps"])

    def test_game_info_identity_mismatch_or_invalid_identity_fails(self):
        for identities in ({"steam_appid": 730}, {"appid": "730"}, {"steam_appid": 570, "appid": 730},
                           {"steam_appid": None}, {}):
            with self.subTest(identities=identities):
                payload = self.game_info()
                del payload["steam_appid"]
                payload.update(identities)
                code, value, clients = self.run_cli(["games", "info", "570"], lambda *args: {"result": payload})
                self.assertNotEqual(code, 0, value)
                self.assertIn("identity does not match", value["error"]["message"])
                self.assertEqual(len(clients[0].calls), 1)

    def test_game_info_appid_identity_description_precedence_and_pure_adapter(self):
        payload = self.game_info()
        payload["appid"] = payload.pop("steam_appid")
        payload["description"] = "Preferred description"
        original = copy.deepcopy(payload)
        evidence = {"route": "/game/get_game_detail", "protocol": "app"}
        data, scope = game_detail(payload, "570", evidence)
        self.assertEqual(payload, original)
        self.assertEqual(data["source"], evidence)
        self.assertIsNone(data["game"]["steam_app_id"])
        self.assertEqual(data["game"]["description"], "Preferred description")
        self.assertEqual(scope["invalid"], 0)

    def test_game_info_raw_retains_original_response_once(self):
        payload = {"status": "ok", "result": self.game_info(), "extra": "raw-only"}
        code, value, clients = self.run_cli(["games", "info", "570", "--raw"], lambda *args: payload)
        self.assertEqual(code, 0, value)
        self.assertEqual(value["data"]["raw_pages"], [payload])
        self.assertEqual(len(clients[0].calls), 1)
        self.assertNotIn("extra", value["data"]["game"])

    def test_game_info_rejects_page_resume_invalid_targets_and_web_before_fetch(self):
        for options in (["--pages", "2"], ["--resume", "invalid-token"]):
            with self.subTest(options=options):
                code, value, clients = self.run_cli(["games", "info", "570", *options],
                                                   lambda *args: self.fail("unexpected fetch"))
                self.assertNotEqual(code, 0, value)
                self.assertIn("one resource", value["error"]["message"])
                self.assertEqual(clients[0].calls, [])
        for target in ([], ["bad"], ["0"], ["-1"], ["https://example.com"]):
            with self.subTest(target=target):
                code, value, clients = self.run_cli(["games", "info", *target],
                                                   lambda *args: self.fail("unexpected fetch"))
                self.assertNotEqual(code, 0, value)
                self.assertIn("numeric Steam app ID", value["error"]["message"])
                self.assertEqual(clients[0].calls, [])
        self.config.protocol_mode = "web"
        code, value, clients = self.run_cli(["games", "info", "570"], lambda *args: self.fail("unexpected fetch"))
        self.assertNotEqual(code, 0, value)
        self.assertIn("requires App protocol", value["error"]["message"])
        self.assertEqual(clients[0].calls, [])

    def test_game_info_invalid_menus_are_reported_without_losing_detail(self):
        for menu, gap in ((42, "invalid_menu_v2_shape"), ([None], "invalid_menu_v2_records")):
            with self.subTest(menu=menu):
                payload = self.game_info()
                payload["menu_v2"] = menu
                code, value, _ = self.run_cli(["games", "info", "570"], lambda *args: {"result": payload})
                self.assertEqual(code, 0, value)
                self.assertEqual(value["data"]["game"]["id"], "570")
                self.assertEqual(value["data"]["menus"], [])
                self.assertIn(gap, value["coverage"]["gaps"])
                self.assertEqual(value["coverage"]["status"], "partial")

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

    def test_banner_all_communities_is_navigation_not_a_fake_topic(self):
        code, value, _ = self.run_cli(["banners"], lambda *args: {
            "result": {"topic_banner": {
                "top_topics": [{
                    "topic_id": -1,
                    "name": "全部",
                    "protocol": "heybox://{\"path\":\"bbs/topic/list\"}",
                    "small_pic_url": "https://img.example/all.png",
                }],
                "subscribed_topics": [],
            }},
        })
        self.assertEqual(code, 0, value)
        item = value["data"]["items"][0]
        self.assertEqual(item["id"], "top:all")
        self.assertIsNone(item["community"])
        self.assertEqual(item["platform_protocol"], "heybox://{\"path\":\"bbs/topic/list\"}")
        self.assertEqual(item["icon_url"], "https://img.example/all.png")
        self.assertEqual(value["coverage"]["status"], "complete")
        self.assertEqual(value["coverage"]["invalid"], 0)

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

    def test_search_unwraps_typed_info_payloads(self):
        cases = (
            (["search", "query"], [{"type": "link", "info": {"linkid": "123", "title": "post"}}], "id", "123"),
            (["search", "query", "--type", "game"], [{"type": "game", "info": {"appid": "123", "name": "game"}}], "id", "123"),
            (["search", "query", "--type", "user"], [{"type": "user", "info": {
                "user": {"userid": "123", "username": "author"},
            }}], "uid", "123"),
        )
        for arguments, records, identity_key, identity in cases:
            with self.subTest(arguments=arguments):
                code, value, _ = self.run_cli(arguments, lambda *args: {
                    "result": {"items": records, "has_more": False},
                })
                self.assertEqual(code, 0, value)
                self.assertEqual(value["data"]["items"][0][identity_key], identity)
                self.assertEqual(value["coverage"]["invalid"], 0)

    def test_game_search_uses_steam_identity_and_platform_metadata(self):
        code, value, _ = self.run_cli(["search", "query", "--type", "game"], lambda *args: {
            "result": {"items": [{"type": "game", "info": {
                "steam_appid": 123,
                "name": "game",
                "appicon": "https://img.example/icon.png",
                "image": "https://img.example/cover.png",
                "platform_infos": [{"key": "steam"}],
                "price": {"current": "3.5", "discount": 30},
                "is_free": False,
                "chinese_support": True,
                "follow_num": 68,
                "score": 9.2,
                "score_comment": 3,
            }}], "has_more": False},
        })
        self.assertEqual(code, 0, value)
        item = value["data"]["items"][0]
        self.assertEqual(item["id"], "123")
        self.assertEqual(item["icon_url"], "https://img.example/icon.png")
        self.assertEqual(item["cover_url"], "https://img.example/cover.png")
        self.assertEqual(item["platforms"], ["steam"])
        self.assertEqual(item["price"], {"current": "3.5", "discount": 30})
        self.assertFalse(item["is_free"])
        self.assertTrue(item["chinese_support"])
        self.assertEqual(item["followers"], 68)
        self.assertEqual(item["score"], 9.2)
        self.assertEqual(item["score_comment"], 3)
        self.assertEqual(value["coverage"]["invalid"], 0)

    def test_search_rejects_typed_payload_of_the_wrong_kind(self):
        code, value, _ = self.run_cli(["search", "query"], lambda *args: {
            "result": {"items": [
                {"type": "game", "info": {"appid": "123", "name": "game"}},
                {"type": "link", "info": {"linkid": "124", "title": "post"}},
            ], "has_more": False},
        })
        self.assertEqual(code, 0, value)
        self.assertEqual([item["id"] for item in value["data"]["items"]], ["124"])
        self.assertEqual(value["coverage"]["invalid"], 1)
        self.assertEqual(value["coverage"]["status"], "partial")

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
