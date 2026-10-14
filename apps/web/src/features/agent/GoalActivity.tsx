import { Button, Space, Tag, Typography } from 'antd'
import type { AgentEvent, AgentTurn, PersistentGoal } from './api'

/** Render the native Goal view, independently of transport and tool names. */
export function GoalActivity({ events, turn, onPause, pausing, onResume, resuming }: { events: AgentEvent[]; turn: AgentTurn;
  onPause: (goal: PersistentGoal) => void; pausing: boolean; onResume: (goal: PersistentGoal) => void; resuming: boolean }) {
  const value = events.findLast(event => event.kind === 'goal.state')?.data.goal
  if (!value || typeof value !== 'object') return null
  const goal = value as PersistentGoal
  const live = turn.status === 'running'
  const continuing = live && goal.phase === 'active' && goal.activation === 'armed'
  const roundLimit = goal.blockedReason?.code === 'round-limit' || (goal.phase === 'active' && goal.roundsStarted >= goal.maxGoalRounds)
  const status = roundLimit ? '已达到续行上限' : goal.phase === 'complete' ? '已完成' : goal.phase === 'paused' ?
    (['queued', 'running', 'stopping'].includes(turn.status) ? '暂停中，正在结束工具' : '已暂停') :
    goal.phase === 'blocked' ? '需要帮助' : continuing ? '持续推进中' :
    goal.roundsStarted >= goal.maxGoalRounds ? '已达到续行上限' : '已保存，等待恢复'
  return <section className="agent-fact" aria-label="持续任务">
    <Space wrap><Typography.Text strong>持续任务</Typography.Text><Tag>{status}</Tag>
      {continuing && <Button size="small" loading={pausing} disabled={pausing} onClick={() => onPause(goal)}>暂停持续任务</Button>}
      {!['queued', 'running', 'stopping'].includes(turn.status) && goal.phase !== 'complete' &&
        <Button size="small" loading={resuming} disabled={resuming} onClick={() => onResume(goal)}>
          {roundLimit ? '继续持续任务（新增64轮）' : '继续持续任务'}
        </Button>}
    </Space>
    <Typography.Paragraph style={{ marginBottom: 4, whiteSpace: 'pre-wrap' }}>{goal.objective}</Typography.Paragraph>
    <Typography.Text type="secondary">已续行 {goal.roundsStarted} / {goal.maxGoalRounds} 轮</Typography.Text>
    {goal.blockedReason && <Typography.Paragraph>{roundLimit ? '目标尚未完成；调整续行上限后可以要求继续。' : goal.blockedReason.message}</Typography.Paragraph>}
    {goal.phase === 'active' && !continuing && <Typography.Paragraph type="secondary">
      目标尚未完成。可点击继续持续任务；重启或停止后不会自行重新执行。
    </Typography.Paragraph>}
  </section>
}
