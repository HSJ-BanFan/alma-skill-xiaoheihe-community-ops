#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小黑盒账号画像 —— 把收藏/CY/自己的帖子/活跃度拉下来，聚合成一份可读的 Markdown。

用法：
    python xhh_profile.py                    # 输出到 stdout
    python xhh_profile.py --out 画像.md       # 写到文件
    python xhh_profile.py --limit 300        # 收藏最多拉多少条

设计原则：
- 只读，不写任何账号数据
- 收藏口味靠"标题关键词"分类，不靠 LLM，跑一次几秒钟
- 输出是 Markdown，能直接贴进聊天或存进笔记
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SKILL_DIR = Path(r"C:\Users\33308\.config\alma\skills\xiaoheihe-community-ops")
STATE_DIR = Path.home() / ".config/alma/state"
TASTE_FILE = STATE_DIR / "xhh_taste.json"
sys.path.insert(0, str(SKILL_DIR))

from xhh_tool import _get_client  # noqa: E402


# ---------- 收藏口味分类规则 ----------
# 每条：(类别名, 正则)。按顺序匹配，命中第一条即归类。
RULES: list[tuple[str, str]] = [
    ("AI/Bot/本地部署", r"AI|ai|DeepSeek|deepseek|GPT|大模型|LLM|提示词|prompt|Bot|机器人|猫娘|部署|LM Studio|语音克隆|变声|RVC|Agent|智能体|模型|算力|token|Gemini|Claude|opencode|dsh|DSH|肥鱼"),
    ("游戏攻略/攻略向", r"攻略|流派|配装|打法|萌新|入门|模组|mod|Mod|MOD|build|Build|机制|彩蛋|通关|配种|卡组|流派|星露谷|尖塔|小丑牌|战地|求生之路|帕鲁|蔚蓝|APEX|绝区零|杀戮"),
    ("工具/网站/软件", r"网站|软件|工具|插件|神器|合集|打包|配置|装机|清灰|装系统|启动器|客户端|APP|app|小程序|脚本|命令行|Windows|win|插件|扩展|浏览器|编辑器|效率"),
    ("学习/考研/英语", r"英语|四六级|考研|语法|词汇|四级|六级|知识点|总结|刷题|学习|复习|公式|线代|概率|高数|笔记|资料|公考|申论|行测|考试|复习|课程|教学|教程"),
    ("Cos/写真/二次元", r"COS|cos|Cos|cosplay|写真|白丝|黑丝|女仆|JK|jk|猫耳|Cosplay|小姐姐|美少女|壁纸|娘|立绘|插画|二创"),
    ("游戏开发/美术", r"Blender|blender|像素|着色器|Shader|Shader|建模|渲染|独立游戏|独游|游戏开发|引擎|Unity|Godot|UE|美术|动效|打光|UI设计"),
    ("求职/工作/实习", r"简历|秋招|校招|面试|实习|内推|招聘|offer|Offer|月薪|工资|打工人|职场|上班|加班|裁员|离职|跳槽|副业|赚米"),
    ("大学生活/省钱", r"大学生|宿舍|省钱|零食|泡面|食堂|护肤|租房|生活费|便宜|薅羊毛|购物|网购|外卖|测评|好用"),
    ("视频/番剧/资源", r"看番|番剧|动漫|电视剧|电影|资源|免费|网盘|播放|B站|b站|流媒体|音乐|歌单|剪辑|网易云|Spotify"),
    ("数码/硬件/外设", r"显卡|CPU|cpu|内存|硬盘|主板|散热|键盘|鼠标|显示器|手机|笔记本|耳机|充电|数码|装机|矿卡|性能|跑分"),
]

TOPIC_NAMES = {
    "7214": "盒友杂谈",
    "549999": "校园生活",
}


def classify(title: str) -> str:
    for name, pat in RULES:
        if re.search(pat, title or ""):
            return name
    return "其他"


def fetch_favourites(client, limit: int = 300) -> list[dict]:
    """拉收藏列表（分页）。返回 [{id, title, author}]"""
    out: list[dict] = []
    offset = 0
    while offset < limit:
        try:
            res = client.transport.signed_request(
                "/bbs/app/profile/fav/folder/v2/links",
                query={"limit": "20", "offset": str(offset)})
        except Exception:
            break
        links = (res.get("result") or {}).get("links") or []
        if not links:
            break
        for item in links:
            sub = item.get("link") or {}
            lid = sub.get("linkid")
            if not lid:
                continue
            out.append({
                "id": str(lid),
                "title": sub.get("title") or "",
                "author": (sub.get("user") or {}).get("username") or "",
            })
        offset += 20
    return out


def fetch_profile_stats(client) -> dict:
    """账号统计：收藏数、CY数、发帖数、粉丝、等级"""
    try:
        res = client.transport.signed_request("/bbs/app/profile/fav/folders", query={})
        result = res.get("result") or {}
    except Exception:
        return {}
    # 统计藏在 user 节点里
    user = result.get("user") or {}
    if not user:
        # 兜底：有的版本直接在 result 里
        user = result if "favour_post_num" in result else {}
    keys = ["favour_post_num", "cy_comment_num", "post_link_num",
            "be_favoured_post_num", "post_article_num"]
    stats = {k: user.get(k) for k in keys if k in user}
    li = user.get("level_info") or {}
    if li.get("level"):
        stats["level"] = li["level"]
    return stats


def fetch_own_posts(client) -> list[dict]:
    try:
        return client.my_posts() or []
    except Exception:
        return []


def build_taste(favs: list[dict], stats: dict) -> dict:
    """机器可读的口味档 —— 给运营侧做收藏判断用，不是给人看的。

    只放能在当下做判断的东西：各类占比、高频作者、最近新增类型。
    故意不输出「所以你应该多收 X」这种建议 —— 那会把判断变成凑比例。
    """
    cats = Counter(classify(f["title"]) for f in favs)
    total = len(favs) or 1

    # 高频作者（>=2 条才算）
    authors = Counter(f["author"] for f in favs if f["author"])
    hot_authors = [
        {"name": a, "count": c}
        for a, c in authors.most_common(12) if c >= 2
    ]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sample_size": len(favs),
        "account": {
            "favourites": stats.get("favour_post_num"),
            "cy_comments": stats.get("cy_comment_num"),
            "posts": stats.get("post_link_num"),
            "level": stats.get("level"),
        },
        "taste": [
            {
                "category": name,
                "count": cnt,
                "share": round(cnt / total, 4),
            }
            for name, cnt in cats.most_common()
        ],
        "hot_authors": hot_authors,
        # 最新收的几条，用来判断"口味是不是变了"
        "recent": [
            {"title": f["title"][:60], "category": classify(f["title"])}
            for f in favs[:10]
        ],
    }


def load_taste(path: Path | None = None) -> dict | None:
    """读上次生成的 taste.json。没有就返回 None。"""
    p = path or TASTE_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_report(favs: list[dict], stats: dict, posts: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    lines.append(f"# 小黑盒账号画像")
    lines.append("")
    lines.append(f"生成时间：{now}")
    lines.append("")

    # 账号概览
    lines.append("## 账号概览")
    lines.append("")
    if stats:
        label = {
            "favour_post_num": "收藏帖子",
            "cy_comment_num": "CY 评论",
            "post_link_num": "发过的帖子",
            "be_favoured_post_num": "被别人收藏",
            "post_article_num": "文章",
            "level": "等级",
        }
        for k, v in stats.items():
            lines.append(f"- {label.get(k, k)}：**{v}**")
    else:
        lines.append("- （没拿到统计，接口可能变了）")
    lines.append(f"- 本次分析收藏样本：**{len(favs)}** 条")
    lines.append("")

    # 口味分布
    lines.append("## 收藏口味分布")
    lines.append("")
    if favs:
        cats = Counter(classify(f["title"]) for f in favs)
        total = len(favs)
        lines.append("| 类别 | 条数 | 占比 |")
        lines.append("|---|---|---|")
        for name, cnt in cats.most_common():
            pct = cnt / total * 100
            bar = "█" * max(1, int(pct / 3))
            lines.append(f"| {name} | {cnt} | {pct:.0f}% {bar} |")
        lines.append("")

        # 每类挑几条代表作
        lines.append("## 各类代表作")
        lines.append("")
        for name, _ in cats.most_common():
            picks = [f for f in favs if classify(f["title"]) == name][:4]
            lines.append(f"**{name}**")
            for p in picks:
                lines.append(f"- {p['title'][:50]}")
            lines.append("")

        # 高频作者
        authors = Counter(f["author"] for f in favs if f["author"])
        hot = [(a, c) for a, c in authors.most_common(8) if c >= 2]
        if hot:
            lines.append("## 反复收藏的作者")
            lines.append("")
            for a, c in hot:
                lines.append(f"- @{a} —— {c} 条")
            lines.append("")
    else:
        lines.append("（没拉到收藏）")
        lines.append("")

    # 自己的帖子
    if posts:
        lines.append("## 自己发过的帖子（最近 10 条）")
        lines.append("")
        for p in posts[:10]:
            title = (p.get("title") or "(无题)")[:40]
            cnum = p.get("comment_num", 0)
            lines.append(f"- {title} —— {cnum} 条评论")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="小黑盒账号画像")
    ap.add_argument("--out", help="输出文件路径（默认 stdout）")
    ap.add_argument("--limit", type=int, default=300, help="收藏最多拉多少条")
    ap.add_argument("--alias", default="main", help="账号别名")
    args = ap.parse_args()

    client = _get_client(args.alias)

    print("拉取收藏…", file=sys.stderr)
    favs = fetch_favourites(client, limit=args.limit)
    print(f"  拿到 {len(favs)} 条", file=sys.stderr)

    print("拉取账号统计…", file=sys.stderr)
    stats = fetch_profile_stats(client)

    print("拉取自己的帖子…", file=sys.stderr)
    posts = fetch_own_posts(client)

    report = build_report(favs, stats, posts)

    # 机器可读的口味档 —— 运营侧做收藏判断时读这个，不用每次都重跑画像
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    taste = build_taste(favs, stats)
    TASTE_FILE.write_text(
        json.dumps(taste, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"口味档已写入 {TASTE_FILE}", file=sys.stderr)

    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"已写入 {args.out}", file=sys.stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
