import { describe, expect, it } from 'vitest'
import { mergeDiscoveredCourses } from './courseDiscovery'
import type { Course } from '../types/contract'

describe('course discovery metadata', () => {
  it('keeps teachers and term from earlier pages and merges observed names', () => {
    const base = { id: 'course', title: '光学', term: null, teacher: null, source_refs: [], archived_at: null, created_at: '', updated_at: '' } satisfies Course
    const rows = mergeDiscoveredCourses([
      { ...base, teacher: '王老师', term: '2026-20271' },
      { ...base, teacher: '李老师' }, base,
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].teacher).toBe('王老师、李老师')
    expect(rows[0].term).toBe('2026-20271')
  })
})
