"""Read-only meme lookup adapter for the community skill.

The meme pipeline is no longer owned here. Collection, compilation, import,
field backfill and storage live in the standalone ``alma-meme-watcher`` plugin,
which treats ``state/meme_radar.db`` as *its* store. This module only reads that
database so reading a post never depends on the watcher being reachable.

Contract:

* Never creates, migrates, repairs or prunes tables — opening the file for
  writing here would race the watcher's own transactions.
* Never writes term data, candidates or review state.
* Returns ``[]`` / ``catalog_unavailable`` when the file is missing, unreadable
  or lacks the expected tables. Callers degrade, they do not fail.
* ``scan()`` is a no-op shim: candidate mining belongs to the watcher now.

Writes that used to live here (``meme_add`` / ``review_candidate`` /
``cache_source`` / ``repair`` / ``prune`` / ``seed``) are intentionally gone.
"""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse
from paths import state_root

DB_PATH = state_root() / "meme_radar.db"

# Admitted definitions only: reviewed and confident enough to state as fact.
_ADMITTED_STATUS = ("local_reference", "accepted")
_MIN_CONFIDENCE = 0.7


def _connect() -> sqlite3.Connection:
    """Open the watcher's database strictly read-only.

    No ``executescript(SCHEMA)`` here on purpose: this module must not create or
    alter tables the watcher owns. A missing file raises, and every public
    entry point turns that into an empty/degraded result.
    """
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=5000")
    return conn


def _now() -> float:
    return time.time()


def _one_line(text: object, limit: int = 160) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _alias_list(raw: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,，;；|/]", raw or "") if x.strip()]


def _term_keys(row) -> list[str]:
    return [key for key in [row["term"]] + _alias_list(row["aliases"]) if key]


_SHORTCODE_RE = re.compile(r"\[(?:cube|heygirl|grandemoji|bigemoji)_[^\]\r\n]+\]", re.I)
_URL_RE = re.compile(r"(?:https?|heybox)://[^\s<>\"']+", re.I)
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|(?<!\w)/(?:Users|home|tmp|var|mnt)/)[^\s<>\"']+")
_STATS_RE = re.compile(r"^\s*(?:\d{2}-\d{2}\s+\d{2}:\d{2}\s*·\s*)?(?:顶|充电|评论)\s+\d+(?:\s*·\s*(?:(?:顶|充电|评论)\s+\d+|话题\s+[^·]+|已收藏))*\s*$")
_TITLE_RE = re.compile(r"^\s*《([^》]+)》\s+楼主\s+.*$")
_FLOOR_HEADER_RE = re.compile(r"^\s*(?:本页返回的楼层上下文\b|link_id=|〔(?:根评论|回复)[^〕]*cid=|〔第\s*\d+\s*张图|……\s*已有评论)")
_INLINE_COMMENT_RE = re.compile(r"^\s+.*?〔cid=\d+〕[：:](.*)$")
_MENTION_RE = re.compile(r"(?<!\w)@[^\s，。！？、:：;；<>\[\]（）()]{1,64}")
_TOPIC_RE = re.compile(r"话题\s+(?:独立游戏开发|盒友杂谈|校园生活|PC游戏|主机游戏|游戏开发|Steam|手机游戏)(?:\s*/\s*(?:独立游戏开发|盒友杂谈|校园生活|PC游戏|主机游戏|游戏开发|Steam|手机游戏))*", re.I)


def semantic_text(text: str) -> str:
    """Internal extraction input only. Displayed post/comment text is unchanged."""
    lines = []
    for line in str(text or "").splitlines():
        if _STATS_RE.search(line) or _FLOOR_HEADER_RE.search(line) or re.match(r"^\s*〔(?:词条|新词|时效|时效数据)〕", line):
            continue
        title, comment = _TITLE_RE.match(line), _INLINE_COMMENT_RE.match(line)
        lines.append(title.group(1) if title else comment.group(1) if comment else line)
    value = "\n".join(lines)
    value = _SHORTCODE_RE.sub(" ", value)
    value = _MENTION_RE.sub(" ", value)
    value = _TOPIC_RE.sub(" ", value)
    value = _URL_RE.sub(" ", value)
    return _PATH_RE.sub(" ", value)[:8000]


def _contains_key(text: str, key: str) -> bool:
    if not key:
        return False
    if re.fullmatch(r"[A-Za-z0-9_]+", key):
        return re.search(r"(?<![A-Za-z0-9_])" + re.escape(key) + r"(?![A-Za-z0-9_])", text, re.I) is not None
    return key.lower() in text.lower()


_SINGLE_CONTEXT = re.compile(r"太|很|好|真|有点|操作|行为|味|死|人|子|完|绝了|离谱")
_SINGLE_BLOCK = {"唐": "朝突装诗"}


def _single_char_context(text: str, key: str) -> bool:
    for match in re.finditer(re.escape(key), text):
        following = text[match.end():match.end() + 1]
        if following and key in _SINGLE_BLOCK and following in _SINGLE_BLOCK[key]:
            continue
        if _SINGLE_CONTEXT.search(text[max(0, match.start() - 8):match.end() + 8]):
            return True
    return False


def _source_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not (parsed.username or parsed.password)
    except ValueError:
        return False


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone())


def lookup(text: str, limit: int = 3) -> list[dict]:
    """Return admitted, unexpired definitions with declared source and review provenance.

    ``usage_context`` and ``avoid_when`` come from the watcher side table when
    present. They are the difference between knowing a term exists and knowing
    how to use it.
    """
    source, now = semantic_text(text), _now()
    if not source.strip():
        return []
    try:
        with closing(_connect()) as conn:
            if not _has_table(conn, "terms") or not _has_table(conn, "term_reviews"):
                return []
            has_meta = _has_table(conn, "watcher_entry_meta")
            cols = ("t.*,r.status,r.reviewed_at,r.source_url,r.actor"
                    + (",m.usage_context as usage_context,m.avoid_when as avoid_when,m.published_at as published_at"
                       if has_meta else ""))
            join = " left join watcher_entry_meta m on m.term=t.term" if has_meta else ""
            # Candidate pre-filter, done in SQL so thousands of rows never enter
            # Python. Match against a temp table of message windows: any term or
            # alias of length 2..24 that appears in the message is, by
            # definition, one of its substrings.
            head = source[:500]
            windows = {head[i:i + n]
                       for n in range(2, 25)
                       for i in range(len(head) - n + 1)}
            if not windows:
                return []
            conn.execute("create temp table if not exists _win(w text primary key)")
            conn.execute("delete from _win")
            conn.executemany("insert or ignore into _win(w) values(?)",
                             ((w.lower(),) for w in windows))
            placeholders = ",".join("?" for _ in _ADMITTED_STATUS)
            sql = f"""select {cols} from terms t
                join term_reviews r on r.term=t.term{join}
                where r.status in ({placeholders}) and t.confidence>=?
                and (t.ttl_days<=0 or t.last_seen + t.ttl_days*86400 > ?)
                and (exists (select 1 from _win where _win.w = lower(t.term))
                     or (trim(t.aliases) != '' and exists (
                            select 1 from _win
                            where instr(',' || lower(replace(replace(t.aliases,'，',','),'；',',')) || ',',
                                        ',' || _win.w || ',') > 0)))
                order by t.last_seen desc"""
            rows = conn.execute(sql, (*_ADMITTED_STATUS, _MIN_CONFIDENCE, now)).fetchall()
    except Exception:
        # Watcher absent, database locked, or schema owned by a newer version.
        return []
    found = []
    for row in rows:
        keys = [key for key in _term_keys(row) if _contains_key(source, key) and
                (len(key) != 1 or _single_char_context(source, key))]
        if not keys:
            continue
        updated = float(row["last_seen"])
        keys_map = row.keys()
        found.append({"term": row["term"], "meaning": row["meaning"], "source": row["source"],
                      "origin": row["origin"] or "",
                      "source_url": row["source_url"], "source_scope": "private_reference" if row["source"].startswith("MEMORY") else "declared_reference",
                      "usage_context": (row["usage_context"] or "") if "usage_context" in keys_map else "",
                      "avoid_when": (row["avoid_when"] or "") if "avoid_when" in keys_map else "",
                      "published_at": (row["published_at"] or None) if "published_at" in keys_map else None,
                      "record_updated_at": updated, "expires_at": updated + row["ttl_days"] * 86400 if row["ttl_days"] > 0 else None,
                      "days_left": max(0, int((updated + row["ttl_days"] * 86400 - now) / 86400)) if row["ttl_days"] > 0 else None,
                      "aliases": [key for key in _term_keys(row) if key != row["term"]],
                      "matched_keys": keys,
                      "review": {"status": row["status"], "recorded_at": row["reviewed_at"], "actor": row["actor"]},
                      "confidence": float(row["confidence"]), "_score": max(map(len, keys)) * 2 + float(row["confidence"])})
    found.sort(key=lambda item: item["_score"], reverse=True)
    for item in found:
        item.pop("_score")
    return found[:max(1, int(limit))]


_FRESH_PATTERNS = (
    ("发售/上架", re.compile(r"发售|上架|开卖|预售|预约|抢购|库存|补货|首发")),
    ("价格", re.compile(r"价格|多少钱|降价|涨价|打折|优惠|史低|白嫖|免费")),
    ("版本/更新", re.compile(r"版本|补丁|DLC|资料片|上线|停服|复刻|重制|移植|大更新|小更新|更新公告", re.I)),
    ("日期", re.compile(r"几号|日期|发布会|跳票|延期|发售日")),
)


def freshness_check(text: str) -> dict:
    """Report whether a time-sensitive claim has a still-fresh cached source.

    Read-only: the watcher owns ``search_cache`` writes now, so this only reads.
    """
    source, now = semantic_text(text)[:6000], _now()
    for reason, pattern in _FRESH_PATTERNS:
        match = pattern.search(source)
        if match:
            cached = []
            try:
                with closing(_connect()) as conn:
                    if _has_table(conn, "search_cache"):
                        rows = conn.execute("select * from search_cache where fetched_at+ttl_seconds>? order by fetched_at desc limit 100", (now,)).fetchall()
                        cached = [dict(row) for row in rows if _source_url(row["source_url"]) and len(row["query"]) >= 2 and _contains_key(source, row["query"])]
            except Exception:
                cached = []
            return {"needs_search": not bool(cached), "status": "cached_source" if cached else "current_fact_unverified",
                    "reason": reason, "keyword": match.group(0),
                    "snippet": _one_line(source[max(0, match.start() - 25):match.end() + 45], 80),
                    "observed_at": now, "cached_sources": cached[:3], "source_coverage": "query_matches_only", "full_fact_coverage": False}
    return {"needs_search": False, "status": "no_temporal_trigger", "reason": "", "keyword": "", "snippet": "", "cached_sources": []}


def scan(text: str, link_id: str = "", max_candidates: int = 20, high_signal_only: bool = False) -> list[str]:
    """No-op shim. Candidate mining belongs to ``alma-meme-watcher``.

    Kept so existing callers keep working without branching; it deliberately
    reports no new candidates because this module no longer writes to the
    ``candidates`` table.
    """
    return []


def annotate(text: str, link_id: str = "") -> list[str]:
    """Flat glossary lines for callers that still want them.

    Newer code paths prefer ``meme_inject.brief_text`` (behaviour, not a
    dictionary dump); this stays for compatibility and carries freshness only.
    """
    source = semantic_text(text)
    if not source.strip():
        return []
    import json
    matches, fresh = lookup(source, 2), freshness_check(source)
    lines = ["〔词条〕%s = %s；来源=%s；记录时间=%.0f" %
             (item["term"], _one_line(item["meaning"], 80), _one_line(item["source"], 50), item["record_updated_at"]) for item in matches]
    if fresh["status"] != "no_temporal_trigger":
        lines.append("〔时效数据〕" + json.dumps({key: fresh[key] for key in ("status", "reason", "snippet", "observed_at")}, ensure_ascii=False, separators=(",", ":")))
    return lines


def stats() -> dict:
    """Read-only counters, for diagnostics only."""
    empty = {"terms": 0, "candidates": 0, "admitted": 0, "available": False}
    try:
        with closing(_connect()) as conn:
            if not _has_table(conn, "terms"):
                return empty
            terms = conn.execute("select count(*) from terms").fetchone()[0]
            candidates = conn.execute("select count(*) from candidates").fetchone()[0] if _has_table(conn, "candidates") else 0
            placeholders = ",".join("?" for _ in _ADMITTED_STATUS)
            admitted = conn.execute(
                f"select count(*) from terms t join term_reviews r on r.term=t.term where r.status in ({placeholders}) and t.confidence>=?",
                (*_ADMITTED_STATUS, _MIN_CONFIDENCE)).fetchone()[0]
            return {"terms": terms, "candidates": candidates, "admitted": admitted, "available": True}
    except Exception:
        return empty


def main():
    import json
    import sys
    argv = sys.argv[1:]
    if not argv:
        print(json.dumps(stats(), ensure_ascii=False))
        return
    if argv[0] == "lookup":
        print(json.dumps(lookup(" ".join(argv[1:])), ensure_ascii=False, indent=2))
        return
    print(json.dumps({"usage": "meme_radar.py [lookup <text>]"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
