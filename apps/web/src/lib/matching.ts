import type { Assignment, SourceRef } from '../types/contract'

/** 平台来源唯一键：按 (platform, external_id) 匹配，不猜测 */
export function sourceRefKey(ref: Pick<SourceRef, 'platform' | 'external_id'>): string {
  return `${ref.platform}:${ref.external_id}`
}

/**
 * PD-3：已提交/已评分（平台已提交）且未归档作业的来源键集合。
 * 与后端 plan_engine._submitted_assignment_refs 同一关联依据（(platform, external_id)）。
 */
export function submittedAssignmentRefKeys(assignments: Assignment[]): Set<string> {
  const keys = new Set<string>()
  for (const a of assignments) {
    if (a.archived_at !== null) continue
    if (a.submission_status !== 'submitted' && a.submission_status !== 'graded') continue
    if (a.source_ref) keys.add(sourceRefKey(a.source_ref))
  }
  return keys
}

/** 任务是否关联平台已提交/已评分作业（直接读取作业的 submission_status 平台事实，不从任务状态推断） */
export function linkedToSubmittedAssignment(refs: SourceRef[], submittedKeys: Set<string>): boolean {
  return refs.some((ref) => submittedKeys.has(sourceRefKey(ref)))
}
