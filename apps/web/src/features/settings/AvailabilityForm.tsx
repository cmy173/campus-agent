import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  DatePicker,
  Divider,
  Input,
  InputNumber,
  Radio,
  Space,
  Tag,
  TimePicker,
  Typography,
} from 'antd'
import dayjs from 'dayjs'
import type { Dayjs } from 'dayjs'
import { useEffect, useState } from 'react'
import { useSaveAvailability } from '../../lib/api/hooks'
import { toApiError } from '../../lib/api/http'
import { WEEKDAY_LABELS } from '../../lib/time'
import { totalWeeklyMinutes, validateAvailability } from '../../lib/validation'
import type { Availability, DateException } from '../../types/contract'

function hhmmToDayjs(value: string): Dayjs {
  const [h, m] = value.split(':').map(Number)
  return dayjs().hour(h).minute(m).second(0).millisecond(0)
}

interface TimeWindow {
  start: string
  end: string
}

function WindowEditor({
  windows,
  onChange,
  minimum = 0,
}: {
  windows: TimeWindow[]
  onChange: (windows: TimeWindow[]) => void
  minimum?: number
}) {
  return (
    <Space orientation="vertical" size={4}>
      {windows.map((w, i) => (
        <Space key={i} size={4}>
          <TimePicker
            aria-label="时段开始时间"
            format="HH:mm"
            minuteStep={5}
            allowClear={false}
            value={hhmmToDayjs(w.start)}
            onChange={(v) =>
              v && onChange(windows.map((x, j) => (j === i ? { ...x, start: v.format('HH:mm') } : x)))
            }
          />
          <Typography.Text>—</Typography.Text>
          <TimePicker
            aria-label="时段结束时间"
            format="HH:mm"
            minuteStep={5}
            allowClear={false}
            value={hhmmToDayjs(w.end)}
            onChange={(v) =>
              v && onChange(windows.map((x, j) => (j === i ? { ...x, end: v.format('HH:mm') } : x)))
            }
          />
          <Button
            type="text"
            size="small"
            aria-label="删除该时段"
            icon={<DeleteOutlined />}
            disabled={windows.length <= minimum}
            onClick={() => onChange(windows.filter((_, j) => j !== i))}
          />
        </Space>
      ))}
      <Button
        size="small"
        type="dashed"
        icon={<PlusOutlined />}
        onClick={() => onChange([...windows, { start: '19:00', end: '22:00' }])}
      >
        添加时段
      </Button>
    </Space>
  )
}

/** 可用时间编辑器：每周窗口 + 日期例外（空窗口=整天不可用）+ 每日上限 */
export function AvailabilityForm({ initial }: { initial: Availability }) {
  const { message } = AntApp.useApp()
  const save = useSaveAvailability()
  const [value, setValue] = useState<Availability>(() => structuredClone(initial))
  const [baseline, setBaseline] = useState<Availability>(() => structuredClone(initial))
  const dirty = JSON.stringify(value) !== JSON.stringify(baseline)

  useEffect(() => {
    if (dirty) return
    setValue(structuredClone(initial))
    setBaseline(structuredClone(initial))
  }, [initial, dirty])

  const issues = validateAvailability(value)
  const weeklyHours = (totalWeeklyMinutes(value.weekly_windows) / 60).toFixed(1)

  const setDayWindows = (
    weekday: number,
    updater: (list: TimeWindow[]) => TimeWindow[],
  ) => {
    setValue((prev) => {
      const list = prev.weekly_windows
        .filter((w) => w.weekday === weekday)
        .map(({ start, end }) => ({ start, end }))
      const next = updater(list).map((w) => ({ ...w, weekday }))
      const others = prev.weekly_windows.filter((w) => w.weekday !== weekday)
      return { ...prev, weekly_windows: [...others, ...next] }
    })
  }

  const updateException = (index: number, ex: DateException) => {
    setValue((prev) => ({
      ...prev,
      date_exceptions: prev.date_exceptions.map((e, i) => (i === index ? ex : e)),
    }))
  }

  const handleSave = () => {
    if (issues.length > 0) {
      message.error('存在校验问题，请修正后再保存')
      return
    }
    save.mutate(
      {
        timezone: value.timezone,
        weekly_windows: value.weekly_windows,
        date_exceptions: value.date_exceptions,
        daily_limit_minutes: value.daily_limit_minutes,
      },
      {
        onSuccess: (saved) => {
          setBaseline(structuredClone(saved))
          setValue(prev => JSON.stringify(prev) === JSON.stringify(value) ? structuredClone(saved) : prev)
          message.success('已保存；计划预览会基于新设置重新生成（旧预览将失效）')
        },
        onError: (err) => message.error(toApiError(err).message),
      },
    )
  }

  return (
    <Card
      title="可用时间"
      extra={
        <Space>
          {dirty && <Tag color="warning">有未保存修改</Tag>}
          <Button size="small" disabled={!dirty} onClick={() => setValue(structuredClone(baseline))}>
            撤销修改
          </Button>
          <Button
            size="small"
            type="primary"
            loading={save.isPending}
            disabled={!dirty || issues.length > 0}
            onClick={handleSave}
          >
            保存
          </Button>
        </Space>
      }
    >
      <Space orientation="vertical" size={12} style={{ width: '100%' }}>
        <Space size={8}>
          <Typography.Text type="secondary">时区：</Typography.Text>
          <Tag>{value.timezone}</Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            每周合计约 {weeklyHours} 小时；不设置时段的星期表示当天不可用
          </Typography.Text>
        </Space>

        {dirty && (
          <Alert
            type="warning"
            showIcon
            title="设置尚未保存"
            description="保存后输入修订号递增，已生成的计划预览会失效，需要重新生成后才能应用。"
          />
        )}

        {issues.length > 0 && (
          <Alert
            type="error"
            showIcon
            title="存在校验问题"
            description={
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {issues.slice(0, 5).map((issue) => (
                  <li key={`${issue.field}-${issue.message}`}>{issue.message}</li>
                ))}
                {issues.length > 5 && <li>…等 {issues.length} 项</li>}
              </ul>
            }
          />
        )}

        {[1, 2, 3, 4, 5, 6, 7].map((weekday) => {
          const windows = value.weekly_windows
            .filter((w) => w.weekday === weekday)
            .map(({ start, end }) => ({ start, end }))
          return (
            <Space key={weekday} align="start" size={16}>
              <Typography.Text style={{ width: 40, display: 'inline-block', paddingTop: 4 }}>
                {WEEKDAY_LABELS[weekday]}
              </Typography.Text>
              <WindowEditor windows={windows} onChange={(list) => setDayWindows(weekday, () => list)} />
            </Space>
          )
        })}

        <Divider style={{ margin: '8px 0' }} />

        <Space align="center" size={12}>
          <Typography.Text>每日学习上限</Typography.Text>
          <InputNumber
            aria-label="每日学习上限（分钟，留空表示不限制）"
            min={15}
            max={720}
            step={15}
            value={value.daily_limit_minutes}
            onChange={(v) => setValue((prev) => ({ ...prev, daily_limit_minutes: v ?? null }))}
          />
          <Typography.Text type="secondary">分钟（留空表示不限制；排程不会让单日超过该值）</Typography.Text>
        </Space>

        <Divider style={{ margin: '8px 0' }} />

        <Space orientation="vertical" size={8} style={{ width: '100%' }}>
          <Space>
            <Typography.Text>日期例外</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              例如运动会、请假；按日期覆盖当天的可用时间（不设置时段 = 整天不可用）
            </Typography.Text>
            <Button
              size="small"
              icon={<PlusOutlined />}
              onClick={() =>
                setValue((prev) => ({
                  ...prev,
                  date_exceptions: [
                    ...prev.date_exceptions,
                    { date: dayjs().add(1, 'day').format('YYYY-MM-DD'), windows: [], note: null },
                  ],
                }))
              }
            >
              添加例外
            </Button>
          </Space>

          {value.date_exceptions.length === 0 && (
            <Typography.Text type="secondary">暂无日期例外</Typography.Text>
          )}

          {value.date_exceptions.map((ex, index) => {
            const override = ex.windows.length > 0
            return (
              <Space key={`${ex.date}-${index}`} align="start" wrap size={8}>
                <DatePicker
                  aria-label="例外日期"
                  value={dayjs(ex.date)}
                  allowClear={false}
                  onChange={(v) => v && updateException(index, { ...ex, date: v.format('YYYY-MM-DD') })}
                />
                <Radio.Group
                  value={override}
                  onChange={(e) =>
                    updateException(index, {
                      ...ex,
                      windows: e.target.value ? [{ start: '09:00', end: '12:00' }] : [],
                    })
                  }
                >
                  <Radio.Button value={false}>整天不可用</Radio.Button>
                  <Radio.Button value={true}>覆盖时段</Radio.Button>
                </Radio.Group>
                {override && (
                  <WindowEditor
                    windows={ex.windows}
                    minimum={1}
                    onChange={(list) => updateException(index, { ...ex, windows: list })}
                  />
                )}
                <Input
                  aria-label="例外备注（可选）"
                  style={{ width: 160 }}
                  placeholder="备注（可选）"
                  value={ex.note ?? ''}
                  onChange={(e) => updateException(index, { ...ex, note: e.target.value || null })}
                />
                <Button
                  type="text"
                  size="small"
                  aria-label="删除该日期例外"
                  icon={<DeleteOutlined />}
                  onClick={() =>
                    setValue((prev) => ({
                      ...prev,
                      date_exceptions: prev.date_exceptions.filter((_, i) => i !== index),
                    }))
                  }
                />
              </Space>
            )
          })}
        </Space>

        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          无可用时间不猜测：未设置窗口的时间不会被安排任务；截止时间临近而时间不足时，计划会明确列出「无法安排」的任务而不是硬塞。
        </Typography.Text>
      </Space>
    </Card>
  )
}
