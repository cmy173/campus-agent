import dayjs from 'dayjs'
import type {
  Assignment,
  Availability,
  Capabilities,
  Connection,
  ContentChunk,
  Course,
  ErrorInfo,
  FeedbackRecord,
  Job,
  JobType,
  LearningTask,
  Plan,
  Recording,
  Resource,
  ResourceVersion,
  SourceRef,
} from '../../../types/contract'
import type {
  ProviderConfig,
  QuestionbankCapabilitySource,
  ReviewPaper,
  ReviewRequest,
} from '../../../types/review'
import { DEFAULT_TIMEZONE, toApiIso } from '../../time'
import { MAX_UPLOAD_BYTES } from '../../upload'

/** 相对今天的时刻（mock 数据始终落在当前一周内，便于演示） */
function at(dayOffset: number, hhmm: string): string {
  const base = dayjs().tz(DEFAULT_TIMEZONE)
  const [h, m] = hhmm.split(':').map(Number)
  return toApiIso(
    base.add(dayOffset, 'day').hour(h).minute(m).second(0).millisecond(0),
    DEFAULT_TIMEZONE,
  )
}

function ago(hours: number): string {
  return toApiIso(dayjs().subtract(hours, 'hour').second(0).millisecond(0), DEFAULT_TIMEZONE)
}

function dayKeyOffset(dayOffset: number): string {
  return dayjs().tz(DEFAULT_TIMEZONE).add(dayOffset, 'day').format('YYYY-MM-DD')
}

export interface RuntimeJob {
  durationMs: number
  startedMs: number | null
  onComplete: () => Partial<Job>
  /** 进度推进时回调（复习卷任务据此更新 phase 等附加字段） */
  onProgress?: (job: Job, progress: number) => void
}

export interface MockDb {
  courses: Course[]
  resources: Resource[]
  resourceVersions: ResourceVersion[]
  chunks: ContentChunk[]
  assignments: Assignment[]
  recordings: Recording[]
  tasks: LearningTask[]
  feedback: FeedbackRecord[]
  availability: Availability
  connections: Connection[]
  capabilities: Map<string, Capabilities>
  jobs: Job[]
  runtimeJobs: Map<string, RuntimeJob>
  /** build_tasks 任务的课程范围（jobId → course_id|null），用于同范围去重复用 */
  buildScopes: Map<string, string | null>
  activePlan: Plan | null
  planHistory: Plan[]
  previews: Map<string, Plan>
  /** 计划输入修订号（任务/可用时间/反馈变化时递增），编码为 ir_<n> */
  inputRevision: number
  planVersion: number
  idempotency: Map<string, unknown>
  /** 已导入文件指纹（文件名:大小 → 资源 ID），用于重复导入去重演示 */
  fileHashes: Map<string, string>
  seq: number
  // ---------- 复习卷（本模块新增） ----------
  providers: ProviderConfig[]
  /** AI 服务商密钥：仅存于模拟进程内存，不进入任何普通响应/日志/导出（模拟本地密钥存储） */
  providerKeys: Map<string, string>
  /** 题库来源能力（模拟 A 侧 get_questionbank_capabilities 上报；configured=false 的来源不假装已连接） */
  questionbankSources: QuestionbankCapabilitySource[]
  papers: ReviewPaper[]
  /** 出卷请求留档（jobId → 请求体原文；测试与界面断言 notes 实际随请求发送） */
  reviewJobRequests: Map<string, ReviewRequest>
  /** 修订请求留档（jobId → 修订请求与来源试卷；断言"未提供=沿用上一版"） */
  reviewRevisionRequests: Map<string, { paper_id: string; request: unknown }>
  /** 每版试卷的生效输入留档（`${paper_id}#${revision}` → 合并后的完整 ReviewRequest），修订时据此沿用上一版 */
  reviewPaperInputs: Map<string, ReviewRequest>
}

export function revisionStr(db: MockDb): string {
  return `ir_${db.inputRevision}`
}

const xzzdRef: SourceRef = {
  platform: 'xzzd',
  external_id: 'XZZD-CS2010-2026F',
  canonical_url: 'https://example.com/xzzd/courses/CS2010',
  observed_at: ago(26),
}

const authExpired: ErrorInfo = {
  code: 'AUTH_EXPIRED',
  message: '登录已失效，请重新登录后继续同步。',
  retryable: false,
  details: { platform: 'zykt' },
}

export function createDb(): MockDb {
  const courses: Course[] = [
    {
      id: 'course_ds',
      title: '数据结构与算法',
      term: '2026-2027 学年秋冬学期',
      teacher: '陈老师',
      source_refs: [xzzdRef],
      archived_at: null,
      created_at: ago(500),
      updated_at: ago(26),
    },
    {
      id: 'course_net',
      title: '计算机网络',
      term: '2026-2027 学年秋冬学期',
      teacher: '吴老师',
      source_refs: [
        {
          platform: 'xzzd',
          external_id: 'XZZD-CS3050-2026F',
          canonical_url: 'https://example.com/xzzd/courses/CS3050',
          observed_at: ago(26),
        },
        {
          platform: 'zykt',
          external_id: 'ZY-2026-CS3050',
          canonical_url: 'https://example.com/zykt/room/CS3050',
          observed_at: ago(70),
        },
      ],
      archived_at: null,
      created_at: ago(500),
      updated_at: ago(26),
    },
    {
      id: 'course_ml',
      title: '机器学习导论',
      term: null,
      teacher: null,
      source_refs: [],
      archived_at: null,
      created_at: ago(48),
      updated_at: ago(48),
    },
  ]

  const resources: Resource[] = [
    {
      id: 'res_ds_ch3',
      course_id: 'course_ds',
      kind: 'handout',
      title: '第3章 哈希表 讲义.pdf',
      source_refs: [
        { ...xzzdRef, external_id: 'RES-DS-CH3', canonical_url: 'https://example.com/xzzd/files/ds-ch3' },
      ],
      version: 2,
      content_hash: 'sha256:9f2c01aa0000000000000000000000000000000000000000000000000000aa01',
      parse_status: 'ready',
      mime_type: 'application/pdf',
      size_bytes: 1048576,
      access_level: 'full',
      file_id: 'file_ds_ch3',
      extent_value: 25,
      extent_unit: 'page',
      chunk_count: 3,
      fetched_at: ago(120),
      source_updated_at: ago(120),
      archived_at: null,
      created_at: ago(120),
      updated_at: ago(120),
    },
    {
      id: 'res_ds_lab',
      course_id: 'course_ds',
      kind: 'handout',
      title: '实验指导书.pdf',
      source_refs: [
        { ...xzzdRef, external_id: 'RES-DS-LAB', canonical_url: 'https://example.com/xzzd/files/ds-lab' },
      ],
      version: 1,
      content_hash: 'sha256:77be10cc0000000000000000000000000000000000000000000000000000cc02',
      parse_status: 'ready',
      mime_type: 'application/pdf',
      size_bytes: 2097152,
      access_level: 'full',
      file_id: 'file_ds_lab',
      extent_value: 12,
      extent_unit: 'page',
      chunk_count: 8,
      fetched_at: ago(72),
      source_updated_at: ago(72),
      archived_at: null,
      created_at: ago(72),
      updated_at: ago(72),
    },
    {
      id: 'res_ds_ch3_v1',
      course_id: 'course_ds',
      kind: 'handout',
      title: '第3章 哈希表 讲义.pdf',
      source_refs: [
        {
          platform: 'zykt',
          external_id: 'ZY-RES-DS-CH3',
          canonical_url: 'https://example.com/zykt/room/CS2010/files/ch3',
          observed_at: ago(200),
        },
      ],
      version: 1,
      content_hash: 'sha256:1a2b3c4d0000000000000000000000000000000000000000000000000000b101',
      parse_status: 'ready',
      mime_type: 'application/pdf',
      size_bytes: 1000000,
      access_level: 'full',
      file_id: 'file_ds_ch3_v1',
      extent_value: 25,
      extent_unit: 'page',
      chunk_count: 3,
      fetched_at: ago(200),
      source_updated_at: ago(200),
      archived_at: null,
      created_at: ago(200),
      updated_at: ago(200),
    },
    {
      id: 'res_ds_lab_copy',
      course_id: 'course_ds',
      kind: 'handout',
      title: '实验指导书副本.pdf',
      source_refs: [],
      version: 1,
      content_hash: 'sha256:77be10cc0000000000000000000000000000000000000000000000000000cc02',
      parse_status: 'ready',
      mime_type: 'application/pdf',
      size_bytes: 2097152,
      access_level: 'full',
      file_id: 'file_ds_lab_copy',
      extent_value: null,
      extent_unit: null,
      chunk_count: 2,
      fetched_at: ago(70),
      source_updated_at: null,
      archived_at: null,
      created_at: ago(70),
      updated_at: ago(70),
    },
    {
      id: 'res_ds_outline',
      course_id: 'course_ds',
      kind: 'slides',
      title: '课程大纲.pptx',
      source_refs: [
        { ...xzzdRef, external_id: 'RES-DS-OUTLINE', canonical_url: 'https://example.com/xzzd/files/ds-outline' },
      ],
      version: 1,
      content_hash: 'sha256:45cd99ef0000000000000000000000000000000000000000000000000000ef03',
      parse_status: 'failed',
      mime_type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      size_bytes: 3145728,
      access_level: 'preview_only',
      file_id: 'file_ds_outline',
      extent_value: 40,
      extent_unit: 'slide',
      chunk_count: null,
      fetched_at: ago(200),
      source_updated_at: ago(200),
      archived_at: null,
      created_at: ago(200),
      updated_at: ago(200),
    },
    {
      id: 'res_ds_scan',
      course_id: 'course_ds',
      kind: 'scan',
      title: '手写笔记扫描件.pdf',
      source_refs: [],
      version: 1,
      content_hash: 'sha256:31fa77dd0000000000000000000000000000000000000000000000000000dd04',
      parse_status: 'pending',
      mime_type: 'application/pdf',
      size_bytes: 4194304,
      access_level: 'restricted',
      file_id: 'file_ds_scan',
      extent_value: 30,
      extent_unit: 'page',
      chunk_count: null,
      fetched_at: ago(48),
      source_updated_at: null,
      archived_at: null,
      created_at: ago(48),
      updated_at: ago(48),
    },
    {
      // PD-1 样例：42.9MB 超大文件（超 30MB 护栏、64MB 上限内），界面须明示解析较慢
      id: 'res_ds_ch6_scan',
      course_id: 'course_ds',
      kind: 'scan',
      title: '第6章 图算法 讲义（扫描版）.pdf',
      source_refs: [],
      version: 1,
      content_hash: 'sha256:6d9e10ab0000000000000000000000000000000000000000000000000000f105',
      parse_status: 'processing',
      mime_type: 'application/pdf',
      size_bytes: 44983910,
      access_level: 'full',
      file_id: 'file_ds_ch6_scan',
      extent_value: 86,
      extent_unit: 'page',
      chunk_count: null,
      fetched_at: ago(6),
      source_updated_at: null,
      archived_at: null,
      created_at: ago(6),
      updated_at: ago(6),
    },
    {
      id: 'res_ds_link',
      course_id: 'course_ds',
      kind: 'link',
      title: '课程主页（外部链接）',
      source_refs: [],
      version: 1,
      content_hash: null,
      parse_status: 'skipped',
      mime_type: null,
      size_bytes: null,
      access_level: 'full',
      file_id: null,
      extent_value: null,
      extent_unit: null,
      chunk_count: null,
      fetched_at: ago(100),
      source_updated_at: null,
      archived_at: null,
      created_at: ago(100),
      updated_at: ago(100),
    },
    {
      id: 'res_net_l4',
      course_id: 'course_net',
      kind: 'handout',
      title: '第4讲 TCP 拥塞控制 课件.pdf',
      source_refs: [
        {
          platform: 'xzzd',
          external_id: 'RES-NET-L4',
          canonical_url: 'https://example.com/xzzd/files/net-l4',
          observed_at: ago(26),
        },
      ],
      version: 1,
      content_hash: 'sha256:88b0cc1100000000000000000000000000000000000000000000000000001105',
      parse_status: 'ready',
      mime_type: 'application/pdf',
      size_bytes: 1572864,
      access_level: 'full',
      file_id: 'file_net_l4',
      extent_value: 20,
      extent_unit: 'page',
      chunk_count: 2,
      fetched_at: ago(50),
      source_updated_at: ago(50),
      archived_at: null,
      created_at: ago(50),
      updated_at: ago(50),
    },
    {
      id: 'res_net_l4_tx',
      course_id: 'course_net',
      kind: 'document',
      title: '第4讲 字幕文本（来自智云课堂）',
      source_refs: [
        {
          platform: 'zykt',
          external_id: 'ZY-TX-L4',
          canonical_url: 'https://example.com/zykt/room/CS3050/transcript',
          observed_at: ago(70),
        },
      ],
      version: 1,
      content_hash: 'sha256:c0ffee2200000000000000000000000000000000000000000000000000002206',
      parse_status: 'ready',
      mime_type: 'text/plain',
      size_bytes: 51200,
      access_level: 'full',
      file_id: 'file_net_l4_tx',
      extent_value: null,
      extent_unit: null,
      chunk_count: 2,
      fetched_at: ago(70),
      source_updated_at: ago(70),
      archived_at: null,
      created_at: ago(70),
      updated_at: ago(70),
    },
    {
      id: 'res_net_scope',
      course_id: 'course_net',
      kind: 'document',
      title: '考试范围.docx',
      source_refs: [
        { ...xzzdRef, external_id: 'RES-NET-SCOPE', canonical_url: 'https://example.com/xzzd/files/net-scope' },
      ],
      version: 1,
      content_hash: 'sha256:5e77aa3300000000000000000000000000000000000000000000000000003307',
      parse_status: 'pending',
      mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      size_bytes: 204800,
      access_level: 'full',
      file_id: 'file_net_scope',
      extent_value: 6,
      extent_unit: 'page',
      chunk_count: null,
      fetched_at: ago(2),
      source_updated_at: ago(2),
      archived_at: null,
      created_at: ago(2),
      updated_at: ago(2),
    },
    {
      id: 'res_ml_intro',
      course_id: 'course_ml',
      kind: 'slides',
      title: '第1讲 课程介绍.pptx',
      source_refs: [],
      version: 1,
      content_hash: 'sha256:aa01bb0200000000000000000000000000000000000000000000000000008808',
      parse_status: 'ready',
      mime_type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      size_bytes: 2621440,
      access_level: 'full',
      file_id: 'file_ml_intro',
      extent_value: 22,
      extent_unit: 'slide',
      chunk_count: 2,
      fetched_at: ago(46),
      source_updated_at: null,
      archived_at: null,
      created_at: ago(46),
      updated_at: ago(46),
    },
  ]

  const resourceVersions: ResourceVersion[] = [
    {
      resource_id: 'res_ds_ch3',
      version: 2,
      content_hash: 'sha256:9f2c01aa0000000000000000000000000000000000000000000000000000aa01',
      mime_type: 'application/pdf',
      size_bytes: 1048576,
      file_id: 'file_ds_ch3',
      extent_value: 25,
      extent_unit: 'page',
      fetched_at: ago(120),
      parse_status: 'ready',
      source_updated_at: ago(120),
      created_at: ago(120),
    },
    {
      resource_id: 'res_ds_ch3',
      version: 1,
      content_hash: 'sha256:11aa02bb0000000000000000000000000000000000000000000000000000bb09',
      mime_type: 'application/pdf',
      size_bytes: 986112,
      file_id: 'file_ds_ch3',
      extent_value: 22,
      extent_unit: 'page',
      fetched_at: ago(300),
      parse_status: 'ready',
      source_updated_at: ago(300),
      created_at: ago(300),
    },
    {
      resource_id: 'res_ds_lab',
      version: 1,
      content_hash: 'sha256:77be10cc0000000000000000000000000000000000000000000000000000cc02',
      mime_type: 'application/pdf',
      size_bytes: 2097152,
      file_id: 'file_ds_lab',
      extent_value: 12,
      extent_unit: 'page',
      fetched_at: ago(72),
      parse_status: 'ready',
      source_updated_at: ago(72),
      created_at: ago(72),
    },
    {
      resource_id: 'res_ds_outline',
      version: 1,
      content_hash: 'sha256:45cd99ef0000000000000000000000000000000000000000000000000000ef03',
      mime_type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      size_bytes: 3145728,
      file_id: 'file_ds_outline',
      extent_value: 40,
      extent_unit: 'slide',
      fetched_at: ago(200),
      parse_status: 'failed',
      source_updated_at: ago(200),
      created_at: ago(200),
    },
    {
      resource_id: 'res_ds_scan',
      version: 1,
      content_hash: 'sha256:31fa77dd0000000000000000000000000000000000000000000000000000dd04',
      mime_type: 'application/pdf',
      size_bytes: 4194304,
      file_id: 'file_ds_scan',
      extent_value: 30,
      extent_unit: 'page',
      fetched_at: ago(48),
      parse_status: 'pending',
      source_updated_at: null,
      created_at: ago(48),
    },
    {
      resource_id: 'res_ds_link',
      version: 1,
      content_hash: null,
      mime_type: null,
      size_bytes: null,
      file_id: null,
      extent_value: null,
      extent_unit: null,
      fetched_at: ago(100),
      parse_status: 'skipped',
      source_updated_at: null,
      created_at: ago(100),
    },
    {
      resource_id: 'res_net_l4',
      version: 1,
      content_hash: 'sha256:88b0cc1100000000000000000000000000000000000000000000000000001105',
      mime_type: 'application/pdf',
      size_bytes: 1572864,
      file_id: 'file_net_l4',
      extent_value: 20,
      extent_unit: 'page',
      fetched_at: ago(50),
      parse_status: 'ready',
      source_updated_at: ago(50),
      created_at: ago(50),
    },
    {
      resource_id: 'res_net_l4_tx',
      version: 1,
      content_hash: 'sha256:c0ffee2200000000000000000000000000000000000000000000000000002206',
      mime_type: 'text/plain',
      size_bytes: 51200,
      file_id: 'file_net_l4_tx',
      extent_value: null,
      extent_unit: null,
      fetched_at: ago(70),
      parse_status: 'ready',
      source_updated_at: ago(70),
      created_at: ago(70),
    },
    {
      resource_id: 'res_net_scope',
      version: 1,
      content_hash: 'sha256:5e77aa3300000000000000000000000000000000000000000000000000003307',
      mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      size_bytes: 204800,
      file_id: 'file_net_scope',
      extent_value: 6,
      extent_unit: 'page',
      fetched_at: ago(2),
      parse_status: 'pending',
      source_updated_at: ago(2),
      created_at: ago(2),
    },
    {
      resource_id: 'res_ml_intro',
      version: 1,
      content_hash: 'sha256:aa01bb0200000000000000000000000000000000000000000000000000008808',
      mime_type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      size_bytes: 2621440,
      file_id: 'file_ml_intro',
      extent_value: 22,
      extent_unit: 'slide',
      fetched_at: ago(46),
      parse_status: 'ready',
      source_updated_at: null,
      created_at: ago(46),
    },
  ]

  const platformProv = (jobId: string) => ({
    extractor: 'mock-extractor',
    extractor_version: '0.1.0',
    job_id: jobId,
    fact_source: 'platform' as const,
    observed_at: ago(50),
  })

  const chunks: ContentChunk[] = [
    {
      id: 'chk_ds_ch3_0',
      resource_id: 'res_ds_ch3',
      resource_version: 2,
      ordinal: 0,
      text: '第 3 章 哈希表：基本概念、冲突处理与复杂度分析',
      locator: { kind: 'page', page: 1 },
      provenance: platformProv('job_parse_0'),
      created_at: ago(120),
    },
    {
      id: 'chk_ds_ch3_1',
      resource_id: 'res_ds_ch3',
      resource_version: 2,
      ordinal: 1,
      text: '开放寻址法：发生冲突时按探测序列（线性探测、二次探测、双重散列）寻找下一个空槽位，删除需要墓碑标记。',
      locator: { kind: 'page', page: 12 },
      provenance: platformProv('job_parse_0'),
      created_at: ago(120),
    },
    {
      id: 'chk_ds_ch3_2',
      resource_id: 'res_ds_ch3',
      resource_version: 2,
      ordinal: 2,
      text: '装载因子 α = n / m 衡量哈希表拥挤程度；α 接近 1 时平均探测次数急剧上升，通常需要扩容并再散列。',
      locator: { kind: 'page', page: 25 },
      provenance: platformProv('job_parse_0'),
      created_at: ago(120),
    },
    {
      id: 'chk_ds_ch3_v1_0',
      resource_id: 'res_ds_ch3',
      resource_version: 1,
      ordinal: 0,
      text: '第 3 章 哈希表（初稿）：从直接寻址表引出哈希表与冲突处理。',
      locator: { kind: 'page', page: 1 },
      provenance: platformProv('job_parse_legacy'),
      created_at: ago(300),
    },
    {
      id: 'chk_ds_ch3_v1_1',
      resource_id: 'res_ds_ch3',
      resource_version: 1,
      ordinal: 1,
      text: '冲突处理方法概述（初稿）：链地址法与开放寻址法的初步对比。',
      locator: { kind: 'page', page: 10 },
      provenance: platformProv('job_parse_legacy'),
      created_at: ago(300),
    },
    {
      id: 'chk_ds_ch3_v1res_0',
      resource_id: 'res_ds_ch3_v1',
      resource_version: 1,
      ordinal: 0,
      text: '第 3 章 哈希表：基本概念与直接寻址表（智云课堂同步版）',
      locator: { kind: 'page', page: 1 },
      provenance: platformProv('job_sync_legacy'),
      created_at: ago(200),
    },
    {
      id: 'chk_ds_ch3_v1res_1',
      resource_id: 'res_ds_ch3_v1',
      resource_version: 1,
      ordinal: 1,
      text: '链地址法与开放寻址法的对比：冲突处理策略与装载因子的影响。',
      locator: { kind: 'page', page: 12 },
      provenance: platformProv('job_sync_legacy'),
      created_at: ago(200),
    },
    {
      id: 'chk_ds_ch3_v1res_2',
      resource_id: 'res_ds_ch3_v1',
      resource_version: 1,
      ordinal: 2,
      text: '再散列与扩容策略：装载因子超过阈值时新建更大表并重新插入全部元素。',
      locator: { kind: 'page', page: 25 },
      provenance: platformProv('job_sync_legacy'),
      created_at: ago(200),
    },
    {
      id: 'chk_ds_lab_copy_0',
      resource_id: 'res_ds_lab_copy',
      resource_version: 1,
      ordinal: 0,
      text: '实验目的：实现并对比开放寻址法与链地址法两种哈希表（重复下载副本，内容与实验指导书一致）。',
      locator: { kind: 'page', page: 1 },
      provenance: platformProv('job_import_1'),
      created_at: ago(70),
    },
    {
      id: 'chk_ds_lab_copy_1',
      resource_id: 'res_ds_lab_copy',
      resource_version: 1,
      ordinal: 1,
      text: '提交要求：源代码 + 实验报告；报告需包含复杂度分析与探测次数统计表。',
      locator: { kind: 'page', page: 8 },
      provenance: platformProv('job_import_1'),
      created_at: ago(70),
    },
    {
      id: 'chk_ds_lab_0',
      resource_id: 'res_ds_lab',
      resource_version: 1,
      ordinal: 0,
      text: '实验目的：实现并对比开放寻址法与链地址法两种哈希表，验证复杂度分析结论。',
      locator: { kind: 'page', page: 1 },
      provenance: platformProv('job_import_1'),
      created_at: ago(72),
    },
    {
      id: 'chk_ds_lab_1',
      resource_id: 'res_ds_lab',
      resource_version: 1,
      ordinal: 1,
      text: '实验环境：C++17 或 Python 3.11；禁止直接调用标准库哈希容器（unordered_map / dict 的哈希部分需自行实现）。',
      locator: { kind: 'page', page: 2 },
      provenance: platformProv('job_import_1'),
      created_at: ago(72),
    },
    {
      id: 'chk_ds_lab_2',
      resource_id: 'res_ds_lab',
      resource_version: 1,
      ordinal: 2,
      text: '任务一：实现哈希函数 h(k) = k mod m，m 取不小于 2n 的最小质数，并说明选取理由。',
      locator: { kind: 'page', page: 3 },
      provenance: platformProv('job_import_1'),
      created_at: ago(72),
    },
    {
      id: 'chk_ds_lab_3',
      resource_id: 'res_ds_lab',
      resource_version: 1,
      ordinal: 3,
      text: '任务二：实现线性探测插入/查找/删除；删除必须使用墓碑（tombstone）标记，说明若直接置空会导致什么问题。',
      locator: { kind: 'page', page: 5 },
      provenance: platformProv('job_import_1'),
      created_at: ago(72),
    },
    {
      id: 'chk_ds_lab_4',
      resource_id: 'res_ds_lab',
      resource_version: 1,
      ordinal: 4,
      text: '任务三：分别统计装载因子 0.25 / 0.5 / 0.75 / 0.9 下的平均成功查找与插入探测次数。',
      locator: { kind: 'page', page: 7 },
      provenance: platformProv('job_import_1'),
      created_at: ago(72),
    },
    {
      id: 'chk_ds_lab_5',
      resource_id: 'res_ds_lab',
      resource_version: 1,
      ordinal: 5,
      text: '性能分析：对比相同装载因子下链地址法与线性探测法的时间/空间开销，并解释「墓碑过多导致探测膨胀」现象。',
      locator: { kind: 'page', page: 9 },
      provenance: platformProv('job_import_1'),
      created_at: ago(72),
    },
    {
      id: 'chk_ds_lab_6',
      resource_id: 'res_ds_lab',
      resource_version: 1,
      ordinal: 6,
      text: '提交要求：源代码与实验报告一并打包；报告需附测试数据表格与探测次数随装载因子变化的图表。',
      locator: { kind: 'page', page: 11 },
      provenance: platformProv('job_import_1'),
      created_at: ago(72),
    },
    {
      id: 'chk_ds_lab_7',
      resource_id: 'res_ds_lab',
      resource_version: 1,
      ordinal: 7,
      text: '评分标准：正确性 60%，性能分析 30%，代码风格与注释 10%。',
      locator: { kind: 'page', page: 12 },
      provenance: platformProv('job_import_1'),
      created_at: ago(72),
    },
    {
      id: 'chk_net_l4_0',
      resource_id: 'res_net_l4',
      resource_version: 1,
      ordinal: 0,
      text: '拥塞控制四大算法：慢启动、拥塞避免、快速重传、快速恢复。',
      locator: { kind: 'page', page: 5 },
      provenance: platformProv('job_sync_1'),
      created_at: ago(50),
    },
    {
      id: 'chk_net_l4_1',
      resource_id: 'res_net_l4',
      resource_version: 1,
      ordinal: 1,
      text: 'TCP Reno 与 TCP Tahoe 的差异：Reno 在快速恢复阶段不回到慢启动，而将 ssthresh 与 cwnd 减半。',
      locator: { kind: 'page', page: 18 },
      provenance: platformProv('job_sync_1'),
      created_at: ago(50),
    },
    {
      id: 'chk_net_l4_tx_0',
      resource_id: 'res_net_l4_tx',
      resource_version: 1,
      ordinal: 0,
      text: '拥塞窗口 cwnd 随确认到达指数增长（慢启动），达到阈值后转为线性增长（拥塞避免）。',
      locator: { kind: 'timestamp', start_seconds: 750, end_seconds: 940 },
      provenance: platformProv('job_sync_1'),
      created_at: ago(70),
    },
    {
      id: 'chk_net_l4_tx_1',
      resource_id: 'res_net_l4_tx',
      resource_version: 1,
      ordinal: 1,
      text: '发生超时或三次重复 ACK 时，乘性减小 cwnd，并调整慢启动阈值。',
      locator: { kind: 'timestamp', start_seconds: 1880, end_seconds: 2130 },
      provenance: platformProv('job_sync_1'),
      created_at: ago(70),
    },
    {
      id: 'chk_ml_intro_0',
      resource_id: 'res_ml_intro',
      resource_version: 1,
      ordinal: 0,
      text: '课程目标：理解监督学习的基本框架，掌握线性模型与损失函数最小化的思想。',
      locator: { kind: 'slide', slide: 1 },
      provenance: {
        extractor: 'mock-extractor',
        extractor_version: '0.1.0',
        job_id: 'job_import_2',
        fact_source: 'user',
        observed_at: ago(46),
      },
      created_at: ago(46),
    },
    {
      id: 'chk_ml_intro_1',
      resource_id: 'res_ml_intro',
      resource_version: 1,
      ordinal: 1,
      text: '考核方式：平时作业 40%，期末项目 60%；项目需提交代码与报告。',
      locator: { kind: 'slide', slide: 12 },
      provenance: {
        extractor: 'mock-extractor',
        extractor_version: '0.1.0',
        job_id: 'job_import_2',
        fact_source: 'user',
        observed_at: ago(46),
      },
      created_at: ago(46),
    },
  ]

  const assignments: Assignment[] = [
    {
      id: 'asg_ds_hw2',
      course_id: 'course_ds',
      title: '作业2：哈希表实现',
      instructions: '实现开放寻址法与链地址法两种哈希表，提交源代码与实验报告。',
      due_at: at(-1, '23:59'),
      due_precision: 'datetime',
      submission_status: 'not_submitted',
      fact_source: 'platform',
      confidence: 1,
      source_ref: { ...xzzdRef, external_id: 'ASG-DS-HW2', canonical_url: 'https://example.com/xzzd/assignments/hw2' },
      attachment_resource_ids: ['res_ds_lab'],
      revision: 1,
      observed_at: ago(26),
      archived_at: null,
      created_at: ago(300),
      updated_at: ago(26),
    },
    {
      id: 'asg_ds_hw3',
      course_id: 'course_ds',
      title: '作业3：图算法编程',
      instructions: '完成最短路与最小生成树各一题，附带运行截图。',
      due_at: at(6, '23:59'),
      due_precision: 'datetime',
      submission_status: 'not_submitted',
      fact_source: 'platform',
      confidence: 1,
      source_ref: { ...xzzdRef, external_id: 'ASG-DS-HW3', canonical_url: 'https://example.com/xzzd/assignments/hw3' },
      attachment_resource_ids: [],
      revision: 1,
      observed_at: ago(26),
      archived_at: null,
      created_at: ago(200),
      updated_at: ago(26),
    },
    {
      id: 'asg_ds_lab',
      course_id: 'course_ds',
      title: '开放性实验报告',
      instructions: '自选一个数据结构主题完成小型实验并撰写报告；平台未公布截止时间。',
      due_at: null,
      due_precision: 'unknown',
      submission_status: 'not_submitted',
      fact_source: 'platform',
      confidence: null,
      source_ref: { ...xzzdRef, external_id: 'ASG-DS-LAB', canonical_url: 'https://example.com/xzzd/assignments/lab' },
      attachment_resource_ids: [],
      revision: 1,
      observed_at: ago(26),
      archived_at: null,
      created_at: ago(300),
      updated_at: ago(26),
    },
    {
      id: 'asg_net_hw3',
      course_id: 'course_net',
      title: '作业3：抓包分析实验',
      instructions: '使用抓包工具分析一次 TCP 连接的建立与释放过程并提交分析文档。',
      due_at: at(4, '23:59'),
      due_precision: 'datetime',
      submission_status: 'not_submitted',
      fact_source: 'platform',
      confidence: 1,
      source_ref: {
        platform: 'xzzd',
        external_id: 'ASG-NET-HW3',
        canonical_url: 'https://example.com/xzzd/assignments/net-hw3',
        observed_at: ago(26),
      },
      attachment_resource_ids: ['res_net_l4'],
      revision: 1,
      observed_at: ago(26),
      archived_at: null,
      created_at: ago(200),
      updated_at: ago(26),
    },
    {
      id: 'asg_net_quiz',
      course_id: 'course_net',
      title: '概念测验 1',
      instructions: '前四讲概念选择题，已在平台提交。',
      due_at: at(-3, '23:59'),
      due_precision: 'datetime',
      submission_status: 'submitted',
      fact_source: 'platform',
      confidence: 1,
      source_ref: {
        platform: 'xzzd',
        external_id: 'ASG-NET-QUIZ1',
        canonical_url: 'https://example.com/xzzd/assignments/net-quiz1',
        observed_at: ago(26),
      },
      attachment_resource_ids: [],
      revision: 1,
      observed_at: ago(26),
      archived_at: null,
      created_at: ago(300),
      updated_at: ago(26),
    },
    {
      id: 'asg_ml_project',
      course_id: 'course_ml',
      title: '期末项目：线性回归实现',
      instructions: '手动录入：实现批量梯度下降训练线性回归，提交代码与报告（平台未连接）。',
      due_at: dayKeyOffset(10),
      due_precision: 'date',
      submission_status: 'unknown',
      fact_source: 'user',
      confidence: null,
      source_ref: null,
      attachment_resource_ids: [],
      revision: 1,
      observed_at: ago(46),
      archived_at: null,
      created_at: ago(46),
      updated_at: ago(46),
    },
  ]

  const recordings: Recording[] = [
    {
      id: 'rec_net_l4',
      course_id: 'course_net',
      title: '第4讲 TCP 拥塞控制',
      started_at: at(-2, '14:00'),
      duration_seconds: 5700,
      canonical_url: 'https://example.com/zykt/room/CS3050/playback/4',
      transcript_status: 'ready',
      subtitle_languages: ['zh-CN'],
      transcript_next_check_at: null,
      fact_source: 'platform',
      source_ref: {
        platform: 'zykt',
        external_id: 'ZY-REC-L4',
        canonical_url: 'https://example.com/zykt/room/CS3050/playback/4',
        observed_at: ago(70),
      },
      observed_at: ago(70),
      created_at: ago(70),
      updated_at: ago(70),
    },
    {
      id: 'rec_net_l5',
      course_id: 'course_net',
      title: '第5讲 路由算法',
      started_at: at(-1, '14:00'),
      duration_seconds: 5400,
      canonical_url: 'https://example.com/zykt/room/CS3050/playback/5',
      transcript_status: 'unavailable',
      subtitle_languages: [],
      transcript_next_check_at: null,
      fact_source: 'platform',
      source_ref: {
        platform: 'zykt',
        external_id: 'ZY-REC-L5',
        canonical_url: 'https://example.com/zykt/room/CS3050/playback/5',
        observed_at: ago(22),
      },
      observed_at: ago(22),
      created_at: ago(22),
      updated_at: ago(22),
    },
    {
      id: 'rec_ds_l6',
      course_id: 'course_ds',
      title: '第6讲 图与图遍历',
      started_at: at(-1, '10:00'),
      duration_seconds: 5400,
      canonical_url: 'https://example.com/zykt/room/CS2010/playback/6',
      transcript_status: 'processing',
      subtitle_languages: null,
      transcript_next_check_at: at(0, '22:00'),
      fact_source: 'platform',
      source_ref: {
        platform: 'zykt',
        external_id: 'ZY-REC-DS-L6',
        canonical_url: 'https://example.com/zykt/room/CS2010/playback/6',
        observed_at: ago(22),
      },
      observed_at: ago(22),
      created_at: ago(22),
      updated_at: ago(22),
    },
  ]

  const tasks: LearningTask[] = [
    {
      id: 'task_ds_read3',
      match_key: 't:course_ds:reading:第3章-哈希表-讲义.pdf',
      course_id: 'course_ds',
      type: 'reading',
      title: '阅读《第3章 哈希表 讲义》第 1–25 页',
      estimated_minutes: 90,
      due_at: at(4, '23:59'),
      due_precision: 'datetime',
      prerequisite_ids: [],
      source_refs: [xzzdRef],
      status: 'pending',
      fact_source: 'inferred',
      revision: 1,
      archived_at: null,
      created_at: ago(100),
      updated_at: ago(100),
    },
    {
      id: 'task_net_rec4',
      match_key: 't:course_net:watching:ZY-REC-L4',
      course_id: 'course_net',
      type: 'watching',
      title: '观看回放：第4讲 TCP 拥塞控制并整理笔记',
      estimated_minutes: 45,
      due_at: at(5, '23:59'),
      due_precision: 'datetime',
      prerequisite_ids: [],
      source_refs: [
        {
          platform: 'zykt',
          external_id: 'ZY-REC-L4',
          canonical_url: 'https://example.com/zykt/room/CS3050/playback/4',
          observed_at: ago(70),
        },
      ],
      status: 'pending',
      fact_source: 'inferred',
      revision: 1,
      archived_at: null,
      created_at: ago(70),
      updated_at: ago(70),
    },
    {
      id: 'task_ds_ex3',
      match_key: null,
      course_id: 'course_ds',
      type: 'practice',
      title: '完成第 3 章练习题集',
      estimated_minutes: 60,
      due_at: at(3, '23:59'),
      due_precision: 'datetime',
      prerequisite_ids: ['task_ds_read3'],
      source_refs: [xzzdRef],
      status: 'pending',
      fact_source: 'inferred',
      revision: 1,
      archived_at: null,
      created_at: ago(100),
      updated_at: ago(100),
    },
    {
      id: 'task_ds_fix2',
      match_key: null,
      course_id: 'course_ds',
      type: 'assignment',
      title: '作业2 订正与补交准备',
      estimated_minutes: 60,
      due_at: at(-1, '23:59'),
      due_precision: 'datetime',
      prerequisite_ids: [],
      source_refs: [xzzdRef],
      status: 'pending',
      fact_source: 'user',
      revision: 1,
      archived_at: null,
      created_at: ago(80),
      updated_at: ago(80),
    },
    {
      id: 'task_net_hw3',
      match_key: 'xzzd:ASG-NET-HW3',
      course_id: 'course_net',
      type: 'assignment',
      title: '完成作业3：抓包分析实验',
      estimated_minutes: 120,
      due_at: at(4, '23:59'),
      due_precision: 'datetime',
      prerequisite_ids: [],
      source_refs: [
        {
          platform: 'xzzd',
          external_id: 'ASG-NET-HW3',
          canonical_url: 'https://example.com/xzzd/assignments/net-hw3',
          observed_at: ago(26),
        },
      ],
      status: 'pending',
      fact_source: 'inferred',
      revision: 1,
      archived_at: null,
      created_at: ago(200),
      updated_at: ago(26),
    },
    {
      id: 'task_ds_mock',
      match_key: null,
      course_id: 'course_ds',
      type: 'review',
      title: '期中模拟自测（连续 200 分钟）',
      estimated_minutes: 200,
      due_at: at(3, '23:59'),
      due_precision: 'datetime',
      prerequisite_ids: [],
      source_refs: [xzzdRef],
      status: 'pending',
      fact_source: 'user',
      revision: 1,
      archived_at: null,
      created_at: ago(90),
      updated_at: ago(90),
    },
    {
      id: 'task_ml_read1',
      match_key: null,
      course_id: 'course_ml',
      type: 'reading',
      title: '阅读第1讲课件并复述课程目标',
      estimated_minutes: 40,
      due_at: dayKeyOffset(5),
      due_precision: 'date',
      prerequisite_ids: [],
      source_refs: [],
      status: 'pending',
      fact_source: 'user',
      revision: 1,
      archived_at: null,
      created_at: ago(46),
      updated_at: ago(46),
    },
  ]

  const feedback: FeedbackRecord[] = [
    {
      id: 'fb_seed_1',
      task_id: 'task_net_rec4',
      outcome: 'skipped',
      actual_minutes: null,
      difficulty: 3,
      note: '状态导入：示例跳过记录',
      created_at: ago(20),
    },
  ]

  const availability: Availability = {
    timezone: DEFAULT_TIMEZONE,
    weekly_windows: [
      { weekday: 1, start: '19:00', end: '22:00' },
      { weekday: 2, start: '19:00', end: '22:00' },
      { weekday: 3, start: '19:00', end: '22:00' },
      { weekday: 4, start: '19:00', end: '22:00' },
      { weekday: 5, start: '19:00', end: '22:00' },
      { weekday: 6, start: '09:00', end: '12:00' },
      { weekday: 6, start: '14:00', end: '17:00' },
      { weekday: 7, start: '09:00', end: '12:00' },
      { weekday: 7, start: '14:00', end: '17:00' },
    ],
    date_exceptions: [
      { date: dayKeyOffset(2), windows: [], note: '校运动会（全天不可用）' },
    ],
    daily_limit_minutes: 180,
    updated_at: ago(96),
  }

  const activePlan: Plan = {
    id: 'plan_active_4',
    version: 4,
    status: 'active',
    input_revision: 'ir_4',
    horizon_start: dayKeyOffset(0),
    horizon_end: dayKeyOffset(7),
    blocks: [
      {
        id: 'blk_4_1',
        task_id: 'task_ds_read3',
        starts_at: at(0, '19:00'),
        ends_at: at(0, '20:30'),
        locked: false,
      },
      {
        id: 'blk_4_2',
        task_id: 'task_net_rec4',
        starts_at: at(0, '20:30'),
        ends_at: at(0, '21:15'),
        locked: false,
      },
      {
        id: 'blk_4_3',
        task_id: 'task_ds_ex3',
        starts_at: at(1, '19:00'),
        ends_at: at(1, '20:00'),
        locked: true,
      },
      {
        id: 'blk_4_4',
        task_id: 'task_net_hw3',
        starts_at: at(3, '19:00'),
        ends_at: at(3, '21:00'),
        locked: false,
      },
    ],
    unassigned: [
      {
        task_id: 'task_ds_fix2',
        reason_code: 'DEADLINE_PASSED',
        shortage_minutes: 0,
        message: '截止时间已过，无法安排在期限内',
        suggestion: '订正已结束的作业目标是补救，请手动确认新的时间窗口或直接完成',
      },
      {
        task_id: 'task_ds_mock',
        reason_code: 'TASK_TOO_LONG_INDIVISIBLE',
        shortage_minutes: 20,
        message: '不拆分的 200 分钟任务找不到连续空闲窗口（最长窗口 180 分钟）',
        suggestion: '可将任务标记为可拆分，或单独预留一段超过 200 分钟的时间',
      },
    ],
    warnings: [
      {
        code: 'SOURCE_DATA_STALE',
        message: '部分数据已过期（智云课堂登录失效），计划可能不完整',
        task_ids: [],
        details: { platform: 'zykt' },
      },
    ],
    summary: {
      capacity_minutes: 1620,
      scheduled_minutes: 480,
      locked_minutes: 60,
      scheduled_task_count: 4,
      unassigned_task_count: 2,
    },
    based_on_plan_id: 'plan_prev_3',
    created_at: ago(30),
    applied_at: ago(29),
    revoked_at: null,
    superseded_by: null,
  }

  const previousPlan: Plan = {
    id: 'plan_prev_3',
    version: 3,
    status: 'superseded',
    input_revision: 'ir_3',
    horizon_start: dayKeyOffset(0),
    horizon_end: dayKeyOffset(7),
    blocks: [
      {
        id: 'blk_3_1',
        task_id: 'task_ds_read3',
        starts_at: at(0, '18:30'),
        ends_at: at(0, '20:00'),
        locked: false,
      },
      {
        id: 'blk_3_2',
        task_id: 'task_ds_ex3',
        starts_at: at(1, '19:00'),
        ends_at: at(1, '20:00'),
        locked: true,
      },
    ],
    unassigned: [],
    warnings: [],
    summary: {
      capacity_minutes: 1620,
      scheduled_minutes: 150,
      locked_minutes: 60,
      scheduled_task_count: 2,
      unassigned_task_count: 0,
    },
    based_on_plan_id: null,
    created_at: ago(120),
    applied_at: ago(120),
    revoked_at: null,
    superseded_by: 'plan_active_4',
  }

  const connections: Connection[] = [
    {
      id: 'conn_xzzd',
      platform: 'xzzd',
      display_name: '学在浙大',
      status: 'ok',
      last_checked_at: ago(1),
      last_sync_at: ago(26),
      last_error: null,
      recovery_action: null,
      entry_url: 'https://course.zju.edu.cn/ua/login',
      created_at: ago(500),
      updated_at: ago(26),
    },
    {
      id: 'conn_zykt',
      platform: 'zykt',
      display_name: '智云课堂',
      status: 'auth_required',
      last_checked_at: ago(2),
      last_sync_at: ago(70),
      last_error: authExpired,
      recovery_action: {
        type: 'user_login',
        entry_url: 'https://classroom.zju.edu.cn/',
        message: '登录已失效，请在连接器窗口完成登录后重试',
      },
      entry_url: 'https://classroom.zju.edu.cn/',
      created_at: ago(500),
      updated_at: ago(2),
    },
  ]

  const capabilities = new Map<string, Capabilities>([
    [
      'conn_xzzd',
      {
        platform: 'xzzd',
        connection_id: 'conn_xzzd',
        connector_version: '0.1.0',
        auth: {
          mode: 'user_session',
          sso: 'zjuam-cas',
          recovery_hint: '会话在浏览器中建立；失效时重新登录即可',
        },
        features: {
          course_list: { status: 'supported', note: null },
          course_meta: { status: 'supported', note: null },
          attachments_meta: { status: 'supported', note: null },
          attachment_download: { status: 'supported', note: null },
          assignments: { status: 'supported', note: null },
          recordings: { status: 'unsupported', note: '该平台不提供回放，回放见智云课堂' },
          subtitles: { status: 'unsupported', note: null },
          playback_seek: { status: 'unsupported', note: null },
          deletion_signal: { status: 'unknown', note: '删除信号依赖平台接口，尚未确认' },
        },
        limits: {
          page_size_max: 50,
          rate_limit_note: '请勿高频请求，同步任务自带节流',
          campus_network_required: false,
        },
        checked_at: ago(1),
      },
    ],
    [
      'conn_zykt',
      {
        platform: 'zykt',
        connection_id: 'conn_zykt',
        connector_version: '0.1.0',
        auth: {
          mode: 'user_session',
          sso: 'zjuam-oauth2',
          recovery_hint: '登录失效时在连接器打开的浏览器窗口内重新登录',
        },
        features: {
          course_list: { status: 'supported', note: null },
          course_meta: { status: 'supported', note: null },
          attachments_meta: { status: 'preview_only', note: '资料元数据可见，附件下载受限' },
          attachment_download: { status: 'preview_only', note: '仅预览，不保存完整视频' },
          assignments: { status: 'unsupported', note: '该平台不提供作业信息' },
          recordings: { status: 'supported', note: null },
          subtitles: { status: 'supported', note: '仅读取平台已有字幕，不做语音转写' },
          playback_seek: { status: 'unknown', note: '回放定位能力待实测确认' },
          deletion_signal: { status: 'unknown', note: null },
        },
        limits: {
          page_size_max: 50,
          rate_limit_note: '回放接口限速较严，批量操作请走同步任务',
          campus_network_required: true,
        },
        checked_at: ago(2),
      },
    ],
  ])

  const jobs: Job[] = [
    {
      id: 'job_export_1',
      type: 'export',
      status: 'succeeded',
      progress: 1,
      errors: [],
      warnings: [],
      result_refs: [{ type: 'export_file', id: 'exp_1' }],
      cancel_requested: false,
      created_at: ago(80),
      started_at: ago(80),
      finished_at: ago(80),
    },
    {
      id: 'job_parse_1',
      type: 'parse',
      status: 'failed',
      progress: 1,
      errors: [
        {
          code: 'PARSE_CORRUPT',
          message: '课程大纲.pptx 文件结构异常，无法解析',
          retryable: true,
          details: { diagnostic_id: 'diag_parse_01' },
        },
      ],
      warnings: [],
      result_refs: [],
      cancel_requested: false,
      created_at: ago(200),
      started_at: ago(200),
      finished_at: ago(200),
    },
    {
      id: 'job_sync_1',
      type: 'sync',
      status: 'partial_failed',
      progress: 1,
      errors: [
        {
          code: 'PERMISSION_DENIED',
          message: '第6章 图算法 讲义.pdf：该内容受平台权限限制，已跳过',
          retryable: false,
          details: { remote_status: 403, access_level: 'restricted' },
        },
        {
          code: 'NOT_FOUND',
          message: '第3讲 回放.mp4：该内容在平台侧不存在或已被移除',
          retryable: false,
          details: { remote_status: 404 },
        },
        {
          code: 'FETCH_FAILED',
          message: '第7讲 习题解答.pdf：平台暂时异常（HTTP 502）',
          retryable: true,
          details: { remote_status: 502 },
        },
      ],
      warnings: [],
      result_refs: [],
      cancel_requested: false,
      created_at: ago(27),
      started_at: ago(27),
      finished_at: ago(26),
    },
    {
      id: 'job_import_1',
      type: 'import',
      status: 'succeeded',
      progress: 1,
      errors: [],
      warnings: [],
      result_refs: [{ type: 'resource', id: 'res_ds_lab' }],
      cancel_requested: false,
      created_at: ago(73),
      started_at: ago(73),
      finished_at: ago(72),
    },
    {
      id: 'job_import_2',
      type: 'import',
      status: 'succeeded',
      progress: 1,
      errors: [],
      warnings: [],
      result_refs: [{ type: 'resource', id: 'res_ml_intro' }],
      cancel_requested: false,
      created_at: ago(46),
      started_at: ago(46),
      finished_at: ago(46),
    },
  ]

  return {
    courses,
    resources,
    resourceVersions,
    chunks,
    assignments,
    recordings,
    tasks,
    feedback,
    availability,
    connections,
    capabilities,
    jobs,
    runtimeJobs: new Map(),
    buildScopes: new Map(),
    activePlan,
    planHistory: [previousPlan],
    previews: new Map(),
    inputRevision: 4,
    planVersion: 4,
    idempotency: new Map(),
    fileHashes: new Map([['实验指导书.pdf:1048576', 'res_ds_lab']]),
    seq: 100,
    providers: [],
    providerKeys: new Map(),
    questionbankSources: [
      {
        id: 'qb_demo',
        name: '示例题库（演示来源）',
        kind: 'search_service',
        configured: true,
        requires_config: false,
        searchable: true,
        fetchable: true,
        answer_available: true,
        formula_support: 'none',
        notes: '模拟模式的内置演示来源；真实来源由 A 连接器配置，未配置时网络/混合模式将被预检提醒',
      },
      {
        id: 'qb_school_past',
        name: '校内历年题（待配置）',
        kind: 'user_url',
        configured: false,
        requires_config: true,
        searchable: false,
        fetchable: false,
        answer_available: false,
        formula_support: 'unknown',
        notes: '演示“来源存在但未配置”的状态：预检面板如实展示，不会假装已连接',
      },
    ],
    papers: [],
    reviewJobRequests: new Map(),
    reviewRevisionRequests: new Map(),
    reviewPaperInputs: new Map(),
  }
}

let db = createDb()

export function getDb(): MockDb {
  return db
}

export function resetMockDb(): void {
  db = createDb()
}

const JOB_RUN_DELAY_MS = 600

export function createJob(
  target: MockDb,
  type: JobType,
  durationMs: number,
  onComplete: () => Partial<Job>,
): Job {
  target.seq += 1
  const job: Job = {
    id: `job_${type}_${target.seq}`,
    type,
    status: 'queued',
    progress: null,
    errors: [],
    warnings: [],
    result_refs: [],
    cancel_requested: false,
    created_at: toApiIso(dayjs(), target.availability.timezone),
    started_at: null,
    finished_at: null,
  }
  target.jobs.unshift(job)
  target.runtimeJobs.set(job.id, { durationMs, startedMs: null, onComplete })
  return job
}

/** 推进模拟后台任务：首次 tick 进入 running，取消请求协作式生效，到时调用完成回调 */
export function tickJobs(target: MockDb, nowMs: number = Date.now()): void {
  for (const [jobId, runtime] of target.runtimeJobs) {
    const job = target.jobs.find((j) => j.id === jobId)
    if (!job) {
      target.runtimeJobs.delete(jobId)
      continue
    }
    if (runtime.startedMs === null) {
      runtime.startedMs = nowMs
      job.status = 'running'
      job.started_at = toApiIso(dayjs(nowMs), target.availability.timezone)
    }
    if (job.cancel_requested) {
      job.status = 'cancelled'
      job.progress = null
      job.finished_at = toApiIso(dayjs(nowMs), target.availability.timezone)
      target.runtimeJobs.delete(jobId)
      continue
    }
    const elapsed = nowMs - runtime.startedMs
    if (elapsed >= runtime.durationMs) {
      const patch = runtime.onComplete()
      Object.assign(job, patch)
      job.progress = 1
      runtime.onProgress?.(job, 1)
      job.finished_at = toApiIso(dayjs(nowMs), target.availability.timezone)
      target.runtimeJobs.delete(jobId)
    } else {
      job.progress = Math.min(0.95, elapsed / runtime.durationMs)
      runtime.onProgress?.(job, job.progress)
    }
  }
}

/** 解析完成时生成占位文本片段，保证 chunk_count 与 /chunks 接口实际内容一致 */
export function generateChunks(target: MockDb, resource: Resource, jobId: string): ContentChunk[] {
  target.chunks = target.chunks.filter((c) => c.resource_id !== resource.id)
  const count = 3
  const generated: ContentChunk[] = Array.from({ length: count }, (_, i) => {
    const ordinal = i
    const useSlide = resource.extent_unit === 'slide'
    return {
      id: `chk_${resource.id}_${ordinal}`,
      resource_id: resource.id,
      resource_version: resource.version,
      ordinal,
      text: `${resource.title} 第 ${i + 1} 段解析文本（模拟解析结果）`,
      locator: useSlide
        ? ({ kind: 'slide', slide: i + 1 } as const)
        : ({ kind: 'page', page: i + 1 } as const),
      provenance: {
        extractor: 'mock-extractor',
        extractor_version: '0.1.0',
        job_id: jobId,
        fact_source: resource.source_refs.length > 0 ? ('platform' as const) : ('user' as const),
        observed_at: toApiIso(dayjs(), target.availability.timezone),
      },
      created_at: toApiIso(dayjs(), target.availability.timezone),
    }
  })
  target.chunks.push(...generated)
  resource.parse_status = 'ready'
  resource.chunk_count = generated.length
  resource.updated_at = toApiIso(dayjs(), target.availability.timezone)
  return generated
}

export const IMPORT_MAX_BYTES = MAX_UPLOAD_BYTES

export function importTypeOk(fileName: string): boolean {
  return /\.(pdf|pptx|docx)$/i.test(fileName)
}

export function kindFromName(fileName: string): Resource['kind'] {
  if (/\.pptx$/i.test(fileName)) return 'slides'
  if (/\.docx$/i.test(fileName)) return 'document'
  if (/\.pdf$/i.test(fileName)) return 'handout'
  return 'other'
}

export { JOB_RUN_DELAY_MS }
