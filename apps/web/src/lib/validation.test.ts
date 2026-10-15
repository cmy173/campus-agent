import { describe, expect, it } from 'vitest'
import type { Availability } from '../types/contract'
import { dateExceptionFor, totalWeeklyMinutes, validateAvailability, validateDailyWindows } from './validation'

function baseAvailability(overrides: Partial<Availability> = {}): Availability {
  return {
    timezone: 'Asia/Shanghai',
    weekly_windows: [],
    date_exceptions: [],
    daily_limit_minutes: 180,
    updated_at: '2026-10-09T00:00:00+08:00',
    ...overrides,
  }
}

describe('validateDailyWindows', () => {
  it('接受合法窗口', () => {
    expect(validateDailyWindows([{ start: '19:00', end: '22:00' }])).toEqual([])
  })

  it('拒绝结束时间不晚于开始时间', () => {
    const errors = validateDailyWindows([{ start: '22:00', end: '19:00' }])
    expect(errors.join('')).toContain('结束时间必须晚于开始时间')
  })

  it('拒绝格式错误的时间', () => {
    const errors = validateDailyWindows([{ start: '9:00', end: '22:00' }])
    expect(errors.join('')).toContain('HH:mm')
  })

  it('拒绝重叠窗口', () => {
    const errors = validateDailyWindows([
      { start: '19:00', end: '22:00' },
      { start: '21:00', end: '23:00' },
    ])
    expect(errors.join('')).toContain('重叠')
  })

  it('相邻但不重叠的窗口是合法的', () => {
    expect(
      validateDailyWindows([
        { start: '09:00', end: '12:00' },
        { start: '12:00', end: '14:00' },
      ]),
    ).toEqual([])
  })
})

describe('validateAvailability', () => {
  it('每日上限超出范围时报错', () => {
    const issues = validateAvailability(baseAvailability({ daily_limit_minutes: 10 }))
    expect(issues.some((i) => i.field === 'daily_limit_minutes')).toBe(true)
    const issues2 = validateAvailability(baseAvailability({ daily_limit_minutes: 800 }))
    expect(issues2.some((i) => i.field === 'daily_limit_minutes')).toBe(true)
  })

  it('每日上限留空表示不限制，不报错', () => {
    expect(validateAvailability(baseAvailability({ daily_limit_minutes: null }))).toEqual([])
  })

  it('缺少时区报错', () => {
    const issues = validateAvailability(baseAvailability({ timezone: '' }))
    expect(issues.some((i) => i.field === 'timezone')).toBe(true)
  })

  it('日期例外重复时报错', () => {
    const issues = validateAvailability(
      baseAvailability({
        date_exceptions: [
          { date: '2026-10-10', windows: [], note: null },
          { date: '2026-10-10', windows: [{ start: '09:00', end: '12:00' }], note: null },
        ],
      }),
    )
    expect(issues.some((i) => i.message.includes('重复'))).toBe(true)
  })

  it('空窗口列表表示整天不可用，是合法例外', () => {
    expect(
      validateAvailability(
        baseAvailability({
          date_exceptions: [{ date: '2026-10-11', windows: [], note: '校运动会（全天不可用）' }],
        }),
      ),
    ).toEqual([])
  })

  it('日期例外的覆盖窗口同样做重叠校验', () => {
    const issues = validateAvailability(
      baseAvailability({
        date_exceptions: [
          {
            date: '2026-10-10',
            windows: [
              { start: '09:00', end: '12:00' },
              { start: '11:00', end: '13:00' },
            ],
            note: null,
          },
        ],
      }),
    )
    expect(issues.some((i) => i.field === 'date_exceptions.2026-10-10')).toBe(true)
  })
})

describe('totalWeeklyMinutes / dateExceptionFor', () => {
  it('统计每周总分钟数，非法格式跳过', () => {
    expect(
      totalWeeklyMinutes([
        { weekday: 1, start: '19:00', end: '22:00' },
        { weekday: 6, start: '09:00', end: '12:00' },
      ]),
    ).toBe(360)
  })

  it('按日期查找例外，未命中返回 undefined', () => {
    const exceptions = [{ date: '2026-10-11', windows: [], note: '校运动会' }]
    expect(dateExceptionFor('2026-10-11', exceptions)?.note).toBe('校运动会')
    expect(dateExceptionFor('2026-10-12', exceptions)).toBeUndefined()
  })
})
