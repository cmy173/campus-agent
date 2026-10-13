"""Optional notice extraction providers, separate from the campus platform adapters.

Only the API key and model name come from the server environment. Endpoints are fixed
constants: neither the browser, the notice text nor the model can choose a request target.
Campus network policy (CAMPUS_NETWORK_POLICY / CAMPUS_PUBLIC_NETWORK_POLICY) does not apply
here; provider requests use the process default network stack.
"""
import http.client
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

from .notice_contract import PROMPT_VERSION, SYSTEM_PROMPT

ENDPOINTS = {'deepseek': 'https://api.deepseek.com/chat/completions'}
DEFAULT_MODELS = {'deepseek': 'deepseek-flash'}
PROVIDER_LABELS = {'deepseek': 'DeepSeek'}
MODEL_PATTERN = re.compile(r'[A-Za-z0-9._:-]{1,64}')
MAX_RESPONSE_BYTES = 1_000_000
MAX_OUTPUT_TOKENS = 4_000
TIMEOUT_SECONDS = 90
# Sent to the provider with every extraction request, so users can see what leaves the machine.
DISCLOSED_INPUT = '通知标题、正文段落、发布日期、学院名称、平台记录 ID 与附件名称'

MESSAGES = {
    'NOT_CONFIGURED': '未配置模型：请在启动服务的终端设置 DEEPSEEK_API_KEY 后重启',
    'CONFIG_INVALID': '模型配置无效：DEEPSEEK_MODEL 只能包含字母、数字与 . _ : -',
    'UNKNOWN_PROVIDER': '未登记的模型提供方',
    'TIMEOUT': '模型请求超时，本次未产生结果，可稍后重试',
    'RATE_LIMITED': '模型提供方限流，请稍后重试',
    'AUTH_REJECTED': '模型提供方拒绝了密钥，请核对配置、权限与额度',
    'UPSTREAM': '模型提供方返回错误，本次未产生结果',
    'NETWORK': '无法连接模型提供方，请检查本机网络',
    'REDIRECT': '模型端点发生跳转，已拒绝请求以避免凭据外泄',
    'EMPTY': '模型返回空内容，本次未产生结果，可重试',
    'TRUNCATED': '模型输出被截断，请缩短通知正文或提高输出上限后重试',
    'RESPONSE_TOO_LARGE': '模型响应超出读取上限，已拒绝该结果',
}


class ModelFailure(Exception):
    """Carries a fixed public message; never the response body, headers or credentials."""

    def __init__(self, code, detail=''):
        message = MESSAGES.get(code, MESSAGES['UPSTREAM'])
        super().__init__(message + (f'（{detail}）' if detail else ''))
        self.code, self.message = code, message + (f'（{detail}）' if detail else '')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def provider_name():
    value = (os.environ.get('NOTICE_MODEL_PROVIDER') or 'deepseek').strip().lower()
    return value if value in ENDPOINTS else 'deepseek'


def configuration(provider=None, runtime_key=None):
    """Provider status for the browser. Contains no key and no caller-supplied endpoint.
    
    Args:
        provider: Provider name (deepseek, etc.)
        runtime_key: Optional API key from runtime configuration (UI input)
    """
    provider = provider or provider_name()
    if provider not in ENDPOINTS:
        raise ModelFailure('UNKNOWN_PROVIDER')
    # Check both key first, then environment variable
    key = (runtime_key or os.environ.get('DEEPSEEK_API_KEY') or '').strip()
    model = (os.environ.get('DEEPSEEK_MODEL') or DEFAULT_MODELS[provider]).strip()
    valid = bool(MODEL_PATTERN.fullmatch(model))
    return {'configured': bool(key) and valid, 'provider': provider,
            'provider_label': PROVIDER_LABELS[provider], 'model': model if valid else '',
            'endpoint': ENDPOINTS[provider], 'prompt_version': PROMPT_VERSION,
            'consent_required': True, 'sends': DISCLOSED_INPUT,
            'error': None if valid or not key else MESSAGES['CONFIG_INVALID']}


def _usage(body, latency_ms):
    usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
    result = {'latency_ms': latency_ms}
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        value = usage.get(key)
        if isinstance(value, int) and 0 <= value < 100_000_000:
            result[key] = value
    return result


class DeepSeekExtractor:
    """Chat Completions with response_format=json_object, the provider's own JSON protocol.

    DeepSeek has no strict json_schema mode, so the contract is stated in the prompt and
    enforced afterwards by notice_contract.validate.
    """

    id = 'deepseek'

    def __init__(self, model, key, timeout=TIMEOUT_SECONDS, max_tokens=MAX_OUTPUT_TOKENS, opener=None):
        self.model, self.key, self.timeout, self.max_tokens = model, key, timeout, max_tokens
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.endpoint = ENDPOINTS['deepseek']

    def extract(self, payload):
        body = {'model': self.model, 'stream': False, 'max_tokens': self.max_tokens,
                'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': SYSTEM_PROMPT},
                             {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]}
        request = urllib.request.Request(self.endpoint, data=json.dumps(body).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json',
                     'Accept': 'application/json', 'User-Agent': 'CampusAgent/0.3'})
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.geturl() != self.endpoint:
                    raise ModelFailure('REDIRECT')
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # The error body may echo request details; classify by status code only.
            code = {401: 'AUTH_REJECTED', 402: 'AUTH_REJECTED', 403: 'AUTH_REJECTED',
                    429: 'RATE_LIMITED'}.get(exc.code, 'UPSTREAM')
            raise ModelFailure(code, f'HTTP {exc.code}') from None
        except (TimeoutError, socket.timeout):
            raise ModelFailure('TIMEOUT') from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, EOFError) as exc:
            if isinstance(getattr(exc, 'reason', exc), (TimeoutError, socket.timeout)):
                raise ModelFailure('TIMEOUT') from None
            raise ModelFailure('NETWORK') from None
        latency_ms = round((time.monotonic() - started) * 1000)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelFailure('RESPONSE_TOO_LARGE')
        try:
            response_body = json.loads(raw)
            choices = response_body['choices']
            choice = choices[0]
            content = choice['message']['content']
            finish = choice.get('finish_reason')
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            raise ModelFailure('UPSTREAM', '响应结构无法确认') from None
        if finish == 'length':
            raise ModelFailure('TRUNCATED')
        if not isinstance(content, str) or not content.strip():
            # Documented json_object behaviour: content can occasionally be empty.
            raise ModelFailure('EMPTY')
        return {'content': content, 'finish_reason': finish or 'stop',
                'usage': _usage(response_body, latency_ms)}


def extractor(provider=None, runtime_key=None):
    """Build the configured extractor or fail with a fixed, key-free message.
    
    Args:
        provider: Provider name (deepseek, etc.)
        runtime_key: Optional API key from runtime configuration (UI input)
    """
    config = configuration(provider)
    if config['provider'] not in ENDPOINTS:
        raise ModelFailure('UNKNOWN_PROVIDER')
    
    # Prefer runtime key if provided, otherwise use environment variable
    key = (runtime_key or os.environ.get('DEEPSEEK_API_KEY') or '').strip()
    
    if not key:
        raise ModelFailure('NOT_CONFIGURED')
    if not config['model']:
        raise ModelFailure('CONFIG_INVALID')
    return DeepSeekExtractor(config['model'], key)
