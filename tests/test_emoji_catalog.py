import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from emoji_catalog import Catalog, EmojiError, parse_creator_assets, save_catalog

SPRITE = "https://static.max-c.com/heybox_web/emoji/test/sheet.png"
SOURCE = {"kind": "official_creator_assets", "js_url": "https://static.max-c.com/creator/app/index-a.js",
          "css_url": "https://static.max-c.com/creator/app/index-a.css", "js_sha256": "a" * 64, "css_sha256": "b" * 64,
          "observed_at": "2026-11-15T00:00:00+00:00", "render_contract": "creator_span_and_shortcode", "live_post_verified": False}
SPAN_TEMPLATE = """getSpanLabel(){return'<span data-emoji="'.concat(this.type,'" class="hb-emoji hb-emoji-').concat(this.type)}"""


def item(native, name, column, row, *, group="cube", unicode="e001", tags=(), cautions=(), meaning=None, usage=None):
    return {"id": f"{group}:{native}", "type": "native_emoji", "group": group, "native_id": native, "name": name,
            "shortcode": f"[{group}_{name}]", "font_unicode": unicode, "visual_description": None, "meaning": meaning,
            "tags": list(tags), "cautions": list(cautions), "annotation_source": "official_name_only",
            "annotation_status": "unannotated",
            "sprite": {"url": SPRITE, "source_url": SPRITE, "columns": 3, "rows": 2, "column": column, "row": row},
            "usage": usage or {"post": "creator_span", "comment": "shortcode", "reply": "shortcode"}}


def snapshot(items):
    return {"schema_version": 1, "catalog_version": "creator-test", "source": dict(SOURCE), "items": items}


ITEMS = [item("11", "叹气", 0, 0, unicode="e00d", tags=["无奈", "疲惫"], meaning="表达疲惫、无奈或叹息。"),
         item("12", "滑稽", 1, 0, unicode="e02d", tags=["调侃"], cautions=["sarcasm"], meaning="常用于调侃，也可能表达无奈。"),
         item("13", "无奈", 2, 0, unicode="e02f"),
         item("21", "摊手", 0, 1, unicode="e02f", tags=["无奈"], meaning="表达没办法。"),
         item("22", "仅评论", 1, 1, unicode="e030", tags=["无奈"], usage={"post": "unsupported", "comment": "shortcode"})]


def creator_js(groups, extra=""):
    lists = "".join("const %s=[%s];function f%d(){return{type:\"%s\",list:%s}}" % (
        variable, ",".join('{name:"%s",id:"%s",unicode:"%s"}' % record for record in records), index, group, variable)
        for index, (group, variable, records) in enumerate(groups))
    return "let r=[1];let q=function(){return{type:\"dialog\",list:r}};r=[2];" + SPAN_TEMPLATE + lists + extra


def creator_css(group, url, columns, rows, cells, length=20):
    base = (f".hb-emoji-{group}{{width:{length}px;width:var(--{group}-length);background-image:url({url});"
            f"background-size:{length * columns}px {length * rows}px;"
            f"background-size:calc(var(--{group}-length) * {columns}) calc(var(--{group}-length) * {rows})}}")
    return base + "".join(
        f".hb-emoji-{group}_{native}{{background-position:{length * x}px {length * y}px;"
        f"background-position:calc(var(--{group}-length) * {x}) calc(var(--{group}-length) * {y})}}"
        for native, (x, y) in cells.items())


CUBE_RECORDS = [("笑", "11", "e001"), ("哭", "12", "e002"), ("怒", "21", "e002")]
CUBE_CSS = creator_css("cube", SPRITE, 3, 2, {"11": (3, 2), "12": (2, 2), "21": (3, 1)})


class CatalogFiles(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def catalog(self, items=ITEMS):
        path = self.root / "synthetic.json"
        path.write_text(json.dumps(snapshot(items), ensure_ascii=False), encoding="utf-8")
        return Catalog(catalog_file=path)

    def code(self, call, *args, **kwargs):
        with self.assertRaises(EmojiError) as raised:
            call(*args, **kwargs)
        return raised.exception.code


class LookupTests(CatalogFiles):
    def test_bundled_catalog_is_readable_without_creating_state(self):
        data_dir = self.root / "data"
        data_dir.mkdir()
        catalog = Catalog(data_dir)
        info = catalog.info()
        self.assertEqual((info["state"], info["saved_error"], info["item_count"]), ("bundled", None, 132))
        self.assertEqual(info["source"]["render_contract"], "creator_span_and_shortcode")
        self.assertEqual(catalog.get("cube:32")["shortcode"], "[cube_笑cry]")
        catalog.search("无奈")
        self.assertEqual(list(data_dir.iterdir()), [])

    def test_each_identity_resolves_to_one_record(self):
        catalog = self.catalog()
        self.assertEqual(catalog.get("cube:21")["name"], "摊手")
        self.assertEqual(catalog.resolve_shortcode("[cube_摊手]")["id"], "cube:21")
        self.assertIsNone(catalog.resolve_shortcode("[cube_不存在]"))
        self.assertIsNone(catalog.resolve_shortcode("cube_摊手"))
        self.assertEqual(self.code(catalog.get, "cube:99"), "unknown_emoji")
        self.assertEqual(self.code(catalog.get, None), "unknown_emoji")

    def test_returned_records_are_copies(self):
        catalog = self.catalog()
        first = catalog.get("cube:11")
        first["tags"].append("changed")
        first["sprite"]["column"] = 9
        catalog.search("无奈")["items"][0]["tags"].clear()
        self.assertEqual(catalog.get("cube:11")["tags"], ["无奈", "疲惫"])
        self.assertEqual(catalog.get("cube:11")["sprite"]["column"], 0)

    def test_malformed_and_empty_catalogs_are_rejected(self):
        (self.root / "broken.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.code(Catalog, catalog_file=self.root / "broken.json"), "invalid_catalog")
        self.assertEqual(self.code(Catalog, catalog_file=self.root / "absent.json"), "invalid_catalog")
        self.assertEqual(self.code(self.catalog, []), "empty_catalog")
        self.assertEqual(self.code(self.catalog, [item("11", "笑", 0, 0), item("11", "哭", 1, 0)]), "duplicate_identity")
        self.assertEqual(self.code(self.catalog, [item("11", "笑", 0, 0), item("12", "笑", 1, 0)]), "duplicate_identity")
        self.assertEqual(self.code(self.catalog, [item("11", "笑", 0, 0), item("12", "哭", 0, 0)]), "conflicting_sprite")
        self.assertEqual(self.code(self.catalog, [item("11", "笑", 3, 0)]), "invalid_catalog")
        self.assertEqual(self.code(self.catalog, [{**item("11", "笑", 0, 0), "shortcode": "[cube_哭]"}]), "invalid_catalog")
        self.assertEqual(self.code(self.catalog, [{**item("11", "笑", 0, 0), "usage": {"dm": "shortcode"}}]), "invalid_catalog")
        for url in ("http://static.max-c.com/sheet.png", "https://example.com/sheet.png"):
            foreign = item("11", "笑", 0, 0)
            foreign["sprite"]["url"] = url
            self.assertEqual(self.code(self.catalog, [foreign]), "unsafe_url")


class SearchTests(CatalogFiles):
    def test_candidates_are_ordered_by_explained_match_strength(self):
        result = self.catalog().search("无奈", limit=10)
        self.assertEqual([found["id"] for found in result["items"]], ["cube:11", "cube:13", "cube:21", "cube:22", "cube:12"])
        scores = [found["rank_score"] for found in result["items"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(scores[0], scores[1])
        self.assertGreater(scores[3], scores[4])
        self.assertIn("tag 无奈", result["items"][0]["match_reason"])
        self.assertIn("meaning", result["items"][0]["match_reason"])
        self.assertIn("name", result["items"][1]["match_reason"])
        self.assertEqual(result["items"][4]["match_reason"], "meaning mentions 无奈")
        self.assertEqual((result["retrieval"], result["matched"]), ("keyword", 5))
        self.assertEqual(result["catalog"]["catalog_version"], "creator-test")
        self.assertFalse(any("prob" in key for found in result["items"] for key in found))

    def test_bundled_resignation_query_finds_sigh_and_shrug(self):
        names = [found["name"] for found in Catalog().search("无奈")["items"]]
        self.assertIn("叹气", names)
        self.assertIn("摊手", names)
        sentence = [found["name"] for found in Catalog().search("今天真的好无奈啊")["items"]]
        self.assertIn("叹气", sentence)
        self.assertIn("摊手", sentence)

    def test_avoided_cautions_and_unusable_targets_are_excluded(self):
        catalog = self.catalog()
        self.assertIn("cube:12", [found["id"] for found in catalog.search("调侃")["items"]])
        self.assertEqual(catalog.search("调侃", avoid=("sarcasm",))["items"], [])
        self.assertNotIn("cube:12", [found["id"] for found in catalog.search("无奈", avoid=["sarcasm"], limit=10)["items"]])
        self.assertNotIn("滑稽", [found["name"] for found in Catalog().search("调侃 反讽", avoid=("sarcasm",))["items"]])
        self.assertIn("cube:22", [found["id"] for found in catalog.search("无奈", usage="comment", limit=10)["items"]])
        self.assertNotIn("cube:22", [found["id"] for found in catalog.search("无奈", usage="post", limit=10)["items"]])
        self.assertNotIn("cube:22", [found["id"] for found in catalog.search("无奈", usage="reply", limit=10)["items"]])

    def test_limit_and_empty_query_listing(self):
        catalog = self.catalog()
        self.assertEqual([found["id"] for found in catalog.search(limit=3)["items"]], ["cube:11", "cube:12", "cube:13"])
        self.assertEqual(len(catalog.search()["items"]), 5)
        self.assertEqual(len(catalog.search("无奈", limit=2)["items"]), 2)
        self.assertEqual(catalog.search("无奈", limit=2)["matched"], 5)
        self.assertEqual(catalog.search("没有这个词")["items"], [])

    def test_invalid_boundaries_are_rejected(self):
        catalog = self.catalog()
        for limit in (0, -1, 201, True, "5", 2.0, None):
            self.assertEqual(self.code(catalog.search, "无奈", limit=limit), "invalid_limit")
        self.assertEqual(self.code(catalog.search, "无奈", usage="dm"), "unknown_usage")
        self.assertEqual(self.code(catalog.search, None), "invalid_query")
        self.assertEqual(self.code(catalog.search, "无奈", avoid=5), "invalid_avoid")


class MarkupTests(CatalogFiles):
    def test_native_markup_resolves_and_ordinary_images_do_not(self):
        catalog = self.catalog()
        native = {"data-emoji": "cube_摊手", "class": "hb-emoji hb-emoji-cube hb-emoji-cube_21"}
        self.assertEqual(catalog.resolve_attributes("span", native)["id"], "cube:21")
        self.assertEqual(catalog.resolve_attributes("SPAN", {"data-emoji": "cube_摊手"})["id"], "cube:21")
        self.assertEqual(catalog.resolve_attributes("span", {"class": "hb-emoji hb-emoji-cube hb-emoji-cube_21"})["id"], "cube:21")
        self.assertEqual(catalog.resolve_attributes("i", {"class": "cube cube-e00d"})["id"], "cube:11")
        self.assertEqual(catalog.resolve_attributes("img", {"class": "hb-emoji", "alt": "[cube_摊手]", "src": SPRITE})["id"], "cube:21")
        unresolved = [
            ("img", {"alt": "[cube_摊手]", "src": SPRITE, "width": "20", "height": "20"}),
            ("img", {"src": SPRITE, "width": "20", "height": "20"}),
            ("img", {"class": "hb-emoji", "src": SPRITE}),
            ("img", {"class": "hb-emoji", "alt": "摊手"}),
            ("span", {"data-emoji": "cube_不存在"}),
            ("span", {"data-emoji": "cube_摊手", "class": "hb-emoji hb-emoji-cube hb-emoji-cube_11"}),
            ("span", {"class": "hb-emoji-cube hb-emoji-cube_21"}),
            ("span", {"class": "hb-emoji hb-emoji-cube_21"}),
            ("span", {"class": "hb-emoji hb-emoji-cube hb-emoji-cube_99"}),
            ("span", {"class": None}),
            ("i", {"class": "cube cube-e02f"}),
            ("i", {"class": "cube cube-ffff"}),
            ("i", {"class": "cube"}),
            ("div", {"data-emoji": "cube_摊手"}),
            ("span", {}),
        ]
        for tag, attrs in unresolved:
            self.assertIsNone(catalog.resolve_attributes(tag, attrs), (tag, attrs))

    def test_render_produces_exact_creator_span_and_shortcodes(self):
        bundled = Catalog()
        self.assertEqual(bundled.render("cube:32", "post"),
                         '<span data-emoji="cube_笑cry" class="hb-emoji hb-emoji-cube hb-emoji-cube_32"></span>')
        self.assertEqual(bundled.render("cube:32", "comment"), "[cube_笑cry]")
        self.assertEqual(bundled.render("cube:32", "reply"), "[cube_笑cry]")
        rendered = bundled.render("heygirl:46", "post")
        self.assertEqual(bundled.resolve_attributes("span", {"data-emoji": "heygirl_挨刀", "class": "hb-emoji hb-emoji-heygirl hb-emoji-heygirl_46"})["id"], "heygirl:46")
        self.assertEqual(rendered, '<span data-emoji="heygirl_挨刀" class="hb-emoji hb-emoji-heygirl hb-emoji-heygirl_46"></span>')

    def test_render_escapes_attribute_values_and_rejects_unsupported_targets(self):
        catalog = self.catalog([item("11", "A&B'C", 0, 0), item("12", "仅评论", 1, 0, usage={"post": "unsupported", "comment": "shortcode"})])
        self.assertEqual(catalog.render("cube:11", "post"),
                         '<span data-emoji="cube_A&amp;B&#x27;C" class="hb-emoji hb-emoji-cube hb-emoji-cube_11"></span>')
        self.assertEqual(catalog.render("cube:11", "comment"), "[cube_A&B'C]")
        self.assertEqual(catalog.render("cube:12", "comment"), "[cube_仅评论]")
        self.assertEqual(self.code(catalog.render, "cube:12", "post"), "unsupported_usage")
        self.assertEqual(self.code(catalog.render, "cube:12", "reply"), "unsupported_usage")
        self.assertEqual(self.code(catalog.render, "cube:11", "dm"), "unknown_usage")
        self.assertEqual(self.code(catalog.render, "cube:99", "post"), "unknown_emoji")


class CreatorSourceTests(CatalogFiles):
    def parse(self, js, css, **source):
        return parse_creator_assets(js, css, {"js_url": SOURCE["js_url"], "css_url": SOURCE["css_url"], **source})

    def test_factories_are_found_under_any_minified_names(self):
        js = creator_js([("cube", "q7", CUBE_RECORDS)],
                        "const $x=[{'name':'\\u8bf6\\u563f',\"id\":'11',unicode:'e019'}];const W=()=>({ type : 'heygirl' , list : $x });")
        heygirl = creator_css("heygirl", "http://cdn.max-c.com/heybox_web/emoji/cube2/sheet.png", 6, 4, {"11": (6, 4)})
        parsed = self.parse(js, CUBE_CSS + heygirl, observed_at="2026-11-15T00:00:00+00:00")
        self.assertEqual([(found["id"], found["name"], found["shortcode"], found["font_unicode"]) for found in parsed["items"]],
                         [("cube:11", "笑", "[cube_笑]", "e001"), ("cube:12", "哭", "[cube_哭]", "e002"),
                          ("cube:21", "怒", "[cube_怒]", "e002"), ("heygirl:11", "诶嘿", "[heygirl_诶嘿]", "e019")])
        self.assertEqual([(found["sprite"]["column"], found["sprite"]["row"]) for found in parsed["items"]], [(0, 0), (1, 0), (0, 1), (0, 0)])
        self.assertEqual(parsed["items"][0]["sprite"], {"url": SPRITE, "source_url": SPRITE, "columns": 3, "rows": 2, "column": 0, "row": 0})
        self.assertEqual(parsed["items"][3]["sprite"]["url"], "https://cdn.max-c.com/heybox_web/emoji/cube2/sheet.png")
        self.assertEqual(parsed["items"][3]["sprite"]["source_url"], "http://cdn.max-c.com/heybox_web/emoji/cube2/sheet.png")
        self.assertEqual((parsed["items"][3]["sprite"]["columns"], parsed["items"][3]["sprite"]["rows"]), (6, 4))
        self.assertEqual(parsed["items"][0]["usage"], {"post": "creator_span", "comment": "shortcode", "reply": "shortcode"})
        digest = hashlib.sha256(js.encode()).hexdigest()
        self.assertEqual(parsed["source"], {
            "kind": "official_creator_assets", "js_url": SOURCE["js_url"], "css_url": SOURCE["css_url"], "js_sha256": digest,
            "css_sha256": hashlib.sha256((CUBE_CSS + heygirl).encode()).hexdigest(), "observed_at": "2026-11-15T00:00:00+00:00",
            "render_contract": "creator_span_and_shortcode", "live_post_verified": False})
        self.assertEqual(parsed["catalog_version"], "creator-" + digest[:16])
        saved = save_catalog(parsed, self.root)
        self.assertEqual(saved["item_count"], 4)
        self.assertEqual(Catalog(self.root).render("heygirl:11", "post"),
                         '<span data-emoji="heygirl_诶嘿" class="hb-emoji hb-emoji-heygirl hb-emoji-heygirl_11"></span>')

    def test_only_unchanged_reviewed_records_keep_their_annotations(self):
        known, other = Catalog().get("cube:43"), Catalog().get("cube:44")
        sprite = known["sprite"]
        offset = lambda found: (sprite["columns"] - found["sprite"]["column"], sprite["rows"] - found["sprite"]["row"])
        cells = {"43": offset(known), "44": offset(other), "999": (2, 1)}
        css = creator_css("cube", sprite["source_url"], sprite["columns"], sprite["rows"], cells)
        js = creator_js([("cube", "Zq", [(known["name"], "43", known["font_unicode"]), ("改名了", "44", other["font_unicode"]),
                                         ("新表情", "999", "e0aa")])])
        kept, renamed, new = self.parse(js, css)["items"]
        self.assertEqual(kept, known)
        self.assertEqual(kept["annotation_status"], "suggested")
        self.assertEqual((other["annotation_status"], renamed["sprite"]), ("suggested", other["sprite"]))
        for found in (renamed, new):
            self.assertEqual((found["annotation_status"], found["annotation_source"]), ("unannotated", "official_name_only"))
            self.assertEqual((found["visual_description"], found["meaning"], found["tags"], found["cautions"]), (None, None, [], []))
        moved = creator_css("cube", sprite["source_url"], sprite["columns"], sprite["rows"], {**cells, "43": (1, 2)})
        self.assertEqual(self.parse(js, moved)["items"][0]["annotation_status"], "unannotated")

    def test_source_metadata_must_match_the_text(self):
        js = creator_js([("cube", "q7", CUBE_RECORDS)])
        self.assertEqual(self.code(self.parse, js, CUBE_CSS, js_sha256="0" * 64), "source_hash_mismatch")
        self.assertEqual(self.code(self.parse, js, CUBE_CSS, css_sha256="0" * 64), "source_hash_mismatch")
        self.assertEqual(self.code(parse_creator_assets, js, CUBE_CSS, {"js_url": "https://example.com/a.js", "css_url": SOURCE["css_url"]}), "unsafe_url")
        self.assertEqual(self.code(parse_creator_assets, js, CUBE_CSS, {"js_url": SOURCE["js_url"]}), "unsafe_url")
        self.assertEqual(self.code(parse_creator_assets, js, CUBE_CSS, None), "invalid_source")
        self.assertTrue(self.parse(js, CUBE_CSS)["source"]["observed_at"])

    def test_unusable_source_text_fails_explicitly(self):
        records = lambda *found: creator_js([("cube", "q7", list(found))])
        cases = [
            ("empty_source", "", CUBE_CSS),
            ("empty_source", records(*CUBE_RECORDS), "  "),
            ("empty_source", SPAN_TEMPLATE + 'const q7=[];function f(){return{type:"cube",list:q7}}', CUBE_CSS),
            ("unsupported_source", records(*CUBE_RECORDS).replace("data-emoji", "data-other"), CUBE_CSS),
            ("unsupported_source", SPAN_TEMPLATE + "const q7=[];", CUBE_CSS),
            ("unsupported_source", SPAN_TEMPLATE + 'function f(){return{type:"cube",list:q7}}', CUBE_CSS),
            ("unsupported_source", records(*CUBE_RECORDS), ".other{color:red}"),
            ("unsupported_source", records(*CUBE_RECORDS), creator_css("cube", SPRITE, 3, 2, {"11": (3, 2)}).replace("px", "em").replace("calc(", "max(")),
            ("malformed_source", SPAN_TEMPLATE + 'const q7=[{name:"笑",id:11,unicode:"e001"}];function f(){return{type:"cube",list:q7}}', CUBE_CSS),
            ("malformed_source", SPAN_TEMPLATE + 'const q7=[{name:"笑",id:"11"}];function f(){return{type:"cube",list:q7}}', CUBE_CSS),
            ("malformed_source", SPAN_TEMPLATE + 'const q7=[{name:"笑",id:"11",unicode:"e001"};function f(){return{type:"cube",list:q7}}', CUBE_CSS),
            ("malformed_source", SPAN_TEMPLATE + 'const q7=[{name:"笑",id:"11",unicode:"e001",run:alert(1)}];function f(){return{type:"cube",list:q7}}', CUBE_CSS),
            ("malformed_source", SPAN_TEMPLATE + 'const q7=[{name:"笑"+x,id:"11",unicode:"e001"}];function f(){return{type:"cube",list:q7}}', CUBE_CSS),
            ("duplicate_identity", records(("笑", "11", "e001"), ("哭", "11", "e002")), CUBE_CSS),
            ("duplicate_identity", records(("笑", "11", "e001"), ("笑", "12", "e002")), CUBE_CSS),
            ("duplicate_identity", creator_js([("cube", "q7", CUBE_RECORDS), ("cube", "q8", CUBE_RECORDS)]), CUBE_CSS),
            ("conflicting_source", records(*CUBE_RECORDS) + "q7=[];", CUBE_CSS),
            ("conflicting_source", records(*CUBE_RECORDS, ("新", "31", "e003")), CUBE_CSS),
            ("conflicting_source", records(*CUBE_RECORDS), CUBE_CSS.replace("background-size:60px 40px", "background-size:80px 40px")),
            ("conflicting_source", records(*CUBE_RECORDS), CUBE_CSS + ".hb-emoji-cube{background-image:url(https://static.max-c.com/other.png)}"),
            ("conflicting_sprite", records(*CUBE_RECORDS), creator_css("cube", SPRITE, 3, 2, {"11": (3, 2), "12": (3, 2), "21": (3, 1)})),
            ("invalid_catalog", records(("a b", "11", "e001")), CUBE_CSS),
            ("invalid_catalog", records(("笑", "11", "zzzz")), CUBE_CSS),
            ("unsafe_url", records(*CUBE_RECORDS), CUBE_CSS.replace("static.max-c.com", "example.com")),
        ]
        for expected, js, css in cases:
            with self.subTest(expected=expected, js=js[-90:], css=css[:90]):
                self.assertEqual(self.code(self.parse, js, css), expected)


class SavedCatalogTests(CatalogFiles):
    def outside_link(self):
        outside = self.root / "outside"
        outside.mkdir()
        data_dir = self.root / "data"
        data_dir.mkdir()
        try:
            os.symlink(outside, data_dir / "emojis", target_is_directory=True)
        except OSError:
            if sys.platform != "win32":
                self.skipTest("symlinks are unavailable")
            import _winapi
            _winapi.CreateJunction(str(outside), str(data_dir / "emojis"))
        return data_dir, outside

    def test_saved_snapshot_replaces_bundled_and_invalid_refresh_keeps_it(self):
        result = save_catalog(snapshot(ITEMS), self.root)
        saved = self.root / "emojis" / "catalog.json"
        self.assertEqual(Path(result["catalog_file"]), saved.resolve())
        self.assertEqual(result["catalog_sha256"], hashlib.sha256(saved.read_bytes()).hexdigest())
        self.assertEqual((result["catalog_version"], result["item_count"]), ("creator-test", 5))
        before = saved.read_bytes()
        catalog = Catalog(self.root)
        self.assertEqual((catalog.info()["state"], catalog.info()["item_count"]), ("saved", 5))
        self.assertEqual(catalog.get("cube:21")["name"], "摊手")
        invalid = [snapshot([]), snapshot([item("11", "笑", 0, 0), item("11", "哭", 1, 0)]), {"items": ITEMS}, None,
                   {**snapshot(ITEMS), "schema_version": 2}, {**snapshot(ITEMS), "source": {**SOURCE, "render_contract": "other"}}]
        for value in invalid:
            with self.assertRaises(EmojiError):
                save_catalog(value, self.root)
        self.assertEqual(saved.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in saved.parent.iterdir()), ["catalog.json"])
        self.assertEqual(Catalog(self.root).info()["state"], "saved")
        save_catalog(snapshot(ITEMS[:2]), self.root)
        self.assertEqual(Catalog(self.root).info()["item_count"], 2)

    def test_corrupt_saved_snapshot_falls_back_to_bundled(self):
        save_catalog(snapshot(ITEMS), self.root)
        (self.root / "emojis" / "catalog.json").write_text("{", encoding="utf-8")
        info = Catalog(self.root).info()
        self.assertEqual((info["state"], info["saved_error"], info["item_count"]), ("bundled", "invalid_catalog", 132))

    def test_cache_cannot_escape_the_data_directory(self):
        data_dir, outside = self.outside_link()
        (outside / "catalog.json").write_text(json.dumps(snapshot(ITEMS), ensure_ascii=False), encoding="utf-8")
        before = (outside / "catalog.json").read_bytes()
        self.assertEqual(self.code(Catalog, data_dir), "path_escape")
        self.assertEqual(self.code(save_catalog, snapshot(ITEMS[:1]), data_dir), "path_escape")
        self.assertEqual((outside / "catalog.json").read_bytes(), before)
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["catalog.json"])


if __name__ == "__main__":
    unittest.main()
