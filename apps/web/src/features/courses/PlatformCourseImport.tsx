import { useState } from 'react'
import { Alert, App, Button, Collapse, Form, Input, Modal, Select, Space, Table, Typography } from 'antd'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../lib/api/client'
import { toApiError } from '../../lib/api/http'
import type { Connection, Course } from '../../types/contract'
import { mergeDiscoveredCourses } from '../../lib/courseDiscovery'

export function PlatformCourseImport({ connection }: { connection: Connection }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [courses, setCourses] = useState<Course[]>([])
  const [selected, setSelected] = useState<React.Key[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [scope, setScope] = useState('current')
  const [warnings, setWarnings] = useState<string[]>([])
  const [form] = Form.useForm<{ external_id: string; title: string; term?: string }>()
  const queryClient = useQueryClient()
  const { message } = App.useApp()

  async function discover(range = scope) {
    setBusy(true); setError(null); setWarnings([]); setCourses([]); setSelected([]); setLoaded(false)
    try {
      const rows: Course[] = []
      const issues: string[] = []
      const seen = new Set<string>()
      let cursor: string | undefined
      let more = true
      do {
        const page = connection.platform === 'zykt'
          ? await api.discoverCourses(connection.id, cursor, range)
          : await api.discoverCourses(connection.id, cursor)
        rows.push(...page.items)
        issues.push(...(page.errors ?? []).map(e => e.message))
        setCourses(mergeDiscoveredCourses(rows))
        if (!page.next_cursor) break
        if (seen.has(page.next_cursor) || seen.size >= 100) throw new Error('平台分页异常，请稍后再试')
        seen.add(page.next_cursor); cursor = page.next_cursor
        more = Boolean(cursor)
      } while (more)
      setCourses(mergeDiscoveredCourses(rows))
      setWarnings([...new Set(issues)])
      setLoaded(true); setSelected([])
    } catch (e) {
      setError(toApiError(e).message)
      void queryClient.invalidateQueries({ queryKey: ['connections'] })
    } finally { setBusy(false) }
  }

  async function importCourses(manual = false) {
    let input
    if (manual) {
      try {
        const values = await form.validateFields()
        input = [{ ...values, external_id: values.external_id.trim(), title: values.title.trim() }]
      } catch { return }
    } else {
      input = courses.filter(c => selected.includes(c.id)).map(c => ({
        title: c.title, term: c.term, teacher: c.teacher,
        external_id: c.source_refs.find(r => r.platform === connection.platform)?.external_id ?? c.id,
      }))
      if (!input.length) return
    }
    setBusy(true); setError(null)
    try {
      await api.importPlatformCourses(connection.id, input)
      await queryClient.invalidateQueries({ queryKey: ['courses'] })
      message.success('课程已导入并挂接来源；请到同步中心同步资料')
      setOpen(false); if (manual) form.resetFields()
    } catch (e) { setError(toApiError(e).message) }
    finally { setBusy(false) }
  }

  return <>
    <Button disabled={connection.status === 'disabled' || connection.status === 'unsupported'} onClick={() => {
      setOpen(true); setError(null); setLoaded(false); setCourses([]); setSelected([]); setWarnings([])
      if (connection.platform === 'zykt') void discover()
    }}>导入{connection.display_name}课程</Button>
    <Modal title={`导入${connection.display_name}课程`} open={open} footer={null} width={760}
      closable={!busy} mask={{ closable: !busy }} onCancel={() => !busy && setOpen(false)}>
      <Space orientation="vertical" style={{ width: '100%' }} size={16}>
        {error && <Alert type="error" showIcon title={error} />}
        {warnings.length > 0 && <Alert type="warning" showIcon title="部分课程读取失败，成功读取的课程仍可导入" description={warnings.join('；')} />}
        {(connection.platform === 'xzzd' || connection.platform === 'zykt') && <>
          <Typography.Text>先在本地连接器窗口登录，再读取自己的平台课程。重复导入会复用已有课程。</Typography.Text>
          {connection.platform === 'zykt' && <>
            <Select value={scope} disabled={busy} style={{ width: '100%' }} onChange={value => { setScope(value); void discover(value) }}
              options={[{ value: 'current', label: '本学期已开课课程（个人课表）' }, { value: 'all', label: '全部课程（含历史学习记录与收藏）' }]} />
            <Typography.Text type="secondary">自动读取个人月课表，不需要输入课号。默认按每年 2 月／9 月划分学期时间范围；学校未返回学期时显示“未提供”。找不到课程可切换全部课程；观看时间不用于认定学期。</Typography.Text>
          </>}
          <Button loading={busy} onClick={() => void discover()}>读取平台课程</Button>
          {(loaded || courses.length > 0) && <>
            <Table<Course> rowKey="id" size="small" dataSource={courses} pagination={{ pageSize: 8 }}
              rowSelection={{ selectedRowKeys: selected, onChange: keys => setSelected(keys), getCheckboxProps: () => ({ disabled: busy }) }}
              columns={[{ title: '课程', dataIndex: 'title' }, { title: '学期', dataIndex: 'term', render: value => value || '未提供' }, { title: '教师', dataIndex: 'teacher' }]} />
            <Button type="primary" disabled={!selected.length} loading={busy} onClick={() => void importCourses()}>导入所选课程</Button>
          </>}
        </>}
        <Collapse items={[{ key: 'manual', label: '高级选项：按平台课号补充课程', children: <>
        <Typography.Text type="secondary">智云课堂课号取自播放页的 course_id 参数；学在浙大取自课程地址中的数字课号。</Typography.Text>
        <Form form={form} layout="vertical">
          <Form.Item name="external_id" label="平台课号" rules={[{ required: true, whitespace: true, message: '请填写课号' }, { pattern: /^\S+$/, message: '填写课号，不要包含空白' }]}><Input placeholder="例如 12345" /></Form.Item>
          <Form.Item name="title" label="课程名称" rules={[{ required: true, whitespace: true, message: '请填写课程名称' }]}><Input /></Form.Item>
          <Form.Item name="term" label="学期（可选）"><Input /></Form.Item>
          <Button loading={busy} onClick={() => void importCourses(true)}>按课号导入</Button>
        </Form>
        </> }]} />
      </Space>
    </Modal>
  </>
}
