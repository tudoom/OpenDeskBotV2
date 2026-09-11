"""断句与称呼的语义兜底（2026-09-09「叫我小朋友」被截成「叫我小」）。

语音 Agent 的断句只靠 VAD 静音：报名字时"叫我…小朋友"中间 0.3 秒的停顿就会把
"小"当成一整句提交，随后小歪开口、麦克风暂停，"朋友"被吞掉。这里做三件事：

1. ``looks_incomplete``：转写以明显没说完的词结尾（叫我 / 我叫 / 称呼 / 和 / 然后…）；
2. ``DeskbotTurnDetector``：LiveKit 的 turn detector 协议实现——半句话就把端点等待
   拉到 max_endpointing_delay，让后半句并进同一轮；
3. ``extract_address_name`` / ``name_is_suspicious``：称呼只有一个字或像截断的半句时，
   memory_add / 结题工具拒绝写入，让小歪先反问确认。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# 明显没说完的句尾：称呼/名字引导词、连词、介词、量词。仅对 ≥3 字的句子生效。
INCOMPLETE_TAILS: tuple[str, ...] = (
    "叫我", "我叫", "叫他", "叫她", "叫你", "叫做", "叫作", "称呼", "称呼我", "称我", "称为",
    "名字是", "名字叫", "外号是", "小名是", "我是", "他是", "她是", "这是", "那是",
    "和", "跟", "与", "或者", "还是", "然后", "还有", "以及", "因为", "但是", "如果", "比如",
    "帮我", "把", "给", "让", "在", "到", "从", "去", "想", "要", "叫", "个", "那个", "这个", "一个",
)
_NAME_CUE = re.compile(
    r"(?:称呼(?:他|她|我|你|主人)?(?:为|是|叫)?|叫(?:他|她|我|你|主人)?|名字(?:是|叫)|叫做|叫作|称(?:他|她|我|你)?为)"
    r"\s*[“\"「『]?(?P<name>[^”\"」』，。！？、,.!?;；:：\s]{0,16})"
)
_PUNCT = "，。！？、,.!?;；:：~～…—-\"“”'‘’「」『』()（） \t"
_STOP_NAMES = {"小", "老", "阿", "大", "那个", "这个", "什么", "随便", "都行", "不", "没"}


def _strip(text: str) -> str:
    return str(text or "").strip().strip(_PUNCT).strip()


_NAME_CUE_TAILS = (
    "叫我", "我叫", "叫他", "叫她", "叫你", "叫做", "叫作", "称呼", "称呼我", "称我", "称为",
    "名字是", "名字叫", "外号是", "小名是",
)
_TRAILING_FILLERS = ("就可以了", "就可以", "就行了", "就行", "就好了", "就好", "好了", "吧", "呀", "啦", "哦", "呢", "了", "哈")


def looks_incomplete(text: str) -> bool:
    """转写看起来是半句话（据此把端点等待拉长，等后半句）。"""
    body = _strip(text)
    if not body:
        return False
    # 称呼/名字引导词收尾：哪怕只有两个字（"我叫"）也是没说完
    for tail in sorted(_NAME_CUE_TAILS, key=len, reverse=True):
        if body.endswith(tail):
            return True
    if len(body) < 3:
        return False
    for tail in sorted(INCOMPLETE_TAILS, key=len, reverse=True):
        if body.endswith(tail):
            # "叫我小" 这种：引导词后只剩 1 个字，也算没说完
            return True
    m = None
    for m in _NAME_CUE.finditer(body):
        pass
    if m is not None and len(m.group("name")) <= 1 and m.end() >= len(body) - 1:
        return True
    return False


def extract_address_name(text: str) -> str | None:
    """从"主人希望我称呼他为“小”"这类记忆/结果文本里抽出称呼；抽不到返回 None。"""
    body = str(text or "")
    if not any(k in body for k in ("称呼", "叫我", "叫他", "叫她", "叫你", "名字", "叫做", "叫作", "称为")):
        return None
    last = None
    for last in _NAME_CUE.finditer(body):
        pass
    if last is None:
        return None
    name = _strip(last.group("name"))
    # 去掉"就行 / 吧 / 呀"这类口语尾巴（"叫他小朋友就行" → 小朋友）
    changed = True
    while changed and len(name) > 1:
        changed = False
        for filler in _TRAILING_FILLERS:
            if name.endswith(filler) and len(name) > len(filler):
                name = name[: -len(filler)]
                changed = True
    return name or ""


def name_is_suspicious(name: str | None) -> bool:
    """称呼像被截断：空、只有 1 个字、或是"小/老/阿"这类只会出现在名字开头的字。"""
    if name is None:
        return False
    body = _strip(name)
    if len(body) <= 1:
        return True
    if body in _STOP_NAMES:
        return True
    return looks_incomplete(body + "呀") and False  # 占位：名字本身不做句尾判断


def address_confirmation_hint(name: str | None) -> str:
    shown = _strip(name or "") or "……"
    return (
        f"称呼「{shown}」看起来不完整，很可能是断句把话截断了（比如“小朋友”只听到“小”）。"
        f"先别记录，向主人确认一句：“你是说叫你「{shown}」吗，还是没说完？”，等主人说清楚再记。"
    )


def is_address_memory(text: str) -> bool:
    return extract_address_name(text) is not None


def supersede_address_memories(entries: Iterable[dict[str, Any]], keep_id: str) -> list[str]:
    """同一主题（称呼）的旧记忆作废：返回应删除的 id 列表。"""
    doomed: list[str] = []
    for entry in entries:
        eid = str(entry.get("id") or "")
        if not eid or eid == keep_id:
            continue
        if is_address_memory(str(entry.get("text") or "")):
            doomed.append(eid)
    return doomed


class DeskbotTurnDetector:
    """LiveKit `_TurnDetector` 协议：半句话 → 概率 0.1（低于阈值 0.5，等到 max 延迟）。"""

    UNLIKELY = 0.5

    @property
    def model(self) -> str:
        return "deskbot-heuristic-zh"

    @property
    def provider(self) -> str:
        return "deskbot"

    async def unlikely_threshold(self, language: Any = None) -> float | None:
        return self.UNLIKELY

    async def supports_language(self, language: Any = None) -> bool:
        return True

    @staticmethod
    def last_user_text(chat_ctx: Any) -> str:
        items = list(getattr(chat_ctx, "items", None) or [])
        for item in reversed(items):
            if getattr(item, "role", None) != "user":
                continue
            text = getattr(item, "text_content", None)
            if text is None:
                content = getattr(item, "content", None)
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "".join(c for c in content if isinstance(c, str))
            return str(text or "")
        return ""

    async def predict_end_of_turn(self, chat_ctx: Any, *, timeout: float | None = None) -> float:
        text = self.last_user_text(chat_ctx)
        return 0.1 if looks_incomplete(text) else 0.9


__all__ = [
    "DeskbotTurnDetector",
    "INCOMPLETE_TAILS",
    "address_confirmation_hint",
    "extract_address_name",
    "is_address_memory",
    "looks_incomplete",
    "name_is_suspicious",
    "supersede_address_memories",
]
