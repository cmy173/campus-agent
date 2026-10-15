"""Own one bounded diagnostic subprocess; do not retain stdin or remote stderr."""
import json
import os
import shutil
import subprocess

from ..config import REPO_ROOT
from ..errors import ApiError


def check(payload: dict) -> dict:
    node = shutil.which('node')
    if not node:
        raise ApiError(503, 'DEPENDENCY_UNAVAILABLE', '请安装 Agent 运行时依赖后重试')
    env = {k: os.environ[k] for k in ('PATH', 'PATHEXT', 'SystemRoot', 'ComSpec', 'USERPROFILE', 'TEMP', 'TMP',
           'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
           'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy') if k in os.environ}
    process = subprocess.Popen([node, str(REPO_ROOT / 'apps/agent-runtime/diagnose.mjs')],
        cwd=REPO_ROOT / 'apps/agent-runtime', env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        stdout, _ = process.communicate(json.dumps(payload), timeout=30)
        if process.returncode or len(stdout) > 100000:
            raise ValueError('invalid diagnostic response')
        result = json.loads(stdout)
        if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
            raise ValueError('invalid diagnostic response')
        return result
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10)
        else:
            process.kill()
        process.communicate(timeout=5)
        return {'ok': False, 'error': {'code': 'CONNECTION_TIMEOUT', 'message': '连接测试超时，请检查服务状态。', 'retryable': True}}
    except (ValueError, OSError):
        raise ApiError(502, 'DIAGNOSTIC_FAILED', '运行时连接测试未完成，请检查 Agent 依赖') from None
