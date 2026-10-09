from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Protocol
import re

TZ = timezone(timedelta(hours=8))

@dataclass(frozen=True)
class Source:
    id: str
    title: str
    text: str
    updated_at: str
    origin: str = 'mock://zju-demo'

@dataclass(frozen=True)
class Activity:
    id: str
    title: str
    start: datetime
    end: datetime
    deadline: datetime
    credits: float
    seats: int
    category: str = 'demo'

class Connector(Protocol):
    def capabilities(self) -> dict: ...
    def notices(self) -> list[Source]: ...
    def activities(self, now: datetime) -> list[Activity]: ...

class MockConnector:
    def capabilities(self):
        return dict(id='zju-demo', operations=['notices.read', 'activities.read'],
                    permissions=['demo.read'], writable=False, auth='none',
                    sync='snapshot', errors=['unsupported_operation'], availability='mock')

    def notices(self):
        return [Source('demo-1', '课程作业（虚构）',
            '提交实验报告。截止：2026-10-15 23:59。地点：课程平台。',
            '2026-10-08T10:00:00+08:00')]

    def activities(self, now):
        base = (now + timedelta(days=2)).replace(hour=14, minute=0, second=0, microsecond=0)
        return [Activity('a1', '科创分享（虚构）', base, base+timedelta(hours=2),
                         base-timedelta(hours=12), 1, 20),
                Activity('a2', '志愿交流（虚构）', base+timedelta(days=1),
                         base+timedelta(days=1, hours=2), base, 1, 10)]

def extract(source: Source) -> dict:
    """Only extract explicitly labelled dates; never infer an absent deadline."""
    match = re.search(r'截止[：:]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})', source.text)
    deadline = None
    if match:
        try:
            deadline = datetime.strptime(match[1], '%Y-%m-%d %H:%M').replace(tzinfo=TZ).isoformat()
        except ValueError:
            pass
    return dict(summary=source.text[:160], deadline=deadline,
                task=source.text.split('。')[0] if '提交' in source.text else None,
                method='规则摘录，非模型摘要', source=asdict(source))

def overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]

def recommend(activities, now, target=2.0, busy=(), category='demo'):
    if target < 0:
        raise ValueError('target must be non-negative')
    selected, total = [], 0.0
    for item in sorted(activities, key=lambda a: (a.start, -a.credits, a.id)):
        if total >= target:
            break
        slot = (item.start, item.end)
        if (item.start <= now or item.deadline <= now or item.seats <= 0
                or item.credits <= 0 or item.end <= item.start or item.category != category):
            continue
        if any(overlaps(slot, b) for b in busy):
            continue
        if any(overlaps(slot, (a.start, a.end)) for a in selected):
            continue
        selected.append(item)
        total += item.credits
    return dict(activities=[asdict(a) for a in selected], credits=total,
                remaining=max(0, target-total),
                reason='按开始时间优先，排除截止、满员和时间冲突；贪心方案不保证全局最优。',
                caveat='全部为演示数据，学分不可视为浙江大学认定结果。')

class Agent:
    def __init__(self, connector: Connector | None = None):
        self.connector = connector or MockConnector()

    def run(self, query, now=None):
        now = now or datetime.now(TZ)
        if any(word in query for word in ('报名', '提交', '登录', '申请')):
            return dict(status='unsupported', answer='当前原型仅支持只读查询，不执行校园事务。', steps=[])
        if any(word in query for word in ('活动', '学分')):
            match = re.search(r'(\d+(?:\.\d+)?)\s*(?:个)?学分', query)
            result = recommend(self.connector.activities(now), now, float(match[1]) if match else 2)
            return dict(status='ok', tool='activities.read', steps=['意图识别', '模拟数据读取', '规则筛选'], result=result)
        if any(word in query for word in ('通知', '作业', '简报')):
            return dict(status='ok', tool='notices.read', steps=['意图识别', '模拟数据读取', '规则摘录'],
                        result=[extract(s) for s in self.connector.notices()])
        return dict(status='unsupported', answer='可尝试：查看通知；推荐能获得2个学分的活动。当前没有接入大模型。', steps=[])
