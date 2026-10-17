"""提取文档公开许可声明。结果是来源证据，不是法律结论或用户使用权鉴定。"""
from __future__ import annotations
import hashlib
import json
import re
from sqlalchemy import select
from .models import ContentChunk, Resource, TextbookRightsReport, TextbookSettings, iso_utc, utc_now


def settings_view(db):
    row = db.get(TextbookSettings, 1)
    return {'rights_check_enabled': row.rights_check_enabled if row else True}


def book_fingerprint(db, book):
    resource = db.get(Resource, book.resource_id) if book.resource_id else None
    value = {key: getattr(book, key) for key in ('title', 'author', 'edition', 'isbn', 'source_url', 'resource_id')}
    value['file_hash'] = resource.content_hash if resource else None
    value['file_version'] = resource.version if resource else None
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def classify_declarations(text: str):
    license_links = list(dict.fromkeys(re.findall(r'https?://creativecommons\.org/(?:licenses/[a-z-]+/[\d.]+|publicdomain/(?:zero|mark)/[\d.]+)/?', text, re.I)))
    restrictions = re.search(r'(?:未经.{0,35}(?:许可|同意).{0,40}(?:不得|禁止)|all rights reserved|版权所有.{0,25}(?:不得|禁止))', text, re.I)
    if license_links and restrictions:
        return {'state': 'conflicting', 'evidence': license_links[:5] + [restrictions.group(0)],
                'message': '同时发现许可链接与限制声明，适用范围可能不同；无法自动确定授权。'}
    if license_links:
        return {'state': 'declaration_found', 'evidence': license_links[:5],
                'message': '检测到公开许可链接；尚未核实声明者是否为权利人、是否适用于当前版本。'}
    if restrictions:
        return {'state': 'restriction_found', 'evidence': [restrictions.group(0)],
                'message': '检测到版权保留或限制声明；这不是对用户个人使用是否合法的判断。'}
    return {'state': 'unknown', 'evidence': [], 'message': '未发现足够许可信息；免费、教材身份和没有禁止声明均不作为已授权证据。'}


def check_book(db, book):
    if not settings_view(db)['rights_check_enabled']:
        return {'state': 'disabled', 'evidence': [], 'message': '授权信息检查已关闭；不会自动作出授权判断。'}
    resource = db.get(Resource, book.resource_id) if book.resource_id else None
    chunks = []
    if resource and resource.parse_status == 'ready':
        chunks = list(db.scalars(select(ContentChunk).where(ContentChunk.resource_id == resource.id,
                            ContentChunk.resource_version == resource.version).order_by(ContentChunk.ordinal).limit(300)))
    # Check title/copyright/front matter, not the full book or private student materials.
    text = '\n'.join(chunk.text for chunk in chunks if (chunk.locator or {}).get('page', 1) <= 10)[:30000]
    result = classify_declarations(text)
    result.update({'source': 'local_document', 'textbook_id': book.id, 'checked_at': iso_utc(utc_now()),
                   'notice': '检查仅供参考，不自动阻止教材解析或出卷；来源与个人使用权需分别理解。'})
    row = db.get(TextbookRightsReport, book.id)
    if row is None:
        row = TextbookRightsReport(textbook_id=book.id)
        db.add(row)
    row.fingerprint = book_fingerprint(db, book)
    row.result = result
    row.checked_at = utc_now()
    db.flush()
    return result


def saved_report(db, book):
    if not settings_view(db)['rights_check_enabled']:
        return {'state': 'disabled', 'evidence': [], 'message': '授权信息检查已关闭。'}
    row = db.get(TextbookRightsReport, book.id)
    if row is None:
        return {'state': 'not_checked', 'evidence': [], 'message': '尚未检查。'}
    if row.fingerprint != book_fingerprint(db, book):
        return {'state': 'stale', 'evidence': [], 'message': '教材版本或文件已变化，旧检查不再适用于当前文件。'}
    return row.result
