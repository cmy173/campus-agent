import type { Course } from '../types/contract'

/** Merge observations by platform identity without erasing richer earlier metadata. */
export function mergeDiscoveredCourses(rows: Course[]): Course[] {
  const byId = new Map<string, Course>()
  for (const course of rows) {
    const previous = byId.get(course.id)
    const names = [previous?.teacher, course.teacher].filter(Boolean).join('、').split(/[、,，;；]+/)
    const teacher = [...new Set(names.map(name => name.trim()).filter(Boolean))].join('、') || null
    byId.set(course.id, { ...course, teacher, term: course.term || previous?.term || null })
  }
  return [...byId.values()]
}
