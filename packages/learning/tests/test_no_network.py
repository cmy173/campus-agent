"""D13：默认零网络调用——socket 层强断言。

思路：monkeypatch 所有出网入口（socket.socket / create_connection / getaddrinfo /
gethostbyname / socket.socket.connect / connect_ex），任何调用直接 AssertionError。
在此环境下跑全流程：解析（PDF/DOCX/PPTX）、资料整理、任务生成（含 B13 封装）、
排程、局部重排、ICS 导出、反馈校正、字幕整理、模型网关默认调用——全部必须无感知完成。

覆盖《04-内容处理与计划引擎》D8 红线：默认实现零网络；模型未启用即降级。
D18（收官轮复核）：全链扩展至 ICS/校正/字幕；补“配置完整但默认 Null 客户端仍零网络”；
守卫补 connect/connect_ex 实例方法级封锁。
"""

from __future__ import annotations

import socket

import pytest

import parse_fixtures as fx
from learning import (
    ModelConfig,
    ModelGateway,
    build_tasks_json,
    calibrate_estimates,
    load_sample,
    load_transcript,
    plan_to_ics,
    revise_plan,
    run_sample,
    transcript_chunks,
)
from learning.model import build_request
from learning.parse import ParseStatus, parse_document


# 真实 socket 类引用（在任何 patch 之前捕获；连接方法被守卫替换后不再恢复原值——monkeypatch 还原）
_REAL_SOCKET_CLS = socket.socket


def _forbidden(*args, **kwargs):  # noqa: ARG001
    raise AssertionError(
        "默认配置下发生了网络调用尝试——违反“默认零网络”红线（D8/D13）"
    )


@pytest.fixture()
def no_network(monkeypatch):
    """封死所有常见出网入口；测试通过即证明全流程未触网。"""
    guard = _forbidden
    # D18：实例方法级封锁——防“预先构造的 socket 对象绕过模块级替换”（须先于类替换）
    monkeypatch.setattr(_REAL_SOCKET_CLS, "connect", guard)
    monkeypatch.setattr(_REAL_SOCKET_CLS, "connect_ex", guard)
    monkeypatch.setattr(socket, "socket", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)
    monkeypatch.setattr(socket, "gethostbyname", guard)
    monkeypatch.setattr(socket, "gethostbyname_ex", guard)
    monkeypatch.setattr(socket, "getfqdn", guard)
    return None


def test_parse_document_pdf_docx_pptx_without_network(no_network, tmp_path):
    """三种格式的解析全程无网络（含缓存写入与缓存命中）。"""
    cache_dir = tmp_path / "pcache"
    docx = parse_document(
        fx.docx_bytes(
            fx.docx_paragraph("第一章 绪论")
            + fx.docx_paragraph("正文段落。本段用于验证默认配置下解析流程不产生任何网络调用。")
        ),
        filename="a.docx",
        cache_dir=cache_dir,
    )
    pptx = parse_document(
        fx.pptx_bytes(
            {1: fx.pptx_title_shape("网络测试") + fx.pptx_body_shape(["要点一：本地解析全程不触网。", "要点二：默认配置零网络调用。"])}
        ),
        filename="b.pptx",
        cache_dir=cache_dir,
    )
    pdf = parse_document(
        fx.pdf_bytes(["Chapter 1 Introduction", "Chapter 2 Method"]),
        filename="c.pdf",
        cache_dir=cache_dir,
    )
    for result in (docx, pptx, pdf):
        assert result.status == ParseStatus.READY
        assert result.chunks
    # 二次解析走缓存，也无网络
    pdf_again = parse_document(
        fx.pdf_bytes(["Chapter 1 Introduction", "Chapter 2 Method"]),
        filename="c.pdf",
        cache_dir=cache_dir,
    )
    assert pdf_again.cache == "hit"


def test_taskgen_schedule_revise_without_network(no_network):
    """资料整理 → 任务生成（B13 封装，含人工任务与配置） → 排程 → 局部重排全链无网络。"""
    ctx = run_sample("baseline")
    options = load_sample("baseline").get("task_options") or {}
    assert ctx["plan"].blocks

    inputs = ctx["inputs"]
    payload = {
        "resources": [item.to_dict() for item in inputs["resources"]],
        "assignments": [item.to_dict() for item in inputs["assignments"]],
        "recordings": [item.to_dict() for item in inputs["recordings"]],
        "existing_tasks": [task.to_dict() for task in ctx["tasks"]],
        "user_tasks": options.get("user_tasks") or [],
        "config": options.get("config"),
    }
    regenerated = build_tasks_json(payload)
    assert sorted(item["id"] for item in regenerated["tasks"]) == sorted(
        task.id for task in ctx["tasks"]
    )
    # 与首次生成一致（样例自带若干内容重复/版本告警，属预期）
    assert [item["code"] for item in regenerated["warnings"]] == [
        item.code for item in ctx["taskgen_warnings"]
    ]

    revised = revise_plan(
        ctx["plan"], ctx["tasks"], inputs["availability"], inputs["schedule_options"]
    )
    assert revised.plan.blocks

    # D17：ICS 导出（时间块 + 截止事件）纳入零网络全链
    ics = plan_to_ics(ctx["plan"], ctx["tasks"], timezone=inputs["availability"].timezone)
    assert "BEGIN:VEVENT" in ics

    # D6：反馈校正（样本不足则如实不调整，同样不得触网）
    report = calibrate_estimates(
        [
            {"task_type": "reading", "estimated_minutes": 60, "actual_minutes": 80},
            {"task_type": "reading", "estimated_minutes": 30, "actual_minutes": 35},
        ]
    )
    assert report.factors()["reading"] > 0

    # D4：回放字幕整理（规范化样例 → 内容片段）
    transcript = load_transcript("transcript-sample")
    chunks = transcript_chunks(transcript)
    assert chunks and all(chunk.text for chunk in chunks)


def test_model_gateway_default_disabled_without_network(no_network):
    """默认禁用配置的模型网关：直接降级，不尝试任何连接。"""
    gateway = ModelGateway(ModelConfig())
    assert not gateway.available()
    request = build_request(
        task="summarize",
        prompt_version="v1",
        instruction="整理以下资料要点",
        context=[{"source": "chunk_1", "text": "资料正文"}],
    )
    response = gateway.generate(request)
    assert response.ok is False
    assert response.error_code == "MODEL_DISABLED"
    assert gateway.usage and gateway.usage[0].attempts == 0


def test_model_gateway_enabled_but_incomplete_config_still_degrades(no_network):
    """启用但缺 base_url/model：视为不可用，仍不触网。"""
    gateway = ModelGateway(ModelConfig(enabled=True, model="some-model"))
    assert not gateway.available()
    request = build_request(
        task="summarize",
        prompt_version="v1",
        instruction="整理以下资料要点",
        context=[{"source": "chunk_1", "text": "资料正文"}],
    )
    assert gateway.generate(request).error_code == "MODEL_DISABLED"


def test_model_gateway_complete_config_with_default_client_makes_no_call(no_network):
    """D18：配置完整（enabled+base_url+model）但未注入厂商客户端——默认 Null 实现，
    依旧零网络：网关走到客户端即立刻降级（attempts=1、MODEL_DISABLED）。"""
    gateway = ModelGateway(
        ModelConfig(enabled=True, base_url="https://api.example.invalid", model="some-model")
    )
    assert gateway.available()
    request = build_request(
        task="summarize",
        prompt_version="v1",
        instruction="整理以下资料要点",
        context=[{"source": "chunk_1", "text": "资料正文"}],
    )
    response = gateway.generate(request)
    assert response.ok is False and response.error_code == "MODEL_DISABLED"
    assert gateway.usage[0].attempts == 1


def test_no_network_fixture_itself_blocks_direct_connect(no_network):
    """元测试：守卫确实生效（直接发连接即触发断言）。"""
    with pytest.raises(AssertionError, match="默认零网络"):
        socket.create_connection(("example.com", 80))
    with pytest.raises(AssertionError, match="默认零网络"):
        socket.getaddrinfo("example.com", 80)
    # D18 新增守卫：构造 socket 与实例方法连网均被封锁
    with pytest.raises(AssertionError, match="默认零网络"):
        socket.socket()
    assert getattr(_REAL_SOCKET_CLS.connect, "__name__", "") == "_forbidden"
    assert getattr(_REAL_SOCKET_CLS.connect_ex, "__name__", "") == "_forbidden"
