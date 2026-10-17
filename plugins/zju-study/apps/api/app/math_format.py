"""仅用于显示/导出：兼容旧卷的数学标记，保留存储原文及普通代码。"""
import re

_WORDS = set(('sin cos tan cot sec csc sinh cosh tanh exp log ln lim sum int frac sqrt bar overline '
              'vec hat dot ddot alpha beta gamma delta epsilon theta lambda mu nu pi rho sigma tau phi psi omega '
              'Gamma Delta Theta Lambda Pi Sigma Phi Psi Omega left right infty partial nabla hbar mathrm mathbf '
              'mathcal text operatorname begin end pmatrix bmatrix matrix cases aligned').split())
_VARIABLE_WORDS = set('iv iz ix iy dx dy dz dt du dv'.split())
_BARE_WORDS = _WORDS - set('begin end pmatrix bmatrix matrix cases aligned text operatorname mathrm mathbf mathcal'.split())
_TOKENS = re.compile(r'```[^\n]*\n[\s\S]*?```|`[^`\n]+`|\$\$[\s\S]*?\$\$|(?<!\\)\$[^\n$]+?(?<!\\)\$|\\{1,2}\[[\s\S]*?\\{1,2}\]|\\{1,2}\([\s\S]*?\\{1,2}\)|https?://[^\s<>]+')
_CANDIDATE = re.compile(r'[A-Za-z0-9\\{}^_=+*/().,!|<>:;\[\] \tα-ωΑ-Ω∞∑∫∮±×÷≤≥≠≈∂∇ℏ−→←↔⇒⇔∈∉⊂⊆∪∩ℂℝℤℕ°-]+')
_ENGLISH = re.compile(r'(?<![A-Za-z\\])[A-Za-z]{2,}(?![A-Za-z])')


def _looks_math(value):
    if re.search(r'\b(?:const|let|def|return|import|function|console|print|class)\b', value):
        return False
    return bool(re.search(r'\\[A-Za-z]+|[=^∑∫∮∂∇]|_[{0-9]|\b(?:sum|int)_|\b[fgh]\([^)]*\)|\b(?:sin|cos|tan|log|ln)\s+[A-Za-z]|[A-Za-z0-9]\s*/\s*[A-Za-z0-9]', value))


def _tex(value):
    # 修复模型在 JSON 字符串内多转义一层的命令，保留矩阵中的 \\\\ 换行。
    value = re.sub(r'(?<!\\)\\\\(?=(?:' + '|'.join(sorted(_WORDS)) + r')(?=[^A-Za-z]|$))', r'\\', value)
    protected = re.split(r'(\\(?:begin|end|text|operatorname|mathrm|mathbf|mathcal)\{[^{}]*\})', value)
    value = ''.join(part if index % 2 else re.sub(r'(?<![A-Za-z\\])(' + '|'.join(sorted(_BARE_WORDS)) + r')(?=[^A-Za-z]|$)', r'\\\1', part) for index, part in enumerate(protected))
    return value.replace('∞', r'\infty ').replace('∑', r'\sum ').replace('∫', r'\int ').replace('∮', r'\oint ').strip()


def _inline(value):
    leading = value[:len(value) - len(value.lstrip())]
    core = value.strip()
    trailing = value[len(value.rstrip()):]
    punctuation = ''
    while core.endswith(('.', ',', ';', ':')):
        punctuation = core[-1] + punctuation
        core = core[:-1]
    if not core or not _looks_math(core) or any(core.count(left) != core.count(right) for left, right in [('(', ')'), ('{', '}'), ('[', ']')]):
        return value
    return leading + '$' + _tex(core) + '$' + punctuation + trailing


def _bare(value):
    def candidate(match):
        text = match.group(0)
        parts = []
        pos = 0
        for word in _ENGLISH.finditer(text):
            if word.group(0) in _WORDS or word.group(0) in _VARIABLE_WORDS:
                continue
            parts.extend((_inline(text[pos:word.start()]), word.group(0)))
            pos = word.end()
        parts.append(_inline(text[pos:]))
        return ''.join(parts)
    return _CANDIDATE.sub(candidate, value)


def normalize_math(value):
    text = str(value or '')
    parts = []
    pos = 0
    for match in _TOKENS.finditer(text):
        parts.append(_bare(text[pos:match.start()]))
        token = match.group(0)
        if token.startswith('```'):
            language, body = token[3:-3].split('\n', 1)
            if language.strip().lower() in ('math', 'latex', 'tex') or (not language.strip() and _looks_math(body)):
                body = body.strip().removeprefix('$$').removesuffix('$$').removeprefix('$').removesuffix('$').strip()
                if body.startswith(('\\(', '\\[')) and body.endswith(('\\)', '\\]')):
                    body = body[2:-2]
                token = '\n\n$$\n' + _tex(body) + '\n$$\n\n'
        elif token.startswith('`') and _looks_math(token[1:-1]):
            token = '$' + _tex(token[1:-1]) + '$'
        elif token.startswith('$$'):
            token = '\n\n$$\n' + _tex(token[2:-2]) + '\n$$\n\n'
        elif re.match(r'^\\{1,2}[([]', token):
            display = token.lstrip('\\').startswith('[')
            body = re.sub(r'^\\{1,2}[([]|\\{1,2}[)\]]$', '', token)
            token = ('\n\n$$\n' + _tex(body) + '\n$$\n\n') if display else ('$' + _tex(body) + '$')
        elif token.startswith('$'):
            token = '$' + _tex(token[1:-1]) + '$'
        parts.append(token)
        pos = match.end()
    parts.append(_bare(text[pos:]))
    return ''.join(parts)
