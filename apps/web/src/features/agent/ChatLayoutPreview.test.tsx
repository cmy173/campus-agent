import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { ChatLayoutPreview } from './ChatLayoutPreview'

describe('presentation-only replacement example', () => {
  it('keeps the same answer, tool result and editable draft through layout/token changes', async () => {
    const user = userEvent.setup()
    const view = render(<ChatLayoutPreview />)
    const input = screen.getByLabelText('示例草稿')
    await user.clear(input)
    await user.type(input, '保持此草稿')
    await user.click(screen.getByText('侧栏布局'))
    await user.click(screen.getByText('高对比主题'))
    expect(view.container.querySelector('.agent-page')).toHaveAttribute('data-layout', 'studio')
    expect(view.container.querySelector('.agent-page')).toHaveAttribute('data-theme', 'contrast')
    expect(screen.getByLabelText('示例草稿')).toHaveValue('保持此草稿')
    expect(screen.getByText(/实验报告需要附原始数据/)).toBeInTheDocument()
    await user.click(screen.getByText('工具执行记录'))
    expect(screen.getByText('读取文件')).toBeInTheDocument()
    expect(screen.getByText('已完成')).toBeInTheDocument()
    await user.click(screen.getByText('查看输入与结果'))
    expect(screen.getByText('实验报告附原始数据；截止时间未给出。')).toBeInTheDocument()
  })
})
