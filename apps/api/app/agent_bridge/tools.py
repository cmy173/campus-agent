"""Read-only domain tools. Identity and scope are supplied by the gateway, never the LLM.

The existing database is a single-person workspace. Scope restricts that local
workspace further; it does not turn the original database into a multiuser store.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import time
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from .. import models as m
from ..errors import ApiError
from ..repo import page, refs_for, decode_cursor, decode_int_cursor, encode_cursor
from ..routers import connections, courses, jobs, plans, resources, search, tasks
from ..plan_engine import resolve_user_tz
from . import artifacts, plan_actions, job_actions, feedback_actions, review_actions
from .files import ConversationFiles
from .store import Store


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(Input):
    pass


class Pagination(Input):
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=20, ge=1, le=50)


class ConnectionArgs(Input):
    connection_id: str | None = Field(default=None, min_length=1, max_length=128)


class CourseArgs(Pagination):
    query: str | None = Field(default=None, min_length=1, max_length=200)
    term: str | None = Field(default=None, min_length=1, max_length=64)


class MaterialArgs(Pagination):
    course_id: str = Field(min_length=1, max_length=128)


class BuildArgs(Input):
    course_id: str = Field(min_length=1, max_length=128)


class FeedbackArgs(Input):
    task_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    outcome: Literal["completed", "partial", "skipped"]
    actual_minutes: int | None = Field(default=None, ge=1, le=1440)
    difficulty: int | None = Field(default=None, ge=1, le=5)
    note: str | None = Field(default=None, max_length=2000)


class SearchArgs(Pagination):
    query: str = Field(min_length=1, max_length=500)
    course_id: str | None = Field(default=None, min_length=1, max_length=128)


class ReadArgs(Pagination):
    resource_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    chunk_ids: list[str] | None = Field(default=None, min_length=1, max_length=50,
        description="来自检索结果的片段ID；指定后不会静默截断，总正文不得超过24000字符。省略时自动按字符预算分页，使用返回的next_cursor继续。")


class AssignmentArgs(Pagination):
    cursor: str | None = Field(default=None, max_length=512,
        description="仅使用上一次结果返回的 next_cursor 继续同一查询；不要重复第一页查询。")
    limit: int = Field(default=20, ge=1, le=50,
        description="单页数量，默认20、上限50。若用户只问最近作业，不要为穷举所有课程而自动翻完全部页面。")
    course_id: str | None = Field(default=None, min_length=1, max_length=128)
    include_instructions: bool = Field(default=False,
        description="默认false，仅返回作业元数据；用户明确询问作业要求/说明正文时才设true。")
    order_by: Literal["id", "due_soon"] = Field(default="id",
        description="默认id保持通用列表顺序；用户问最近/最早截止作业时用due_soon，按截止时间升序（未知截止排末）。")
    due_before: str | None = Field(default=None, max_length=64,
        description="可选截止时间上界，必须是带时区的 ISO-8601 日期时间，如 2026-11-01T00:00:00+08:00；仅在用户给出范围时使用。")
    due_after: str | None = Field(default=None, max_length=64,
        description="可选截止时间下界，必须是带时区的 ISO-8601 日期时间，如 2026-10-01T00:00:00+08:00；不要传日期字符串或无时区时间。")


class AssignmentNextArgs(Pagination):
    cursor: str | None = Field(default=None, max_length=512,
        description="仅使用本工具上一页返回的 next_cursor 按课程ID继续；不要传assignments.list或旧版截止时间游标。")
    limit: int = Field(default=20, ge=1, le=50,
        description="每页课程数，默认20、上限50；有next_cursor时逐页继续，不能把单页当全部课程。")
    due_before: str | None = Field(default=None, max_length=64,
        description="可选截止上界；必须是含时区的 ISO-8601 日期时间。")
    due_after: str | None = Field(default=None, max_length=64,
        description="可选截止下界；必须是含时区的 ISO-8601 日期时间。")


class TaskArgs(Pagination):
    course_id: str | None = Field(default=None, min_length=1, max_length=128)
    status: Literal["pending", "in_progress", "completed", "cancelled"] | None = None
    due_before: str | None = Field(default=None, max_length=64)


class PlanArgs(Input):
    plan_id: str | None = Field(default=None, min_length=1, max_length=128)


class JobArgs(Input):
    job_id: str = Field(min_length=1, max_length=128)
    wait_seconds: int | None = Field(default=None, ge=0, le=14,
        description="可选等待秒数(0-14)。省略时，对校园同步与复习卷生成 job 默认在本次调用内最多等待14秒；"
                    "显式传0表示立即读取。校园同步从受理时起总观察窗口最多60秒，复习卷最多45秒，重复调用共享窗口；"
                    "解析/构建等其他类型即使省略也立即返回。超窗仍running时说明后台未结束及缓存边界。")


class BlockedTime(Input):
    starts_at: str = Field(max_length=64)
    ends_at: str = Field(max_length=64)


class TemporaryWindow(Input):
    weekday: int = Field(ge=1, le=7)
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class PreviewArgs(Input):
    period: Literal["this_week", "tomorrow", "dates"] = "this_week"
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    blocked: list[BlockedTime] = Field(default_factory=list, max_length=64)
    temporary_windows: list[TemporaryWindow] | None = Field(default=None, max_length=56)
    unavailable_evenings: list[Annotated[int, Field(ge=1, le=7)]] = Field(default_factory=list, max_length=7,
        description="本周不可用的晚上，ISO 星期1至7；服务端按18:00至次日00:00转换并在预览中明示。仅适用于this_week。")


class ActionArgs(Input):
    action_id: str = Field(min_length=1, max_length=128)


class CampusQueryArgs(Input):
    capability: Literal["courses", "grades", "profile", "notices", "college_notices"]
    query: dict[str, Any] = Field(default_factory=dict)
    platform: Literal["zdbk", "eta", "zdbk_public", "ckc_public"] | None = None


class CampusAcademicArgs(Input):
    day: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    term: str | None = Field(default=None, max_length=64)


class CampusNoticeArgs(Input):
    record_id: str = Field(min_length=1, max_length=128)


class CampusNoticeImportArgs(CampusNoticeArgs):
    course_id: str = Field(min_length=1, max_length=128)


class CampusImportedArgs(Input):
    batch_id: str | None = Field(default=None, max_length=128)
    document_id: str | None = Field(default=None, max_length=128)


class CampusWebvpnArgs(Input):
    service: Literal["cas", "zdbk"] = "zdbk"


class ConnectionSyncArgs(Input):
    """Which platform to refresh; omitted ids mean "every connected platform"."""

    platform: Literal["xzzd", "zykt"] | None = None
    connection_id: str | None = Field(default=None, min_length=1, max_length=128)
    kinds: list[Literal["assignments", "resources"]] | None = Field(default=None, min_length=1, max_length=2,
        description="限制同步类型；省略表示同步课件和作业两类。")
    wait_seconds: int = Field(default=0, ge=0, le=15,
        description="入队后本次最多等待15秒观察真实状态（0=立即返回）。仍running时用 jobs.read(wait_seconds=14) 有界等待，不要连续即时轮询。")


class ConnectionCoursesArgs(Input):
    platform: Literal["xzzd", "zykt"] | None = None
    connection_id: str | None = Field(default=None, min_length=1, max_length=128)
    cursor: str | None = Field(default=None, max_length=256,
        description="上一页返回的 next_cursor；不传读第一页")


class ConnectionCourseImportArgs(ConnectionCoursesArgs):
    """Bind discovered platform courses locally; an empty selection with all=true takes every one."""

    external_ids: list[str] = Field(default_factory=list, max_length=200)
    all: bool = False


class ArtifactExportArgs(artifacts.ExportArgs):
    """Render trusted structured content into real PDF/XLSX/Markdown and deliver it here.

    Input is data only: the renderer, output directory, fonts, modules and interpreter are
    chosen by the server. No code, command, expression, output path, font path or remote
    URL is accepted, and the agent cannot select the program that runs.
    """


class ReviewModeArgs(Input):
    """Which engine generates the paper. Secrets and provider addresses stay server-side."""

    mode: Literal["ai", "bank", "hybrid"] = "ai"
    provider_config_id: str | None = Field(default=None, min_length=1, max_length=128,
        description="已保存的 AI 服务配置ID；省略则用服务端已启用且已存密钥的第一个配置。不接受地址或密钥。")


class ReviewQuota(Input):
    type: Literal["single_choice", "multiple_choice", "true_false", "fill_blank", "calculation",
                  "short_answer", "proof"]
    count: int = Field(ge=1, le=100)
    points_each: int | None = Field(default=None, ge=1, le=200)


class ReviewConstraints(Input):
    question_types: list[ReviewQuota] = Field(min_length=1, max_length=20)
    difficulty: Literal["easy", "medium", "hard", "mixed"] | None = None
    total_points: int | None = Field(default=None, ge=1, le=500)
    duration_minutes: int | None = Field(default=None, ge=5, le=600)
    answer_display: Literal["separate", "inline"] = "separate"

    @model_validator(mode="after")
    def bounded_total(self):
        if sum(quota.count for quota in self.question_types) > 100:
            raise ValueError("题目总数不得超过 100（与复习卷引擎上限一致）")
        if len({(quota.type, quota.points_each) for quota in self.question_types}) != len(self.question_types):
            raise ValueError("同题型同分值的配额不能重复声明")
        return self


class ReviewResourceRef(Input):
    resource_id: str = Field(min_length=1, max_length=128)
    version: int | None = Field(default=None, ge=1)
    chunk_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)


class ReviewAssignmentRef(Input):
    assignment_id: str = Field(min_length=1, max_length=128)
    include_attachments: bool = True


class ReviewMaterialSelection(Input):
    resources: list[ReviewResourceRef] = Field(default_factory=list, max_length=100)
    assignments: list[ReviewAssignmentRef] = Field(default_factory=list, max_length=100)


class ReviewRequestArgs(ReviewModeArgs):
    course_id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=120)
    materials: ReviewMaterialSelection | None = Field(default=None,
        description="省略=该课程当前可用的全部材料；给出时只能使用本会话已授权课程的资源/作业ID。")
    notes: str = Field(default="", description="出卷要求/备注原文；不超过 20000 字符，不静默截断。")
    constraints: ReviewConstraints

    @model_validator(mode="after")
    def bounded_notes(self):
        if len(self.notes) > 20000:
            raise ValueError("notes 超过 20000 字符上限")
        return self


class ReviewGenerateArgs(ReviewRequestArgs):
    if_match: str | None = Field(default=None, max_length=256,
        description="preflight 返回的 fingerprint。提供后若材料/备注/配额已变化，服务端拒绝而不是偷换快照。")
    new_generation: bool = Field(default=False,
        description="仅当用户明确要求再出一份全新试卷时传 true。默认 false：同一会话内课程/模式/服务配置/"
                    "冻结材料/备注/约束完全相同的出卷请求会复用已在途或已完成的原 job（响应 data.reused=true、"
                    "data.reused_reason 说明原因），不会重复受理或重复计费。")
    wait_seconds: int = Field(default=0, ge=0, le=14,
        description="受理后在本工具内等待任务进入终态的秒数上限（0=只入队立即返回）。"
                    "复习卷生成的实测内部耗时为十秒量级，建议直接给足上限（14）而不是反复轮询："
                    "一次调用内即可拿到真实 succeeded/failed/cancelled，省下模型请求预算。"
                    "仍未终态时返回 running，请用 jobs.read 继续，绝不会假装完成。"
                    "等待窗口按该 job 的受理时刻计算，重复调用不会重置窗口。")


class ReviewReadArgs(Input):
    paper_id: str = Field(min_length=1, max_length=128)
    revision: int | None = Field(default=None, ge=1, description="省略=最新版本")


class ReviewExportArgs(ReviewReadArgs):
    format: Literal["markdown", "student", "answers"] = Field(default="markdown",
        description="markdown=完整卷（含答案与解析）；student=学生版；answers=答案版。")


def _file_store(ctx: ToolContext) -> ConversationFiles:
    if ctx.files is not None:
        return ctx.files
    from .runtime import get_runtime
    return ConversationFiles(get_runtime().store)


def _job_owned(db: Session, ctx: ToolContext, job_id: str) -> bool:
    """Ownership is a durable receipt, never "the caller knew the id".

    `tasks.build`/`connections.sync` keep their existing receipts; a review job is owned only
    through this conversation's own `reviews.generate` receipt.
    """
    if job_actions.owns(db, ctx.conversation_id, job_id, ctx.connection_ids, course_ids=ctx.course_ids):
        return True
    return review_actions.owns_job(db, ctx.conversation_id, job_id)


def _job_with_ownership(db: Session, ctx: ToolContext, job: dict) -> dict:
    """Mark a freshly accepted job so later reads can be scoped to this conversation."""
    return {**job, "requested_by_conversation": ctx.conversation_id}


@dataclass(frozen=True)
class ToolContext:
    principal: str
    conversation_id: str
    course_ids: frozenset[str] | None = None
    connection_ids: frozenset[str] | None = None
    job_ids: frozenset[str] = frozenset()
    allow_uncoursed_tasks: bool = False
    request_key: str | None = None
    # Session file store. None means "resolve the runtime-owned store"; tests inject an
    # isolated store so no fixture writes into the personal conversation file area.
    files: ConversationFiles | None = None


SPECS: dict[str, tuple[type[Input], str]] = {
    "campus.status": (Empty, "读取当前用户的浙大教务、ETA、校级/学院通知连接状态及缓存来源；不读取已保存凭据状态。仅当用户询问校园门户缓存/当前平台连接，或任务确实依赖个人校园数据且任务专用工具无法直接给出所需状态时调用。若任务是读取本机缓存且对应专用工具可直接读取，不要先调用本工具：本工具检查连接/session元数据，不能证明某项缓存存在、完整或最新。若用户问凭据是否保存或允许自动重登，改用campus.credentials.status；普通校园知识、公开教务入口、活动或通知问题不要读取个人状态。它不发起登录或同步。"),
    "campus.credentials.status": (Empty, "只读返回校园通行证/教务与WebVPN凭据是否已保存、自动重登开关、浏览器复用授权状态及当前教务/连接器状态；绝不返回密码、验证码、用户名提示、保存时间或Cookie，也不发起网络请求、登录或同步。仅当用户询问凭据保存、自动登录、浏览器授权或当前认证状态时调用。它已包含当前校园工作区与平台连接状态；若用户只问认证/连接状态，不要再调用campus.status或connections.status。已保存凭据、自动重登开关、浏览器复用授权、工作区connected和远端会话有效性是不同概念：configured/connected布尔值都不能验证学校当前接受该凭据，也不能证明自动登录失败的具体原因；只有用户要求的安全只读数据访问才会实际验证远端会话。"),
    "campus.query": (CampusQueryArgs, "读取浙大校园缓存：courses课表、grades成绩、profile基本信息、notices校级公开通知摘要、college_notices已登记学院通知。全校公开通知使用notices；仅当用户明确指定学院，且账号学院已匹配已登记来源时才用college_notices，否则可能返回UNSUPPORTED。notices的query保持空对象或省略；不要臆造limit等过滤参数，服务不支持时会返回INVALID_INPUT。其他能力仅传该数据源明确支持的筛选条件。纯缓存读取：不访问学校远端、不读浏览器Cookie、不提交密码，因此不会因读取而登录；已授权会话失效时返回结构化连接错误及缓存的fetched_at/freshness，需新数据时改用campus.sync。不将缓存说成刚同步。"),
    "campus.sync": (CampusQueryArgs, "通过迁入的浙大连接器只读同步校园信息并保存缓存。这是唯一会访问学校远端的校园读取入口：仅在已有有效授权且用户设置允许时最多进行一次自动重登（验证码/MFA/拒绝/限流后进入冷却，不切换通道重投密码，不扩大读取范围）；需学校登录时明确返回AUTH_REQUIRED，失败保留旧缓存。校级公开通知只返回notices已登记来源的有限摘要，可能标记LIMITED_SCOPE；用户问全校通知时不要为扩大覆盖范围改查college_notices，除非用户明确指定学院且账号有可匹配来源。用户问最新/今天/近期校级通知时，直接调用一次campus.sync(capability=notices)，不要先调用campus.query或公网搜索；按结果逐条给出published_at，并将通知标题原文作为Markdown链接文本，例如[通知标题](source)，source必须逐字取自对应记录；不得补造正文或历史内容，并说明LIMITED_SCOPE。校园同步参数与campus.query相同，不支持wait_seconds；不要臆造该字段或其他未声明参数。只做只读教务查询。"),
    "campus.academics": (CampusAcademicArgs, "从校园缓存生成day所在整周的课程安排和学分/绩点统计。问某日/下午时按events.date及start/end筛选；unplaced或推算校历不能证明无课。保留学期、校历来源和精度；不自行换算官方绩点。"),
    "campus.notice.read": (CampusNoticeArgs, "读取已登记学院通知正文及附件链接；record_id必须来自campus.query/sync(capability=college_notices)。校级notices公开摘要不支持正文读取(UNSUPPORTED)；若只需标题、日期和来源，直接使用列表结果，不要调用或重试本工具。"),
    "campus.imported.read": (CampusImportedArgs, "列出旧数据导入批次，或指定batch_id及document_id读原始资料片段、派生任务与证据；历史导入不表示学校新同步。"),
    "campus.webvpn.entry": (CampusWebvpnArgs, "返回浙大WebVPN已登记的通行证/教务浏览器入口。只在用户明确询问校外访问或VPN入口、且目标服务明确时调用；普通询问校园平台用途/入口无需查VPN映射。此映射不等于已登录或数据连通。先用浏览器完成VPN登录，再进行学校站点登录。映射源于门户观察，失效时须实际读取门户；不宣称HTTP教务已走VPN。"),
    "campus.notice.import": (CampusNoticeImportArgs, "将已缓存学院通知正文导入明确课程资料，保留来源与附件未读声明；重复请求复用，正文变化生成新版本。然后可materials.read/search、tasks.build或复习生成；不把附件当已解析。普通本地操作自动。"),
    "connections.status": (ConnectionArgs, "读取当前用户的学在浙大/智云课堂连接与本地缓存状态，不发起登录或同步。仅当用户询问自己的连接/同步状态，或任务需要连接/session元数据且任务专用工具不能直接返回所需状态时调用。若任务只需本机缓存且对应专用读取工具可直接执行，不要先调用本工具；它不能证明某项缓存存在、完整或最新。普通校园知识、公开入口或不依赖个人缓存的问题不要读取个人连接状态。需要远端新数据时用 connections.sync。"),
    "connections.courses": (ConnectionCoursesArgs, "读取学在浙大/智云课堂账号里实际存在的课程列表（平台实时读取，不落库）。当用户问课程/作业/课件而本机课程库为空，或不确定平台上有哪些课时先调用；随后用 connections.courses.import 挂接到本机，再 connections.sync 拉取资料与作业。"),
    "connections.courses.import": (ConnectionCourseImportArgs, "把平台课程挂接到本机课程库（本地绑定，不提交作业）：用 external_ids 指定当前页课号，或用 all=true 导入当前页全部课程；term/cursor与发现时一致。若返回next_cursor或continuations须逐页继续，不能把首批当全量。重复导入复用课程，随后connections.sync及jobs.read，再读取资料。"),
    "connections.sync": (ConnectionSyncArgs, "触发学在浙大/智云课堂只读后台同步，返回真实 job 状态。默认同步课件与作业，可能处理多门课程的资料；按最窄任务范围选择 kinds：只查作业/截止日期用 ['assignments']，只查课件用 ['resources']，两者都需要才传两项。课程列表另用 connections.courses。不要为作业查询触发课件读取。任务终态前 assignments.list/materials.list 仍可能只是旧缓存；若仍running，可用 jobs.read(wait_seconds=14) 在工具内有界等待；所有等待从受理时最多60秒，不要连续即时轮询，超时就说明后台未结束及缓存边界。平台会话失效时返回 AUTH_REQUIRED，需用户在受控窗口登录后重试。"),
    "courses.list": (CourseArgs, "仅当用户要浏览/筛选课程目录、课程学期或需要单独核验本地课程清单时使用。不要仅为把 assignments.list 返回的 course_id 转成课程名而调用：assignments.list 已逐项返回 course_title 和 course_term；逐课程截止摘要使用 assignments.next_by_course。课程列表只反映本机缓存，不证明学校端全量。"),
    "materials.list": (MaterialArgs, "列出课程资料与解析状态；元数据不表示已读正文。"),
    "materials.search": (SearchArgs, "按关键词检索已解析的当前版本正文依据；当前为子串检索，不是语义搜索。返回命中摘要，完整原文用materials.read。可省略course_id跨本会话授权课程检索；空结果不证明要求不存在。"),
    "materials.read": (ReadArgs, "读取指定版本原文；ID和版本来自materials.list/search。省略chunk_ids时按24000字符预算自动分页，用next_cursor继续；保留位置和未覆盖内容。版本变化需重新检索。"),
    "assignments.list": (AssignmentArgs, "读取本机缓存快照中的学校作业列表，保留提交状态与未知截止；每项同时返回课程名和学期，通常无需另调courses.list来把course_id映射成课程名。通用清单用本工具；用户明确问每门课最近/下一项截止作业时改用assignments.next_by_course，避免全局分页遗漏课程。用户问整体最近/最早截止时可传order_by=due_soon，结果按截止时间升序、未知截止排末；游标按截止时间和ID续页。默认只返回必要元数据，不加载说明正文；仅当用户明确询问作业要求时设include_instructions=true。只有用户要求完整清单且返回next_cursor时才继续，不要重复首屏。日期范围必须使用带时区的ISO-8601日期时间。数据来自本机快照；为空或过期时先按需同步，缓存范围不能证明学校全量覆盖。"),
    "assignments.next_by_course": (AssignmentNextArgs, "按当前授权范围的本机课程逐门返回状态：每门课程有已知截止作业时返回截止最近的一项；只有未知截止作业时标记unknown_deadline_only；没有本机作业记录时标记no_cached_assignments。无缓存不代表学校端无作业。每页按课程ID分页，next_cursor只能续接本工具；超过limit必须继续，不能把部分页称为全部。due_at_local是中国标准时间(+08:00)，可原样展示。调用本工具后无需再调用courses.list或assignments.list来拼接相同的逐课程任务；只有用户另问作业正文、提交状态或完整清单时才调用对应工具。日期范围必须是含时区的ISO-8601日期时间。"),
    "tasks.list": (TaskArgs, "读取本地学习任务。完成学习不等于学校平台提交。"),
    "plans.read": (PlanArgs, "读取生效或指定计划、锁定时间块与输入版本。"),
    "jobs.read": (JobArgs, "读取当前会话拥有的后台任务状态与结果引用。校园后台任务（同步、任务生成、出卷等）一律用本工具按其 job ID 读取；connections.sync 的同步 job(type=sync) 用 wait_seconds=14 在同一次调用内有界等待；重复调用共享受理后60秒总等待上限，避免jobs.read无等待即时轮询。超限仍running时说明后台未结束且本机缓存可能旧。终态区分succeeded/failed/cancelled/partial_failed。本机其他后台进程用上游 job_list/job_output，不要用它们读校园 job。"),
    "availability.read": (Empty, "读取长期可用时间；未配置时请用户提供本次可用窗口，不得编造。"),
    "plans.preview": (PreviewArgs, "生成本次安排预览和差异，不生效。本周/明天由服务器按用户时区计算；临时约束不改长期偏好。本机未配置长期可用时间时，可按一次性的默认 08:00–22:00 窗口生成预览并在 warnings 中说明，不等同于已配置偏好。"),
    "plans.apply": (ActionArgs, "应用用户已在可信界面接受的具体动作；模型不能自行授权。返回持久化操作回执。"),
    "plans.revoke": (ActionArgs, "执行用户已接受的撤销动作，恢复前版；需使用撤销动作 ID，不能使用任意计划 ID。"),
    "tasks.build": (BuildArgs, "从指定课程的已导入资料生成本地学习任务，返回真实后台 job；规则生成不调用模型，重复请求复用。"),
    "jobs.cancel": (JobArgs, "协作停止本会话发起的任务；running 且 cancel_requested 只表示请求停止，不能宣称已完成。"),
    "tasks.feedback": (FeedbackArgs, "仅记录用户明确报告的本地学习结果，先读任务版本；未报告的时间/难度留空。不修改学校提交状态。"),
    "artifacts.export": (ArtifactExportArgs, "把已读取并整理好的结构化内容渲染成真正的 PDF/XLSX/Markdown 并交付到本会话文件区，返回可下载文件ID、大小和sha256。只接受数据（标题、章节段落/表格、sheet的有限行列、来源锚点）：不接受代码、命令、表达式、模块名、输出路径、字体路径或远端URL，渲染程序由服务端固定。字符串以=开头仍是普通文本，不计算公式；未知事实保持unknown，不要用当前日期补全。任何格式失败都会返回结构化错误，不会产生假文件。交付后可用会话文件下载；需要用户看到的成果无需再手工publish。"),
    "reviews.preflight": (ReviewRequestArgs, "出卷前无副作用预检：核对本会话已授权课程的可用材料、缺口、provider与限额，返回 fingerprint。只返回元数据与缺口，不发送正文、不生成题目、不创建快照或job；materials省略时用该课程当前可用材料。资料未解析/缺正文时必须在missing中如实报告，不能假装已读过。"),
    "reviews.generate": (ReviewGenerateArgs, "受理复习卷生成：冻结材料快照并写入持久 review_generate 后台job，返回job ID与试卷ID。需要用户明确要求出卷；受理不等于成功，完成与否必须读 jobs.read。建议传 wait_seconds=14 在本调用内等待真实终态（复习卷生成实测十秒量级），一次拿到 succeeded/failed/cancelled 比分多次轮询省预算；等待超时仍返回 running，绝不假装完成。同一可信调用ID重复调用返回同一job，不重复扣费；参数变化返回幂等冲突。同一会话内课程/模式/服务配置/冻结材料/备注/约束相同的出卷请求会复用已在途或已完成的原 job（data.reused=true，data.reused_reason=identical_request_in_flight 或 identical_request_completed），不会因新的调用ID重复受理或重复计费；此前的 job 为 failed/cancelled/partial_failed 时仍会正常新建，便于恢复。仅当用户明确要求再出一份全新试卷时才传 new_generation=true。有 preflight 结果时传 if_match，材料变化会被拒绝而不是偷换快照。"),
    "reviews.read": (ReviewReadArgs, "读取本会话已生成复习卷的指定版本（含题目、答案、解析与冻结快照依据）。只能读取本会话自己出过的卷；版本不存在或尚未生成成功时明确报错，不用其他版本顶替。"),
    "reviews.export": (ReviewExportArgs, "把本会话复习卷的指定版本导出为 Markdown/学生版HTML/答案版HTML并发布到本会话文件区，返回可下载文件ID与哈希。导出复用原有渲染器并校验paper/revision/snapshot归属；未完成生成的卷不能导出。需要PDF版本时先导出得到正文，再用 artifacts.export 生成PDF并保留题目与证据来源。"),
}


# Arguments that only widen *observation* or pick a generation control mode, never the business
# request itself. They are declared per tool and used by exactly one place — the gateway's call
# cache identity — so that a trusted call id can be replayed with a longer or shorter wait window
# without turning into a bogus "different parameters" conflict.
#
# The rule is deliberately not global: a tool that does not declare an observation argument keeps
# a raw-arguments identity, so a changed business argument still conflicts. `new_generation` is
# intentionally NOT here: it changes which paper is produced, so it stays part of the identity.
OBSERVATION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "jobs.read": ("wait_seconds",),
    "jobs.cancel": ("wait_seconds",),
    "connections.sync": ("wait_seconds",),
    "reviews.generate": ("wait_seconds",),
}


def observation_arguments(name: str) -> tuple[str, ...]:
    """Observation-only argument names for one tool; empty for every other tool."""
    return OBSERVATION_ARGUMENTS.get(name, ())


def cache_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The gateway's cache identity: the business arguments with observation parameters removed.

    Read and write must both call this (never one raw and one normalised), otherwise a first call
    that already carries `wait_seconds` stores a fingerprint the replay of the *same* call can
    never match. The raw arguments stay untouched for journalling and for tool execution.
    """
    ignored = observation_arguments(name)
    if not ignored:
        return dict(arguments)
    return {key: value for key, value in arguments.items() if key not in ignored}


def schemas() -> list[dict[str, Any]]:
    return [{"name": name, "model_name": "campus_" + name.replace(".", "_"),
             "description": description, "input_schema": schema.model_json_schema(),
             "effects": "write" if name in {"campus.notice.import", "plans.preview", "plans.apply", "plans.revoke",
                                             "tasks.build", "jobs.cancel", "tasks.feedback", "connections.sync",
                                             "connections.courses.import", "artifacts.export", "reviews.generate",
                                             "reviews.export"} else "read", "version": 1}
            for name, (schema, description) in SPECS.items()]


def _deny() -> None:
    raise ApiError(403, "SCOPE_DENIED", "当前会话无权读取该对象")


def _course(db: Session, ctx: ToolContext, course_id: str) -> None:
    if ctx.course_ids is not None and course_id not in ctx.course_ids:
        _deny()
    courses.ensure_course(db, course_id)


def _scoped(stmt, column, ctx):
    return stmt.where(column.in_(ctx.course_ids)) if ctx.course_ids is not None else stmt


def _assignment_next_by_course(db: Session, args: dict, ctx: ToolContext):
    """Return one explicit local-cache status for every scoped course on this page."""
    course_stmt = _scoped(select(m.Course).where(m.Course.archived_at.is_(None)), m.Course.id, ctx)
    if args["cursor"]:
        raw_cursor = decode_cursor(args["cursor"])
        if not raw_cursor.startswith("course:"):
            raise ApiError(400, "VALIDATION_FAILED", "课程分页游标版本不匹配，请重新查询")
        course_stmt = course_stmt.where(m.Course.id > raw_cursor.removeprefix("course:"))
    courses_page = list(db.scalars(course_stmt.order_by(m.Course.id).limit(args["limit"] + 1)))
    has_more = len(courses_page) > args["limit"]
    courses_page = courses_page[:args["limit"]]
    next_cursor = encode_cursor("course:" + courses_page[-1].id) if has_more and courses_page else None
    course_ids = [course.id for course in courses_page]
    if not course_ids:
        return {"items": [], "next_cursor": None}, [], []

    base = select(m.Assignment).where(
        m.Assignment.archived_at.is_(None), m.Assignment.course_id.in_(course_ids))
    counts = db.execute(select(m.Assignment.course_id, func.count(m.Assignment.id),
        func.sum(case((m.Assignment.due_precision == "unknown", 1), else_=0)))
        .where(m.Assignment.archived_at.is_(None), m.Assignment.course_id.in_(course_ids))
        .group_by(m.Assignment.course_id)).all()
    count_by_course = {course_id: (int(total), int(unknown or 0))
                       for course_id, total, unknown in counts}

    before, after = _time(args["due_before"]), _time(args["due_after"])
    if before and after and after > before:
        raise ApiError(400, "INVALID_INPUT", "日期范围起点晚于终点")
    candidates = base.where(m.Assignment.due_at.is_not(None), m.Assignment.due_precision != "unknown")
    if before:
        candidates = candidates.where(m.Assignment.due_at <= before)
    if after:
        candidates = candidates.where(m.Assignment.due_at >= after)
    ranked = select(m.Assignment.id.label("assignment_id"), m.Assignment.course_id.label("course_id"),
        func.row_number().over(partition_by=m.Assignment.course_id,
            order_by=(m.Assignment.due_at.asc(), m.Assignment.id.asc())).label("course_rank"))\
        .where(*candidates.whereclause.clauses).subquery()
    selected = db.execute(select(ranked.c.course_id, ranked.c.assignment_id)
                          .where(ranked.c.course_rank == 1)).all()
    selected_ids = {course_id: assignment_id for course_id, assignment_id in selected}
    assignment_by_id = {assignment.id: assignment for assignment in
                        db.scalars(select(m.Assignment).where(m.Assignment.id.in_(selected_ids.values())))}
    source_refs_by_assignment = {}
    if selected_ids:
        source_refs = db.scalars(select(m.SourceRef).where(
            m.SourceRef.owner_type == m.OWNER_ASSIGNMENT,
            m.SourceRef.owner_id.in_(selected_ids.values())).order_by(
                m.SourceRef.seq, m.SourceRef.platform, m.SourceRef.external_id))
        for ref in source_refs:
            source_refs_by_assignment.setdefault(ref.owner_id, []).append(ref)

    result_items = []
    evidence = []
    for course in courses_page:
        total, unknown_count = count_by_course.get(course.id, (0, 0))
        assignment = assignment_by_id.get(selected_ids.get(course.id))
        if assignment is not None:
            due_at = assignment.due_at
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
            local_due = due_at.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
            source_refs = source_refs_by_assignment.get(assignment.id, [])
            status = "next_due"
            result_items.append({"id": assignment.id, "course_id": course.id, "course_title": course.title,
                "course_term": course.term,
                "course_status": status, "assignment_title": assignment.title,
                "due_at": due_at.astimezone(timezone.utc).isoformat(), "due_at_local": local_due,
                "submission_status": assignment.submission_status, "source_ref": source_refs[0].to_contract() if source_refs else None,
                "cached_assignment_count": total, "unknown_deadline_count": unknown_count})
            evidence.append(_evidence({"id": assignment.id,
                                      "source_refs": [ref.to_contract() for ref in source_refs]}))
        else:
            if args["due_before"] or args["due_after"]:
                status = "no_matching_deadline_in_range"
            elif total and unknown_count == total:
                status = "unknown_deadline_only"
            elif total:
                status = "no_known_deadline"
            else:
                status = "no_cached_assignments"
            result_items.append({"id": None, "course_id": course.id, "course_title": course.title,
                "course_term": course.term,
                "course_status": status, "assignment_title": None, "due_at": None, "due_at_local": None,
                "submission_status": None, "source_ref": None,
                "cached_assignment_count": total, "unknown_deadline_count": unknown_count})
    warnings = ["课程与作业均来自本机缓存；未证明学校平台全量，‘无缓存作业’不代表学校端没有作业"]
    return {"items": result_items, "next_cursor": next_cursor}, warnings, evidence


def _whole_workspace(db, ctx):
    required = set(db.scalars(select(m.Course.id).where(m.Course.archived_at.is_(None))))
    required.update(db.scalars(select(m.LearningTask.course_id).where(m.LearningTask.archived_at.is_(None), m.LearningTask.course_id.is_not(None))))
    if not ctx.allow_uncoursed_tasks or ctx.course_ids is None or not required.issubset(ctx.course_ids):
        raise ApiError(403, "SCOPE_DENIED", "安排需要完整本地工作区范围；课程变化后请新建会话")
    # Retained locks and warnings may refer to archived tasks outside today's task
    # query. Reuse the read adapter's complete plan-reference scope validation.
    execute("plans.read", {}, db, ctx)


def _preview_dates(args, tz):
    today = datetime.now(tz).date()
    try:
        if args["period"] == "dates":
            start, end = date.fromisoformat(args["start_date"] or ""), date.fromisoformat(args["end_date"] or "")
        else:
            if args["start_date"] is not None or args["end_date"] is not None:
                raise ValueError()
            start = today + timedelta(days=1) if args["period"] == "tomorrow" else today
            end = start if args["period"] == "tomorrow" else today + timedelta(days=6 - today.weekday())
        if start < today or end < start or (end - start).days > 30:
            raise ValueError()
        return start, end
    except ValueError:
        raise ApiError(400, "INVALID_INPUT", "请给出今天起最多 31 天的有效安排范围；相对日期不能同时指定绝对日期") from None


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("missing timezone")
        return result.astimezone(timezone.utc)
    except ValueError:
        raise ApiError(400, "INVALID_INPUT", "日期时间必须包含有效时区") from None


def _evening_blocks(args, start, end, tz):
    days = args["unavailable_evenings"]
    if not days:
        return []
    if args["period"] != "this_week" or len(set(days)) != len(days):
        raise ApiError(400, "INVALID_INPUT", "星期晚间约束只接受本周、且星期不可重复")
    monday = start - timedelta(days=start.weekday())
    blocks = []
    for weekday in sorted(days):
        day = monday + timedelta(days=weekday - 1)
        if day < start or day > end:
            raise ApiError(400, "INVALID_INPUT", "指定的本周晚间已过去，请说明是否指下周")
        first = datetime.combine(day, datetime.min.time(), tz) + timedelta(hours=18)
        last = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
        blocks.append({"starts_at": first.isoformat(), "ends_at": last.isoformat()})
    return blocks


def _evidence(item: dict) -> dict:
    entity_id = item.get("chunk_id") or item.get("id") or item.get("resource_id")
    source_refs = item.get("source_refs") or []
    return {"source_id": str(entity_id), "entity_id": item.get("resource_id") or entity_id,
            "source_system": source_refs[0]["platform"] if source_refs else "local",
            "version": item.get("resource_version", item.get("version", item.get("revision"))),
            "locator": item.get("locator"),
            "observed_at": source_refs[0].get("observed_at") if source_refs else None}


def _envelope(data: dict, *, scope: str, evidence: list[dict] | None = None,
              missing: list[str] | None = None, warnings: list[str] | None = None) -> dict:
    missing = list(missing or [])
    cursor = data.get("next_cursor")
    if cursor:
        missing.append("存在未读取的后续分页")
    evidence = evidence or []
    times = [e["observed_at"] for e in evidence if e.get("observed_at")]
    fetched_at = min(times) if times else None
    stale = (fetched_at is None or _time(fetched_at) < datetime.now(timezone.utc) - timedelta(hours=24))
    return {"status": "partial" if missing else "ok", "data": data, "evidence": evidence,
            "coverage": {"scope": scope, "complete": not missing, "missing": missing},
            "freshness": {"fetched_at": fetched_at, "stale": stale},
            "warnings": list(warnings or []), "next_cursor": cursor, "job_id": None, "error": None}


_SYNC_UNAVAILABLE = {"disabled", "unsupported", "service_unavailable", "blocked"}


def _scoped_connections(db: Session, ctx: ToolContext, args: dict) -> list[dict]:
    """Resolve the requested (or every usable) platform connection inside this conversation's grant."""
    every = connections.list_connections(db)["items"]
    items = [item for item in every if ctx.connection_ids is None or item["id"] in ctx.connection_ids]
    if args.get("connection_id"):
        chosen = [item for item in items if item["id"] == args["connection_id"]]
        if not chosen:
            # Distinguish "not authorized here" from "does not exist": only the former is a denial.
            if any(item["id"] == args["connection_id"] for item in every):
                _deny()
            raise ApiError(404, "NOT_FOUND", "该连接不存在")
        return chosen
    if args.get("platform"):
        chosen = [item for item in items if item["platform"] == args["platform"]]
        if not chosen:
            if any(item["platform"] == args["platform"] for item in every):
                _deny()
            raise ApiError(404, "NOT_FOUND", "该平台尚未配置连接")
        return chosen
    chosen = [item for item in items if item["status"] not in _SYNC_UNAVAILABLE]
    if not chosen:
        raise ApiError(409, "NO_CONNECTION", "没有可用平台连接；请先在设置中检查连接状态")
    return chosen


def _discovery_connections(db: Session, args: dict, ctx: ToolContext) -> list[dict]:
    chosen = _scoped_connections(db, ctx, args)
    if args.get("cursor") and len(chosen) != 1:
        raise ApiError(400, "INVALID_INPUT", "分页必须指定唯一 connection_id 或 platform，不能向多个平台复用游标")
    return chosen


def _platform_failure(connection: dict, exc: ApiError) -> dict:
    # Never forward arbitrary remote exception text into the model or journal.
    return {"platform": connection["platform"], "connection_id": connection["id"],
            "error": {"code": exc.code, "message": "该平台读取未完成；其他平台的结果已保留", "retryable": exc.retryable}}


def _discovery_result(courses, continuations, errors, chosen, **stats):
    return {"courses": courses, "next_cursor": continuations[0]["cursor"] if len(chosen) == 1 and continuations else None,
            "continuations": continuations, "errors": errors,
            "stats": {"platforms": len({c["platform"] for c in courses}), "courses": len(courses), **stats}}


_STALE_CACHE_WARNING = ("学校会话未授权或已失效：以下为本地缓存数据（可能过期），不代表学校端当前状态；"
                        "需要实时数据请在授权窗口登录后重试")


def _campus_query_envelope(data: dict) -> dict:
    """Build the campus.query/sync envelope without turning usable cache into failure.

    A dead school session still leaves the last verified cache readable. Reporting that
    as `status=error` made the model treat available data as a failed query. Records plus
    a connection error are `partial` data with the structured error kept for recovery.
    """
    error = data.get("error")
    records = data.get("records") or []
    warnings = list(data.get("warnings") or [])
    if error and records:
        status = "partial"
        warnings.append(_STALE_CACHE_WARNING)
    elif error:
        status = "error"
    elif data.get("completeness") == "complete":
        status = "ok"
    else:
        status = "partial"
    return {"status": status,
            "data": data,
            "evidence": [{"source_id": str(r.get("id") or r.get("code") or i),
                          "source_system": data["platform"], "observed_at": r.get("fetched_at") or data.get("fetched_at"),
                          "url": r.get("source") or r.get("origin")} for i, r in enumerate(records)],
            "coverage": {"scope": "campus:" + data.get("capability", ""),
                         "complete": status == "ok" and data.get("completeness") == "complete",
                         "missing": [] if status == "ok" else ["学校数据完整性未确认"]},
            "freshness": {"fetched_at": data.get("fetched_at"), "stale": data.get("freshness") != "fresh"},
            "warnings": warnings, "next_cursor": data.get("next_cursor"), "job_id": None, "error": error}


def _continuation(connection: dict, found: dict, args: dict) -> list[dict]:
    cursor = found.get("next_cursor")
    return ([{"connection_id": connection["id"], "platform": connection["platform"],
              "cursor": cursor}] if cursor else [])


def _connection_courses(db: Session, args: dict, ctx: ToolContext) -> tuple[dict, list[str]]:
    """List the courses that actually exist in the platform account (a real live read)."""
    courses, warnings, continuations, errors = [], [], [], []
    chosen = _discovery_connections(db, args, ctx)
    for connection in chosen:
        if connection["status"] in {"disabled", "service_unavailable", "blocked", "unsupported"}:
            warnings.append(f"{connection['platform']} 当前不可用（{connection['status']}），未读取")
            errors.append(_platform_failure(connection, ApiError(409, "UNSUPPORTED", "平台当前不可用")))
            continue
        try:
            found = connections.discover_platform_courses(connection["id"], db, cursor=args.get("cursor"))
        except ApiError as exc:
            if len(chosen) == 1:
                raise
            errors.append(_platform_failure(connection, exc))
            continue
        if found.get("errors"):
            errors.append(_platform_failure(connection, ApiError(502, "PROTOCOL_CHANGED", "部分课程记录无法解析")))
        for item in found.get("items", []):
            courses.append(dict(item) | {"platform": connection["platform"],
                                         "connection_id": connection["id"]})
        if found.get("next_cursor"):
            continuations.extend(_continuation(connection, found, args))
            warnings.append(f"{connection['platform']} 还有后续分页，可用 cursor 继续读取")
    if not courses and not warnings:
        warnings.append("平台没有返回课程；这不等于课程不存在，请核对平台登录状态")
    return _discovery_result(courses, continuations, errors, chosen), warnings


def _connection_course_import(db: Session, args: dict, ctx: ToolContext) -> tuple[dict, list[str]]:
    """Bind discovered platform courses locally so they can be synced and read."""
    bound, warnings, needs_login, continuations, errors = [], [], [], [], []
    wanted_ids = list(dict.fromkeys(args.get("external_ids") or []))
    if not args.get("all") and not wanted_ids:
        raise ApiError(400, "INVALID_INPUT", "请用 external_ids 指定课号，或用 all=true 导入该平台全部课程")
    chosen_connections = _discovery_connections(db, args, ctx)
    for connection in chosen_connections:
        if connection["status"] == "auth_required":
            needs_login.append(connection["platform"])
            errors.append(_platform_failure(connection, ApiError(409, "AUTH_REQUIRED", "需要登录")))
            continue
        if connection["status"] in {"disabled", "service_unavailable", "blocked", "unsupported"}:
            warnings.append(f"{connection['platform']} 当前不可用（{connection['status']}），未导入")
            errors.append(_platform_failure(connection, ApiError(409, "UNSUPPORTED", "平台当前不可用")))
            continue
        try:
            found = connections.discover_platform_courses(connection["id"], db, cursor=args.get("cursor"))
        except ApiError as exc:
            if len(chosen_connections) == 1:
                raise
            errors.append(_platform_failure(connection, exc))
            if exc.code in {"AUTH_REQUIRED", "AUTH_EXPIRED", "INTERACTION_REQUIRED"}:
                needs_login.append(connection["platform"])
            continue
        continuations.extend(_continuation(connection, found, args))
        if found.get("errors"):
            errors.append(_platform_failure(connection, ApiError(502, "PROTOCOL_CHANGED", "部分课程记录无法解析")))
        # Platform adapters key their course records by the platform course id; `external_id`
        # only exists once a course has been bound locally (mixed shapes appear after import).
        available = {(item.get("external_id") or item.get("id")): item for item in found.get("items", [])
                     if item.get("external_id") or item.get("id")}
        if args.get("all"):
            chosen = list(available.values())
        else:
            chosen = [available[external_id] for external_id in wanted_ids if external_id in available]
            missing = [external_id for external_id in wanted_ids if external_id not in available]
            if missing:
                warnings.append("以下课号未在平台发现，未导入：" + "、".join(missing[:10]))
        if not chosen:
            if available:
                warnings.append(f"{connection['platform']} 没有匹配的课号；请先用 connections.courses 查看可导入课程")
            continue
        result = connections.bind_platform_courses(db, connection["id"], chosen)
        bound.extend(dict(item) | {"platform": connection["platform"], "connection_id": connection["id"]}
                     for item in result["items"])
    if needs_login:
        warnings.append("以下平台需要先登录一次：" + "、".join(needs_login))
    if not bound and not continuations:
        if needs_login:
            raise ApiError(409, "AUTH_REQUIRED", "学在浙大/智云课堂的登录已失效，请在连接器窗口登录一次后重试",
                           retryable=False, details={"platforms": needs_login})
        raise ApiError(409, "CONFLICT", "没有导入任何课程；请先用 connections.courses 查看平台课程")
    warnings.append("已挂接的平台课程还需要 connections.sync 才会写入课件与作业")
    return _discovery_result(bound, continuations, errors, chosen_connections,
                             imported=len(bound), requires_login=needs_login), warnings


def _connection_sync(db: Session, args: dict, ctx: ToolContext) -> tuple[dict, list[str]]:
    """Refresh platform data (学在浙大/智云课堂) instead of answering from an empty cache.

    This is the capability the agent was missing: without it, "这周有哪些作业" could only
    search what had already been imported locally. It enqueues the same reviewed,
    read-only sync job the UI's 立即同步 uses — bounded to the conversation's connection
    scope, one job per platform, debounced against an active job — and never invents a
    session: a dead platform session comes back as AUTH_REQUIRED for one manual login.
    """
    chosen = _scoped_connections(db, ctx, args)
    jobs_out: list[dict] = []
    warnings: list[str] = []
    needs_login: list[str] = []
    errors: list[dict] = []
    for item in chosen:
        if item["status"] == "auth_required":
            needs_login.append(item["platform"])
            continue
        try:
            job, reason = connections.enqueue_connection_sync(
                db, item["id"], allowed_course_ids=ctx.course_ids, kinds=args.get("kinds"))
        except ApiError as exc:
            if exc.code in {"AUTH_EXPIRED", "AUTH_REQUIRED"}:
                needs_login.append(item["platform"])
                continue
            if len(chosen) == 1:
                raise
            errors.append(_platform_failure(item, exc))
            continue
        jobs_out.append(job.to_contract() | {"platform": item["platform"], "reused": reason == "reused"})
    if not jobs_out:
        if needs_login:
            raise ApiError(409, "AUTH_REQUIRED",
                           "学在浙大/智云课堂的登录已失效，请在连接器窗口登录一次后重试，任务进度会保留",
                           retryable=False, details={"platforms": needs_login})
        if errors:
            raise ApiError(502, errors[0]["error"]["code"], "所有选定平台同步均未受理，已有缓存保留",
                           retryable=errors[0]["error"]["retryable"])
        raise ApiError(409, "NO_CONNECTION", "没有可同步的平台连接")

    # The REST path commits after enqueueing. A tool request has its own Session;
    # without this commit its queued receipt referred to a job rolled back on close.
    # Commit before any bounded wait so the independent worker can claim the job.
    db.commit()

    if args.get("wait_seconds") and len(jobs_out) == 1:
        deadline = time.monotonic() + args["wait_seconds"]
        while time.monotonic() < deadline:
            time.sleep(0.4)
            db.expire_all()  # the worker writes from another thread/session
            current = jobs.get_job(jobs_out[0]["id"], db)
            jobs_out[0] = current | {"platform": jobs_out[0]["platform"], "reused": jobs_out[0]["reused"]}
            if current["status"] in {"succeeded", "failed", "cancelled", "partial_failed"}:
                break

    if needs_login:
        warnings.append("以下平台需要先登录一次：" + "、".join(needs_login))
    if errors:
        warnings.append("部分平台同步未受理；成功入队的任务已保留，请按errors分别处理")
    warnings.append("同步是后台任务；完成前读到的仍是旧缓存，请用 jobs.read 查看进度与结果引用")
    return {"jobs": jobs_out, "job": jobs_out[0] if len(jobs_out) == 1 else None, "errors": errors,
            "stats": {"platforms": len(chosen), "jobs": len(jobs_out), "requires_login": needs_login}}, warnings


def _await_sync_job(db: Session, job_id: str, seconds: int) -> dict[str, Any]:
    """Read a sync job until terminal or the already-capped observation window expires."""
    current = jobs.get_job(job_id, db)
    if seconds <= 0 or current.get("status") in review_actions.TERMINAL_JOB_STATES:
        return current
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.4)
        db.expire_all()
        current = jobs.get_job(job_id, db)
        if current.get("status") in review_actions.TERMINAL_JOB_STATES:
            break
    return current


def execute(name: str, arguments: dict, db: Session, ctx: ToolContext, *,
            allow_auth_recovery: bool = True) -> dict:
    """Execute one registered tool.

    ``allow_auth_recovery=False`` is for the trusted read-only recovery probe: a campus
    explicit sync may only refresh with the session that is already validated and must not
    start a password login or browser-cookie adoption. Every user/model call keeps the
    default, so the user's own ``campus.sync`` retains its bounded authorized recovery.
    """
    if ctx.principal != "local" or not ctx.conversation_id:
        _deny()
    if name not in SPECS:
        raise ApiError(400, "UNSUPPORTED", "未注册的校园工具")
    try:
        args = SPECS[name][0].model_validate(arguments).model_dump()
    except ValidationError as exc:
        # Give the model a repair target without echoing values, private content,
        # unknown user-supplied property names, or Pydantic's raw exception.
        issues = []
        for issue in exc.errors(include_url=False, include_context=False, include_input=False)[:16]:
            loc = issue["loc"]
            field = "<extra>" if issue["type"] == "extra_forbidden" else ".".join(map(str, loc))
            issues.append({"field": field, "type": issue["type"]})
        raise ApiError(400, "INVALID_INPUT", "工具参数不符合严格契约；请按字段错误修正后重试",
                       details={"validation_errors": issues}) from None
    if args.get("course_id"):
        _course(db, ctx, args["course_id"])
    if name.startswith("campus."):
        from ..campus_workspace import get_campus_workspace, OWNER
        from connectors.campus.connector_contracts import UserContext
        from connectors.campus.campus_common import CampusError
        workspace = get_campus_workspace()
        try:
            if name == "campus.status":
                data = workspace.status()
                # Credential metadata has its own purpose-scoped tool. Do not ship
                # masked usernames or credential-save timestamps with every portal
                # status lookup (and do not expose the raw browser-grant record).
                if isinstance(data, dict):
                    data.pop("credentials", None)
                    data.pop("last_auto_login", None)
                    data.pop("browser_authorization", None)
            elif name == "campus.credentials.status":
                from ..campus_credentials import all_status as credential_status
                credentials = credential_status()
                state = workspace.status()
                data = {
                    "credentials": [{"scope": item["scope"], "configured": bool(item["configured"]),
                                     "auto_relogin": bool(item["auto_relogin"])}
                                    for item in credentials.get("items", []) if isinstance(item, dict)],
                    "browser_session_authorized": bool(state.get("browser_session_authorized")),
                    "campus_workspace_connected": bool(state.get("connected")),
                    "platform_connections": [{"platform_id": item["platform_id"],
                                               "connected": bool(item.get("connected"))}
                                              for item in state.get("connectors", [])
                                              if isinstance(item, dict) and isinstance(item.get("platform_id"), str)],
                    "note": "凭据已保存、自动重登开关、浏览器复用授权和当前连接是不同状态；此本机状态快照不能验证学校是否接受凭据或证明登录失败原因。读取此工具不会尝试登录或访问校园网络。",
                }
            elif name in {"campus.query", "campus.sync"}:
                capability = args["capability"]
                if capability == "courses":
                    # The connector adapters never implemented `courses`; the real ZDBK
                    # timetable lives in the campus manager's snapshot (workbench-era code
                    # the Agent path never migrated). Prefer it directly instead of going
                    # through the connector first: that path would trigger one redundant
                    # automatic login and clear the snapshot it just wiped.
                    data = (workspace.sync_timetable(allow_recovery=allow_auth_recovery)
                            if name == "campus.sync" else workspace.timetable_records())
                    if not data.get("records"):
                        # Keep the legacy connector cache as a last-resort read.
                        data = workspace.query(capability, args["query"], args["platform"],
                                               refresh=False, allow_recovery=allow_auth_recovery)
                else:
                    data = workspace.query(capability, args["query"], args["platform"],
                                           refresh=name == "campus.sync", allow_recovery=allow_auth_recovery)
                data.setdefault("capability", capability)
                return _campus_query_envelope(data)
            elif name == "campus.academics":
                if args["day"]:
                    # Validate, then pass the ISO string: the connector parses it itself.
                    date.fromisoformat(args["day"])
                data = workspace.manager.academic_view(OWNER, args["day"], args["term"])
            elif name == "campus.notice.read":
                found = workspace.manager.service.cached_college_notice(UserContext(OWNER), args["record_id"])
                if not found:
                    raise ApiError(404, "NOT_FOUND",
                        "本机没有该学院通知。仅可读取 campus.query/sync(capability=college_notices) 返回的记录；校级 notices 摘要无需读取正文。")
                data = {"platform": found[0], "notice": found[1].model_dump(mode="json")}
            elif name == "campus.webvpn.entry":
                data = workspace.webvpn_entry(args["service"])
            elif name == "campus.notice.import":
                from ..campus_domain import import_notice
                data = import_notice(db, args["course_id"], args["record_id"])
            else:
                from ..campus_migration import list_imports, read_imported_document
                if bool(args["batch_id"]) != bool(args["document_id"]):
                    raise ApiError(400, "INVALID_INPUT", "读取导入资料需要同时给出batch_id和document_id")
                data = read_imported_document(workspace.store, args["batch_id"], args["document_id"]) if args["batch_id"] else list_imports(workspace.store)
        except (ValidationError, ValueError, TypeError):
            raise ApiError(400, "INVALID_INPUT", "校园查询参数无效") from None
        except CampusError as exc:
            if name == "campus.notice.read" and exc.code == "UNSUPPORTED":
                raise ApiError(409, "UNSUPPORTED",
                    "仅支持 campus.query/sync(capability=college_notices) 返回的学院通知 ID；校级 notices 摘要不支持正文读取，请直接使用列表字段。") from None
            from connectors.campus.connector_contracts import standard_error
            error = standard_error(exc)
            raise ApiError(409, error.code.value, error.message) from None
        if name in {"campus.status", "campus.credentials.status"}:
            # These tools report local connection/secret-store metadata, not
            # fetched campus records. Requiring a remote fetched_at falsely
            # marks a complete local status read as partial and confuses the
            # remote-session limitation with missing local fields.
            observed_at = datetime.now(timezone.utc).isoformat()
            if name == "campus.credentials.status":
                scope = "local_auth_metadata"
                source = "local_credential_store_and_workspace"
                warning = "这是本机状态快照；不验证学校端是否接受凭据或远端 session 是否有效"
            else:
                scope = "local_campus_connection_metadata"
                source = "local_campus_workspace"
                warning = "这是本机连接器/session 状态快照；本工具不主动验证远端连通性或登录有效性"
            return _envelope(data, scope=scope,
                evidence=[{"source_system": source, "observed_at": observed_at}],
                warnings=[warning])
        return _envelope(data, scope=name, missing=["校园数据以其原始获取时间及完整性声明为准"])
    data: dict
    evidence: list[dict] = []
    warnings: list[str] = []
    missing: list[str] = []
    scope = "local_workspace"

    if name == "tasks.feedback":
        task = db.get(m.LearningTask, args["task_id"])
        if task is None or (ctx.course_ids is not None and task.course_id not in ctx.course_ids
                            and not (task.course_id is None and ctx.allow_uncoursed_tasks)):
            _deny()
        if not ctx.request_key:
            raise ApiError(403, "RUNTIME_DENIED", "反馈需要可信调用标识")
        data = feedback_actions.record(db, ctx.conversation_id, ctx.request_key, args)
        warnings.append("这是本地学习反馈，学校提交状态未改变")
        evidence = [_evidence(data["task"]) | {"observed_at": datetime.now(timezone.utc).isoformat()}]
    elif name == "tasks.build":
        if not ctx.request_key:
            raise ApiError(403, "RUNTIME_DENIED", "生成任务需要可信调用标识")
        data = {"job": job_actions.build(db, ctx.conversation_id, ctx.request_key, args["course_id"])}
        warnings.append("生成请求已受理；完成与否须读取真实 job 状态")
    elif name == "jobs.cancel":
        data = {"job": job_actions.cancel(db, ctx.conversation_id, args["job_id"], ctx.connection_ids,
                                          course_ids=ctx.course_ids)}
        warnings.append("停止本轮对话与停止后台任务是两个操作；running 状态仍需等待取消检查点")
    elif name in {"availability.read", "plans.preview", "plans.apply", "plans.revoke"}:
        _whole_workspace(db, ctx)
        availability = db.get(m.Availability, 1)
        if name == "availability.read":
            data = {"configured": availability is not None, "availability": availability.to_contract() if availability else None}
        elif name == "plans.preview":
            if not ctx.request_key:
                raise ApiError(403, "RUNTIME_DENIED", "预览需要可信调用标识")
            tz = resolve_user_tz(availability.timezone if availability else "Asia/Shanghai")
            start, end = _preview_dates(args, tz)
            evenings = _evening_blocks(args, start, end, tz)
            windows = args["temporary_windows"]
            # The user asked for a concrete arrangement but this machine has no persisted
            # availability setting. Failing the whole task with CONFLICT would leave the
            # Agent unable to act at all, so preview with one disclosed default window
            # instead. It is never persisted as a long-term preference and the warning
            # below tells the model to surface it before the user accepts anything.
            defaulted = availability is None and not windows
            if defaulted:
                windows = [{"weekday": d, "start": "08:00", "end": "22:00"} for d in range(1, 8)]
            data = {"action": plan_actions.preview(db, ctx.conversation_id, ctx.request_key, start, end,
                    blocked=args["blocked"] + evenings, windows=windows)}
            if defaulted:
                warnings.append("本机未配置长期可用时间，本次预览按默认 08:00–22:00 计算（仅本次临时，不写入长期偏好）；"
                                "接受前应告知用户并可要求调整时段")
            if evenings:
                warnings.append("本次预览将‘晚上’按18:00至次日00:00排除；可在接受前提供更精确的时间")
            warnings.append("预览尚未生效；请检查差异与未分配任务后接受方案")
        else:
            data = {"receipt": plan_actions.execute(db, ctx.conversation_id, args["action_id"], name.split(".")[1])}
            warnings.append("这是操作回执；当前安排请读取 plans.read")
        evidence = [{"source_id": name, "entity_id": ctx.conversation_id, "source_system": "local",
                     "version": None, "locator": None, "observed_at": datetime.now(timezone.utc).isoformat()}]
    elif name == "connections.courses":
        data, warnings = _connection_courses(db, args, ctx)
        if data["errors"] or data["continuations"]:
            missing.append("平台读取失败或仍有未读取分页；按errors与continuations继续")
        evidence = [{"source_id": str(item.get("external_id") or ""), "entity_id": None,
                     "source_system": item.get("platform") or "platform", "version": None, "locator": None,
                     "observed_at": datetime.now(timezone.utc).isoformat()} for item in data["courses"]]
        scope = "platform_courses:" + (args.get("platform") or "all")
    elif name == "connections.courses.import":
        data, warnings = _connection_course_import(db, args, ctx)
        if data["errors"] or data["continuations"]:
            missing.append("仅导入已成功读取的当前页；按errors与continuations继续")
        evidence = [{"source_id": str(item.get("id")), "entity_id": item.get("id"),
                     "source_system": item.get("platform") or "platform", "version": None, "locator": None,
                     "observed_at": datetime.now(timezone.utc).isoformat()} for item in data["courses"]]
        scope = "platform_courses_import:" + (args.get("platform") or "all")
    elif name == "connections.sync":
        data, warnings = _connection_sync(db, args, ctx)
        if data["errors"] or data["stats"]["requires_login"]:
            missing.append("部分平台未同步，已保留其他平台任务；按errors及requires_login分别处理")
    elif name == "connections.status":
        cid = args["connection_id"]
        if cid and ctx.connection_ids is not None and cid not in ctx.connection_ids:
            _deny()
        data = ({"items": [connections.get_connection(cid, db)], "next_cursor": None}
                if cid else connections.list_connections(db))
        if ctx.connection_ids is not None:
            data["items"] = [i for i in data["items"] if i["id"] in ctx.connection_ids]
    elif name == "courses.list":
        stmt = _scoped(select(m.Course).where(m.Course.archived_at.is_(None)), m.Course.id, ctx)
        if args["query"]:
            stmt = stmt.where(m.Course.title.contains(args["query"], autoescape=True))
        if args["term"]:
            stmt = stmt.where(m.Course.term == args["term"])
        rows, cursor = page(db, stmt, m.Course, args["cursor"], args["limit"])
        data = {"items": [courses.course_to_contract(db, c) for c in rows], "next_cursor": cursor}
        if args["query"] and (len(rows) > 1 or cursor):
            warnings.append("课程名称匹配多个对象，请按学期或课程ID确认后继续，不要自行选择其中一门")
    elif name == "materials.list":
        stmt = select(m.Resource).where(m.Resource.course_id == args["course_id"], m.Resource.archived_at.is_(None))
        rows, cursor = page(db, stmt, m.Resource, args["cursor"], args["limit"])
        data = {"items": [resources.resource_to_contract(db, r) for r in rows], "next_cursor": cursor}
        if any(r.parse_status != "ready" for r in rows):
            missing.append("部分资料正文尚未解析")
    elif name == "materials.search":
        data = search.search_scoped(db=db, q=args["query"], course_id=args["course_id"], kind=None,
                                    cursor=args["cursor"], limit=args["limit"], course_ids=ctx.course_ids)
        if ctx.course_ids is not None and not args["course_id"]:
            scope = "authorized_courses"
        # Search is a discovery step: do not repeat up to 50 full 6000-character
        # chunks in the next model prompt. Retain IDs/version/locator so the model
        # can read the exact evidence, and explicitly mark excerpts as incomplete.
        excerpt_limit = min(1200, 24000 // max(1, len(data["items"])))
        for item in data["items"]:
            text = item["text"]
            item["text_is_excerpt"] = len(text) > excerpt_limit
            item["original_text_chars"] = len(text)
            if item["text_is_excerpt"]:
                match = text.lower().find(args["query"].lower())
                start = max(0, min(max(0, match - 200), len(text) - excerpt_limit))
                item["text"] = text[start:start + excerpt_limit]
                item["excerpt_start_offset"] = start
        if any(i["text_is_excerpt"] for i in data["items"]):
            missing.append("检索返回命中摘要而非完整原文；使用materials.read读取片段")
        if not data["items"]:
            warnings.append("没有找到正文依据；不能据此推断要求不存在")
    elif name == "materials.read":
        resource = db.get(m.Resource, args["resource_id"])
        if resource is None or resource.archived_at is not None:
            raise ApiError(404, "NOT_FOUND", "原文不存在或已归档")
        _course(db, ctx, resource.course_id)
        if resource.version != args["expected_version"]:
            raise ApiError(409, "STALE_VERSION", "资料版本已变化，请重新检索")
        if resource.parse_status != "ready":
            raise ApiError(409, "NO_EVIDENCE", "当前资料尚无可读取正文")
        if args["chunk_ids"] is not None:
            if args["cursor"] is not None:
                raise ApiError(400, "INVALID_INPUT", "指定片段读取不能同时使用分页游标")
            requested = set(args["chunk_ids"])
            chunks = list(db.scalars(select(m.ContentChunk).where(m.ContentChunk.resource_id == resource.id,
                          m.ContentChunk.resource_version == resource.version, m.ContentChunk.id.in_(requested))
                          .order_by(m.ContentChunk.ordinal, m.ContentChunk.id)))
            if {c.id for c in chunks} != requested:
                raise ApiError(400, "INVALID_INPUT", "指定片段不属于本次读取的资料范围")
            data = {"items": [c.to_contract() for c in chunks], "next_cursor": None}
        else:
            # Return whole chunks up to the text budget, not a predictable failure
            # for the default 20 x 6000-character imported notice chunks. Keep the
            # existing ordinal cursor protocol and never split/lossily summarize evidence.
            stmt = select(m.ContentChunk).where(m.ContentChunk.resource_id == resource.id,
                                               m.ContentChunk.resource_version == resource.version)
            if args["cursor"] is not None:
                stmt = stmt.where(m.ContentChunk.ordinal > decode_int_cursor(args["cursor"]))
            rows = list(db.scalars(stmt.order_by(m.ContentChunk.ordinal).limit(args["limit"] + 1)))
            selected, characters = [], 0
            for chunk in rows[:args["limit"]]:
                if characters + len(chunk.text) > 24000:
                    if not selected:
                        raise ApiError(400, "BUDGET_EXCEEDED", "单个正文片段超出24000字符读取预算，需重新分段导入")
                    break
                selected.append(chunk)
                characters += len(chunk.text)
            cursor = encode_cursor(str(selected[-1].ordinal)) if selected and len(rows) > len(selected) else None
            data = {"items": [chunk.to_contract() for chunk in selected], "next_cursor": cursor}
        data["resource"] = resources.resource_to_contract(db, resource)
        if not data["resource"]["chunk_count"]:
            raise ApiError(409, "NO_EVIDENCE", "当前版本没有可读取的正文片段")
        if len(data["items"]) < data["resource"]["chunk_count"]:
            missing.append("仅返回指定或分页片段，尚未覆盖资料全文")
        scope = f"material:{resource.id}:v{resource.version}"
        if sum(len(i["text"]) for i in data["items"]) > 24000:
            raise ApiError(400, "BUDGET_EXCEEDED", "正文超出单次读取预算，请缩小片段范围")
    elif name == "assignments.next_by_course":
        data, warnings, evidence = _assignment_next_by_course(db, args, ctx)
        if data["next_cursor"]:
            missing.append("存在未读取的后续课程分页")
    elif name == "assignments.list":
        stmt = _scoped(select(m.Assignment).where(m.Assignment.archived_at.is_(None)), m.Assignment.course_id, ctx)
        if args["course_id"]:
            stmt = stmt.where(m.Assignment.course_id == args["course_id"])
        before, after = _time(args["due_before"]), _time(args["due_after"])
        if before and after and after > before:
            raise ApiError(400, "INVALID_INPUT", "日期范围起点晚于终点")
        # Unknown dates stay visible alongside any date-filtered result.
        if before:
            stmt = stmt.where(or_(m.Assignment.due_at.is_(None), m.Assignment.due_at <= before))
        if after:
            stmt = stmt.where(or_(m.Assignment.due_at.is_(None), m.Assignment.due_at >= after))
        if args["order_by"] == "due_soon":
            if args["cursor"]:
                raw_cursor = decode_cursor(args["cursor"])
                if "|" not in raw_cursor:
                    raise ApiError(400, "VALIDATION_FAILED", "截止时间排序游标无效")
                due_cursor, cursor_id = raw_cursor.rsplit("|", 1)
                if not cursor_id:
                    raise ApiError(400, "VALIDATION_FAILED", "截止时间排序游标无效")
                if due_cursor == "unknown":
                    stmt = stmt.where(m.Assignment.due_at.is_(None), m.Assignment.id > cursor_id)
                else:
                    due_value = _time(due_cursor)
                    stmt = stmt.where(or_(m.Assignment.due_at.is_(None),
                        m.Assignment.due_at > due_value,
                        and_(m.Assignment.due_at == due_value, m.Assignment.id > cursor_id)))
            ordered = stmt.order_by(case((m.Assignment.due_at.is_(None), 1), else_=0),
                                    m.Assignment.due_at.asc(), m.Assignment.id.asc())
            rows = list(db.scalars(ordered.limit(args["limit"] + 1)))
            has_more = len(rows) > args["limit"]
            rows = rows[:args["limit"]]
            cursor = None
            if has_more and rows:
                last = rows[-1]
                if last.due_at is None:
                    key = "unknown"
                else:
                    value = last.due_at
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=timezone.utc)
                    key = value.astimezone(timezone.utc).isoformat()
                cursor = encode_cursor(f"{key}|{last.id}")
        else:
            rows, cursor = page(db, stmt, m.Assignment, args["cursor"], args["limit"])
        course_ids = {a.course_id for a in rows}
        course_details = ({row.id: (row.title, row.term) for row in db.scalars(
            select(m.Course).where(m.Course.id.in_(course_ids)))} if course_ids else {})
        items = []
        for assignment in rows:
            item = assignment.to_contract(refs_for(db, m.OWNER_ASSIGNMENT, assignment.id))
            title, term = course_details.get(assignment.course_id, (None, None))
            item["course_title"] = title
            item["course_term"] = term
            items.append(item)
        if not args["include_instructions"]:
            for item in items:
                item.pop("instructions", None)
        data = {"items": items, "next_cursor": cursor}
        if any(a.due_precision == "unknown" for a in rows):
            warnings.append("包含截止时间未知的作业，请核对平台原文")
    elif name == "tasks.list":
        stmt = select(m.LearningTask).where(m.LearningTask.archived_at.is_(None))
        if ctx.course_ids is not None:
            allowed = m.LearningTask.course_id.in_(ctx.course_ids)
            if ctx.allow_uncoursed_tasks:
                allowed = or_(allowed, m.LearningTask.course_id.is_(None))
            stmt = stmt.where(allowed)
        if args["course_id"]:
            stmt = stmt.where(m.LearningTask.course_id == args["course_id"])
        if args["status"]:
            stmt = stmt.where(m.LearningTask.status == args["status"])
        before = _time(args["due_before"])
        if before:
            stmt = stmt.where(or_(m.LearningTask.due_at.is_(None), m.LearningTask.due_at <= before))
        rows, cursor = page(db, stmt, m.LearningTask, args["cursor"], args["limit"])
        data = {"items": [tasks.task_to_contract(db, t) for t in rows], "next_cursor": cursor}
        warnings.append("本地任务状态不代表学校作业提交状态")
    elif name == "plans.read":
        data = plans.get_plan(args["plan_id"], db) if args["plan_id"] else plans.get_current_plan(db)
        plan = data if args["plan_id"] else data.get("plan")
        task_ids = {b["task_id"] for b in (plan or {}).get("blocks", [])}
        task_ids.update(i["task_id"] for i in (plan or {}).get("unassigned", []))
        task_ids.update(tid for w in (plan or {}).get("warnings", []) for tid in w.get("task_ids", []))
        for task_id in task_ids:
            task = db.get(m.LearningTask, task_id)
            if task is None or (ctx.course_ids is not None and task.course_id not in ctx.course_ids
                                and not (task.course_id is None and ctx.allow_uncoursed_tasks)):
                _deny()
    elif name == "jobs.read":
        if args["job_id"] not in ctx.job_ids and not _job_owned(db, ctx, args["job_id"]):
            _deny()
        data = jobs.get_job(args["job_id"], db)
        # Bounded observation of selected background jobs avoids rapid model polling. It is a read:
        # no state is mutated and the row is re-read from the database, never a cached dict.
        requested_wait = args.get("wait_seconds")
        if requested_wait is None:
            requested_wait = 14 if data.get("type") in {"sync", "review_generate"} else 0
        window = review_actions.job_wait_window(db, data, requested_wait)
        if window > 0:
            if data.get("type") == "sync":
                data = _await_sync_job(db, args["job_id"], window)
            else:
                data = review_actions.await_job(db, ctx, args["job_id"], window)
            if data.get("status") not in review_actions.TERMINAL_JOB_STATES:
                warnings.append(f"已在本调用内等待约 {window} 秒仍未终态，任务仍在后台运行；"
                                "可再用 jobs.read（带 wait_seconds）或有界等待读取真实状态")
        # Payload is deliberately excluded by the existing job contract.
    elif name in {"reviews.preflight", "reviews.generate"}:
        if name == "reviews.preflight":
            gaps: list = []
            try:
                outcome = review_actions.preflight(db, ctx, args)
            except ApiError as exc:
                # "No usable material" is an honest gap report, not a tool crash: answer
                # can_generate=false with the missing entries so the model can tell the user
                # exactly what is needed instead of inventing a paper.
                if exc.code not in {"VALIDATION_FAILED", "NO_EVIDENCE"} or not isinstance(exc.details, dict):
                    raise
                gaps = list(exc.details.get("missing") or exc.details.get("gaps") or [])
                outcome = None
            if outcome is None:
                data = {"fingerprint": None, "materials": {"items": [], "missing": gaps, "notes": None,
                                                           "total_chars": 0}, "provider": None, "limits": None,
                        "bank": None, "selection": None, "can_generate": False}
                warnings.append("该选择没有可用正文；不能出卷，请补齐缺失材料后再预检")
            else:
                materials = dict(outcome["view"]["materials"])
                # Preflight is metadata only: never ship whole material bodies to the model.
                materials["items"] = [{key: value for key, value in item.items() if key != "text"}
                                      for item in materials["items"]]
                data = {"fingerprint": outcome["view"]["fingerprint"], "materials": materials,
                        "provider": outcome["view"]["provider"], "limits": outcome["view"]["limits"],
                        "bank": outcome["view"]["bank"], "selection": outcome["selection"],
                        "can_generate": not materials["missing"]
                                        and (args["mode"] != "bank" or outcome["view"]["bank"]["usable"])}
                warnings.extend(outcome["view"]["warnings"])
            if not data["materials"]["items"]:
                data["can_generate"] = False
                missing.append("该课程当前没有可用材料（或许可材料无正文）；不能出卷，也不能假装读过")
            elif data["materials"]["missing"]:
                missing.append("部分材料不可用；确认是否继续或补齐后再生成")
            if args["mode"] == "bank" and not (data["bank"] or {}).get("usable"):
                warnings.append("bank 模式当前没有可用题库来源；请配置题库来源或改用 ai 模式")
            warnings.append("预检不发送正文、不创建快照、不入队；生成前材料变化会使旧 fingerprint 失效并需重新预检")
            scope = "review_preflight:" + args["course_id"]
            evidence = [{"source_id": str(item.get("id") or item.get("resource_id") or item.get("assignment_id")),
                         "entity_id": item.get("resource_id") or item.get("assignment_id"),
                         "source_system": "local", "version": item.get("version"),
                         "locator": None, "observed_at": datetime.now(timezone.utc).isoformat()}
                        for item in data["materials"]["items"]]
        else:
            data = review_actions.generate(db, ctx, args)
            data["job"] = _job_with_ownership(db, ctx, data["job"])
            data["observed_within_call"] = data["job"]["status"] in review_actions.TERMINAL_JOB_STATES
            if data.get("reused"):
                # F2: reuse is not acceptance. Report the existing generation's real state and
                # point the model at observation/read/export instead of re-submitting the request.
                if data["job"]["status"] == "succeeded":
                    warnings.append(f"同一业务请求已有完成的出卷（job {data['job']['id']}）；本次复用该试卷，"
                                    "未重复受理或重复计费，可直接 reviews.read/reviews.export")
                else:
                    warnings.append(f"同一业务请求已在途（job {data['job']['id']}）；本次复用原作业，"
                                    "未重复受理或重复计费，请用 jobs.read 查看真实进度")
                warnings.append("如用户明确要求另出一份全新试卷，请显式传 new_generation=true；不要靠重复调用试探")
            if args.get("wait_seconds"):
                # Bounded same-call observation of the independent worker, anchored to the job's
                # acceptance time so a repeated call cannot reset the window and spin one turn.
                # Still never reports acceptance as success: only a real terminal row changes it.
                window = review_actions.wait_window_seconds(db, data["job"]["id"], args["wait_seconds"])
                if window <= 0 and data["job"]["status"] not in review_actions.TERMINAL_JOB_STATES:
                    warnings.append("该作业已超过服务端等待窗口，本次不再等待；请用 jobs.read 读取真实状态")
                data["job"] = review_actions.await_job(db, ctx, data["job"]["id"], window)
                data["observed_within_call"] = data["job"]["status"] in review_actions.TERMINAL_JOB_STATES
                if data["observed_within_call"]:
                    warnings.append(f"已在本次调用内等待后台任务进入终态：{data['job']['status']}")
                else:
                    warnings.append("等待窗口内后台任务仍未终态，请用 jobs.read 继续；受理不是成功")
            data["job"] = _job_with_ownership(db, ctx, data["job"])
            if not data.get("reused"):
                warnings.append("出卷请求已受理并持久入队；必须用 jobs.read 查看真实进度，受理本身不是成功")
                warnings.append("生成会消耗已配置模型的额度；请按已配置 provider 与实际用量核对，不要在失败后自动放量重试")
            scope = "review_generate:" + args["course_id"]
            evidence = [{"source_id": item.get("resource_id") or item.get("assignment_id"),
                         "entity_id": item.get("resource_id") or item.get("assignment_id"),
                         "source_system": "frozen_snapshot", "version": item.get("version"),
                         "locator": None, "observed_at": datetime.now(timezone.utc).isoformat()}
                        for item in data["selection"].get("resources", []) + data["selection"].get("assignments", [])]
    elif name == "reviews.read":
        data = review_actions.read(db, ctx, args)
        if data["truncated"]:
            missing.append(f"仅返回前 {review_actions.MAX_QUESTIONS_RETURNED} 题；完整卷请用 reviews.export")
        if not data["questions"]:
            warnings.append("该版本没有题目；不要据此声明出卷成功")
        warnings.append("题目正文由模型按冻结材料生成；出处引用必须与快照材料版本一起核对")
        scope = f"review_paper:{args['paper_id']}:v{data['paper']['revision']}"
        evidence = [{"source_id": item.get("resource_id") or item.get("assignment_id"),
                     "entity_id": item.get("resource_id") or item.get("assignment_id"),
                     "source_system": "frozen_snapshot", "version": item.get("version"),
                     "locator": None, "observed_at": datetime.now(timezone.utc).isoformat()}
                    for item in data["snapshot"]["items"]]
    elif name == "reviews.export":
        data = review_actions.export(db, ctx, _file_store(ctx), args)
        warnings.append("已发布到本会话文件区；文件哈希来自磁盘实际内容")
        warnings.append("导出保留试卷版本与冻结材料指纹；原引擎的Markdown导出为既有可靠路径")
        scope = f"review_export:{args['paper_id']}"
        evidence = [{"source_id": data["file"]["id"], "entity_id": args["paper_id"], "source_system": "session_files",
                     "version": data["paper"]["revision"], "locator": data["file"]["name"],
                     "observed_at": datetime.now(timezone.utc).isoformat()}]
    elif name == "artifacts.export":
        data = artifacts.export(db, ctx, args, _file_store(ctx))
        evidence = [{"source_id": item["id"], "entity_id": item["id"], "source_system": "session_files",
                     "version": None, "locator": item["name"],
                     "observed_at": datetime.now(timezone.utc).isoformat()} for item in data["files"]]
        scope = "artifact:export"
        warnings.append("产物已发布到本会话文件区并可下载；文件哈希来自磁盘实际内容，不是按后缀推断")
        warnings.append("渲染器只负责排版：未提供的日期/出处保持 unknown，模型填写的事实仍需人工核对原文")
    else:
        raise AssertionError(name)

    if "items" in data and name not in {"connections.status", "assignments.next_by_course"}:
        evidence = [_evidence(i | {"source_refs": data["resource"]["source_refs"]}) for i in data["items"]] if name == "materials.read" else [_evidence(i) for i in data["items"]]
    # Workspace queries cannot prove the school data feed is complete.
    if name in {"assignments.list", "assignments.next_by_course", "materials.list", "courses.list"}:
        missing.append("范围仅为本机已导入数据，未证明学校平台全量覆盖")
    if args.get("course_id"):
        scope = "course:" + args["course_id"]
    result = _envelope(data, scope=scope, evidence=evidence, missing=missing, warnings=warnings)
    if name == "courses.list" and args["query"] and (len(data["items"]) > 1 or data["next_cursor"]):
        result["status"] = "needs_user"
    if name in {"tasks.build", "jobs.cancel", "jobs.read", "connections.sync", "reviews.generate"}:
        job = data.get("job") if name == "connections.sync" else data.get("job", data)
        if job:
            result["job_id"] = job["id"]
            result["freshness"] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "stale": False}
            if job["status"] in {"queued", "running"}:
                # Report the real state instead of collapsing both into "queued": a model that
                # reads only the envelope status would otherwise be unable to tell an unclaimed
                # queue entry from a job an independent worker is actively processing.
                result["status"] = job["status"]
        observed_jobs = data.get("jobs", []) if name == "connections.sync" else ([job] if job else [])
        failures = [item for item in observed_jobs if item["status"] in {"failed", "partial_failed"}]
        if failures:
            result["coverage"]["complete"] = False
            result["coverage"]["missing"].append("后台任务有失败；读取状态成功不代表业务完成，保留已有结果引用")
            all_failed = all(item["status"] == "failed" for item in observed_jobs)
            result["status"] = "error" if all_failed else "partial"
            if all_failed:
                causes = [cause.get("code") for item in failures for cause in item.get("errors", []) if isinstance(cause, dict)]
                code = next((code for code in causes if code in {"TIMEOUT", "RATE_LIMITED", "NETWORK_UNREACHABLE",
                    "AUTH_REQUIRED", "AUTH_EXPIRED", "INTERACTION_REQUIRED", "FORBIDDEN", "PROTOCOL_CHANGED"}), "JOB_FAILED")
                result["error"] = {"code": code, "message": "后台任务未完成；请按错误核对数据源，不要把入队或状态读取当成成功",
                                   "retryable": False}
        elif any(item["status"] in {"queued", "running"} for item in observed_jobs):
            # Never collapse an actively processed job back into "queued": a model reading only
            # the envelope would poll a job that is already running. Report the furthest real
            # pending state across the observed jobs.
            pending = {item["status"] for item in observed_jobs} & {"queued", "running"}
            result["status"] = "running" if "running" in pending else "queued"
    return result
