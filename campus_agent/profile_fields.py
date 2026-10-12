"""Shared six-field profile whitelist and normalization."""
import re
from .campus_common import plain

PROFILE_KEYS = ('student_id', 'name', 'college', 'major', 'class_name', 'year')
ALIASES = {
    'student_id': ('xh', 'studentid', 'student_id', '学号'),
    'name': ('xm', 'name', '姓名'),
    'college': ('jgmc', 'xymc', 'yxbmc', 'bmmc', 'collegename', 'college', '学院', '学院/园', '学院/学园', '所属学院', '院系'),
    'major': ('zymc', 'majorname', 'major', '专业', '主修专业', '所属专业'),
    'class_name': ('bjmc', 'classname', 'class_name', '行政班', '行政班级', '班级'),
    'year': ('njdm', 'njmc', 'nj', 'rxnf', 'enrollmentyear', 'grade', 'year', '年级', '入学年份', '入学年级'),
}
SCHEMA_CODES = {'college': ('xydm','xy_id','jg_id','yxdm','bmdm'), 'major': ('zydm','zy_id'),
                'class_name': ('bjdm','bj_id'), 'year': ('nj_id',)}


def text(value):
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return plain(value).strip().split('@@@')[-1]
    if isinstance(value, list) and len(value) == 1:
        return text(value[0])
    return ''


def basic_fields(row):
    if not isinstance(row, dict):
        return {}
    lookup = {str(k).lower(): v for k, v in row.items()}
    return {key: next((text(lookup[a]) for a in aliases if a in lookup and text(lookup[a])), '')
            for key, aliases in ALIASES.items()}
