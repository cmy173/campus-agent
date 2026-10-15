"""Reviewed fixed service configuration; never supplied by callers."""
from urllib.parse import urlsplit

CAS = 'https://zjuam.zju.edu.cn/cas/'
BASE = 'https://zdbk.zju.edu.cn/jwglxt/'
SERVICE = BASE + 'xtgl/login_ssologin.html'
ETA_SERVICE = 'http://eta.zju.edu.cn/zftal-xgxt-web/teacher/xtgl/index/check.zf'
MENU = BASE + 'xtgl/index_cxLeafGnmkdm.html'
TIMETABLE = BASE + 'kbcx/xskbcx_cxXsKb.html'
TIMETABLE_INDEX = BASE + 'kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N253508&layout=default'
GRADES = BASE + 'cxdy/xscjcx_cxXscjIndex.html?doType=query&gnmkdm=N5083'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36 CampusAgent/0.3'
CONNECTOR_REVISION = '2026-10-12.custom-status'

_REQUEST_STAGES = {
    ('zjuam.zju.edu.cn', '/cas/login'): ('cas_login', '校园认证'),
    ('zjuam.zju.edu.cn', '/cas/v2/getPubKey'): ('cas_key', '认证公钥'),
    ('zjuam.zju.edu.cn', '/cas/kaptcha'): ('cas_captcha', '登录验证码'),
    ('zdbk.zju.edu.cn', urlsplit(SERVICE).path): ('service_login', '教务登录'),
    ('zdbk.zju.edu.cn', urlsplit(MENU).path): ('menu', '教务菜单'),
    ('zdbk.zju.edu.cn', urlsplit(TIMETABLE_INDEX).path): ('terms', '学期列表'),
    ('zdbk.zju.edu.cn', urlsplit(TIMETABLE).path): ('timetable', '课表'),
    ('zdbk.zju.edu.cn', urlsplit(GRADES).path): ('grades', '成绩'),
    ('zdbk.zju.edu.cn', '/jwglxt/kaptcha'): ('timetable_captcha', '教务验证码'),
}
_JSON_PATHS = {urlsplit(url).path for url in (MENU, TIMETABLE, GRADES)}


def request_options(url, has_form):
    """Fixed wire configuration, separate from network connection management."""
    parsed = urlsplit(url)
    stage, label = _REQUEST_STAGES.get((parsed.hostname, parsed.path), ('campus', '校园服务'))
    headers = {'User-Agent': USER_AGENT, 'Accept': '*/*', 'Referer': BASE+'xtgl/index_initMenu.html'}
    is_eta = parsed.hostname == 'eta.zju.edu.cn'
    is_api = is_eta and parsed.path.startswith('/zftal-xgxt-web/') and parsed.path != urlsplit(ETA_SERVICE).path
    if is_eta:
        stage, label = 'profile_eta', 'ETA 基本信息'
        headers.update({'Referer': 'https://eta.zju.edu.cn/' if is_api else 'https://zjuam.zju.edu.cn/',
                        'Accept': 'application/json, text/plain, */*' if is_api else 'text/html, application/xhtml+xml, */*; q=0.8'})
    elif parsed.path.startswith(('/jwglxt/xsxxxggl/', '/jwglxt/xjgl/')):
        stage, label = 'profile_zdbk', '教务基本信息'
    if has_form:
        headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
        if is_eta:
            headers['Origin'] = 'https://eta.zju.edu.cn'
        if parsed.hostname == 'zdbk.zju.edu.cn' and parsed.path in _JSON_PATHS:
            headers.update({'X-Requested-With':'XMLHttpRequest', 'Accept':'application/json, text/javascript, */*; q=0.01'})
    return {'stage':stage, 'label':label, 'headers':headers,
            'timeout':20 if stage in ('timetable','profile_eta') else 12,
            'limit':8_000_000 if is_api else 2_000_000,
            'reject_statuses':(901,922,429) if is_eta else (),
            'rejection_code':'ETA_ACCESS_REJECTED' if is_eta else 'ACCESS_REJECTED',
            'rejection_message':'ETA 返回访问拒绝响应（HTTP {status}），已停止该来源请求；保留缓存并继续教务查询。可在 ETA 官网核对访问情况' if is_eta else '校园服务拒绝访问'}
