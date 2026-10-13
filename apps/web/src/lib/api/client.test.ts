import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './client'
import { getDb, resetMockDb } from './mock/db'

afterEach(() => vi.restoreAllMocks())

describe('完整任务列表', () => {
  it('跨页取完、去重，并保留原始任务名称和归档请求参数', async () => {
    resetMockDb()
    const a = { ...getDb().tasks[0], id: 'task_first', title: '第一项' }
    const b = { ...a, id: 'task_tail', title: '第 105 项的中文名称' }
    const get = vi.spyOn(api, 'getTasks')
      .mockResolvedValueOnce({ items: [a], next_cursor: 'page_2' })
      .mockResolvedValueOnce({ items: [a, b], next_cursor: null })
    const result = await api.getAllTasks({ include_archived: true })
    expect(result.items.map(t => t.title)).toEqual(['第一项', '第 105 项的中文名称'])
    expect(get).toHaveBeenLastCalledWith({ include_archived: true, cursor: 'page_2', limit: 200 })
    expect(result.next_cursor).toBeNull()
  })
  it('重复游标报错，不把残缺数据当作完整任务列表', async () => {
    vi.spyOn(api, 'getTasks').mockResolvedValue({ items: [], next_cursor: 'same' })
    await expect(api.getAllTasks()).rejects.toThrow('分页异常')
  })
})
