"""D2 切分与标题启发式单元测试（纯函数，确定性）。"""

from learning.parse.chunking import (
    _merge_blocks,
    cap_text,
    chunk_text_blocks,
    detect_heading,
    split_blocks,
    split_long,
)


def test_detect_heading_patterns():
    assert detect_heading("第3章 机器学习") == 1
    assert detect_heading("第2节 方法") == 2
    assert detect_heading("11 模型评估") == 1
    assert detect_heading("1.1 背景") == 2
    assert detect_heading("2.3.1 细节") == 3
    assert detect_heading("一、概述") == 1
    assert detect_heading("   1.2 带缩进   ") == 2


def test_detect_heading_rejects_non_headings():
    assert detect_heading("这是正文，不是标题") is None
    assert detect_heading("结尾有句号。") is None
    assert detect_heading("x" * 41) is None  # 超长行
    assert detect_heading("多行\n内容") is None
    assert detect_heading("") is None


def test_split_blocks_drops_blank_and_strips():
    assert split_blocks("A\n\n  B  \n\n\nC\n") == ["A", "B", "C"]


def test_merge_blocks_keeps_headings_separate():
    blocks = split_blocks("第1章 引言\n\n短\n\n" + "A" * 50)
    assert _merge_blocks(blocks) == ["第1章 引言", "短\n" + "A" * 50]


def test_merge_flushes_buffer_before_heading():
    blocks = split_blocks("短\n\n第2章 方法")
    assert _merge_blocks(blocks) == ["短", "第2章 方法"]


def test_split_long_respects_line_boundaries():
    block = "\n".join(f"line-{i:03d}" for i in range(30))
    parts = split_long(block, max_chars=60)
    assert len(parts) > 1
    assert all(len(p) <= 60 for p in parts)
    assert [ln for p in parts for ln in p.split("\n")] == block.split("\n")


def test_split_long_hard_cut_for_single_huge_line():
    parts = split_long("Z" * 100, max_chars=40)
    assert [len(p) for p in parts] == [40, 40, 20]


def test_cap_text():
    assert cap_text("abc", 5) == ("abc", False)
    assert cap_text("abcdef", 5) == ("abcde", True)


def test_chunk_text_blocks_heading_not_merged_into_body():
    assert chunk_text_blocks("第1章 引言\n\n短") == ["第1章 引言", "短"]
    assert chunk_text_blocks("第1章 引言\n\n") == ["第1章 引言"]
