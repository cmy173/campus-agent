import { Button, Result } from 'antd'
import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <Result
      status="404"
      title="页面不存在"
      subTitle="你访问的页面地址无效或已被移动。"
      extra={
        <Link to="/today">
          <Button type="primary">回到今日</Button>
        </Link>
      }
    />
  )
}
