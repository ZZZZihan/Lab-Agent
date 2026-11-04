"""Chinese tokenization for indexing and queries. No model download."""

from __future__ import annotations

import logging
import re

import jieba

jieba.setLogLevel(logging.ERROR)
jieba.initialize()

for word in (
    "清衡",
    "组会",
    "学术例会",
    "赫利俄斯",
    "流明",
    "交互式",
    "续约",
    "脱敏",
    "门禁",
    "配额",
    "备案",
    "轮值",
    "抽检",
    "A100",
    "学术活动",
    "登录节点",
    "引用对齐",
    "人工抽检",
    "发布门禁",
    "开发集",
    "测试集",
    "拒答",
    "实体会议室",
    " faithfulness",
):
    jieba.add_word(word.strip())

STOPWORDS = {
    "的",
    "了",
    "吗",
    "呢",
    "吧",
    "啊",
    "和",
    "与",
    "及",
    "或",
    "在",
    "是",
    "有",
    "不",
    "也",
    "就",
    "都",
    "而",
    "被",
    "把",
    "对",
    "从",
    "以",
    "为",
    "其",
    "这",
    "那",
    "一个",
    "我们",
    "你们",
    "他们",
    "以及",
    "如果",
    "是否",
    "如何",
    "什么",
    "哪些",
    "哪个",
    "哪里",
    "多少",
    "可以",
    "需要",
    "进行",
    "使用",
    "通过",
    "关于",
    "其中",
    "已经",
    "没有",
    "不是",
    "这个",
    "那个",
    "请问",
    "一下",
    "怎么",
    "怎样",
    "实验室",
    "还是",
    "或者",
    "因为",
    "所以",
    "哪间",
    "几点",
    "谁",
}

_IDENT = re.compile(
    r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|(?:[a-z0-9-]+\.)+(?:lab|com|net)"
    r"|/mnt/[\w./-]+"
    r"|notes/[\w./-]+"
    r"|git\.qingheng\.lab/[\w.-]+"
    r"|\d+(?:\.\d+){3}/\d+"
    r"|\d{2,4}(?:-\d{2,4}){1,3}"
    r"|[A-Za-z]{1,12}-\d{2,4}"
    r"|\d+(?:GB|TB|MB)"
    r"|\d+:\d+"
    r"|\d+(?:\.\d+)?",
    re.IGNORECASE,
)
_CJK_BIGRAM = re.compile(r"[\u4e00-\u9fff]{2}")


def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens: list[str] = []
    seen_idents: list[str] = []
    for match in _IDENT.finditer(lowered):
        ident = match.group(0).strip(".,;:，。")
        if ident:
            seen_idents.append(ident)
    for token in jieba.lcut(lowered, cut_all=False):
        token = token.strip().lower()
        if not token or token in STOPWORDS:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        if all(not char.isalnum() and char not in "._/-" for char in token):
            continue
        tokens.append(token)
    tokens.extend(seen_idents)
    return tokens


def content_tokens(text: str) -> list[str]:
    return [token for token in tokenize(text) if token not in STOPWORDS]


def char_bigrams(text: str) -> list[str]:
    compact = "".join(char for char in text if "\u4e00" <= char <= "\u9fff")
    return [compact[index : index + 2] for index in range(len(compact) - 1)]


def tsquery(tokens: list[str], limit: int = 32) -> str:
    parts: list[str] = []
    for token in tokens:
        cleaned = re.sub(r"[^\w\u4e00-\u9fff./:-]+", "", token, flags=re.UNICODE)
        cleaned = cleaned.replace(":", "").replace("-", "").replace("/", "").replace(".", "")
        if cleaned:
            parts.append(cleaned)
        if len(parts) >= limit:
            break
    return " | ".join(dict.fromkeys(parts))
