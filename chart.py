from typing import Self
from judge_line import JudgeLine


class Chart:
    version: int
    offset: float
    judge_lines: list[JudgeLine]
    block_areas: list

    def __init__(self, version: int, offset: float, judge_lines: list[JudgeLine],
                 block_areas: list | None = None) -> None:
        self.version = version
        self.offset = offset
        self.judge_lines = judge_lines
        # 4.0.1 噪点红场。没有这个字段的谱面就是空列表, 旧算法完全不看它。
        self.block_areas = list(block_areas or [])

    @classmethod
    def from_dict(cls, d: dict) -> Self:
        version = d['formatVersion']
        areas = d.get('blockAreaList') or []
        if version == 1:
            lines = [*map(JudgeLine.from_dict_v1, d['judgeLineList'])]
        elif version == 2:
            lines = [*map(JudgeLine.from_dict, d['judgeLineList'])]
        else:
            # formatVersion 3(Phigros 3.20.0+新官谱)：
            # 结构同v2，但speedEvents移除了floorPosition，需要累积推导
            lines = [*map(JudgeLine.from_dict_v3, d['judgeLineList'])]
        return cls(version, d['offset'], lines, areas)


__all__ = ['Chart']
