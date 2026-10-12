"""Undergraduate platform protocol and normalization, independent of persistence."""
import hashlib
import json
import re
from urllib.parse import urlsplit, urlencode, urljoin
from html.parser import HTMLParser
from ..profile_fields import basic_fields, ALIASES
from ..campus_common import CampusError, plain, decode_page
from ..campus_endpoints import BASE, MENU, TIMETABLE, TIMETABLE_INDEX, GRADES, _REQUEST_STAGES
from ..academic_defaults import academic_year, semester_order, SEMESTER_PATTERN

class _TermOptions(HTMLParser):
    """Read the authenticated page's actual options, never guess enrollment years."""
    def __init__(self, text):
        super().__init__()
        self.options = {'xnm': [], 'xqm': []}
        self.select = None
        self.option = None
        self.feed(text)
        self.finish_option()

    def finish_option(self):
        if self.select and self.option is not None:
            self.options[self.select].append(self.option)
        self.option = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select':
            self.finish_option()
            self.select = next((attrs.get(key) for key in ('id', 'name')
                                if attrs.get(key) in self.options), None)
        elif tag == 'option' and self.select:
            # HTML permits an omitted </option>; the next option implicitly closes it.
            self.finish_option()
            self.option = {'value': attrs.get('value', ''), 'label': '', 'selected': 'selected' in attrs}

    def handle_data(self, data):
        if self.option is not None:
            self.option['label'] += data

    def handle_endtag(self, tag):
        if tag == 'option' and self.select and self.option is not None:
            self.finish_option()
        elif tag == 'select':
            self.finish_option()
            self.select = self.option = None


def _field(row, *names):
    for name in names:
        if name in row and row[name] is not None:
            return plain(row[name])
    return ''


def normalize_profile(row):
    if not isinstance(row, dict):
        return None
    result = basic_fields(row)
    return result if result['student_id'] and result['name'] else None


def normalize_course(row):
    # Some ZDBK versions encode display columns in kcb instead of separate fields.
    block = [plain(x).replace('zwf', '').strip() for x in re.split(r'<br\s*/?>', str(row.get('kcb') or ''), flags=re.I)]
    name = _field(row, 'kcmc') or (block[0] if block else '')
    if not name:
        raise CampusError('PROTOCOL_CHANGED', '课表条目缺少课程名称，未更新本地数据')
    return {'name': name, 'code': _field(row, 'kch', 'kch_id'),
            'teacher': _field(row, 'xm', 'jsxm') or (block[2] if len(block) > 2 else ''),
            'location': _field(row, 'cdmc') or (block[3] if len(block) > 3 else ''),
            'weekday': _field(row, 'xqj'), 'periods': _field(row, 'jcs', 'jc'),
            'start_period': _field(row, 'djj'), 'duration': _field(row, 'skcd'),
            'weeks': _field(row, 'zcd'), 'season': _field(row, 'xxq'), 'odd_even': _field(row, 'dsz')}


def normalize_grade(row):
    name = _field(row, 'kcmc')
    if not name:
        raise CampusError('PROTOCOL_CHANGED', '成绩条目缺少课程名称，未更新本地数据')
    selection = _field(row, 'xkkh')
    match = re.match(r'^\(([^)]+)\)-([^-]+)-', selection)
    return {'name': name, 'code': _field(row, 'kch') or (match[2] if match else ''),
            'term': match[1] if match else _field(row, 'xnmmc', 'xnm'),
            'score': _field(row, 'cj'), 'credits': _field(row, 'xf'), 'gpa': _field(row, 'jd'),
            'selection_id': selection}


class ZdbkProtocol:
    def connect_zdbk(self):
        from ..campus_network import check_url
        self.authenticated = False
        callback = self.service_callback('zdbk')
        for _ in range(8):
            status, headers, raw = self.request(callback)
            if status in (301, 302, 303, 307, 308):
                location = next((v for k, v in headers.items() if k.lower() == 'location'), '')
                callback = check_url(urljoin(callback, location))
                if not location or urlsplit(callback).hostname != 'zdbk.zju.edu.cn':
                    raise CampusError('SERVICE_LOGIN_FAILED', '教务网未建立有效会话')
                continue
            if status != 200:
                raise CampusError('SERVICE_LOGIN_FAILED', '教务网登录暂不可用')
            break
        else:
            raise CampusError('SERVICE_LOGIN_FAILED', '教务网登录跳转次数超限')
        # Probe an authenticated read endpoint; JS patch/landing pages are insufficient.
        menu = self.json_request(MENU, {'doType': ''})
        if not isinstance(menu, dict) or not isinstance(menu.get('result'), list) or not menu['result']:
            raise CampusError('UNSUPPORTED_ACCOUNT', '未能验证本科教务网权限；当前连接仅支持本科生')
        # Keep the CAS master session in memory; only service tickets are single-use.
        # The cookie policy prevents sending the master cookie to service hosts.
        self.authenticated = True
        self.modules = menu['result']

    def page_request(self, url, form=None):
        status, headers, raw = self.request(url, form)
        parsed=urlsplit(url)
        stage=_REQUEST_STAGES.get((parsed.hostname,parsed.path),('campus',''))[0]
        diagnostic={'stage':stage,'method':'GET' if form is None else 'POST','host':parsed.hostname,'http_status':status}
        if stage!='campus':diagnostic['path']=parsed.path
        if stage=='timetable' and isinstance(form,dict):
            if re.fullmatch(SEMESTER_PATTERN,str(form.get('xqm',''))):diagnostic['query_semester']=form['xqm']
            if academic_year(form.get('xnm','')):diagnostic['query_year']=str(form['xnm'])
        mime=next((str(v) for k,v in headers.items() if k.lower()=='content-type'),'').split(';')[0].lower()
        diagnostic['content_kind']='json' if mime=='application/json' else 'html' if mime in ('text/html','application/xhtml+xml') else 'other'
        if status in (301,302,303,307,308,401):
            raise CampusError('SESSION_EXPIRED','校园会话已失效，请手动重新登录',diagnostic|{'reason':'HTTP_AUTH'})
        if status in (403,429,901,922):
            raise CampusError('ACCESS_REJECTED',f'校园服务拒绝本次请求（HTTP {status}），请使用官方页面核对',diagnostic|{'reason':'HTTP_ACCESS'})
        if not 100 <= status <= 599:
            raise CampusError('UPSTREAM_STATUS_UNKNOWN',f'教务返回未识别的自定义响应（HTTP {status}），已暂停并保留缓存；未认定为服务器故障、限流或会话失效',
                              diagnostic|{'reason':'HTTP_CUSTOM_STATUS'})
        if status!=200:
            raise CampusError('UPSTREAM_ERROR',f'校园查询暂不可用（HTTP {status}）',diagnostic|{'reason':'HTTP_STATUS'})
        try:text,encoding=decode_page(raw,headers)
        except CampusError as exc:
            raise CampusError(exc.code,str(exc),diagnostic|{'reason':'TEXT_ENCODING'}) from None
        self.page_encoding=encoding
        if re.search(r'name=[\"\']execution[\"\']|/cas/login|统一身份认证|浙大通行证', text):
            raise CampusError('SESSION_EXPIRED', '校园会话已失效，请手动重新登录',diagnostic|{'reason':'LOGIN_PAGE'})
        if 'captcha_error' in text:
            raise CampusError('CAPTCHA_REQUIRED', '教务网需要验证码，请填写后重新同步',diagnostic|{'reason':'CAPTCHA_HINT'})
        if '没有访问权限' in text or '无访问权限' in text:
            raise CampusError('ACCESS_REJECTED', '教务网返回“没有访问权限”，请核对官方页面权限与请求适配；未认定为会话失效')
        self.last_page_diagnostic=diagnostic|{'page_encoding':encoding}
        return text

    def json_request(self, url, form):
        text = self.page_request(url, form)
        try:
            value = json.loads(text)
        except ValueError:
            raise CampusError('PROTOCOL_CHANGED', '校园查询返回了非 JSON 内容，无法确认结果',
                              getattr(self,'last_page_diagnostic',{})|{'reason':'NON_JSON'}) from None
        if isinstance(value, dict) and (value.get('success') is False or
                value.get('error') not in (None,False,0,'','0','false','False')):
            parsed=urlsplit(url)
            stage=_REQUEST_STAGES.get((parsed.hostname,parsed.path),('campus',''))[0]
            error=value.get('error')
            diagnostic={'stage':stage,'method':'POST','host':parsed.hostname,'reason':'UPSTREAM_REJECTED',
                'http_status':200,'upstream_flags':{'success_false':value.get('success') is False,
                    'error_kind':'object' if isinstance(error,dict) else 'list' if isinstance(error,list)
                    else 'string' if isinstance(error,str) else 'boolean' if isinstance(error,bool)
                    else 'missing' if error is None else 'number' if isinstance(error,(int,float)) else 'other'},
                'response_shape':{k:'list' if isinstance(value.get(k),list) else 'missing' if k not in value else 'other'
                                  for k in ('kbList','items','xsxx')}}
            if stage!='campus':diagnostic['path']=parsed.path
            if stage=='timetable' and re.fullmatch(SEMESTER_PATTERN,str(form.get('xqm',''))):
                diagnostic['query_semester']=form['xqm']
                if academic_year(form.get('xnm','')):diagnostic['query_year']=str(form['xnm'])
            messages=' '.join(str(value.get(k,'')) for k in ('msg','message','error'))
            if re.search(r'验证码|captcha',messages,re.I):
                raise CampusError('CAPTCHA_REQUIRED','教务查询要求验证码，请填写后再同步',diagnostic|{'reason':'CAPTCHA_HINT'})
            if re.search(r'无访问权限|没有访问权限|权限不足',messages):
                raise CampusError('ACCESS_REJECTED','教务查询提示权限不足，已保留缓存',diagnostic|{'reason':'PERMISSION_HINT'})
            raise CampusError('UPSTREAM_ERROR', '校园查询返回错误，未更新本地数据',diagnostic)
        return value

    def terms(self):
        self.term_warnings = []
        options = _TermOptions(self.page_request(TIMETABLE_INDEX)).options
        years, semesters, unsupported, shapes = {}, {}, [], []
        for option in options['xnm']:
            value = option['value']
            year = academic_year(value)
            if year:
                years[year] = option
        for option in options['xqm']:
            value = option['value']
            label = re.sub(r'\s+', '', plain(option['label'])).strip(' -—')
            # Ignore explicit aggregate/placeholder options; never query them as one semester.
            if not value or value in ('0', '-1') or re.fullmatch(
                    r'(?:请选择(?:学期)?|选择学期|全部(?:学期|学年)?|所有(?:学期|学年)?|全学年|全年|全学期)', label):
                continue
            # The school's literal option is already the query contract. Numeric codes are
            # opaque identifiers, not a reason to guess/rewrite their calendar semantics.
            if re.fullmatch(SEMESTER_PATTERN, value):
                semesters[value] = option
            else:
                unsupported.append(value if re.fullmatch(r'-?\d{1,4}', value) else '<非数值编码>')
                # Describe an unfamiliar option without publishing a token or label.
                shapes.append({'length': min(len(value), 128), 'has_pipe': '|' in value,
                               'has_season': any(c in value for c in '秋冬春夏'), 'selected': option['selected'],
                               'components': [{'length': min(len(part),128),
                                   'kind': 'digits' if re.fullmatch(r'[0-9]+',part) else
                                           'letters' if re.fullmatch(r'[A-Za-z]+',part) else
                                           'empty' if not part else 'replacement' if '\ufffd' in part else
                                           'cjk' if re.fullmatch(r'[\u4e00-\u9fff]+',part) else 'other'} for part in value.split('|')[:4]],
                               'page_encoding':getattr(self,'page_encoding','unknown')})
        if unsupported:
            self.term_warnings = [{'code': 'UNSUPPORTED_TERM_OPTIONS', 'stage': 'terms',
                'message': '部分学期选项暂无法查询（' + '、'.join(sorted(set(unsupported))) +
                           '）；继续同步可查询学期，课表范围不标记为完整',
                'diagnostic': {'stage': 'terms', 'reason': 'UNSUPPORTED_TERM_OPTIONS',
                               'semester_codes': sorted(set(unsupported)), 'option_shapes': shapes[:20]}}]
        if not years or not semesters or len(years)*len(semesters) > 180:
            codes = sorted({o['value'] if re.fullmatch(r'-?\d{1,4}', o['value']) else '<非数值编码>'
                            for o in options['xqm'] if o['value']})
            raise CampusError('TERM_DISCOVERY_FAILED', '无法确认教务网可查询的学期范围，未推断全部课表',
                              {'stage': 'terms', 'reason': 'TERM_OPTIONS', 'semester_codes': codes, 'option_shapes': shapes[:20]})
        terms = [{'year': y, 'query_year': years[y]['value'], 'semester': s, 'range_source': 'page',
                  'term': (plain(years[y]['label']) or y) + ' · ' + (plain(semesters[s]['label']) or '学期编码 ' + s),
                  'current': years[y]['selected'] and semesters[s]['selected']}
                 for y in years for s in semesters]
        return sorted(terms, key=lambda t: (t['current'], t['year'], semester_order(t['semester'])), reverse=True)

    def timetable(self, year, semester, captcha=''):
        payload = self.json_request(TIMETABLE, {'xnm': year, 'xqm': semester, 'captcha_value': captcha})
        if not isinstance(payload, dict) or not isinstance(payload.get('kbList'), list):
            raise CampusError('PROTOCOL_CHANGED', '课表响应缺少 kbList，未更新本地数据')
        if len(payload['kbList']) > 1000:
            raise CampusError('PROTOCOL_CHANGED', '课表数据规模或格式不符合约定')
        profile = normalize_profile(payload.get('xsxx')) or normalize_profile(payload)
        rows, skipped = [], 0
        self.timetable_warnings = []
        for row in payload['kbList']:
            if isinstance(row, dict) and _field(row, 'sfyjskc') == '1':
                continue
            try:
                if not isinstance(row, dict):
                    raise CampusError('PROTOCOL_CHANGED', '无效课程条目')
                rows.append(normalize_course(row))
            except CampusError:
                skipped += 1
        if skipped:
            self.timetable_warnings = [{'code': 'PARTIAL_TIMETABLE',
                'message': f'{skipped} 条课程缺少可用字段，已保留其余课程及原缓存，未标记完整', 'skipped': skipped}]
            if not rows:
                raise CampusError('PROTOCOL_CHANGED', '本学期没有可解析课程，保留上次课表并继续其他学期')
        return profile, rows

    def grades(self):
        collected, seen, total = [], set(), None
        for page in range(1, 21):
            payload = self.json_request(GRADES, {'queryModel.currentPage': page,
                'queryModel.showCount': 100, 'queryModel.sortName': 'xkkh', 'queryModel.sortOrder': 'asc'})
            if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
                raise CampusError('PROTOCOL_CHANGED', '成绩响应缺少 items，未更新本地数据')
            rows = payload['items']
            if len(rows) > 2000 or any(not isinstance(row, dict) for row in rows):
                raise CampusError('PROTOCOL_CHANGED', '成绩数据规模或格式不符合约定')
            count = payload.get('totalCount', payload.get('records'))
            if count is not None:
                try:
                    total = int(count)
                    if total < 0:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise CampusError('PROTOCOL_CHANGED', '成绩总数格式不符合约定') from None
            fresh = 0
            for row in rows:
                fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    collected.append(normalize_grade(row))
                    fresh += 1
            if total is not None and len(collected) >= total:
                return collected, True, total
            if not rows or (len(rows) < 100 and total is None):
                return collected, total is None or len(collected) >= total, total
            if not fresh or len(collected) >= 2000:
                break
        return collected, False, total



class ProfilePage(HTMLParser):
    """Only self-profile fields from inputs, selected options and labelled table cells."""
    def __init__(self, html):
        super().__init__(); self.values={}; self.select=''; self.option=None; self.cell=None; self.cells=[]
        self.feed(html); self.finish_option()
        for i in range(len(self.cells)-1):
            key=self.cells[i].strip().rstrip('：:')
            if any(key in names for names in ALIASES.values()):self.values.setdefault(key,self.cells[i+1])
    def finish_option(self):
        if self.option and self.option['selected']:self.values[self.select]=self.option['label']
        self.option=None
    def handle_starttag(self, tag, attrs):
        a=dict(attrs); key=a.get('name') or a.get('id') or ''
        if tag=='input' and any(key.lower() in names for names in ALIASES.values()) and a.get('value'):
            self.values[key]=a['value']
        if tag=='select':self.finish_option();self.select=key
        if tag=='option' and self.select:self.finish_option();self.option={'selected':'selected' in a,'label':''}
        if tag in ('td','th'):self.cell=''
    def handle_data(self,data):
        if self.cell is not None:self.cell+=data
        if self.option is not None:self.option['label']+=data
    def handle_endtag(self,tag):
        if tag=='option':self.finish_option()
        if tag=='select':self.finish_option();self.select=''
        if tag in ('td','th') and self.cell is not None:self.cells.append(self.cell.strip());self.cell=None


def zdbk_profile(client):
    module = next((m for m in getattr(client, 'modules', []) if isinstance(m,dict) and
                  any(w in str(m.get('gnmkmc','')) for w in ('个人信息','学生信息查询','学籍信息查询')) and
                  re.fullmatch(r'/?(?:jwglxt/)?(?:xsxxxggl|xjgl)/(?:xsgrxxwh|xsxxwh|xsxxcx)_cx[A-Za-z0-9_]+\.html',
                               str(m.get('dyym','')))), None)
    if not module:
        return None
    path = str(module['dyym']).lstrip('/')
    if path.startswith('jwglxt/'):path=path[len('jwglxt/'):]
    code=str(module.get('gnmkdm',''))
    if not re.fullmatch(r'N\d{4,10}',code):
        raise CampusError('PROFILE_PROTOCOL_CHANGED','教务个人信息菜单编码无法使用')
    url=BASE+path+'?'+urlencode({'gnmkdm':code,'layout':'default'})
    fields=basic_fields(ProfilePage(client.page_request(url)).values)
    if not any(fields.values()):raise CampusError('PROFILE_UNAVAILABLE','教务个人信息页未提供可识别的基本字段')
    return fields, BASE+path
