import type { Availability, DateException, WeeklyWindow } from '../types/contract'
import { minutesFromHHmm } from './time'

const HHMM_RE = /^([01]\d|2[0-3]):[0-5]\d$/
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

export interface ValidationIssue {
  field: string
  message: string
}

/** 校验单日内的多个时间窗口：格式、先后顺序、重叠 */
export function validateDailyWindows(windows: { start: string; end: string }[]): string[] {
  const errors: string[] = []
  for (const w of windows) {
    if (!HHMM_RE.test(w.start) || !HHMM_RE.test(w.end)) {
      errors.push('时间格式应为 HH:mm')
      continue
    }
    if (minutesFromHHmm(w.end) <= minutesFromHHmm(w.start)) {
      errors.push(`窗口 ${w.start}–${w.end} 的结束时间必须晚于开始时间`)
    }
  }
  const sorted = [...windows]
    .filter((w) => HHMM_RE.test(w.start) && HHMM_RE.test(w.end))
    .sort((a, b) => minutesFromHHmm(a.start) - minutesFromHHmm(b.start))
  for (let i = 1; i < sorted.length; i++) {
    if (minutesFromHHmm(sorted[i].start) < minutesFromHHmm(sorted[i - 1].end)) {
      errors.push(`窗口 ${sorted[i - 1].start}–${sorted[i - 1].end} 与 ${sorted[i].start}–${sorted[i].end} 重叠`)
    }
  }
  return errors
}

export function validateAvailability(input: Pick<
  Availability,
  'weekly_windows' | 'date_exceptions' | 'daily_limit_minutes' | 'timezone'
>): ValidationIssue[] {
  const issues: ValidationIssue[] = []

  if (!input.timezone) {
    issues.push({ field: 'timezone', message: '请选择时区' })
  }
  if (
    input.daily_limit_minutes !== null &&
    (!Number.isInteger(input.daily_limit_minutes) ||
      input.daily_limit_minutes < 15 ||
      input.daily_limit_minutes > 720)
  ) {
    issues.push({ field: 'daily_limit_minutes', message: '每日学习上限需为 15–720 分钟之间的整数，或留空表示不限制' })
  }

  const byWeekday = new Map<number, { start: string; end: string }[]>()
  for (const w of input.weekly_windows) {
    const list = byWeekday.get(w.weekday) ?? []
    list.push({ start: w.start, end: w.end })
    byWeekday.set(w.weekday, list)
  }
  for (const [weekday, list] of byWeekday) {
    for (const msg of validateDailyWindows(list)) {
      issues.push({ field: `weekly_windows.${weekday}`, message: `星期 ${weekday}：${msg}` })
    }
  }

  const seenDates = new Set<string>()
  for (const ex of input.date_exceptions) {
    if (!DATE_RE.test(ex.date)) {
      issues.push({ field: 'date_exceptions', message: `日期 ${ex.date} 格式应为 YYYY-MM-DD` })
      continue
    }
    if (seenDates.has(ex.date)) {
      issues.push({ field: 'date_exceptions', message: `日期 ${ex.date} 存在重复例外` })
    }
    seenDates.add(ex.date)
    if (ex.windows.length > 0) {
      for (const msg of validateDailyWindows(ex.windows)) {
        issues.push({ field: `date_exceptions.${ex.date}`, message: `${ex.date}：${msg}` })
      }
    }
  }

  return issues
}

/** 空窗口列表的语义：未设置任何窗口表示暂无可用时间（不是"每天全天可用"） */
export function totalWeeklyMinutes(windows: WeeklyWindow[]): number {
  return windows.reduce((sum, w) => {
    if (!HHMM_RE.test(w.start) || !HHMM_RE.test(w.end)) return sum
    return sum + Math.max(0, minutesFromHHmm(w.end) - minutesFromHHmm(w.start))
  }, 0)
}

export function dateExceptionFor(dateKey: string, exceptions: DateException[]): DateException | undefined {
  return exceptions.find((e) => e.date === dateKey)
}
