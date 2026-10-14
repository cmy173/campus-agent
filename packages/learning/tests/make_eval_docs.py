"""D3 评估样例生成器（开发工具，非 pytest 收集项）。

生成 `samples/evaluation/` 下 5 份**自编**评估文档（非真实课件；内容由本脚本定义，
人工标注文件 `labels.json` 与之对照撰写）：

- doc01-lecture-handout.docx   中文讲义（标题层级 + 正文）
- doc02-lecture-slides.pptx    中文课件（标题/正文/表格/备注/纯图片页）
- doc03-notes.pdf              英文讲义（文本层 PDF，3 页）
- doc04-scanned.pdf            纯图形页 PDF（预期 needs_ocr）
- doc05-appendix.docx          中文附录（表格 + 公式 + 插图，验证限制注记）

运行：cd packages/learning && python tests/make_eval_docs.py
zip 时间戳固定为 (2026,10,9)，重复运行字节一致（PDF 为手工构造，本身确定）。
"""

from __future__ import annotations

import hashlib
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parse_fixtures import (  # noqa: E402
    docx_bytes,
    docx_formula_paragraph,
    docx_image_paragraph,
    docx_paragraph,
    docx_table,
    pdf_bytes,
    pptx_body_shape,
    pptx_bytes,
    pptx_pic_shape,
    pptx_table_shape,
    pptx_title_shape,
)

OUT_DIR = Path(__file__).resolve().parent.parent / "samples" / "evaluation"
FIXED_DATE = (2026, 10, 9, 0, 0, 0)


def rezip_deterministic(data: bytes) -> bytes:
    """把 zip 包重打包为固定时间戳版本（夹具默认用当前时间，不满足字节稳定）。"""
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name in src.namelist():
            info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(info, src.read(name))
    return buf.getvalue()


# ---------------------------------------------------------------- 内容（与 labels.json 对照）

DOC01 = [
    docx_paragraph("第2章 线性表", outline_lvl=0),
    docx_paragraph("本讲讨论线性表的两种基本实现：顺序存储与链式存储，并比较二者在插入、删除与随机访问上的时间开销。"),
    docx_paragraph("2.1 顺序存储", outline_lvl=1),
    docx_paragraph("顺序表用一段连续空间保存元素，按下标直接寻址，随机访问为 O(1)；在表中间插入或删除需要搬移后续元素，平均开销为 O(n)。"),
    docx_paragraph("2.2 链式存储", outline_lvl=1),
    docx_paragraph("链表以结点和指针串联元素，按位置访问需要从头遍历，开销为 O(n)；已知位置指针时插入与删除只需修改指针，为 O(1)，但每个结点需要额外的指针空间。"),
]

DOC05_TABLE = [
    ["操作", "顺序表", "链表"],
    ["插入（已知位置）", "O(n)", "O(1)"],
    ["删除（已知位置）", "O(n)", "O(1)"],
    ["随机访问", "O(1)", "O(n)"],
]

DOC05 = [
    docx_paragraph("附录A 复杂度速查", outline_lvl=0),
    docx_paragraph("下表汇总两种实现在常见操作上的时间复杂度，供复习时对照："),
    docx_table(DOC05_TABLE),
    docx_formula_paragraph("x^2"),
    docx_image_paragraph(),
    docx_paragraph("注：上表假设已持有目标位置的指针；本页公式与插图未提取为文本，请按原文位置查阅。"),
]

DOC02_SLIDES = {
    1: pptx_title_shape("数据结构 · 第4讲")
    + pptx_body_shape(["线性表：顺序与链接两种实现"])
    + pptx_pic_shape(),
    2: pptx_title_shape("两种实现的比较")
    + pptx_body_shape(
        [
            "顺序表按下标寻址，随机访问快；",
            "链表插入删除代价低，但每个结点需要指针空间。",
        ]
    )
    + pptx_table_shape(
        [
            ["比较项", "顺序表", "链表"],
            ["随机访问", "O(1)", "O(n)"],
            ["已知位置插入", "O(n)", "O(1)"],
        ]
    ),
    3: pptx_title_shape("课堂练习") + pptx_body_shape(["请在纸上写出单链表中删除给定结点的步骤。"]),
    4: pptx_pic_shape(),
}

# 段落间用“仅空格行”分隔：pypdf 提取会丢弃完全空行、但保留空格行，
# 解析器的分块正则 \n\s*\n 由此生效（空行版本实测会被并成单块）。
_PARA = " "

DOC03_PAGES = [
    [
        "1 Introduction",
        _PARA,
        "This handout reviews arrays and linked lists and compares their costs for indexing, insertion, and deletion in the worst case.",
    ],
    [
        "1.1 Arrays",
        _PARA,
        "Arrays store elements in contiguous memory so that random access by index costs O(1) time.",
    ],
    [
        "1.2 Linked lists",
        _PARA,
        "Linked lists walk from the head to a position in O(n) time, while inserting at a known node costs O(1).",
    ],
]

DOC04_PAGES = [None, None]


# ---------------------------------------------------------------- 生成


def _docx_doc(blocks: list[str]) -> bytes:
    return rezip_deterministic(docx_bytes("".join(blocks)))


def _pptx_doc() -> bytes:
    return rezip_deterministic(pptx_bytes(DOC02_SLIDES, notes={3: ["下课前提醒：作业2截止 10月20日 23:59。"]}))


def _pdf_doc(pages: list[list[str] | None]) -> bytes:
    texts = ["\n".join(page) if page is not None else None for page in pages]
    return pdf_bytes(texts)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        "doc01-lecture-handout.docx": _docx_doc(DOC01),
        "doc02-lecture-slides.pptx": _pptx_doc(),
        "doc03-notes.pdf": _pdf_doc(DOC03_PAGES),
        "doc04-scanned.pdf": _pdf_doc(DOC04_PAGES),
        "doc05-appendix.docx": _docx_doc(DOC05),
    }
    for name, data in files.items():
        path = OUT_DIR / name
        path.write_bytes(data)
        print(f"{name}  {len(data)} bytes  sha256:{hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
