import dayjs from 'dayjs'
import type { Dayjs } from 'dayjs'
import utc from 'dayjs/plugin/utc'
import timezone from 'dayjs/plugin/timezone'
import isoWeek from 'dayjs/plugin/isoWeek'
import 'dayjs/locale/zh-cn'

dayjs.extend(utc)
dayjs.extend(timezone)
dayjs.extend(isoWeek)
dayjs.locale('zh-cn')

export const DEFAULT_TIMEZONE = 'Asia/Shanghai'

export const WEEKDAY_LABELS: Record<number, string> = {
  1: '周一',
  2: '周二',
  3: '周三',
  4: '周四',
  5: '周五',
  6: '周六',
  7: '周日',
}

/** 生成带偏移量的 ISO 8601 字符串（保存给后端用） */
export function toApiIso(value: Dayjs, tz: string = DEFAULT_TIMEZONE): string {
  return value.tz(tz).format('YYYY-MM-DDTHH:mm:ssZ')
}

export function nowIso(tz: string = DEFAULT_TIMEZONE): string {
  return toApiIso(dayjs(), tz)
}

/** 解析后端 ISO 字符串为 Dayjs（保留绝对时刻） */
export function parseIso(iso: string): Dayjs {
  return dayjs(iso)
}

/** 按用户时区格式化时间 */
export function fmtDateTime(
  iso: string | null | undefined,
  tz: string = DEFAULT_TIMEZONE,
  pattern = 'YYYY-MM-DD HH:mm',
): string {
  if (!iso) return '—'
  return dayjs(iso).tz(tz).format(pattern)
}

export function fmtDate(
  iso: string | null | undefined,
  tz: string = DEFAULT_TIMEZONE,
): string {
  return fmtDateTime(iso, tz, 'YYYY年M月D日')
}

export function fmtDayLabel(iso: string, tz: string = DEFAULT_TIMEZONE): string {
  return dayjs(iso).tz(tz).format('M月D日 ddd')
}

export function fmtTime(iso: string, tz: string = DEFAULT_TIMEZONE): string {
  return dayjs(iso).tz(tz).format('HH:mm')
}

export function fmtTimeRange(
  startIso: string,
  endIso: string,
  tz: string = DEFAULT_TIMEZONE,
): string {
  return `${fmtTime(startIso, tz)}–${fmtTime(endIso, tz)}`
}

/** 秒数 → 00:12:30 / 12:30 */
export function fmtDurationSeconds(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const mm = String(m).padStart(2, '0')
  const ss = String(sec).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`
}

/** 分钟数 → 1 小时 30 分钟 */
export function fmtMinutes(minutes: number): string {
  if (minutes < 60) return `${minutes} 分钟`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m === 0 ? `${h} 小时` : `${h} 小时 ${m} 分钟`
}

export function addMinutesIso(iso: string, minutes: number, tz: string = DEFAULT_TIMEZONE): string {
  return toApiIso(dayjs(iso).add(minutes, 'minute'), tz)
}

export function dayKey(iso: string, tz: string = DEFAULT_TIMEZONE): string {
  return dayjs(iso).tz(tz).format('YYYY-MM-DD')
}

export function todayKey(tz: string = DEFAULT_TIMEZONE): string {
  return dayjs().tz(tz).format('YYYY-MM-DD')
}

export function sameDayInTz(aIso: string, bIso: string, tz: string = DEFAULT_TIMEZONE): boolean {
  return dayKey(aIso, tz) === dayKey(bIso, tz)
}

/** 数据新鲜度：最近成功同步超过该阈值即视为过期 */
export const STALE_THRESHOLD_HOURS = 24

export function isStale(
  lastSuccessIso: string | null,
  now: Dayjs = dayjs(),
  thresholdHours: number = STALE_THRESHOLD_HOURS,
): boolean {
  if (!lastSuccessIso) return true
  return now.diff(dayjs(lastSuccessIso), 'hour', true) > thresholdHours
}

export function fmtRelativeToNow(
  iso: string,
  tz: string = DEFAULT_TIMEZONE,
  now: Dayjs = dayjs(),
): string {
  const target = dayjs(iso)
  const diffMinutes = Math.round(target.diff(now, 'minute', true))
  const abs = Math.abs(diffMinutes)
  const suffix = diffMinutes >= 0 ? '后' : '前'
  if (abs < 1) return '现在'
  if (abs < 60) return `${abs} 分钟${suffix}`
  if (abs < 60 * 48) return `${Math.round(abs / 60)} 小时${suffix}`
  return fmtDateTime(iso, tz, 'M月D日 HH:mm')
}

export interface WeekDay {
  key: string
  label: string
  isoDate: string
  isToday: boolean
}

/** 以 anchor 所在周的周一为首，返回 7 天 */
export function weekDays(anchorIso: string, tz: string = DEFAULT_TIMEZONE): WeekDay[] {
  const anchor = dayjs(anchorIso).tz(tz)
  const monday = anchor.startOf('isoWeek')
  const today = todayKey(tz)
  return Array.from({ length: 7 }, (_, i) => {
    const d = monday.add(i, 'day')
    const key = d.format('YYYY-MM-DD')
    return {
      key,
      label: `${d.format('M/D')} ${WEEKDAY_LABELS[d.isoWeekday()]}`,
      isoDate: key,
      isToday: key === today,
    }
  })
}

export function minutesFromHHmm(value: string): number {
  const [h, m] = value.split(':').map(Number)
  return h * 60 + m
}
