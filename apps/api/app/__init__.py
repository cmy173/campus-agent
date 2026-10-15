"""浙大学习工作台后端（FastAPI）。"""
from .config import ensure_repo_root_on_path

# 领域包（connectors.campus.*）以仓库根为导入根，须在子模块导入前生效。
ensure_repo_root_on_path()
