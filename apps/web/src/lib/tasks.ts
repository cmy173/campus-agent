import type { LearningTask, Plan } from '../types/contract'
import { DEFAULT_TIMEZONE, dayKey, todayKey } from './time'

/**
 * 「今日任务」视图过滤：今日计划时间块内 ∪ 今日截止 ∪ 今日完成记录。
 * 原 mock 端曾以未入契约的 scope=today 参数实现（真机被忽略导致不过滤），
 * 收敛为客户端视图过滤，只用契约内 GET /tasks + GET /plans/current。
 */
export function filterTodayTasks(
  tasks: LearningTask[],
  plan: Plan | null | undefined,
  tz: string = DEFAULT_TIMEZONE,
): LearningTask[] {
  const today = todayKey(tz)
  const blockTaskIds = new Set(
    (plan?.blocks ?? [])
      .filter((b) => dayKey(b.starts_at, tz) === today)
      .map((b) => b.task_id),
  )
  return tasks.filter((t) => {
    if (blockTaskIds.has(t.id)) return true
    if (t.due_at !== null && dayKey(t.due_at, tz) === today) return true
    if (t.status === 'completed' && dayKey(t.updated_at, tz) === today) return true
    return false
  })
}
