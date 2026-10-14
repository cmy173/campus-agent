"""复习卷引擎测试 fixture：两章课件 + 作业正文/附件 + 多行中文备注 + 网络候选题 + 模拟模型输出。

- 章节文本为自编小样例（非真实教材），用于验证“课件与作业共同影响蓝图”。
- AI 输出为手工构造的“模拟模型响应”（不是真实模型产物），用于解析和校验链路测试。
"""

from __future__ import annotations

import json
from typing import Any

from learning.review import BlueprintConstraints, ChunkInput, MaterialInput, build_snapshot, kp_id_for

COURSE_ID = "course_ds"
COURSE_TITLE = "数据结构与算法"

CHAPTER3 = "第3章 栈与队列"
SECTION31 = "3.1 括号匹配与边界条件"
SECTION32 = "3.2 队列"
CHAPTER4 = "第4章 树与二叉树"
SECTION41 = "4.1 二叉搜索树"

NOTES = (
    "重点考察边界条件，增加一道综合计算题，答案单独显示。\n"
    "括号匹配要多出题，循环队列的假溢出也请覆盖。\n"
    "答案单独显示、公式用 LaTeX 排版。\n"
    "请重点考察“红黑树”的删除操作。"
)

KP_BOUNDARY = kp_id_for(SECTION31)
KP_QUEUE = kp_id_for(SECTION32)
KP_STACK_CHAPTER = kp_id_for(CHAPTER3)
KP_BST = kp_id_for(SECTION41)
KP_TREE_CHAPTER = kp_id_for(CHAPTER4)
KP_LOOP_QUEUE = kp_id_for("循环队列")


def _slide(n: int) -> dict[str, Any]:
    return {"kind": "slide", "slide": n}


def _page(n: int) -> dict[str, Any]:
    return {"kind": "page", "page": n}


def courseware_materials() -> list[MaterialInput]:
    ch3 = MaterialInput(
        material_id="res_ch3",
        kind="resource",
        title="第3章 栈与队列.pptx",
        version="2",
        format="pptx",
        parse_status="ready",
        chunks=[
            ChunkInput(chunk_id="ch3_1", ordinal=0, text=CHAPTER3, kind="heading", heading_path=(CHAPTER3,), locator=_slide(1)),
            ChunkInput(
                chunk_id="ch3_2",
                ordinal=1,
                text="栈是后进先出的线性表，只能在栈顶插入和删除。入栈 push 与出栈 pop 的时间复杂度均为 O(1)。",
                heading_path=(CHAPTER3,),
                locator=_slide(2),
            ),
            ChunkInput(chunk_id="ch3_3", ordinal=2, text=SECTION31, kind="heading", heading_path=(CHAPTER3, SECTION31), locator=_slide(3)),
            ChunkInput(
                chunk_id="ch3_4",
                ordinal=3,
                text="表达式求值、括号匹配与函数调用栈是栈的典型应用。括号匹配需要处理边界条件：空串、单个左括号、多层嵌套。",
                heading_path=(CHAPTER3, SECTION31),
                locator=_slide(4),
            ),
            ChunkInput(chunk_id="ch3_5", ordinal=4, text=SECTION32, kind="heading", heading_path=(CHAPTER3, SECTION32), locator=_slide(5)),
            ChunkInput(
                chunk_id="ch3_6",
                ordinal=5,
                text="队列是先进先出结构，循环队列通过取模运算避免假溢出。队满判别条件为 (rear+1)%n == front。",
                heading_path=(CHAPTER3, SECTION32),
                locator=_slide(6),
            ),
        ],
    )
    ch4 = MaterialInput(
        material_id="res_ch4",
        kind="resource",
        title="第4章 树与二叉树.pdf",
        version="1",
        format="pdf",
        parse_status="ready",
        chunks=[
            ChunkInput(chunk_id="ch4_1", ordinal=0, text=CHAPTER4, kind="heading", heading_path=(CHAPTER4,), locator=_page(1)),
            ChunkInput(
                chunk_id="ch4_2",
                ordinal=1,
                text="二叉树的三种遍历：前序、中序、后序。中序遍历二叉搜索树可得到有序序列。",
                heading_path=(CHAPTER4,),
                locator=_page(2),
            ),
            ChunkInput(chunk_id="ch4_3", ordinal=2, text=SECTION41, kind="heading", heading_path=(CHAPTER4, SECTION41), locator=_page(3)),
            ChunkInput(
                chunk_id="ch4_4",
                ordinal=3,
                text="二叉搜索树的查找、插入与删除平均时间复杂度为 O(log n)，最坏情况退化为链表 O(n)。",
                heading_path=(CHAPTER4, SECTION41),
                locator=_page(4),
            ),
            ChunkInput(
                chunk_id="ch4_5",
                ordinal=4,
                text="平衡二叉树（AVL）通过旋转保持高度平衡，左旋与右旋是基础操作。",
                heading_path=(CHAPTER4, SECTION41),
                locator=_page(5),
            ),
        ],
    )
    return [ch3, ch4]


def assignment_materials() -> list[MaterialInput]:
    assignment = MaterialInput(
        material_id="asg_hw2",
        kind="assignment",
        title="作业2：栈与树的应用",
        version="2026-10-09T10:00:00+08:00",
        format="text",
        parse_status="ready",
        chunks=[
            ChunkInput(
                chunk_id="asg_1",
                ordinal=0,
                text=(
                    "作业要求：1) 用栈实现括号匹配，需覆盖边界条件（空串、深嵌套）。"
                    "2) 给定二叉搜索树，写出中序遍历序列并分析最坏时间复杂度。"
                    "3) 参照附件实验报告模板，分析循环队列假溢出与队满判别。"
                ),
                locator={"kind": "text", "start_offset": 0, "end_offset": 96},
            )
        ],
    )
    attachment = MaterialInput(
        material_id="att_hw2",
        kind="attachment",
        title="hw2-report.docx",
        version="1",
        format="docx",
        parse_status="ready",
        linked_assignment_id="asg_hw2",
        chunks=[
            ChunkInput(
                chunk_id="att_1",
                ordinal=0,
                text="附件说明：实验报告需包含循环队列的测试用例，并解释取模运算在队满判别中的作用。",
                locator={"kind": "text", "start_offset": 0, "end_offset": 38},
            )
        ],
    )
    return [assignment, attachment]


def sample_provider() -> dict[str, Any]:
    return {
        "provider_id": "prov_local_1",
        "protocol": "openai_chat",
        "model": "local-qwen",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "sk-must-not-appear-anywhere",
        "timeout_s": 60,
    }


def sample_materials() -> list[MaterialInput]:
    return [*courseware_materials(), *assignment_materials()]


def sample_snapshot(notes: str = NOTES, provider: dict[str, Any] | None = None):
    return build_snapshot(
        COURSE_ID,
        COURSE_TITLE,
        sample_materials(),
        notes=notes,
        provider=provider if provider is not None else sample_provider(),
    )


def sample_constraints(mode: str = "ai", **overrides: Any) -> BlueprintConstraints:
    base: dict[str, Any] = {
        "mode": mode,
        "types": {"choice": 4, "true_false": 2, "fill_blank": 2, "calculation": 2, "short_answer": 1},
        "total_points": 100,
    }
    base.update(overrides)
    return BlueprintConstraints.from_dict(base)


def bank_candidates() -> list[dict[str, Any]]:
    """A 侧风格候选（JSON 形状；含缺答案/缺解析/重复/不匹配/非法题型/低置信度）。"""
    return [
        {
            "candidate_id": "bc_1",
            "stem": "以下关于括号匹配的说法，正确的是？",
            "qtype": "choice",
            "options": ["空串是合法的括号序列", "单个左括号是合法序列", "嵌套必须成对出现", "括号匹配不需要考虑栈"],
            "answer": "A",
            "explanation": "空串不包含任何括号，按定义合法。",
            "kp_hints": [SECTION31],
            "difficulty": "easy",
            "source": {"url": "https://example.com/q/1", "site": "example题库", "title": "括号匹配练习", "fetched_at": "2026-10-11T09:00:00+08:00"},
            "extraction_confidence": 0.9,
        },
        {
            "candidate_id": "bc_2",
            "stem": "以下关于括号匹配的说法，正确的是？",
            "qtype": "choice",
            "options": ["空串是合法的括号序列", "单个左括号是合法序列", "嵌套必须成对出现", "括号匹配不需要考虑栈"],
            "answer": "A",
            "explanation": "与 bc_1 同题（用于验证去重）。",
            "kp_hints": [SECTION31],
            "difficulty": "easy",
            "source": {"url": "https://example.com/q/1-copy", "site": "example题库"},
            "extraction_confidence": 0.8,
        },
        {
            "candidate_id": "bc_3",
            "stem": "循环队列中，判断队满的常见条件是（用 rear、front、n 表示）。",
            "qtype": "fill_blank",
            "options": [],
            "answer": "(rear+1)%n == front",
            "explanation": None,
            "kp_hints": ["循环队列"],
            "difficulty": "medium",
            "source": {"url": "https://example.com/q/3", "site": "example题库"},
            "extraction_confidence": 0.7,
        },
        {
            "candidate_id": "bc_4",
            "stem": "已知二叉搜索树先序序列为 [8,3,1,6,10]，写出其中序遍历结果。",
            "qtype": "calculation",
            "options": [],
            "answer": None,
            "explanation": "中序遍历二叉搜索树得到有序序列。",
            "kp_hints": [SECTION41],
            "difficulty": "medium",
            "source": {"url": "https://example.com/q/4", "site": "example题库"},
            "extraction_confidence": 0.85,
        },
        {
            "candidate_id": "bc_5",
            "stem": "循环队列使用取模运算的主要目的是什么？",
            "qtype": "choice",
            "options": ["提高入队速度", "避免假溢出并复用存储空间", "减少内存分配", "保证先进先出"],
            "answer": "B",
            "explanation": "取模让下标回绕，从而复用空间并避免假溢出。",
            "kp_hints": ["循环队列", SECTION32],
            "difficulty": "easy",
            "source": {"url": "https://example.com/q/5", "site": "example题库"},
            "extraction_confidence": 0.6,
        },
        {
            "candidate_id": "bc_6",
            "stem": "编写一个函数判断括号序列是否合法。",
            "qtype": "编程题",
            "options": [],
            "answer": "见参考实现",
            "explanation": "不支持题型（应被拒绝）。",
            "kp_hints": [SECTION31],
            "source": {"url": "https://example.com/q/6", "site": "example题库"},
        },
        {
            "candidate_id": "bc_7",
            "stem": "傅里叶变换的频域卷积定理是什么？",
            "qtype": "choice",
            "options": ["A 项", "B 项", "C 项", "D 项"],
            "answer": "A",
            "explanation": "与课程知识点无关（应被判 NO_MATCH）。",
            "kp_hints": ["傅里叶变换"],
            "source": {"url": "https://example.com/q/7", "site": "example题库"},
        },
        {
            "candidate_id": "bc_8",
            "stem": "栈的入栈和出栈操作发生在哪一端？",
            "qtype": "choice",
            "options": ["栈底", "栈顶", "两端均可", "中间"],
            "answer": "B",
            "explanation": None,
            "kp_hints": [CHAPTER3],
            "difficulty": "easy",
            "source": {"url": "https://example.com/q/8", "site": "example题库"},
            "extraction_confidence": 0.3,
        },
        {
            "candidate_id": "bc_9",
            "stem": "中序遍历二叉搜索树得到的序列有什么性质？",
            "qtype": "fill_blank",
            "options": [],
            "answer": ["有序（递增）序列"],
            "explanation": "由二叉搜索树的性质决定。",
            "kp_hints": [CHAPTER4],
            "difficulty": "easy",
            "source": {"url": "https://example.com/q/9", "site": "example题库"},
            "usage_note": "仅供学习使用，转载需注明来源",
            "extraction_confidence": 0.9,
        },
    ]


# ---------------------------------------------------------------------------
# 模拟模型输出（手工构造；用于解析/校验，不代表真实模型行为）
# ---------------------------------------------------------------------------


def _q(
    qtype: str,
    stem: str,
    answer: Any,
    refs: list[dict[str, Any]],
    kp_ids: list[str],
    *,
    options: list[str] | None = None,
    explanation: str = "依据课件与作业内容解析。",
    scoring: list[str] | None = None,
    difficulty: str = "medium",
) -> dict[str, Any]:
    return {
        "qtype": qtype,
        "stem": stem,
        "options": [{"key": chr(65 + i), "text": t} for i, t in enumerate(options or [])],
        "answer": answer,
        "explanation": explanation,
        "scoring": scoring or [],
        "kp_ids": kp_ids,
        "difficulty": difficulty,
        "points": 0,  # 分值由引擎按蓝图重算
        "source_refs": refs,
    }


def ai_valid_payload(extra_ref_bank_id: str | None = None) -> dict[str, Any]:
    refs_bank = ([{"bank_candidate_id": extra_ref_bank_id}] if extra_ref_bank_id else [])
    return {
        "title": "数据结构与算法复习卷（第3-4章）",
        "questions": [
            _q("choice", "括号序列“(()”是否为合法括号序列？", "B",
               [{"chunk_id": "ch3_4"}], [KP_BOUNDARY],
               options=["合法，只需左右括号数量相等", "不合法，左括号多余", "合法，栈为空即为合法", "无法判断"], difficulty="easy"),
            _q("choice", "循环队列队满的判别条件是？", "C",
               [{"chunk_id": "ch3_6"}], [KP_QUEUE],
               options=["rear == front", "front == n", "(rear+1)%n == front", "rear+1 == n"]),
            _q("choice", "中序遍历二叉搜索树得到的序列是？", "A",
               [{"chunk_id": "ch4_2"}], [KP_TREE_CHAPTER],
               options=["有序序列", "逆序序列", "层序序列", "任意序列"], difficulty="easy"),
            _q("choice", "二叉搜索树删除操作的平均时间复杂度是？", "B",
               [{"chunk_id": "ch4_4"}], [KP_BST],
               options=["O(1)", "O(log n)", "O(n^2)", "O(n log n)"]),
            _q("true_false", "入栈 push 与出栈 pop 的时间复杂度均为 O(1)。", True,
               [{"chunk_id": "ch3_2"}], [KP_STACK_CHAPTER], difficulty="easy"),
            _q("true_false", "循环队列一定比普通队列节省内存。", False,
               [{"chunk_id": "ch3_6"}], [KP_QUEUE],
               explanation="课件说明循环队列通过取模复用空间，但并非任何场景都“节省内存”，需结合容量判断。"),
            _q("fill_blank", "循环队列判满条件常写作 ____（用 rear、front、n 表示）。", ["(rear+1)%n == front"],
               [{"chunk_id": "ch3_6"}], [KP_QUEUE]),
            _q("fill_blank", "对任意二叉搜索树，其中序遍历所访问的键值关系为 ____（递增/递减）。", ["递增"],
               [{"chunk_id": "ch4_2"}, {"notes_line": 1}], [KP_TREE_CHAPTER]),
            _q("calculation", "已知先序序列 [8,3,1,6,10] 构造的二叉搜索树，求中序遍历并推导最坏查找复杂度。",
               "中序：1,3,6,8,10。最坏情况树退化为链表，查找为 O(n)。",
               [{"chunk_id": "ch4_4"}, {"notes_line": 1}] + refs_bank, [KP_BST, KP_TREE_CHAPTER],
               explanation="先序确定根与左右子树；中序有序；退化链表时逐节点查找。",
               scoring=["正确写出中序序列（3分）", "说明退化原因（2分）", "给出复杂度结论（2分）"],
               difficulty="hard"),
            _q("calculation", "对输入串“（a+[b)】”说明括号匹配过程并指出边界条件。",
               "用栈自左向右扫描：遇到右括号与栈顶比对，类型不匹配即失败；边界条件包括空串与深嵌套。",
               [{"chunk_id": "ch3_4"}, {"chunk_id": "asg_1"}], [KP_BOUNDARY, "kp_unknown_x"],
               explanation="课件给出括号匹配边界条件；作业要求覆盖空串与深嵌套。",
               scoring=["说明栈的匹配过程（3分）", "覆盖空串/深嵌套边界（2分）"],
               difficulty="hard"),
            _q("short_answer", "结合作业要求，说明循环队列假溢出的成因与测试用例设计。",
               "假溢出源于 front 前移后空间不可复用；测试应覆盖队满、队空与临界回绕。",
               [{"chunk_id": "att_1"}, {"chunk_id": "asg_1"}], [KP_LOOP_QUEUE],
               explanation="附件要求包含测试用例并解释取模作用；作业要求分析假溢出。",
               scoring=["成因分析（3分）", "测试用例设计（3分）", "取模作用（2分）"]),
        ],
    }


def ai_valid_output(extra_ref_bank_id: str | None = None) -> str:
    return json.dumps(ai_valid_payload(extra_ref_bank_id), ensure_ascii=False)


def ai_valid_output_fenced() -> str:
    return "以下是试卷：\n```json\n" + ai_valid_output() + "\n```\n请查收。"
