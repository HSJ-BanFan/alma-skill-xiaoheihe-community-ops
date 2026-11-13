"""Backfill ``avoid_when`` for imported meme entries via Magpie.

This is the replacement for the copy deleted during the 14:10 meme_radar
retirement. Two behaviour changes from the original, both learned the hard way:

1. **The model can vanish.** ``antigravity/gemini-3.8-flash`` started timing out
   mid-run at item ~4100 of 6228 and every item then burned the full 120s
   timeout. A dead model must be detectable and reportable, not silently
   retried forever.
2. **Failures must be visible.** The old script swallowed the exception into a
   counter, so stderr stayed empty while 29 items failed in a row and the
   progress counter appeared merely "slow".

Model choice: ``opencode-go/deepseek-v4.1-flash``. It answers in ~2s and needs
``max_tokens`` of several hundred because reasoning tokens are charged before
the content, so a small cap yields an empty string.

Writes only to ``watcher_entry_meta.avoid_when``. Uses WAL + short commits so
the reader (the Xiaoheihe skill) is never blocked.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DB_PATH = Path.home() / ".config" / "alma" / "state" / "meme_radar.db"
SOURCE_DB = Path.home() / ".config" / "alma" / "state" / "memewiki" / "memes-source.db"
URL = "http://127.0.0.1:3425/v1/chat/completions"
HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer magpie-alma"}

DEFAULT_MODEL = "opencode-go/deepseek-v4.1-flash"
# Reasoning tokens are charged first; a small cap returns empty content.
MAX_TOKENS = 800
DEFAULT_WORKERS = 4
RETRIES = 2

PROMPT = """你是网络社区运营专家。下面是一个网络梗词条，请判断：**在哪些场合绝对不能使用这个梗**。

词条名：{title}
标签：{tags}
用法：{usage}

要求：
- 只输出一行纯文本，不要 JSON、不要编号、不要解释、不要引号，不要以"禁用："开头。
- 30~60 字，写具体的禁用场景（哪些对象、哪些情境）。
- 涉及真人、疾病、灾难、性别、地域、政治、未成年人等敏感面时必须点名。
- 如果确实没有明显禁忌，输出：无明显禁忌，常规调侃即可。
"""

_write_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma busy_timeout=30000")
    conn.execute("""create table if not exists watcher_entry_meta(
        term text primary key, usage_context text, avoid_when text, published_at text)""")
    conn.commit()


def pending_terms(conn: sqlite3.Connection, limit: int | None) -> list[str]:
    sql = """
    select t.term from terms t
    left join watcher_entry_meta m on m.term = t.term
    where t.source = 'memewiki_import'
      and (m.avoid_when is null or trim(m.avoid_when) = '')
    order by t.heat desc, t.term
    """
    if limit:
        sql += f" limit {int(limit)}"
    return [r[0] for r in conn.execute(sql)]


def ask(term: str, usage: str, tags: str, model: str) -> tuple[str | None, str | None]:
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": PROMPT.format(
            title=term, tags=tags or "", usage=(usage or "")[:400])}],
    }
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.load(resp)
        choice = data["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        text = text.strip("\"'「」“”").replace("\n", " ").strip()
        if text.startswith("禁用："):
            text = text[3:].strip()
        if not text:
            # Reasoning ate the budget — this is a real failure, not a no-op.
            return None, f"empty content (finish={choice.get('finish_reason')})"
        return text[:300], None
    except Exception as error:
        return None, f"{type(error).__name__}: {str(error)[:80]}"


def run(limit: int | None, workers: int, model: str) -> dict:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    terms = pending_terms(conn, limit)
    if not terms:
        conn.close()
        return {"pending": 0, "done": 0, "failed": 0}

    usage_map: dict[str, tuple[str, str]] = {}
    if SOURCE_DB.exists():
        src = sqlite3.connect(SOURCE_DB)
        for title, usage, tags in src.execute("select title, usage, tags from memes"):
            usage_map[title] = (usage or "", tags or "")
        src.close()

    log(f"待补 {len(terms)} 条 | 模型 {model} | 并发 {workers}")
    total, done, failed = len(terms), 0, []
    pending_commit: list[str] = []
    started = time.time()

    def work(term: str):
        usage, tags = usage_map.get(term, ("", ""))
        last_err = None
        for attempt in range(RETRIES + 1):
            text, err = ask(term, usage, tags, model)
            if text:
                return term, text, None
            last_err = err
            # A timeout usually means the model is gone; back off briefly then retry.
            if attempt < RETRIES:
                time.sleep(3 * (attempt + 1))
        return term, None, last_err

    consecutive_failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, t): t for t in terms}
        for idx, fut in enumerate(as_completed(futures), 1):
            term, text, err = fut.result()
            if text:
                with _write_lock:
                    conn.execute(
                        "insert into watcher_entry_meta(term, usage_context, avoid_when, published_at) "
                        "values(?,?,?,?) on conflict(term) do update set avoid_when=excluded.avoid_when",
                        (term, "", text, None))
                    pending_commit.append(term)
                    if len(pending_commit) >= 25:
                        conn.commit()
                        pending_commit.clear()
                done += 1
                consecutive_failures = 0
            else:
                failed.append({"term": term, "error": err})
                consecutive_failures += 1
                if consecutive_failures == 8:
                    log(f"⚠️ 连续失败 {consecutive_failures} 条，最后错误：{err}")
                    log("   上游模型可能已不可用，建议 Ctrl-C 后换模型重跑")

            if idx % 50 == 0 or idx == total:
                elapsed = time.time() - started
                rate = idx / elapsed if elapsed else 0
                left = (total - idx) / rate / 60 if rate else 0
                log(f"  {idx}/{total} | 成功 {done} 失败 {len(failed)} | "
                    f"{rate:.2f}条/秒 | 剩余约 {left:.0f} 分钟")

    with _write_lock:
        conn.commit()
    conn.close()

    result = {"pending": total, "done": done, "failed": len(failed),
              "failures": failed[:10], "seconds": round(time.time() - started, 1),
              "model": model}
    if failed:
        log(f"失败 {len(failed)} 条，样例：{failed[:3]}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 条，用于试水")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()
    print(json.dumps(run(args.limit, args.workers, args.model), ensure_ascii=False, indent=2))
