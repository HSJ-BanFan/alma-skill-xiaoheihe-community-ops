"""Official native emoji catalog: lookup, keyword search, markup and Creator source parsing."""
from __future__ import annotations

import copy
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from assets import atomic, digest, owned, platform_url
from xhh_sdk.exceptions import XhhConfigError

BUNDLED = Path(__file__).resolve().parent / "data" / "native_emojis.json"
SCHEMA_VERSION = 1
RENDER_CONTRACT = "creator_span_and_shortcode"
TARGETS = ("post", "comment", "reply")
MAX_LIMIT = 200
WEIGHTS = {"name_exact": 100, "tag_exact": 80, "name_overlap": 60, "tag_overlap": 50, "meaning": 30}
UNANNOTATED = {"visual_description": None, "meaning": None, "tags": [], "cautions": [],
               "annotation_source": "official_name_only", "annotation_status": "unannotated"}

GROUP = re.compile(r"[a-z][a-z0-9]*")
NATIVE_ID = re.compile(r"[0-9]{1,6}")
FONT_UNICODE = re.compile(r"[0-9a-f]{4,6}")
NAME = re.compile(r'[^\s\x00-\x1f\[\]_"<>]+')
SHA256 = re.compile(r"[0-9a-f]{64}")
NATIVE_CLASS = re.compile(r"hb-emoji-([a-z][a-z0-9]*)_([0-9]+)")
SEPARATORS = re.compile(r"[\s,，。、！!？?；;：:]+")

FACTORY = re.compile(r"""(?:return|=>\s*\()\s*\{\s*type\s*:\s*(["'])([^"'\\]*)\1\s*,\s*list\s*:\s*([A-Za-z_$][\w$]*)\s*\}""")
TOKEN = re.compile(r"""\s*(?:([\[\]{}:,])|([A-Za-z_$][\w$]*)|"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)')""")
ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)")
RULE = re.compile(r"\.hb-emoji-([a-z][a-z0-9]*)(?:_([0-9]+))?\s*\{([^{}]*)\}")
UNIT = r"(?:calc\(\s*var\(\s*--[\w-]+\s*\)\s*\*\s*(-?[0-9]+)\s*\)|(-?[0-9]+(?:\.[0-9]+)?)px|(0))"
PAIR = re.compile(UNIT + r"\s+" + UNIT)
PIXELS = re.compile(r"([0-9]+(?:\.[0-9]+)?)px")
SPRITE_URL = re.compile(r"""url\(\s*["']?([^"')\s]+)["']?\s*\)""")


class EmojiError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _need(condition, message, code="invalid_catalog"):
    if not condition:
        raise EmojiError(code, message)


def _cdn(url):
    try:
        if isinstance(url, str):
            return platform_url(url)
    except (XhhConfigError, ValueError):
        pass
    raise EmojiError("unsafe_url", "emoji source must be an HTTPS Xiaoheihe CDN URL")


def cache_path(data_dir, *parts):
    root = Path(data_dir)
    try:
        return owned(root, root.joinpath("emojis", *parts))
    except XhhConfigError:
        raise EmojiError("path_escape", "emoji cache path escapes its data directory") from None


def _span(item):
    group, name, native = (html.escape(item[key], quote=True) for key in ("group", "name", "native_id"))
    return f'<span data-emoji="{group}_{name}" class="hb-emoji hb-emoji-{group} hb-emoji-{group}_{native}"></span>'


FORMS = {"creator_span": _span, "shortcode": lambda item: item["shortcode"]}


def _target(usage):
    _need(isinstance(usage, str) and usage in TARGETS, "usage must be one of " + ", ".join(TARGETS), "unknown_usage")
    return usage


def _record(item):
    _need(isinstance(item, dict), "emoji record is not an object")
    texts = ("id", "group", "native_id", "name", "shortcode", "font_unicode", "annotation_source", "annotation_status")
    _need(all(isinstance(item.get(key), str) and item[key] for key in texts), "emoji record has missing text fields")
    group, native, name = item["group"], item["native_id"], item["name"]
    _need(item.get("type") == "native_emoji" and GROUP.fullmatch(group) and NATIVE_ID.fullmatch(native)
          and NAME.fullmatch(name) and FONT_UNICODE.fullmatch(item["font_unicode"]), "emoji identity is malformed")
    _need(item["id"] == f"{group}:{native}" and item["shortcode"] == f"[{group}_{name}]", "emoji identity fields disagree")
    _need(all(item.get(key) is None or isinstance(item[key], str) for key in ("visual_description", "meaning"))
          and all(isinstance(item.get(key), list) and all(isinstance(value, str) and value for value in item[key])
                  for key in ("tags", "cautions")), "emoji annotation is malformed")
    usage, sprite = item.get("usage"), item.get("sprite")
    _need(isinstance(usage, dict) and usage and all(target in TARGETS and isinstance(form, str) and (form in FORMS or form == "unsupported")
                                                    for target, form in usage.items()), "emoji usage is malformed")
    _need(isinstance(sprite, dict) and isinstance(sprite.get("source_url"), str), "emoji sprite is malformed")
    _cdn(sprite.get("url"))
    _need(all(type(sprite.get(key)) is int for key in ("columns", "rows", "column", "row"))
          and 0 <= sprite["column"] < sprite["columns"] and 0 <= sprite["row"] < sprite["rows"], "emoji sprite cell is outside its grid")


def _validate(snapshot):
    _need(isinstance(snapshot, dict) and snapshot.get("schema_version") == SCHEMA_VERSION, "catalog schema is not supported")
    version, source, items = (snapshot.get(key) for key in ("catalog_version", "source", "items"))
    _need(isinstance(version, str) and version, "catalog version is missing")
    _need(isinstance(source, dict) and source.get("render_contract") == RENDER_CONTRACT, "catalog source contract is not supported")
    for kind in ("js", "css"):
        _cdn(source.get(kind + "_url"))
        _need(isinstance(source.get(kind + "_sha256"), str) and SHA256.fullmatch(source[kind + "_sha256"]), "catalog source hash is missing")
    _need(isinstance(items, list), "catalog items are missing")
    _need(items, "catalog has no emojis", "empty_catalog")
    identities, grids, cells = set(), {}, set()
    for item in items:
        _record(item)
        sprite = item["sprite"]
        for identity in (item["id"], item["shortcode"]):
            _need(identity not in identities, "emoji identity is duplicated: " + identity, "duplicate_identity")
            identities.add(identity)
        grid, cell = (sprite["columns"], sprite["rows"]), (sprite["url"], sprite["column"], sprite["row"])
        _need(grids.setdefault(sprite["url"], grid) == grid and cell not in cells, "emoji sprite cells conflict: " + item["id"], "conflicting_sprite")
        cells.add(cell)
    return snapshot


def _load(path):
    try:
        snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise EmojiError("invalid_catalog", "emoji catalog cannot be read") from None
    return _validate(snapshot)


def _reasons(term, item):
    found, name = [], item["name"].casefold()
    if term == name:
        found.append((WEIGHTS["name_exact"], f"name equals {term}"))
    elif term in name or (len(name) > 1 and name in term):
        found.append((WEIGHTS["name_overlap"], f"name {item['name']} overlaps {term}"))
    tags = [("tag_exact" if term == tag.casefold() else "tag_overlap", tag) for tag in item["tags"]
            if term in tag.casefold() or (len(tag) > 1 and tag.casefold() in term)]
    if tags:
        kind, tag = max(tags, key=lambda entry: WEIGHTS[entry[0]])
        found.append((WEIGHTS[kind], f"tag {tag} {'equals' if kind == 'tag_exact' else 'overlaps'} {term}"))
    if term in (item["meaning"] or "").casefold():
        found.append((WEIGHTS["meaning"], f"meaning mentions {term}"))
    return found


class Catalog:
    def __init__(self, data_dir=None, catalog_file=None):
        snapshot, self._saved_error = None, None
        if data_dir is not None:
            saved = cache_path(data_dir, "catalog.json")
            if saved.exists():
                try:
                    snapshot = _load(saved)
                except EmojiError as error:
                    self._saved_error = error.code
        self._state = "saved" if snapshot else "bundled"
        self._snapshot = snapshot or _load(catalog_file or BUNDLED)
        self._items = {item["id"]: item for item in self._snapshot["items"]}
        self._shortcodes = {item["shortcode"]: item for item in self._snapshot["items"]}
        fonts = {}
        for item in self._snapshot["items"]:
            fonts.setdefault((item["group"], item["font_unicode"]), []).append(item)
        self._fonts = {key: found[0] for key, found in fonts.items() if len(found) == 1}

    def info(self):
        return {"schema_version": SCHEMA_VERSION, "catalog_version": self._snapshot["catalog_version"],
                "source": copy.deepcopy(self._snapshot["source"]), "state": self._state,
                "saved_error": self._saved_error, "item_count": len(self._items)}

    def get(self, emoji_id):
        item = self._items.get(emoji_id) if isinstance(emoji_id, str) else None
        _need(item is not None, f"emoji ID is not in the catalog: {emoji_id!r:.80}", "unknown_emoji")
        return copy.deepcopy(item)

    def search(self, query="", *, usage=None, limit=5, avoid=()):
        _need(isinstance(query, str), "search query must be text", "invalid_query")
        _need(type(limit) is int and 1 <= limit <= MAX_LIMIT, f"limit must be an integer from 1 to {MAX_LIMIT}", "invalid_limit")
        if usage is not None:
            _target(usage)
        try:
            avoid = {avoid} if isinstance(avoid, str) else set(avoid or ())
        except TypeError:
            raise EmojiError("invalid_avoid", "avoid must be a list of caution tags") from None
        terms = list(dict.fromkeys(term for term in SEPARATORS.split(query.casefold()) if term))
        ranked = []
        for item in self._items.values():
            if avoid.intersection(item["cautions"]) or (usage and item["usage"].get(usage) not in FORMS):
                continue
            reasons = [reason for term in terms for reason in _reasons(term, item)]
            if reasons or not terms:
                ranked.append((sum(score for score, _ in reasons), "; ".join(text for _, text in reasons) or "listed without a query", item))
        ranked.sort(key=lambda entry: -entry[0])
        items = [{**copy.deepcopy(item), "rank_score": score, "match_reason": reason} for score, reason, item in ranked[:limit]]
        return {"retrieval": "keyword", "query": query, "matched": len(ranked), "catalog": self.info(), "items": items}

    def resolve_shortcode(self, value):
        return copy.deepcopy(self._shortcodes.get(value)) if isinstance(value, str) else None

    def resolve_attributes(self, tag, attrs):
        if not isinstance(tag, str) or not isinstance(attrs, dict):
            return None
        tag, classes, claims = tag.lower(), (attrs.get("class") or "").split(), []
        if tag == "i":
            if len(classes) == 2 and classes[1].startswith(classes[0] + "-"):
                claims.append(self._fonts.get((classes[0], classes[1][len(classes[0]) + 1:])))
        elif tag in ("span", "img"):
            marked = "hb-emoji" in classes
            if attrs.get("data-emoji") is not None:
                claims.append(self._shortcodes.get(f"[{attrs['data-emoji']}]"))
            for group, native in (match.groups() for match in map(NATIVE_CLASS.fullmatch, classes) if match):
                claims.append(self._items.get(f"{group}:{native}") if marked and "hb-emoji-" + group in classes else None)
            if tag == "img" and marked and attrs.get("alt") is not None:
                claims.append(self._shortcodes.get(attrs["alt"]))
        if not claims or any(claim is None or claim is not claims[0] for claim in claims):
            return None
        return copy.deepcopy(claims[0])

    def render(self, emoji_id, usage):
        item = self.get(emoji_id)
        form = FORMS.get(item["usage"].get(_target(usage)))
        _need(form is not None, f"{item['id']} cannot be used in a {usage}", "unsupported_usage")
        return form(item)


def _unescape(text):
    def replace(match):
        code = match.group(1)
        if len(code) > 1:
            return chr(int(code[1:], 16))
        _need(code in "\"'\\/", "Creator emoji list has an unsupported string escape", "malformed_source")
        return code
    try:
        return ESCAPE.sub(replace, text).encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        raise EmojiError("malformed_source", "Creator emoji list has an invalid string") from None


def _records(js, position):
    def malformed(condition):
        _need(condition, "Creator emoji list cannot be parsed", "malformed_source")

    def take():
        nonlocal position
        match = TOKEN.match(js, position)
        malformed(match is not None)
        position = match.end()
        mark, word, double, single = match.groups()
        if mark or word:
            return ("mark", mark) if mark else ("word", word)
        return "string", _unescape(double if double is not None else single)

    def expect(*marks):
        kind, mark = take()
        malformed(kind == "mark" and mark in marks)
        return mark

    records = []
    expect("[")
    opening = take()
    if opening == ("mark", "]"):
        return records
    while True:
        malformed(opening == ("mark", "{"))
        record, mark = {}, ","
        while mark == ",":
            kind, key = take()
            malformed(kind in ("word", "string") and key not in record)
            expect(":")
            kind, record[key] = take()
            malformed(kind == "string")
            mark = expect(",", "}")
        _need(set(record) == {"name", "id", "unicode"}, "Creator emoji record has unexpected fields", "malformed_source")
        records.append(record)
        if expect(",", "]") == "]":
            return records
        opening = take()


def _groups(js, known):
    groups = {}
    for _, group, variable in FACTORY.findall(js):
        if group not in known:
            continue
        _need(group not in groups, "Creator emoji group is defined more than once: " + group, "duplicate_identity")
        bindings = [match.end() - 1 for match in re.finditer(r"(?<![\w$.])" + re.escape(variable) + r"\s*=\s*\[", js)]
        _need(bindings, "Creator emoji list was not found: " + group, "unsupported_source")
        _need(len(bindings) == 1, "Creator emoji list is bound more than once: " + group, "conflicting_source")
        groups[group] = _records(js, bindings[0])
        _need(groups[group], "Creator emoji group is empty: " + group, "empty_source")
    return groups


def _unit(scale, pixels, cell):
    if scale is not None:
        return int(scale)
    if pixels is None:
        return 0
    return float(pixels) / cell if cell else None


def _pair(values, cell, what):
    found = set()
    for match in filter(None, map(PAIR.fullmatch, values)):
        units = (_unit(match.group(1), match.group(2), cell), _unit(match.group(4), match.group(5), cell))
        if None not in units:
            found.add(units)
    _need(found, "Creator sprite geometry cannot be read: " + what, "unsupported_source")
    _need(len(found) == 1, "Creator sprite geometry disagrees: " + what, "conflicting_source")
    pair = found.pop()
    _need(all(value == int(value) for value in pair), "Creator sprite geometry is not a whole grid: " + what, "unsupported_source")
    return int(pair[0]), int(pair[1])


def _sprites(css):
    rules = {}
    for group, native, body in RULE.findall(css):
        for declaration in body.split(";"):
            name, _, value = declaration.partition(":")
            if value.strip():
                rules.setdefault((group, native), {}).setdefault(name.strip().lower(), []).append(re.sub(r"\s*!important$", "", value.strip()))
    sprites = {}
    for (group, native), found in rules.items():
        if native or "background-image" not in found:
            continue
        urls = {match.group(1) for match in map(SPRITE_URL.fullmatch, found["background-image"]) if match}
        _need(len(urls) == 1, "Creator sprite image is ambiguous: " + group, "conflicting_source" if urls else "unsupported_source")
        source_url = urls.pop()
        cell = next((float(match.group(1)) for match in map(PIXELS.fullmatch, found.get("width", ())) if match), None)
        columns, rows = _pair(found.get("background-size", ()), cell, group)
        _need(columns > 0 and rows > 0, "Creator sprite grid is empty: " + group, "unsupported_source")
        sprites[group] = {"url": _cdn(re.sub(r"^http://", "https://", source_url)), "source_url": source_url,
                          "columns": columns, "rows": rows, "cell": cell, "cells": {}}
    for (group, native), found in rules.items():
        if native and group in sprites and "background-position" in found:
            sprite = sprites[group]
            x, y = _pair(found["background-position"], sprite["cell"], f"{group}_{native}")
            sprite["cells"][native] = (-x % sprite["columns"], -y % sprite["rows"])
    return sprites


def _source(js, css, source):
    _need(isinstance(source, dict), "source metadata must be an object", "invalid_source")
    hashes = {"js": digest(js.encode("utf-8", "surrogatepass")), "css": digest(css.encode("utf-8", "surrogatepass"))}
    for kind, value in hashes.items():
        _cdn(source.get(kind + "_url"))
        _need(source.get(kind + "_sha256", value) == value, f"{kind} text does not match its declared hash", "source_hash_mismatch")
    observed, live = source.get("observed_at") or datetime.now(timezone.utc).isoformat(), source.get("live_post_verified", False)
    _need(isinstance(observed, str) and isinstance(live, bool), "source metadata is malformed", "invalid_source")
    return {"kind": "official_creator_assets", "js_url": source["js_url"], "css_url": source["css_url"],
            "js_sha256": hashes["js"], "css_sha256": hashes["css"], "observed_at": observed,
            "render_contract": RENDER_CONTRACT, "live_post_verified": live}


def parse_creator_assets(js, css, source):
    _need(isinstance(js, str) and js.strip() and isinstance(css, str) and css.strip(), "Creator JavaScript and CSS text are required", "empty_source")
    _need('data-emoji="' in js and "hb-emoji hb-emoji-" in js, "Creator span contract was not found", "unsupported_source")
    sprites, source = _sprites(css), _source(js, css, source)
    _need(sprites, "Creator sprite rules were not found", "unsupported_source")
    groups = _groups(js, sprites)
    _need(set(groups) == set(sprites), "Creator emoji factories were not found for every sprite group", "unsupported_source")
    annotated = {item["id"]: item for item in _load(BUNDLED)["items"]}
    items = []
    for group, records in groups.items():
        sheet = sprites[group]
        for record in records:
            name, native = record["name"], record["id"]
            _need(native in sheet["cells"], f"Creator CSS has no sprite cell for {group}_{native:.20}", "conflicting_source")
            sprite = {"url": sheet["url"], "source_url": sheet["source_url"], "columns": sheet["columns"], "rows": sheet["rows"],
                      "column": sheet["cells"][native][0], "row": sheet["cells"][native][1]}
            known = annotated.get(f"{group}:{native}")
            annotation = known if known and known["name"] == name and known["sprite"] == sprite else UNANNOTATED
            items.append({"id": f"{group}:{native}", "type": "native_emoji", "group": group, "native_id": native, "name": name,
                          "shortcode": f"[{group}_{name}]", "font_unicode": record["unicode"],
                          **{key: copy.deepcopy(annotation[key]) for key in UNANNOTATED},
                          "sprite": sprite, "usage": {"post": "creator_span", "comment": "shortcode", "reply": "shortcode"}})
    return _validate({"schema_version": SCHEMA_VERSION, "catalog_version": "creator-" + source["js_sha256"][:16], "source": source, "items": items})


def save_catalog(snapshot, data_dir):
    _validate(snapshot)
    try:
        data = json.dumps(snapshot, ensure_ascii=False, indent=2).encode()
    except (TypeError, ValueError):
        raise EmojiError("invalid_catalog", "emoji catalog cannot be serialized") from None
    path = cache_path(data_dir, "catalog.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic(path, data)
    return {"catalog_file": str(path.resolve()), "catalog_sha256": digest(data), "catalog_version": snapshot["catalog_version"],
            "item_count": len(snapshot["items"]), "state": "saved"}
