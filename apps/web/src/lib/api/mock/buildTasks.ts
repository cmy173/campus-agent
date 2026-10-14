import dayjs from 'dayjs'
import type { Assignment, JobWarning, LearningTask, Recording, Resource, SourceRef } from '../../../types/contract'
import { toApiIso } from '../../time'
import type { MockDb } from './db'

/**
 * 模拟 build_tasks（B13）：规则镜像 D3 taskgen 的确定性行为——
 * 资料族/版本/内容重复合并、稳定 match_key、既有任务用户修正保留、warnings 原样上报。
 * 与真实后端的差异仅限可实现面：D 的超长 key 截断附加 sha1 摘要分支在本模拟数据下不可达，未实现。
 */

const MINUTES_PER_UNIT: Record<string, number> = { page: 3, slide: 2 }
const DEFAULT_MINUTES: Record<string, number> = {
  reading: 60,
  watching: 60,
  assignment: 90,
}

export interface BuildTasksOutcome {
  created: LearningTask[]
  updated: LearningTask[]
  warnings: JobWarning[]
}

/** D taskgen.slugify_title 的 JS 镜像（NFKC；空白、斜杠、反斜杠、竖线、冒号 → -） */
export function slugifyTitle(title: string): string {
  return title
    .normalize('NFKC')
    .trim()
    .replace(/[\s/\\|:]+/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^-+|-+$/g, '')
}

function buildTaskMatchKey(courseId: string, taskType: string, key: string): string {
  return `t:${courseId}:${taskType}:${key}`
}

function refKey(ref: SourceRef): string {
  return `${ref.platform}|${ref.external_id}`
}

/** D _merge_refs：按 (platform, external_id) 去重后按序保留 */
function mergeRefs(refs: SourceRef[]): SourceRef[] {
  const unique = new Map<string, SourceRef>()
  for (const ref of refs) {
    if (!unique.has(refKey(ref))) unique.set(refKey(ref), ref)
  }
  return [...unique.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([, ref]) => ref)
}

interface Candidate {
  matchKey: string
  courseId: string
  type: LearningTask['type']
  title: string
  estimatedMinutes: number
  dueAt: string | null
  duePrecision: LearningTask['due_precision']
  sourceRefs: SourceRef[]
  factSource: LearningTask['fact_source']
  prerequisiteIds: string[]
}

function resourceCandidates(db: MockDb, courseId: string | null, warnings: JobWarning[]): Candidate[] {
  const resources = db.resources
    .filter((r) => r.archived_at === null && (courseId === null || r.course_id === courseId))
    .slice()
    .sort((a, b) =>
      `${a.course_id}|${a.kind}|${slugifyTitle(a.title)}|${String(a.version).padStart(5, '0')}|${a.id}` <
      `${b.course_id}|${b.kind}|${slugifyTitle(b.title)}|${String(b.version).padStart(5, '0')}|${b.id}`
        ? -1
        : 1,
    )

  const families = new Map<string, Resource[]>()
  for (const resource of resources) {
    const key = `${resource.course_id}|${resource.kind}|${slugifyTitle(resource.title)}`
    families.set(key, [...(families.get(key) ?? []), resource])
  }

  const familiesSorted = [...families.entries()].sort(([keyA, a], [keyB, b]) => {
    const courseKindA = keyA.slice(0, keyA.lastIndexOf('|'))
    const courseKindB = keyB.slice(0, keyB.lastIndexOf('|'))
    if (courseKindA !== courseKindB) return courseKindA < courseKindB ? -1 : 1
    const maxA = Math.max(...a.map((m) => m.version))
    const maxB = Math.max(...b.map((m) => m.version))
    if (maxA !== maxB) return maxB - maxA
    if (a.length !== b.length) return b.length - a.length
    return keyA < keyB ? -1 : 1
  })

  const seenHash = new Map<string, Resource>()
  const keptByKey = new Map<string, Resource[]>()
  const keptOrder: string[] = []
  for (const [key, members] of familiesSorted) {
    let overlap: { skipped: Resource; previous: Resource } | null = null
    for (const member of [...members].sort((a, b) => a.version - b.version || (a.id < b.id ? -1 : 1))) {
      if (member.content_hash && seenHash.has(`${member.course_id}|${member.content_hash}`)) {
        overlap = { skipped: member, previous: seenHash.get(`${member.course_id}|${member.content_hash}`)! }
        break
      }
    }
    if (overlap) {
      const targetKey = `${overlap.previous.course_id}|${overlap.previous.kind}|${slugifyTitle(overlap.previous.title)}`
      keptByKey.set(targetKey, [...(keptByKey.get(targetKey) ?? []), ...members])
      warnings.push({
        code: 'DUPLICATE_CONTENT',
        message: `资料《${overlap.skipped.title}》（${overlap.skipped.id}）与《${overlap.previous.title}》（${overlap.previous.id}）内容相同，已合并到同一任务，不重复生成`,
        task_ids: [],
        details: { kept_resource_id: overlap.previous.id, skipped_resource_id: overlap.skipped.id },
      })
      continue
    }
    for (const member of members) {
      if (member.content_hash) {
        const hashKey = `${member.course_id}|${member.content_hash}`
        if (!seenHash.has(hashKey)) seenHash.set(hashKey, member)
      }
    }
    keptByKey.set(key, members)
    keptOrder.push(key)
  }

  const candidates: Candidate[] = []
  for (const key of keptOrder) {
    const membersSorted = [...(keptByKey.get(key) ?? [])].sort(
      (a, b) => a.version - b.version || (a.id < b.id ? -1 : 1),
    )
    const current = membersSorted[membersSorted.length - 1]
    const versions = [...new Set(membersSorted.map((m) => m.version))].sort((a, b) => a - b)
    if (versions.length > 1) {
      warnings.push({
        code: 'TASK_VERSION_UPDATED',
        message: `《${current.title}》存在 ${versions.length} 个版本（${membersSorted.length} 个文件），任务按最新版本 v${current.version} 估算并保留全部来源引用`,
        task_ids: [],
        details: { course_id: current.course_id, versions },
      })
    }
    let minutes = DEFAULT_MINUTES.reading
    if (current.extent_value && current.extent_unit && MINUTES_PER_UNIT[current.extent_unit]) {
      minutes = Math.max(1, current.extent_value * MINUTES_PER_UNIT[current.extent_unit])
    } else if (current.extent_unit && !MINUTES_PER_UNIT[current.extent_unit]) {
      warnings.push({
        code: 'UNSUPPORTED_EXTENT',
        message: `《${current.title}》：无法识别的篇幅单位 ${current.extent_unit}，按类型默认耗时估算`,
        task_ids: [],
        details: { resource_id: current.id },
      })
    }
    candidates.push({
      matchKey: buildTaskMatchKey(current.course_id, 'reading', slugifyTitle(current.title)),
      courseId: current.course_id,
      type: 'reading',
      title: current.title,
      estimatedMinutes: minutes,
      dueAt: null,
      duePrecision: 'unknown',
      sourceRefs: mergeRefs(membersSorted.flatMap((m) => m.source_refs)),
      factSource: 'inferred',
      prerequisiteIds: [],
    })
  }
  return candidates
}

function assignmentCandidates(db: MockDb, courseId: string | null): Candidate[] {
  return db.assignments
    .filter((a) => a.archived_at === null && (courseId === null || a.course_id === courseId))
    .slice()
    .sort((a, b) => (a.id < b.id ? -1 : 1))
    .map((assignment: Assignment) => ({
      matchKey: buildTaskMatchKey(
        assignment.course_id,
        'assignment',
        assignment.source_ref?.external_id ?? slugifyTitle(assignment.title),
      ),
      courseId: assignment.course_id,
      type: 'assignment' as const,
      title: assignment.title,
      estimatedMinutes: DEFAULT_MINUTES.assignment,
      dueAt: assignment.due_at,
      duePrecision: assignment.due_precision,
      sourceRefs: assignment.source_ref ? [assignment.source_ref] : [],
      factSource: 'inferred' as const,
      prerequisiteIds: [],
    }))
}

function recordingCandidates(db: MockDb, courseId: string | null, warnings: JobWarning[]): Candidate[] {
  return db.recordings
    .filter((r) => courseId === null || r.course_id === courseId)
    .slice()
    .sort((a, b) => (a.id < b.id ? -1 : 1))
    .map((recording: Recording) => {
      let minutes = DEFAULT_MINUTES.watching
      if (recording.duration_seconds && recording.duration_seconds > 0) {
        minutes = Math.max(1, Math.ceil((recording.duration_seconds / 60) * 1.0))
      } else {
        warnings.push({
          code: 'MISSING_DURATION',
          message: `回放《${recording.title}》缺少时长，按默认 ${minutes} 分钟估算`,
          task_ids: [],
          details: { recording_id: recording.id },
        })
      }
      return {
        matchKey: buildTaskMatchKey(
          recording.course_id,
          'watching',
          recording.source_ref?.external_id ?? slugifyTitle(recording.title),
        ),
        courseId: recording.course_id,
        type: 'watching' as const,
        title: recording.title,
        estimatedMinutes: minutes,
        dueAt: null,
        duePrecision: 'unknown' as const,
        sourceRefs: recording.source_ref ? [recording.source_ref] : [],
        factSource: 'inferred' as const,
        prerequisiteIds: [],
      }
    })
}

function sameRefs(a: SourceRef[], b: SourceRef[]): boolean {
  if (a.length !== b.length) return false
  const keyOf = (refs: SourceRef[]) => refs.map(refKey).sort().join('\u0000')
  return keyOf(a) === keyOf(b)
}

/** 执行一次任务生成并落库（镜像 B13：稳定 match_key 对齐、用户修正保留、归档不复活） */
export function runBuildTasks(db: MockDb, courseId: string | null): BuildTasksOutcome {
  const warnings: JobWarning[] = []
  const candidates = [
    ...resourceCandidates(db, courseId, warnings),
    ...assignmentCandidates(db, courseId),
    ...recordingCandidates(db, courseId, warnings),
  ]
  const candidateKeys = new Set(candidates.map((c) => c.matchKey))
  const existing = db.tasks.filter(
    (t) => t.match_key !== null && t.archived_at === null && (courseId === null || t.course_id === courseId),
  )
  for (const task of existing) {
    if (!candidateKeys.has(task.match_key!)) {
      warnings.push({
        code: 'EXISTING_TASK_UNMATCHED',
        message: `已有任务 ${task.match_key} 在本次输入中无对应来源，已原样保留（可能为用户自建或来源已移除）`,
        task_ids: [task.match_key!],
        details: { task_id: task.id },
      })
    }
  }

  const outcome: BuildTasksOutcome = { created: [], updated: [], warnings }
  const now = toApiIso(dayjs(), db.availability.timezone)
  for (const candidate of candidates) {
    const row = db.tasks.find((t) => t.match_key === candidate.matchKey)
    if (row === undefined) {
      db.seq += 1
      const created: LearningTask = {
        id: `task_${db.seq}`,
        match_key: candidate.matchKey,
        course_id: candidate.courseId,
        type: candidate.type,
        title: candidate.title,
        estimated_minutes: candidate.estimatedMinutes,
        due_at: candidate.dueAt,
        due_precision: candidate.duePrecision,
        prerequisite_ids: candidate.prerequisiteIds,
        source_refs: candidate.sourceRefs,
        status: 'pending',
        fact_source: candidate.factSource,
        revision: 1,
        archived_at: null,
        created_at: now,
        updated_at: now,
      }
      db.tasks.push(created)
      outcome.created.push(created)
      continue
    }
    if (row.archived_at !== null) continue

    // 既有任务的用户修正优先（D §5）：耗时、状态、来源标记、前置关系不被重新生成覆盖
    if (row.estimated_minutes > 0) {
      candidate.estimatedMinutes = row.estimated_minutes
      candidate.factSource = row.fact_source
    }
    candidate.prerequisiteIds = row.prerequisite_ids.length > 0 ? [...row.prerequisite_ids] : []
    const mergedStatus = row.status

    const changed =
      row.title !== candidate.title ||
      row.type !== candidate.type ||
      row.estimated_minutes !== candidate.estimatedMinutes ||
      row.due_at !== candidate.dueAt ||
      row.due_precision !== candidate.duePrecision ||
      row.status !== mergedStatus ||
      row.fact_source !== candidate.factSource ||
      !sameRefs(row.source_refs, candidate.sourceRefs) ||
      row.prerequisite_ids.join('\u0000') !== candidate.prerequisiteIds.join('\u0000')
    if (!changed) continue

    row.title = candidate.title
    row.type = candidate.type
    row.estimated_minutes = candidate.estimatedMinutes
    row.due_at = candidate.dueAt
    row.due_precision = candidate.duePrecision
    row.status = mergedStatus
    row.fact_source = candidate.factSource
    row.source_refs = candidate.sourceRefs
    row.prerequisite_ids = candidate.prerequisiteIds
    row.revision += 1
    row.updated_at = now
    outcome.updated.push(row)
  }
  return outcome
}
