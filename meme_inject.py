"""Turn meme lookups into a short behavioural brief, not a dictionary dump.

Design borrowed from dsh-memewiki's hard-won rule: knowing a term exists is
worthless on its own. What changes behaviour is *when to use it*, *when it is
forbidden*, and *don't explain it unless asked*. So this module emits at most
one entry, keeps the whole block under a few hundred characters, and always
carries a reuse rule — never a bare meaning string.

The dictionary itself is owned by the standalone ``alma-meme-watcher`` plugin.
``meme_radar`` in this skill is only a read-only reader over that database, so
this module never mines candidates or writes term data either.

Touches no network and never writes term data. The only file it may write is
the slider config at ``state/meme_tone.json``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Hard cap so a busy post can never flood the prompt with lore.
MAX_ENTRY_CHARS = 320
MAX_BLOCK_CHARS = 620

# Posts that demand a straight answer. Meme use drops to zero here regardless of
# what the dictionary allows — the radar knows the term, not the situation.
_SERIOUS_RE = re.compile(
    r"求助|求助帖|怎么解决|怎么办|救救|排障|报错|崩溃|蓝屏|开不了机|装不上|驱动|"
    r"退款|投诉|维权|被骗|翻车|避雷|求推荐|求教|请问|有没有大佬|"
    r"生病|受伤|医院|手术|去世|悼念|讣告|事故|报警|法律|离婚|分手"
)

# Casual ground where light teasing is community-normal.
_PLAYFUL_RE = re.compile(
    r"整活|沙雕|搞笑|笑死|哈哈|绷不住|离谱|抽象|玩梗|乐|吐槽|段子|表情包|"
    r"什么梗|啥梗|太好笑|笑不活了|乐子"
)

_ASK_RE = re.compile(
    r"(?:是)?(?:什么梗|啥梗|什么来头|什么来历|什么出处|什么意思|啥意思|怎么用|怎么接|怎么玩|如何用)"
    r"(?:啊|呀|呢|的)?[？?。！!~～]*$"
)


# ---------- 玩梗尺度滑杆（0-100） ----------
#
# 数值越大越放得开。它是**上限**，不是命令：帖文语气可以把实际尺度往下压，
# 但永远不会往上抬。求助/排障帖另有一条硬规则，任何设置都绕过不了。

CONFIG_PATH = Path.home() / ".config" / "alma" / "state" / "meme_tone.json"
DEFAULT_PLAYFULNESS = 45

_BANDS = (
    (20, {"level": "strict", "label": "很低",
          "allow": "只接正面梗、自嘲、夸人",
          "deny": "黑称一律不接、不复读，别人刷也不跟"}),
    (45, {"level": "low", "label": "偏低",
          "allow": "损操作、损发挥",
          "deny": "不损长相、不损疾病，不带头用黑称"}),
    (70, {"level": "medium", "label": "中等",
          "allow": "按社区语境来，损操作、损战绩、损名言都行",
          "deny": "冲家人的词不带头用"}),
    (100, {"level": "high", "label": "很高",
           "allow": "放开了玩，别人先甩的黑称可以接",
           "deny": "只对真事，不自己编新攻击"}),
)


def _band_for(value: int) -> dict:
    for ceiling, band in _BANDS:
        if value <= ceiling:
            return band
    return _BANDS[-1][1]


def playfulness() -> int:
    """Read the slider, clamped to 0-100. Missing or corrupt config is harmless."""
    try:
        raw = json.loads(CONFIG_PATH.read_text("utf-8"))
        value = int(raw.get("playfulness", DEFAULT_PLAYFULNESS))
    except Exception:
        return DEFAULT_PLAYFULNESS
    return max(0, min(100, value))


def set_playfulness(value: int) -> dict:
    """Persist the slider. Other keys in the file are preserved."""
    clamped = max(0, min(100, int(value)))
    data = {}
    try:
        data = json.loads(CONFIG_PATH.read_text("utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data["playfulness"] = clamped
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    band = _band_for(clamped)
    return {"playfulness": clamped, "label": band["label"], "allow": band["allow"], "deny": band["deny"]}


def _trim(text: object, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    cut = value[:limit]
    # Prefer cutting at a clause boundary so the tail never reads amputated.
    for sep in ("；", "。", "，", ";", ","):
        pos = cut.rfind(sep)
        if pos >= limit * 0.55:
            return cut[:pos]
    return cut


def _asks_about_term(text: str, hit: dict) -> bool:
    """True only when the user is explicitly asking what this term means.

    The user usually asks with an alias ("玉芬啊玉芬是什么梗"), not the canonical
    term, so every known spelling has to be accepted here.
    """
    if not _ASK_RE.search(text or ""):
        return False
    lowered = (text or "").lower()
    spellings = [str(hit.get("term") or "").lower()]
    # meme_radar returns matching keys through the canonical row; aliases are
    # not always present on the hit, so fall back to the asked-about substring.
    aliases = hit.get("aliases")
    if isinstance(aliases, str):
        spellings.extend(a.strip().lower() for a in re.split(r"[,，;；|/]", aliases) if a.strip())
    elif isinstance(aliases, (list, tuple)):
        spellings.extend(str(a).strip().lower() for a in aliases if str(a).strip())
    if any(s and s in lowered for s in spellings):
        return True
    # Last resort: the question target itself is the term being asked about.
    asked = _ASK_RE.sub("", text or "").strip(" ？?。！!~～，,是")
    return bool(asked) and asked.lower() in spellings


def tone_for(text: str) -> dict:
    """Decide how loose the reply may be.

    Two inputs, and their order matters:

    * a post that asks for real help always wins — no setting can make joking
      on a broken-laptop thread acceptable;
    * otherwise the configured slider sets the ceiling, and the post's own tone
      can only pull it *down*, never push it up.

    The slider is therefore a limit on looseness, not a licence to ignore
    context. See ``playfulness()`` for the 0-100 bands.
    """
    slider = playfulness()
    slider_band = _band_for(slider)

    if _SERIOUS_RE.search(text or ""):
        return {"level": "serious", "label": "严肃", "slider": slider,
                "rule": "这个帖在求助/排障/维权，别玩梗，直接给能用的答案。"}

    # Post tone can only tighten the ceiling, never loosen it.
    if _PLAYFUL_RE.search(text or ""):
        effective = min(slider, 70)
        band = _band_for(effective)
    else:
        effective = min(slider, 45)
        band = _band_for(effective)

    label = band["label"]
    lines = [f"该梗按「{band['label']}」档使用。"]
    if band["allow"]:
        lines.append("可接：" + band["allow"] + "。")
    if band["deny"]:
        lines.append("不可：" + band["deny"] + "。")
    return {"level": band["level"], "label": label, "slider": effective,
            "rule": "".join(lines)}


def _same_gist(a: str, b: str) -> bool:
    """True when two snippets say essentially the same thing.

    Character-overlap is enough here: imported entries literally reuse the
    usage opening as their meaning, so the check only has to catch that case,
    not judge semantic similarity.
    """
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a.startswith(b[:20]) or b.startswith(a[:20]):
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return shorter[:24] in longer


def _entry_block(hit: dict, asked: bool) -> str:
    term = hit.get("term", "")
    meaning = _trim(hit.get("meaning"), 70)
    usage = _trim(hit.get("usage_context"), 80)
    avoid = _trim(hit.get("avoid_when"), 80)
    lines = [f"「{term}」{meaning}"]
    # Imported entries derive meaning from the opening sentence of usage, so the
    # two often match almost verbatim. Repeating it wastes the whole budget.
    if usage and not _same_gist(meaning, usage):
        lines.append(f"接法：{usage}")
    if avoid:
        lines.append(f"禁用：{avoid}")
    if asked:
        # Explicit question: one line of origin is welcome, more is lecturing.
        origin = _trim(hit.get("origin"), 60)
        if origin:
            lines.append(f"出处：{origin}")
    block = "\n".join(lines)
    return block[:MAX_ENTRY_CHARS]


def brief(text: str, *, limit: int = 1) -> dict:
    """Return a ready-to-inject brief, or an empty one when nothing matched.

    ``{"text": str, "terms": [...], "tone": {...}}`` — ``text`` is '' when the
    radar has nothing to say, so callers can concatenate unconditionally.
    """
    try:
        import meme_radar
        hits = meme_radar.lookup(text, limit=limit)
    except Exception:
        return {"text": "", "terms": [], "tone": tone_for(text)}

    tone = tone_for(text)
    if not hits:
        return {"text": "", "terms": [], "tone": tone}

    hit = hits[0]
    asked = _asks_about_term(text, hit)

    if tone["level"] == "serious":
        # Still tell the model the term is known, so it neither guesses nor
        # performs the meme on a thread where that would land badly.
        head = ["[梗雷达] 这帖是严肃求助，下面这个梗别用："]
        head.append(f"「{hit.get('term','')}」{_trim(hit.get('meaning'), 50)}")
        body = "\n".join(head)
    else:
        rules = [
            "[梗雷达·接梗规则]",
            "1. 命中词条就先接、别科普：照“接法”回一句，像本来就会。",
            "2. 只有对方明确问“这是什么梗”才解释出处，一两句够。",
            "3. “禁用”里写到的场景直接不用，别硬玩。",
            "4. 查不到就说不知道，别硬编，别把两个不相干的梗拼一起。",
            "5. 用短句，别客服腔。",
        ]
        body = "\n".join(rules) + f"\n\n[尺度·{tone['label']}] {tone['rule']}\n\n" + _entry_block(hit, asked)

    return {"text": body[:MAX_BLOCK_CHARS], "terms": [h.get("term", "") for h in hits], "tone": tone}


def brief_text(text: str, *, limit: int = 1) -> str:
    """Convenience wrapper returning just the injectable string."""
    return brief(text, limit=limit)["text"]


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if argv and argv[0] in {"tone", "scale"}:
        # `python meme_inject.py tone`        -> show current slider
        # `python meme_inject.py tone 65`     -> set and show
        if len(argv) > 1:
            print(json.dumps(set_playfulness(argv[1]), ensure_ascii=False, indent=2))
        else:
            value = playfulness()
            band = _band_for(value)
            print(json.dumps({"playfulness": value, **band}, ensure_ascii=False, indent=2))
    else:
        print(brief_text(" ".join(argv) or ""))
