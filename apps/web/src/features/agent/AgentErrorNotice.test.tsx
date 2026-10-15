import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it } from 'vitest'
import { AgentErrorNotice } from './AgentErrorNotice'
import { ApiError } from '../../lib/api/http'

it('shows recovery language with request diagnostics collapsed by default', async () => {
  const user = userEvent.setup()
  render(<AgentErrorNotice error={new ApiError(0, { code: 'NETWORK_ERROR', message: 'socket closed', retryable: true, request_id: 'request_marker' })} />)
  expect(screen.getByText('连接暂时中断')).toBeInTheDocument()
  const details = screen.getByText('查看错误详情').closest('details')!
  expect(details).not.toHaveAttribute('open')
  expect(details).toHaveTextContent('request_marker')
  await user.click(screen.getByText('查看错误详情'))
  expect(details).toHaveAttribute('open')
})

it('does not put a stack trace in the alert title or interpret it as HTML', () => {
  render(<AgentErrorNotice error={{ code: 'RUNTIME_FAILED', message: 'Traceback\n<script>unsafe</script>\nC:\\private\\session' }} />)
  expect(screen.getByText('本次操作未完成')).toBeInTheDocument()
  expect(screen.getByText('查看错误详情').closest('details')).not.toHaveAttribute('open')
  expect(document.querySelector('script')).toBeNull()
})
