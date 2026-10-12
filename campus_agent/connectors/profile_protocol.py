"""Read only the signed-in student's basic fields; discard unrelated personal data."""
import base64
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlencode, urljoin, urlsplit, parse_qs

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ETA_BASE = 'https://eta.zju.edu.cn/zftal-xgxt-web'
from ..campus_endpoints import ETA_SERVICE
ETA_CURRENT = ETA_BASE + '/teacher/xtgl/login/getCurrentUser.zf'
ETA_ROLES = ETA_BASE + '/teacher/xtgl/index/getUserRoleInfo.zf'
ETA_COMMON = ETA_BASE + '/teacher/xshx/commonGrxx.zf'
# Public frontend codec constants, not credentials. HTTPS remains mandatory.
ETA_REQUEST_KEY = b'789ABCDEF0123456'
ETA_RESPONSE_KEY = b'CDEF0789AB123456'
from ..profile_fields import PROFILE_KEYS, ALIASES, SCHEMA_CODES, text, basic_fields


def _error(code, message, diagnostic=None):
    from ..campus_common import CampusError
    return CampusError(code, message, diagnostic)


def display_label(value):
    if isinstance(value, list):
        return ' / '.join(text(v) for v in value[:8] if text(v))[:500]
    return text(value)


def schema_key(code, label=''):
    return next((k for k,aliases in ALIASES.items() if code in aliases+SCHEMA_CODES.get(k,())
                 or label.rstrip('：:') in aliases), None)


def schema_fields(form):
    flat=form.get('fields')
    fields=[f for f in flat[:2000] if isinstance(f,dict)] if isinstance(flat,list) else []
    groups=form.get('groupFields',[])
    if not isinstance(groups,list) or len(groups)>100:
        return fields
    return fields+[field for group in groups if isinstance(group,dict) and isinstance(group.get('fields'),list)
                   for field in group['fields'][:200] if isinstance(field,dict)]


def value_kind(value):
    return 'list' if isinstance(value,list) else 'object' if isinstance(value,dict) else 'empty' if value in (None,'') else 'scalar'


def schema_shape(form):
    """Only canonical basic-field names and types; never upstream names or values."""
    fields=schema_fields(form); recognized=[]
    for field in fields:
        codes=str(field.get('fieldCode','')).lower().split('|')
        keys=[schema_key(c,text(field.get('fieldName')) if len(codes)==1 else '') for c in codes]
        if any(keys):
            recognized.append({'fields':[k for k in keys if k], 'composite':len(codes)>1,
                'value_kind':value_kind(field.get('defaultValue')), 'label_kind':value_kind(field.get('defaultLabel'))})
    return {'group_count':len(form.get('groupFields',[])) if isinstance(form.get('groupFields'),list) else 0,
            'field_count':len(fields),'basic_fields':recognized[:30],
            'layout':'flat_and_grouped' if form.get('fields') and form.get('groupFields') else
                     'flat' if isinstance(form.get('fields'),list) else 'grouped' if isinstance(form.get('groupFields'),list) else 'unrecognized',
            'known_containers':{k:value_kind(form[k]) for k in
                ('form','formData','formDatas','definition','data','groupFields','fields','rows','values','curDatas') if k in form}}


def unwrap_form(payload):
    """Find a definition in known envelopes, without traversing unrelated modules."""
    if not isinstance(payload,dict):
        raise _error('ETA_PROTOCOL_CHANGED','ETA 个人基本信息表单格式不符合协议')
    queue=[(payload,0,{})];seen=0
    while queue and seen<20:
        value,depth,context=queue.pop(0);seen+=1
        context=dict(context)
        for key in ('values','curDatas','data'):
            values=value.get(key)
            if isinstance(values,dict) and not any(k in values for k in ('groupFields','formDataUrl','modules')) and any(basic_fields(values).values()):
                context.update(values)
        if schema_fields(value) or value.get('formDataUrl'):
            return value|{'values':context|value.get('values',{})} if context and isinstance(value.get('values',{}),dict) else value
        if depth<3:
            for key in ('form','formData','formDatas','definition','data'):
                child=value.get(key)
                if isinstance(child,str) and len(child)<200_000 and child.lstrip().startswith('{'):
                    try:child=json.loads(child)
                    except ValueError:continue
                if isinstance(child,dict):queue.append((child,depth+1,context))
    return payload


def form_fields(form, actual=None):
    """The official userInfo form uses groupFields/fields/defaultValue/defaultLabel."""
    result = basic_fields(form)
    for container in ('data', 'formData', 'values', 'curDatas'):
        values = basic_fields(form.get(container))
        result.update({k: v for k, v in values.items() if v})
    if actual is None:
        actual={k:v for container in ('data','formData','values','curDatas')
                if isinstance(form.get(container),dict) for k,v in form[container].items()}
    actual = actual if isinstance(actual, dict) else {}
    direct = basic_fields(actual)
    result.update({k: v for k,v in direct.items() if v})
    lookup = {str(k).lower(): v for k,v in actual.items()}
    for field in schema_fields(form):
        code = str(field.get('fieldCode', '')).lower()
        if '|' in code:
            codes=code.split('|')
            labels=field.get('defaultLabel')
            defaults=lookup.get(code,field.get('defaultValue'))
            options=field.get('options',[])
            for i,component in enumerate(codes[:6]):
                key=schema_key(component)
                if not key or direct.get(key):continue
                raw=lookup.get(component,defaults[i] if isinstance(defaults,list) and len(defaults)==len(codes) else None)
                option=next((o for o in options[:2000] if isinstance(o,dict) and text(o.get('value'))==text(raw)),None) if isinstance(options,list) else None
                options=option.get('children',[]) if option else []
                paired=component[:-2] if component.endswith('dm') else ''
                value=display_label(lookup.get(paired)) if paired else ''
                # Only a complete ordered display path can fill multiple fields.
                if not value and component not in lookup and code not in lookup and isinstance(labels,list) and len(labels)==len(codes):
                    value=text(labels[i])
                if not value and option:value=text(option.get('label'))
                if not value and (key in ('year','student_id','name') or isinstance(raw,str) and '@@@' in raw):value=text(raw)
                if value:result[key]=value
            continue
        key = schema_key(code,text(field.get('fieldName')))
        if not key:
            continue
        if direct.get(key):
            continue
        from_actual = code in lookup
        raw = lookup[code] if from_actual else field.get('defaultValue')
        # The renderer pairs e.g. xydm with xy, or uses selected option labels.
        paired = code[:-2] if code.endswith('dm') else ''
        label = display_label(lookup.get(paired)) if from_actual and paired else ''
        if not from_actual:
            label = display_label(field.get('defaultLabel'))
        if not label and isinstance(raw,str) and '@@@' in raw:
            label=text(raw)
        value = label
        options = field.get('options')
        if not value and isinstance(options, list):
            option = next((o for o in options[:2000] if isinstance(o, dict)
                           and text(o.get('value')) == text(raw)), None)
            if option:
                value = display_label(option.get('label'))
        coded = field.get('editType') in ('select','radio','checkbox','cascader') or code.endswith(('dm','_id'))
        if not value and (not coded or key in ('student_id','year','name')):
            value = text(raw)
        if value:
            result[key] = value
        elif from_actual:
            result[key] = ''
    return result


def self_values(payload, identity):
    """Unwrap an own-record response, never select the first of multiple students."""
    value = payload
    for _ in range(4):
        if isinstance(value, dict):
            nested = next((value[k] for k in ('formData','values','data','rows') if isinstance(value.get(k), (dict,list))), None)
            if nested is None:
                break
            value = nested
        elif isinstance(value, list):
            if len(value) == 1 and isinstance(value[0], dict):
                value = value[0]
            else:
                raise _error('ETA_PROTOCOL_CHANGED', 'ETA 本人表单返回了多条记录，未选择其他学生资料')
        else:
            raise _error('ETA_PROTOCOL_CHANGED', 'ETA 本人表单实际数据格式无法识别')
    if not isinstance(value, dict):
        raise _error('ETA_PROTOCOL_CHANGED', 'ETA 本人表单实际数据格式无法识别')
    student_id = basic_fields(value).get('student_id')
    if student_id and student_id != identity:
        raise _error('PROFILE_IDENTITY_MISMATCH', 'ETA 本人表单实际数据学号不一致，未合并资料')
    return value


def form_data_request(form, identity, form_code='userInfo'):
    """Only follow userInfo's own GET data declaration in reviewed ETA namespaces."""
    source = form.get('formDataUrl')
    if form_code not in ('userInfo','wdzd'):
        raise _error('ETA_PROTOCOL_CHANGED','ETA 返回了未适配的基本信息表单')
    if not isinstance(source, str) or not 1 <= len(source) <= 1000:
        raise _error('ETA_PROTOCOL_CHANGED', 'ETA 本人表单实际数据入口格式无法识别')
    if source.startswith('/zftal-xgxt-web/') or '://' in source or source.startswith('//'):
        url = urljoin('https://eta.zju.edu.cn/', source)
    else:
        url = ETA_BASE+'/'+source.lstrip('/')
    from ..campus_network import check_url
    parsed = urlsplit(check_url(url))
    path = parsed.path
    namespace = '/zftal-xgxt-web/'
    relative = path[len(namespace):] if path.startswith(namespace) else ''
    name = relative.rsplit('/', 1)[-1]
    allowed = re.fullmatch(r'(?:teacher/xshx|xsxx/xsxxxg|xsxx/xsxx/xsgl)/'
                          r'(?:(?:get|query|cx|read|load|find)[A-Za-z0-9_\-]{1,80}|grxx|userInfo|formData|tableData|details|detail)\.zf', relative, re.I)
    allowed = allowed or re.fullmatch(r'dynamic/form/(?:data|formData|read)/'+form_code+r'/[A-Za-z0-9_\-]{1,64}\.zf', relative)
    if (parsed.hostname != 'eta.zju.edu.cn' or not allowed or
            re.search(r'list|search|page|export|download|save|delete|update|insert|remove|apply', name, re.I)):
        raise _error('ETA_PROTOCOL_CHANGED', 'ETA 本人表单数据入口不在已适配的只读范围，未访问其他模块')
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(k not in ('dataId','xh','gnczid','formCode','extendCode') or len(v) != 1 for k,v in query.items()):
        raise _error('ETA_PROTOCOL_CHANGED', 'ETA 本人表单数据入口包含未适配参数')
    record_id = text(form.get('dataId')) or identity
    if not re.fullmatch(r'[A-Za-z0-9_\-]{1,128}', record_id):
        raise _error('ETA_PROTOCOL_CHANGED', 'ETA 本人表单记录标识格式无法识别')
    for key in ('dataId','xh'):
        if key in query and query[key][0] not in ('', identity, record_id, eta_encode(identity), eta_encode(record_id)):
            raise _error('PROFILE_IDENTITY_MISMATCH', 'ETA 本人表单入口的身份参数不一致，未读取资料')
    if query.get('formCode', [form_code])[0] != form_code:
        raise _error('ETA_PROTOCOL_CHANGED', 'ETA 返回了非基本信息表单入口，未读取资料')
    params = {k:v[0] for k,v in query.items() if k not in ('dataId','xh')}
    if any(not re.fullmatch(r'[A-Za-z0-9_\-]{0,64}', v) for v in params.values()):
        raise _error('ETA_PROTOCOL_CHANGED', 'ETA 本人表单入口参数格式无法识别')
    params['dataId'] = eta_encode(record_id)
    if 'xh' in query:
        params['xh'] = eta_encode(identity)
    params.setdefault('gnczid', '')
    return parsed._replace(query=urlencode(params)).geturl(), parsed._replace(query='').geturl()


def eta_encode(value):
    data = value.encode('utf-8')
    data += b'\0' * ((-len(data)) % 16)
    encryptor = Cipher(algorithms.AES(ETA_REQUEST_KEY), modes.CBC(ETA_REQUEST_KEY)).encryptor()
    return base64.b64encode(encryptor.update(data)+encryptor.finalize()).decode('ascii')


def eta_decode(raw):
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
        encoded = value if isinstance(value, str) else raw.strip()
    except (ValueError, UnicodeError):
        encoded = raw.strip()
    try:
        ciphertext = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise _error('ETA_PROTOCOL_CHANGED', 'ETA 响应格式无法解析，保留已有基本信息') from None
    if not ciphertext or len(ciphertext) % 16:
        raise _error('ETA_PROTOCOL_CHANGED', 'ETA 编码响应不符合协议，保留已有基本信息')
    for key in (ETA_RESPONSE_KEY, b'0123456789ABCDEF'):
        try:
            decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
            value = json.loads((decryptor.update(ciphertext)+decryptor.finalize()).rstrip(b'\0'))
            if isinstance(value, dict):
                return value
        except (ValueError, UnicodeError):
            pass
    raise _error('ETA_PROTOCOL_CHANGED', 'ETA 编码协议已变化，保留已有基本信息')


def eta_json(client, url, form=None):
    status, _, raw = client.request(url, form)
    if status in (901, 922, 429):
        raise _access_error(url, status, 'GET' if form is None else 'POST')
    if status in (301,302,303,307,308,401):
        client.eta_authenticated=False
        raise _error('SESSION_EXPIRED', 'ETA 站点会话已失效，保留缓存和其他校园网站会话',
            {'stage':'profile_eta','method':'GET' if form is None else 'POST','host':'eta.zju.edu.cn',
             'path':urlsplit(url).path,'reason':'HTTP_AUTH','http_status':status})
    if status == 403:
        raise _error('ETA_UNAVAILABLE', 'ETA 会话或访问权限不可用，其他教务信息继续获取',
            {'stage':'profile_eta','method':'GET' if form is None else 'POST','host':'eta.zju.edu.cn',
             'path':urlsplit(url).path,'reason':'HTTP_ACCESS','http_status':status})
    if status != 200:
        raise _error('ETA_UNAVAILABLE', f'ETA 暂不可用（HTTP {status}）',
            {'stage':'profile_eta','method':'GET' if form is None else 'POST','host':'eta.zju.edu.cn',
             'path':urlsplit(url).path,'reason':'HTTP_STATUS','http_status':status})
    payload = eta_decode(raw)
    if str(payload.get('code')) != '0':
        raise _error('ETA_UNAVAILABLE', 'ETA 未返回有效本人信息，保留缓存')
    return payload.get('data')


def eta_current(client):
    """Verify an existing site session before exchanging another single-use ticket."""
    from ..campus_common import CampusError
    def has_identity(current):
        return isinstance(current,dict) and any(text(current.get(k)) for k in ('xh','zgh','yhm','studentId','student_id'))
    site_cookie=any(c.domain.lstrip('.').lower()=='eta.zju.edu.cn' and not c.is_expired() for c in client.jar)
    if getattr(client,'eta_authenticated',False) or site_cookie:
        try:
            current=eta_json(client,ETA_CURRENT)
        except CampusError as exc:
            # A timeout/access rejection is not proof of expiry; avoid fresh ticket loops.
            if exc.code!='SESSION_EXPIRED':raise
        else:
            if has_identity(current):return current
        client.eta_authenticated=False
    callback = client.service_callback('eta')
    for _ in range(8):
        status, headers, _ = client.request_navigation(callback)
        if status in (901, 922, 429):
            raise _access_error(callback, status)
        if status in (301,302,303,307,308):
            location = next((v for k,v in headers.items() if k.lower() == 'location'), '')
            callback = urljoin(callback, location)
            parsed = urlsplit(callback)
            if not location or parsed.hostname != 'eta.zju.edu.cn' or parsed.scheme not in ('http','https'):
                raise _error('ETA_UNAVAILABLE', 'ETA 登录未建立有效站内会话')
            callback = parsed._replace(scheme='https').geturl()
            continue
        if status != 200:
            raise _error('ETA_UNAVAILABLE', f'ETA 登录暂不可用（HTTP {status}）')
        break
    else:
        raise _error('ETA_UNAVAILABLE', 'ETA 登录跳转次数超限')
    current = eta_json(client, ETA_CURRENT)
    if not has_identity(current):
        roles = eta_json(client, ETA_ROLES)
        if not isinstance(roles, dict) or not isinstance(roles.get('jsList'), list):
            raise _error('ETA_UNAVAILABLE', 'ETA 未验证登录用户，HTTP 200 不代表已登录')
        role = next((r for r in roles['jsList'] if isinstance(r,dict) and r.get('jsdm') == roles.get('mrjsdm')), None)
        if not role or not re.fullmatch(r'[A-Za-z0-9_\-]{1,64}', str(role.get('jsdm', ''))):
            raise _error('ETA_UNAVAILABLE', 'ETA 默认角色响应无法使用')
        eta_json(client, ETA_BASE+'/teacher/xtgl/login/switchRole.zf', {'jsdm': role['jsdm']})
        current = eta_json(client, ETA_CURRENT)
        if not has_identity(current):
            current = roles
    return current


def eta_profile(client, expected_id=''):
    from ..campus_common import CampusError
    client.eta_field_diagnostic = {}
    client.eta_followup_warnings = []
    client.eta_invalidated_fields = []
    client.eta_field_urls = {}
    current=eta_current(client)
    identity = next((text(current.get(k)) for k in ('xh','zgh','yhm','studentId','student_id') if text(current.get(k))), '')
    if not identity or not re.fullmatch(r'[A-Za-z0-9_\-]{1,128}', identity):
        raise _error('ETA_UNAVAILABLE', 'ETA 未返回可确认的本人标识')
    if expected_id and identity != expected_id:
        client.eta_authenticated=False
        raise _error('PROFILE_IDENTITY_MISMATCH', 'ETA 与已有教务学号不一致，未合并个人资料')
    client.eta_authenticated=True
    catalog = eta_json(client, ETA_COMMON+'?path=grxx')
    modules = catalog.get('modules') if isinstance(catalog, dict) else None
    catalog_shape=[{'form_code':m.get('formCode') if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',str(m.get('formCode',''))) else '<other>',
                    'extend_code':m.get('extendCode') if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',str(m.get('extendCode',''))) else '<other>',
                    'academic_title':any('学籍' in str(m.get(k,'')) for k in ('formName','showName','name','title','gnmkdm'))}
                   for m in modules[:100] if isinstance(m,dict)] if isinstance(modules,list) else []
    module = next((m for m in modules[:100] if isinstance(m,dict) and m.get('formCode') == 'userInfo'), None) if isinstance(modules,list) else None
    if not module or not re.fullmatch(r'[A-Za-z0-9_\-]{1,64}', str(module.get('extendCode',''))):
        raise _error('ETA_PROTOCOL_CHANGED', 'ETA 个人基本信息表单入口已变化，未访问其他个人模块')
    path = ETA_BASE+'/dynamic/form/find/userInfo/'+module['extendCode']+'.zf'
    payload=eta_json(client,path+'?'+urlencode({'dataId':eta_encode(identity)}))
    payload_id=basic_fields(payload).get('student_id')
    if payload_id and payload_id!=identity:
        raise _error('PROFILE_IDENTITY_MISMATCH','ETA 表单包装的学号不一致，未合并资料')
    form=unwrap_form(payload)
    attempts=[{'path':urlsplit(path).path,'schema_shape':schema_shape(payload)}]
    def available(candidate):
        return bool(schema_fields(candidate) or candidate.get('formDataUrl') or any(form_fields(candidate).values()))
    if not available(form) and module['extendCode']!='default':
        # Only the public frontend's known userInfo default, never arbitrary URL probing.
        fallback=ETA_BASE+'/dynamic/form/find/userInfo/default.zf'
        try:
            fallback_payload=eta_json(client,fallback+'?'+urlencode({'dataId':eta_encode(identity)}))
            fallback_id=basic_fields(fallback_payload).get('student_id')
            if fallback_id and fallback_id!=identity:
                raise _error('PROFILE_IDENTITY_MISMATCH','ETA 回退表单包装的学号不一致，未合并资料')
            candidate=unwrap_form(fallback_payload)
            attempts.append({'path':urlsplit(fallback).path,'schema_shape':schema_shape(fallback_payload)})
            if available(candidate):form,path=candidate,fallback
        except CampusError as exc:
            if exc.code=='PROFILE_IDENTITY_MISMATCH':raise
            client.eta_followup_warnings.append(exc.public()|{'stage':'profile_eta'})
            attempts.append({'path':urlsplit(fallback).path,'code':exc.code})
    client.eta_form_available=available(form)
    inline = form_fields(form)
    if inline.get('student_id') and inline['student_id'] != identity:
        raise _error('PROFILE_IDENTITY_MISMATCH', 'ETA 基本信息表单的学号不一致，未合并资料')
    actual = None
    if form.get('formDataUrl'):
        data_url, source_url = form_data_request(form, identity)
        actual = self_values(eta_json(client, data_url), identity)
        path = source_url
    fields = form_fields(form, actual)
    # jgdm in the observed userInfo schema is 籍贯, not an academic institution.
    # Withdraw only the earlier ETA college mapping, preserving independent sources.
    if any(str(f.get('fieldCode','')).lower()=='jgdm' for f in schema_fields(form)) and not fields.get('college'):
        client.eta_invalidated_fields=['college']
    if not client.eta_form_available:
        fields=basic_fields(current)
    if fields.get('student_id') and fields['student_id'] != identity:
        raise _error('PROFILE_IDENTITY_MISMATCH', 'ETA 实际表单学号不一致，未合并资料')
    fields['student_id'] = identity
    fields['name'] = fields.get('name') or text(current.get('xm'))
    field_urls={k:path if client.eta_form_available else ETA_CURRENT for k,v in fields.items() if v}
    # The public frontend's Wdzd component is a separate own academic record,
    # discovered in this user's commonGrxx catalog; never query other modules.
    academic_module=next((m for m in modules[:100] if isinstance(m,dict) and m.get('formCode')=='wdzd' and
                         re.fullmatch(r'[A-Za-z0-9_\-]{1,64}',str(m.get('extendCode','')))),None)
    if academic_module:
        academic_path=ETA_BASE+'/dynamic/form/find/wdzd/'+academic_module['extendCode']+'.zf'
        try:
            academic_payload=eta_json(client,academic_path+'?'+urlencode({'dataId':eta_encode(identity)}))
            outer_id=basic_fields(academic_payload).get('student_id')
            if outer_id and outer_id!=identity:
                raise _error('PROFILE_IDENTITY_MISMATCH','ETA 本人学籍表单身份不一致，未合并资料')
            academic_form=unwrap_form(academic_payload)
            academic_inline=form_fields(academic_form)
            if academic_inline.get('student_id') and academic_inline['student_id']!=identity:
                raise _error('PROFILE_IDENTITY_MISMATCH','ETA 本人学籍表单学号不一致，未合并资料')
            academic_actual=None
            if academic_form.get('formDataUrl'):
                academic_url,academic_path=form_data_request(academic_form,identity,'wdzd')
                academic_actual=self_values(eta_json(client,academic_url),identity)
            academic_fields=form_fields(academic_form,academic_actual)
            if academic_fields.get('student_id') and academic_fields['student_id']!=identity:
                raise _error('PROFILE_IDENTITY_MISMATCH','ETA 本人学籍实际数据身份不一致，未合并资料')
            for key in ('college','major','class_name','year'):
                if academic_fields.get(key):
                    fields[key]=academic_fields[key]
                    field_urls[key]=academic_path
            attempts.append({'path':urlsplit(academic_path).path,'schema_shape':schema_shape(academic_form),
                             'data_request':academic_actual is not None})
        except CampusError as exc:
            if exc.code=='PROFILE_IDENTITY_MISMATCH':raise
            client.eta_followup_warnings.append(exc.public()|{'stage':'profile_eta'})
            attempts.append({'path':urlsplit(academic_path).path,'code':exc.code})
    client.eta_field_urls=field_urls
    client.eta_field_diagnostic = {'stage':'profile_eta', 'method':'GET', 'host':'eta.zju.edu.cn',
        'path':urlsplit(path).path, 'reason':'PROFILE_FIELDS' if client.eta_form_available else 'FORM_SCHEMA_MISSING', 'data_request': actual is not None,
        'field_status':{k:'available' if fields.get(k) else 'unavailable' for k in PROFILE_KEYS},
        'schema_shape':schema_shape(form),'form_attempts':attempts,'form_catalog':catalog_shape}
    return fields, path if client.eta_form_available else ETA_CURRENT


def _access_error(url, status, method='GET'):
    from ..campus_common import CampusError
    return CampusError('ETA_ACCESS_REJECTED', f'ETA 返回访问拒绝响应（HTTP {status}），已停止该来源请求；保留缓存并继续教务查询',
        {'stage': 'profile_eta', 'method': method, 'host': 'eta.zju.edu.cn', 'path': urlsplit(url).path,
         'reason': 'HTTP_RATE_LIMIT' if status == 429 else 'HTTP_ACCESS_REJECTED', 'http_status': status})
