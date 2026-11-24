"""Recognize standalone shopping commands without interpreting free-form text."""

import re
from typing import Literal

from pydantic import BaseModel, Field


class NavigationCommand(BaseModel):
    command: Literal["next", "previous", "compare", "select", "unknown"]
    indexes: list[int] = Field(default_factory=list)
    raw: str


_NUMBER = r"(?:[0-9]{1,4}|[零一二两三四五六七八九]|[一二两三四五六七八九]?十[一二三四五六七八九]?)"


def _index(value: str) -> int:
    if value.isascii():
        return int(value)
    digits = "零一二三四五六七八九"
    value = value.replace("两", "二")
    if "十" in value:
        tens, units = value.split("十")
        return 10 * (digits.index(tens) if tens else 1) + digits.index(units or "零")
    return digits.index(value)


def parse_navigation(query: str) -> NavigationCommand:
    text = re.sub(r"[。！？?!.\s]+$", "", query.strip())
    if re.fullmatch(r"再?换一个|下一个|再来一个|再?换一款|还有吗|其他的呢|别的", text):
        return NavigationCommand(command="next", raw=query)
    if re.fullmatch(r"上一个|前一个|刚才那个|回到上一个", text):
        return NavigationCommand(command="previous", raw=query)
    if re.fullmatch(r"(?:对比|比较|比对)(?:一下|这两个|一下这两个)?", text):
        return NavigationCommand(command="compare", raw=query)
    compare = re.fullmatch(
        rf"(?:对比|比较|比对)(?:一下)?\s*第({_NUMBER})(?:个|款)?\s*(?:和|与|、)\s*第({_NUMBER})(?:个|款)?",
        text,
    )
    if compare:
        return NavigationCommand(command="compare", indexes=[_index(n) for n in compare.groups()], raw=query)
    if re.fullmatch(r"就这个|就选它", text):
        return NavigationCommand(command="select", raw=query)
    select = re.fullmatch(rf"(?:就)?选?第({_NUMBER})(?:个|款)(?:吧)?", text)
    if select:
        return NavigationCommand(command="select", indexes=[_index(select[1])], raw=query)
    return NavigationCommand(command="unknown", raw=query)
