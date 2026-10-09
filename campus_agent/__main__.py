import argparse
import json
from .core import Agent

parser = argparse.ArgumentParser(description='Campus Agent 离线基础原型（虚构数据）')
parser.add_argument('query', nargs='?', default='查看通知')
args = parser.parse_args()
print(json.dumps(Agent().run(args.query), ensure_ascii=False, indent=2, default=str))
