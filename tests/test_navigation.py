import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import xhh
from browser import Browser
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import XhhConfigError


class Fixture:
    def __init__(self):
        self.config = XhhConfig(pkey="fixture", heybox_id="101", protocol_mode="web")
        self.transport = self
        self.wrong_user = False
    def signed_request(self, route, query=None):
        if route.endswith("user/link/list"):
            return {"post_links": [{"linkid": 11, "title": "post", "user": {"userid": 202}}],
                    "user": {"userid": "999" if self.wrong_user else query["userid"], "username": "author",
                             "fan_num": 4, "post_draft_num": 3}}
        if route.endswith("folders"):
            return {"result": {"folders": [{"folder_id": "0", "name": "default", "count": 1}]}}
        if route.endswith("links"):
            return {"result": {"links": [{"linkid": 11, "title": "saved"}], "has_more": False}}
        raise AssertionError(route)


class NavigationTests(unittest.TestCase):
    def test_public_profile_filters_private_editor_fields(self):
        data, scope = Browser(Fixture()).execute(xhh.parser().parse_args(["user", "202"]))
        self.assertEqual((data["uid"], data["name"]), ("202", "author"))
        self.assertNotIn("post_draft_num", data)
        self.assertEqual(data["statistics"]["followers"], 4)

    def test_top_level_posts_are_normalized(self):
        data, scope = Browser(Fixture()).execute(xhh.parser().parse_args(["user", "202", "--view", "posts"]))
        self.assertEqual(data["items"][0]["id"], "11")
        self.assertIsNotNone(scope["resume"])

    def test_default_folder_zero_and_content(self):
        browser = Browser(Fixture())
        data, scope = browser.execute(xhh.parser().parse_args(["favourites", "folders"]))
        self.assertEqual(data["items"][0]["id"], "0")
        data, scope = browser.execute(xhh.parser().parse_args(["favourites", "posts", "0"]))
        self.assertEqual(data["items"][0]["title"], "saved")
        self.assertEqual(scope["completion"], "complete")

    def test_mismatching_profile_not_misattributed(self):
        fixture = Fixture()
        fixture.wrong_user = True
        with self.assertRaises(XhhConfigError):
            Browser(fixture).execute(xhh.parser().parse_args(["user", "202"]))


if __name__ == "__main__":
    unittest.main()
