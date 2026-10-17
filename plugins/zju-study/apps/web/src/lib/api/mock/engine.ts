import dayjs from 'dayjs'
import type {
  LearningTask,
  Plan,
  PlanBlock,
  PlanWarning,
  UnassignedItem,
  UnassignedReasonCode,
} from '../../../types/contract'
import { DEFAULT_TIMEZONE, minutesFromHHmm, toApiIso } from '../../time'
import { linkedToSubmittedAssignment, submittedAssignmentRefKeys } from '../../matching'
import { dateExceptionFor } from '../../validation'
import type { MockDb } from './db'
import { revisionStr } from './db'

/**
 * 模拟排程引擎（简化版规则排程）。
 *
 * 真实规则排程归智能体 D；此处仅生成满足硬约束的确定性示例，
 * 用于网页端验证"预览含新增/移动/保留/无法安排"与不可行原因展示。
 * 同一输入产生同一输出；不做全局最优声明。
 *
 * 模拟简化：单个任务只在一个连续空闲段内安排（不跨段拼接），
 * 因此超过每日上限的任务在模拟中视为不可拆分。
 */

const MIN_CHUNK_MINUTES = 15
const ROUND_MINUTES = 5

interface Interval {
  startMs: number
  endMs: number
}

interface DayState {
  dayKey: string
  free: Interval[]
  /** 与该日可用时间冲突的锁定块任务 ID */
  lockedConflicts: string[]
}

function roundUpTo(ms: number, minutes: number): number {
  return Math.ceil(ms / (minutes * 60000)) * minutes * 60000
}

function intervalMinutes(iv: Interval): number {
  return Math.floor((iv.endMs - iv.startMs) / 60000)
}

function subtractInterval(intervals: Interval[], s: number, e: number): Interval[] {
  const out: Interval[] = []
  for (const iv of intervals) {
    if (e <= iv.startMs || s >= iv.endMs) {
      out.push(iv)
      continue
    }
    if (s > iv.startMs) out.push({ startMs: iv.startMs, endMs: Math.min(iv.endMs, s) })
    if (e < iv.endMs) out.push({ startMs: Math.max(iv.startMs, e), endMs: iv.endMs })
  }
  return out.filter((iv) => intervalMinutes(iv) >= MIN_CHUNK_MINUTES)
}

function trimToCapacity(intervals: Interval[], capacityMinutes: number): Interval[] {
  const out = intervals.map((iv) => ({ ...iv }))
  let total = out.reduce((sum, iv) => sum + intervalMinutes(iv), 0)
  for (let i = out.length - 1; i >= 0 && total > capacityMinutes; i--) {
    const shorten = Math.min(intervalMinutes(out[i]), total - capacityMinutes)
    out[i] = { ...out[i], endMs: out[i].endMs - shorten * 60000 }
    total -= shorten
  }
  return out.filter((iv) => intervalMinutes(iv) >= MIN_CHUNK_MINUTES)
}

function cloneDays(days: DayState[]): DayState[] {
  return days.map((d) => ({
    dayKey: d.dayKey,
    free: d.free.map((iv) => ({ ...iv })),
    lockedConflicts: [...d.lockedConflicts],
  }))
}

function totalMinutesBefore(days: DayState[], beforeMs: number): number {
  let total = 0
  for (const day of days) {
    for (const iv of day.free) {
      if (iv.startMs >= beforeMs) continue
      total += Math.floor((Math.min(iv.endMs, beforeMs) - iv.startMs) / 60000)
    }
  }
  return total
}

function totalMinutes(days: DayState[]): number {
  let total = 0
  for (const day of days) {
    for (const iv of day.free) total += intervalMinutes(iv)
  }
  return total
}

function maxContiguousMinutesBefore(days: DayState[], beforeMs: number): number {
  let max = 0
  for (const day of days) {
    for (const iv of day.free) {
      if (iv.startMs >= beforeMs) continue
      max = Math.max(max, Math.floor((Math.min(iv.endMs, beforeMs) - iv.startMs) / 60000))
    }
  }
  return max
}

/** 任务截止时刻：date 精度按当日 23:59:59 处理；未知/缺失视为无截止 */
export function taskDueMs(task: LearningTask): number {
  if (!task.due_at) return Infinity
  const base = Date.parse(task.due_at)
  if (Number.isNaN(base)) return Infinity
  if (task.due_precision === 'date') return base + 24 * 3600 * 1000 - 1000
  if (task.due_precision === 'unknown') return Infinity
  return base
}

function isActive(task: LearningTask): boolean {
  return task.status === 'pending' || task.status === 'in_progress'
}

/** 模拟简化：超过每日上限的单个任务无法放入一个连续段，视为不可拆分 */
function isIndivisible(task: LearningTask, dailyLimit: number | null): boolean {
  return dailyLimit !== null && task.estimated_minutes > dailyLimit
}

function takeFromDays(
  source: DayState[],
  task: LearningTask,
  nowMs: number,
  indivisible: boolean,
): { pieces: Interval[]; days: DayState[] } | null {
  const beforeMs = taskDueMs(task)
  if (beforeMs <= nowMs) return null
  const work = cloneDays(source)
  const pieces: Interval[] = []
  let need = task.estimated_minutes

  outer: for (const day of work) {
    let idx = 0
    while (idx < day.free.length) {
      const iv = day.free[idx]
      if (iv.startMs >= beforeMs) break
      const usableEnd = Math.min(iv.endMs, beforeMs)
      const avail = Math.floor((usableEnd - iv.startMs) / 60000)
      let take = 0
      if (!indivisible) {
        take = avail >= need ? need : avail >= MIN_CHUNK_MINUTES ? avail : 0
      } else if (avail >= need) {
        take = need
      }
      if (take <= 0) {
        idx += 1
        continue
      }
      const pieceEnd = iv.startMs + take * 60000
      pieces.push({ startMs: iv.startMs, endMs: pieceEnd })
      need -= take
      const rest = iv.endMs - pieceEnd
      if (rest >= MIN_CHUNK_MINUTES * 60000) {
        day.free[idx] = { startMs: pieceEnd, endMs: iv.endMs }
        idx += 1
      } else {
        day.free.splice(idx, 1)
      }
      if (need <= 0) break outer
    }
  }

  if (need > 0) return null
  return { pieces, days: work }
}

function classify(
  task: LearningTask,
  daysBefore: DayState[],
  nowMs: number,
  indivisible: boolean,
): { code: UnassignedReasonCode; shortage: number; message: string; suggestion: string } {
  const beforeMs = taskDueMs(task)
  if (beforeMs <= nowMs && task.due_at !== null) {
    return {
      code: 'DEADLINE_PASSED',
      shortage: 0,
      message: '截止时间已过，无法再安排在期限内',
      suggestion: '请确认该任务是否仍需完成：可修改截止时间后重新生成计划，或将其标记完成/取消',
    }
  }
  const totalBefore = beforeMs === Infinity ? totalMinutes(daysBefore) : totalMinutesBefore(daysBefore, beforeMs)
  const maxContiguous = maxContiguousMinutesBefore(daysBefore, beforeMs)
  if (indivisible && maxContiguous < task.estimated_minutes) {
    return {
      code: 'TASK_TOO_LONG_INDIVISIBLE',
      shortage: task.estimated_minutes - maxContiguous,
      message: `任务需连续 ${task.estimated_minutes} 分钟，但最长连续空闲段仅 ${maxContiguous} 分钟，且该任务不可拆分`,
      suggestion: '可缩短单次时长、延长该日可用窗口，或将该任务调整为可拆分后重试',
    }
  }
  if (task.due_at !== null && totalBefore < task.estimated_minutes) {
    return {
      code: 'INSUFFICIENT_WINDOW_BEFORE_DUE',
      shortage: task.estimated_minutes - totalBefore,
      message: `截止前剩余可用时间不足：还需 ${task.estimated_minutes} 分钟，截止前仅剩 ${Math.max(0, totalBefore)} 分钟`,
      suggestion: '请增加截止前的可用时间窗口，或调整截止时间',
    }
  }
  if (task.due_at === null && totalBefore < task.estimated_minutes) {
    return {
      code: 'INSUFFICIENT_TOTAL_TIME',
      shortage: task.estimated_minutes - totalBefore,
      message: `计划范围内可用时间不足：还需 ${task.estimated_minutes} 分钟，剩余可用 ${Math.max(0, totalBefore)} 分钟`,
      suggestion: '请增加每周可用时间窗口，或减少任务预估耗时',
    }
  }
  return {
    code: 'FRAGMENTED_WINDOW',
    shortage: Math.max(0, task.estimated_minutes - maxContiguous),
    message: `可用时间内找不到满足要求的连续时段（最长连续 ${maxContiguous} 分钟，任务需 ${task.estimated_minutes} 分钟）`,
    suggestion: '请延长任一可用窗口的长度，使任务能完整安排',
  }
}

export function generatePreview(db: MockDb): Plan {
  const availability = db.availability
  const tz = availability.timezone || DEFAULT_TIMEZONE
  const now = dayjs()
  const nowMs = now.valueOf()
  const horizonStart = now.tz(tz).startOf('day')
  const dailyLimit = availability.daily_limit_minutes

  // PD-3：已提交/已评分作业对应的任务保留在库但不排程（镜像后端 plan_engine），锁定块也不再占容量
  const submittedRefs = submittedAssignmentRefKeys(db.assignments)
  const excludedTaskIds = new Set(
    db.tasks
      .filter((t) => linkedToSubmittedAssignment(t.source_refs, submittedRefs))
      .map((t) => t.id),
  )

  const days: DayState[] = []

  for (let i = 0; i < 7; i++) {
    const day = horizonStart.add(i, 'day')
    const key = day.format('YYYY-MM-DD')
    const ex = dateExceptionFor(key, availability.date_exceptions)
    let windows: { start: string; end: string }[]
    if (ex && ex.windows.length === 0) {
      windows = []
    } else if (ex && ex.windows.length > 0) {
      windows = ex.windows.map((w) => ({ start: w.start, end: w.end }))
    } else {
      windows = availability.weekly_windows.filter((w) => w.weekday === day.isoWeekday())
    }
    let intervals: Interval[] = windows
      .map((w) => ({
        startMs: day.add(minutesFromHHmm(w.start), 'minute').valueOf(),
        endMs: day.add(minutesFromHHmm(w.end), 'minute').valueOf(),
      }))
      .filter((iv) => intervalMinutes(iv) >= MIN_CHUNK_MINUTES)
    if (i === 0) {
      const floorMs = roundUpTo(nowMs + 60000, ROUND_MINUTES)
      intervals = intervals
        .map((iv) => ({ startMs: Math.max(iv.startMs, floorMs), endMs: iv.endMs }))
        .filter((iv) => intervalMinutes(iv) >= MIN_CHUNK_MINUTES)
    }

    let dayLockedMinutes = 0
    const lockedConflicts: string[] = []
    for (const b of db.activePlan?.blocks ?? []) {
      if (!b.locked || excludedTaskIds.has(b.task_id)) continue
      if (dayjs(b.starts_at).tz(tz).format('YYYY-MM-DD') !== key) continue
      const s = Date.parse(b.starts_at)
      const e = Date.parse(b.ends_at)
      dayLockedMinutes += Math.floor((e - s) / 60000)
      const inside = intervals.some((iv) => iv.startMs <= s && e <= iv.endMs)
      if (!inside) lockedConflicts.push(b.task_id)
      intervals = subtractInterval(intervals, s, e)
    }

    if (dailyLimit !== null) {
      intervals = trimToCapacity(intervals, Math.max(0, dailyLimit - dayLockedMinutes))
    }
    days.push({ dayKey: key, free: intervals, lockedConflicts })
  }

  const capacityMinutes = totalMinutes(days)

  const blocks: PlanBlock[] = []
  const unassigned: UnassignedItem[] = []
  const warnings: PlanWarning[] = []

  const keptLockedTaskIds = new Set<string>()
  for (const b of db.activePlan?.blocks ?? []) {
    if (!b.locked || excludedTaskIds.has(b.task_id)) continue
    const task = db.tasks.find((t) => t.id === b.task_id)
    if (!task || !isActive(task)) continue
    keptLockedTaskIds.add(b.task_id)
    blocks.push({
      id: b.id,
      task_id: b.task_id,
      starts_at: b.starts_at,
      ends_at: b.ends_at,
      locked: true,
    })
  }

  const unassignedIds = new Set<string>()

  const candidates = db.tasks
    .filter((t) => isActive(t) && !excludedTaskIds.has(t.id))
    .slice()
    .sort((a, b) => {
      const dueA = taskDueMs(a)
      const dueB = taskDueMs(b)
      if (dueA !== dueB) return dueA - dueB
      if (b.estimated_minutes !== a.estimated_minutes) return b.estimated_minutes - a.estimated_minutes
      return a.id.localeCompare(b.id)
    })

  for (const task of candidates) {
    if (keptLockedTaskIds.has(task.id)) continue

    const blockingPrereqId = task.prerequisite_ids.find((id) => unassignedIds.has(id))
    if (blockingPrereqId) {
      const prereq = db.tasks.find((t) => t.id === blockingPrereqId)
      unassigned.push({
        task_id: task.id,
        reason_code: 'DEPENDENCY_CONFLICT',
        shortage_minutes: 0,
        message: `前置任务「${prereq?.title ?? blockingPrereqId}」尚未安排，需先调整前置任务`,
        suggestion: '请先解决前置任务的排程问题（调整截止时间或可用时间）',
      })
      unassignedIds.add(task.id)
      continue
    }

    const existing = db.activePlan?.blocks.find((b) => b.task_id === task.id && !b.locked)
    const beforeMs = taskDueMs(task)
    const indivisible = isIndivisible(task, dailyLimit)

    let placed: { pieces: Interval[]; keepId?: string } | null = null

    if (existing) {
      const s = Date.parse(existing.starts_at)
      const e = Date.parse(existing.ends_at)
      if (s > nowMs && e <= beforeMs) {
        const keepDays = cloneDays(days)
        let kept = false
        for (const day of keepDays) {
          const idx = day.free.findIndex((iv) => iv.startMs <= s && e <= iv.endMs)
          if (idx >= 0) {
            const iv = day.free[idx]
            const replacement: Interval[] = []
            if (s - iv.startMs >= MIN_CHUNK_MINUTES * 60000) {
              replacement.push({ startMs: iv.startMs, endMs: s })
            }
            if (iv.endMs - e >= MIN_CHUNK_MINUTES * 60000) {
              replacement.push({ startMs: e, endMs: iv.endMs })
            }
            day.free.splice(idx, 1, ...replacement)
            kept = true
            break
          }
        }
        if (kept) {
          placed = { pieces: [{ startMs: s, endMs: e }], keepId: existing.id }
          days.length = 0
          days.push(...keepDays)
        }
      }
    }

    if (!placed) {
      const attempt = takeFromDays(days, task, nowMs, indivisible)
      if (attempt) {
        placed = { pieces: attempt.pieces }
        days.length = 0
        days.push(...attempt.days)
      }
    }

    if (!placed) {
      const { code, shortage, message, suggestion } = classify(task, days, nowMs, indivisible)
      unassigned.push({
        task_id: task.id,
        reason_code: code,
        shortage_minutes: Math.max(0, shortage),
        message,
        suggestion,
      })
      unassignedIds.add(task.id)
      continue
    }

    placed.pieces.forEach((piece, index) => {
      const id = placed.keepId && index === 0 ? placed.keepId : `blk_cand_${(db.seq += 1)}`
      blocks.push({
        id,
        task_id: task.id,
        starts_at: toApiIso(dayjs(piece.startMs), tz),
        ends_at: toApiIso(dayjs(piece.endMs), tz),
        locked: false,
      })
    })
  }

  const problemConnections = db.connections.filter(
    (c) =>
      c.status !== 'ok' ||
      !c.last_sync_at ||
      dayjs().diff(dayjs(c.last_sync_at), 'hour', true) > 24,
  )
  if (problemConnections.length > 0) {
    warnings.push({
      code: 'SOURCE_DATA_STALE',
      message: `「${problemConnections
        .map((c) => c.display_name)
        .join('」「')}」未处于正常同步状态，计划可能未反映最新变化`,
      task_ids: [],
      details: { connection_ids: problemConnections.map((c) => c.id) },
    })
  }

  for (let i = 0; i < 7; i++) {
    const day = horizonStart.add(i, 'day')
    const key = day.format('YYYY-MM-DD')
    const ex = dateExceptionFor(key, availability.date_exceptions)
    if (ex) {
      warnings.push({
        code: 'DATE_EXCEPTION_APPLIED',
        message: `可用时间例外：${day.format('M月D日')}${ex.note ? `（${ex.note}）` : ''}${ex.windows.length === 0 ? '，全天不可用' : '，按自定义窗口'} `,
        task_ids: [],
        details: { date: key },
      })
    }
  }

  const lockedConflictTaskIds = new Set<string>()
  for (const day of days) {
    for (const id of day.lockedConflicts) lockedConflictTaskIds.add(id)
  }
  for (const taskId of lockedConflictTaskIds) {
    const task = db.tasks.find((t) => t.id === taskId)
    warnings.push({
      code: 'LOCKED_BLOCK_CONFLICT',
      message: `锁定时间块「${task?.title ?? taskId}」与最新可用时间冲突，请调整锁定或可用时间`,
      task_ids: [taskId],
      details: {},
    })
  }

  db.seq += 1
  const scheduledBlocks = blocks.sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at))
  const lockedBlockMinutes = scheduledBlocks
    .filter((b) => b.locked)
    .reduce((sum, b) => sum + Math.floor((Date.parse(b.ends_at) - Date.parse(b.starts_at)) / 60000), 0)
  const scheduledMinutes = scheduledBlocks.reduce(
    (sum, b) => sum + Math.floor((Date.parse(b.ends_at) - Date.parse(b.starts_at)) / 60000),
    0,
  )

  const plan: Plan = {
    id: `plan_cand_${db.seq}`,
    version: db.planVersion + 1,
    status: 'candidate',
    input_revision: revisionStr(db),
    horizon_start: horizonStart.format('YYYY-MM-DD'),
    horizon_end: horizonStart.add(7, 'day').format('YYYY-MM-DD'),
    blocks: scheduledBlocks,
    unassigned,
    warnings,
    summary: {
      capacity_minutes: capacityMinutes,
      scheduled_minutes: scheduledMinutes,
      locked_minutes: lockedBlockMinutes,
      scheduled_task_count: new Set(scheduledBlocks.map((b) => b.task_id)).size,
      unassigned_task_count: unassigned.length,
    },
    based_on_plan_id: db.activePlan?.id ?? null,
    created_at: toApiIso(now, tz),
    applied_at: null,
    revoked_at: null,
    superseded_by: null,
  }
  return plan
}
