"""Conversation inputs and published output snapshots, independent of UI layout."""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import stat
from datetime import date, datetime, time, timedelta
from zipfile import BadZipFile, ZipFile
from xml.etree.ElementTree import ParseError
from pathlib import Path
from typing import Annotated, BinaryIO, Literal
from pydantic import BaseModel, Field

from ..config import MAX_UPLOAD_BYTES
from ..errors import ApiError
from .store import Store, now

LOG = logging.getLogger("app.agent_bridge.files")

# How rolling back one published file ended:
#   removed        the id is gone from the download index and no content was left behind;
#   retained       the file is still fully deliverable through the normal download entry;
#   cleanup_pending the id is gone but a content blob still needs sweeping (never advertised);
#   uncertain      neither could be established, so nothing may be claimed about it.
DiscardOutcome = Literal['removed', 'retained', 'cleanup_pending', 'uncertain']
RollbackState = Literal['rolled_back', 'retained', 'unknown']

# Columns of `conversation_files`; a row captured before a delete must be restorable verbatim.
FILE_COLUMNS = ('id', 'conversation_id', 'kind', 'name', 'size', 'sha256', 'mime_type',
                'storage_name', 'request_key', 'created_at', 'source_path')

# The stdlib MIME table does not know these Office/text types on every host, and a wrong
# type makes a real deliverable look like an unknown binary blob in the download list.
MIME_TYPES = {
    '.pdf': 'application/pdf',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.md': 'text/markdown; charset=utf-8',
}


class TextPreview(BaseModel):
    kind: Literal['text']
    text: str
    truncated: bool


class PdfPreview(BaseModel):
    kind: Literal['pdf']
    truncated: Literal[False]


class ImagePreview(BaseModel):
    kind: Literal['image']
    truncated: Literal[False]


class DownloadPreview(BaseModel):
    kind: Literal['download']
    truncated: Literal[False]
    message: str | None = None


class SpreadsheetSheetPreview(BaseModel):
    name: str
    rows: list[list[str | int | float | bool | None]]


class SpreadsheetPreview(BaseModel):
    kind: Literal['spreadsheet']
    sheets: list[SpreadsheetSheetPreview]
    truncated: bool
    has_formulas: bool


Preview = Annotated[TextPreview | PdfPreview | ImagePreview | DownloadPreview | SpreadsheetPreview,
                    Field(discriminator='kind')]


class ConversationFiles:
    def __init__(self, store: Store):
        self.store = store
        self.directory = store.path.parent / 'conversation-files'

    @staticmethod
    def public(row) -> dict:
        return {key: row[key] for key in ('id', 'conversation_id', 'kind', 'name', 'size', 'sha256', 'mime_type', 'created_at')}

    def list(self, cid: str) -> list[dict]:
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM conversation_files WHERE conversation_id=? ORDER BY created_at,id', (cid,)).fetchall()
        return [self.public(row) for row in rows]

    @staticmethod
    def _no_links(path: Path) -> None:
        """Check the un-resolved chain: resolve() alone hides links and junctions."""
        for entry in (*reversed(path.parents), path):
            try:
                metadata = entry.lstat()
            except FileNotFoundError:
                continue  # A cached delivery may outlive its workspace source.
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ApiError(403, 'SCOPE_DENIED', '文件路径包含链接或重解析点，不能交付')

    def get(self, cid: str, file_id: str) -> tuple[dict, Path]:
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM conversation_files WHERE id=? AND conversation_id=?', (file_id, cid)).fetchone()
        if row is None:
            raise ApiError(404, 'NOT_FOUND', '文件不属于当前会话或已不可用')
        return self.public(row), self._local_path(row['storage_name'])

    def save(self, cid: str, stream: BinaryIO, name: str, kind: str, request_key: str, source_path: str = '') -> dict:
        name = re.split(r'[/\\]', name)[-1].strip()
        if not name or len(name) > 240 or any(ord(c) < 32 for c in name):
            raise ApiError(400, 'INVALID_INPUT', '文件名不能为空、含控制字符或超过240个字符')
        suffix = Path(name).suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,12}', suffix):
            suffix = '.bin'
        fid = 'file_' + secrets.token_hex(16)
        self._no_links(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._no_links(self.directory)
        target = self.directory / (fid + suffix)
        size, sha = 0, hashlib.sha256()
        try:
            with target.open('xb') as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ApiError(413, 'FILE_TOO_LARGE', f'文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB 上限')
                    sha.update(chunk)
                    output.write(chunk)
            mime = MIME_TYPES.get(suffix) or mimetypes.guess_type(name)[0] or 'application/octet-stream'
            stamp = now()
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT * FROM conversation_files WHERE conversation_id=? AND request_key=?', (cid, request_key)).fetchone()
                if old:
                    if (old['name'], old['sha256'], old['kind']) != (name, sha.hexdigest(), kind):
                        raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '同一文件请求键不能用于不同内容')
                    target.unlink()
                    return self.public(old)
                db.execute('INSERT INTO conversation_files VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (fid, cid, kind, name, size, sha.hexdigest(), mime, target.name, request_key, stamp, source_path))
            return {'id': fid, 'conversation_id': cid, 'kind': kind, 'name': name, 'size': size,
                    'sha256': sha.hexdigest(), 'mime_type': mime, 'created_at': stamp}
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def discard(self, cid: str, file_id: str) -> DiscardOutcome:
        """Remove a file this request just published, so a failed batch delivers nothing.

        Deliberately narrow: only a row of *this* conversation is touched. A caller can therefore
        never delete another conversation's file.

        The index row goes first, and the content deletion is *verified*: if the content cannot be
        deleted, the exact captured row values are put back, so the file stays genuinely
        downloadable and is reported as retained. That keeps the only dangerous state — a live
        index row pointing at missing content — from ever being the outcome: either the row is gone
        (with at most an unadvertised blob left to sweep) or the row is back with its content
        present. The unlink target itself is validated with the same check the download entry uses,
        so a storage name that is not a plain file of this directory is refused, not followed.

        Every `retained` return is decided by that same download check (`servable`), never by the
        mere existence of an index row: an index row whose content is already missing is leftover
        state to repair, not a deliverable file.
        """
        row_deleted = False
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM conversation_files WHERE id=? AND conversation_id=?',
                             (file_id, cid)).fetchone()
            if row is None:
                return 'uncertain'
            values = dict(row)
            try:
                db.execute('DELETE FROM conversation_files WHERE id=? AND conversation_id=?', (file_id, cid))
                row_deleted = True
            except sqlite3.Error as exc:
                # The row survived, but a row alone does not prove the content can be served: the
                # content may already be gone, in which case this is leftover state to repair.
                LOG.warning('file index removal failed for %s: %s: %s', file_id, type(exc).__name__, exc)
                if self.servable(cid, file_id) is not False:
                    return 'retained'
        if not row_deleted:
            # The content was verified missing, so the surviving row can serve nothing. Drop it if
            # the store lets us (its own transaction, never nested); if it cannot be dropped, the
            # leftover row is still a known cleanup item, not an unknown state.
            if not self._forget_orphan_row(cid, file_id):
                LOG.warning('orphaned index row %s (content missing) still needs cleanup', file_id)
            return 'cleanup_pending'
        try:
            path = self._local_path(values['storage_name'])
        except ApiError:
            # The stored name is not a plain file of this session's directory, so there is nothing
            # to unlink. The row is already gone: only an unadvertised cleanup item remains, unless
            # the name happens to describe a restorable plain file after all.
            return 'retained' if self._restore(cid, values) else 'cleanup_pending'
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            LOG.warning('file content deletion failed for %s (%s): %s; restoring the index row',
                        file_id, type(exc).__name__, exc)
            if self._restore(cid, values):
                return 'retained'
            return 'cleanup_pending'
        return 'removed'

    def _local_path(self, storage_name: str) -> Path:
        """A stored blob path, or a refusal when it is not a plain file of this directory.

        The `unlink` in `discard` must never be steered outside the session file area by a tampered
        or unexpected `storage_name`. This is the exact check the download entry uses, so
        "advertised as retained" and "servable by `get`" cannot diverge.
        """
        try:
            path = self.directory / storage_name
            self._no_links(path)
            if not path.is_file() or path.resolve().parent != self.directory.resolve():
                raise ValueError()
        except (ApiError, OSError, RuntimeError, ValueError):
            raise ApiError(404, 'NOT_FOUND', '文件内容已不可用') from None
        return path

    def _content_present(self, path: Path) -> bool:
        try:
            return path.is_file()
        except OSError as exc:  # pragma: no cover - defensive; a status read must not raise here
            LOG.warning('could not check file content presence for %s: %s: %s',
                        path.name, type(exc).__name__, exc)
            return False

    def servable(self, cid: str, file_id: str) -> bool | None:
        """Whether the *normal conversation download entry* can really serve this id.

        True only when the index row exists, the stored name is a plain file of this session's
        directory (no link/reparse point, no escape) and the content is present — the same checks
        `get` performs. False means it cannot be served. None means the question could not be
        answered (e.g. the store is temporarily unreadable), which must never be reported as
        downloadable.
        """
        try:
            with self.store.connection() as db:
                row = db.execute('SELECT storage_name FROM conversation_files WHERE id=? AND conversation_id=?',
                                 (file_id, cid)).fetchone()
        except Exception as exc:  # noqa: BLE001 - an unknown state must not become a receipt
            LOG.warning('could not read file index for %s: %s: %s', file_id, type(exc).__name__, exc)
            return None
        if row is None:
            return False
        try:
            self._local_path(row['storage_name'])
        except ApiError:
            return False
        except Exception as exc:  # noqa: BLE001 - an unknown state must not become a receipt
            LOG.warning('could not verify file content for %s: %s: %s', file_id, type(exc).__name__, exc)
            return None
        return True

    def _forget_orphan_row(self, cid: str, file_id: str) -> bool:
        """Drop an index row whose content is already gone, so it is not left as a broken receipt.

        Used only after the content was verified missing: the row can no longer serve anything, and
        keeping it would leave a permanent 404 in the user's file list.
        """
        try:
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM conversation_files WHERE id=? AND conversation_id=?', (file_id, cid))
        except sqlite3.Error as exc:
            LOG.warning('could not remove orphaned index row %s: %s: %s', file_id, type(exc).__name__, exc)
            return False
        return True

    def _restore(self, cid: str, values: dict) -> bool:
        """Reinstate a captured index row only when its content is still present.

        Returns whether the id is downloadable again; an id that cannot be restored honestly stays
        an unadvertised cleanup item instead of a receipt the user cannot open.
        """
        if not self._content_present(self.directory / values['storage_name']):
            return False
        try:
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                columns = ','.join(FILE_COLUMNS)
                placeholders = ','.join('?' for _ in FILE_COLUMNS)
                db.execute(f'INSERT OR IGNORE INTO conversation_files ({columns}) VALUES ({placeholders})',
                           tuple(values.get(column, '') for column in FILE_COLUMNS))
            return self.servable(cid, values['id']) is True
        except Exception as exc:  # noqa: BLE001 - reported as pending cleanup, never as deliverable
            LOG.warning('could not restore file index row %s: %s: %s',
                        values.get('id'), type(exc).__name__, exc)
            return False

    def discard_state(self, cid: str, file_id: str) -> RollbackState:
        """Classify one batch member after a rollback attempt, by the download entry's own check.

        `retained` requires the id to be genuinely servable — index row *and* valid, present
        content — never the mere absence of an exception or the mere existence of a row. A row whose
        content is missing is leftover state to repair, so it is classified as not deliverable.
        """
        state = self.servable(cid, file_id)
        if state is None:
            return 'unknown'
        return 'retained' if state else 'rolled_back'

    def _publish_source(self, cid: str, source: Path, workspace: Path, attachment_ids: list[str]) -> Path:
        """Only the runtime workspace or exact server-owned selected copies grant access.

        Neither a caller path nor a saved source_path grants directory access. Keep the
        original path until after lstat checks so symlinks/reparse points cannot disappear
        behind realpath normalization (including an intermediate link followed by '..').
        """
        try:
            root = Path(os.path.abspath(workspace))
            source = source.expanduser()
            if not source.is_absolute():
                source = root / source
            lexical = Path(os.path.abspath(source))
            if lexical.is_relative_to(root):
                self._no_links(root)
                self._no_links(source)
                real_root = root.resolve(strict=True)
                resolved = source.resolve()
                if not real_root.is_dir() or not resolved.is_relative_to(real_root):
                    raise ApiError(403, 'SCOPE_DENIED', '文件不在本轮任务目录或已授权附件范围内')
                return resolved
            # The conversation-files directory is shared across conversations. It is
            # never a granted root; only copies selected by the user for this turn count.
            with self.store.connection() as db:
                rows = db.execute('SELECT id,storage_name FROM conversation_files WHERE conversation_id=?', (cid,)).fetchall()
            for row in rows:
                if row['id'] not in attachment_ids:
                    continue
                expected = Path(os.path.abspath(self.directory / row['storage_name']))
                if lexical != expected:
                    continue
                _, allowed = self.get(cid, row['id'])
                self._no_links(source)
                resolved = source.resolve()
                if resolved == allowed.resolve():
                    return resolved
            raise ApiError(403, 'SCOPE_DENIED', '文件不在本轮任务目录或已授权附件范围内')
        except (OSError, RuntimeError, ValueError):
            raise ApiError(403, 'SCOPE_DENIED', '无法验证文件路径的授权范围') from None

    def publish(self, cid: str, source: Path, key: str, *, workspace: Path, attachment_ids: list[str] | None = None) -> dict:
        # Check before looking up an idempotent snapshot too: replay must not confer
        # authority on a newly substituted link or an old, formerly-unrestricted path.
        requested = source
        source = self._publish_source(cid, requested, workspace, attachment_ids or [])
        with self.store.connection() as db:
            old = db.execute('SELECT * FROM conversation_files WHERE conversation_id=? AND request_key=?', (cid, key)).fetchone()
        if old:
            if old['source_path'] != str(source):
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '同一交付调用不能用于不同文件')
            return self.get(cid, old['id'])[0]
        if not source.is_file():
            raise ApiError(404, 'NOT_FOUND', '需要交付的文件不存在或不是普通文件')
        try:
            # Revalidate the original chain next to the read; O_NOFOLLOW additionally
            # closes a final-component substitution race on platforms that support it.
            self._publish_source(cid, requested, workspace, attachment_ids or [])
            descriptor = os.open(source, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise ApiError(404, 'NOT_FOUND', '需要交付的文件不存在或不是普通文件')
                checked = self._publish_source(cid, requested, workspace, attachment_ids or [])
                if checked != source or not os.path.samestat(opened, checked.stat()):
                    raise ApiError(403, 'SCOPE_DENIED', '文件路径在交付前已变化')
                return self.save(cid, stream, source.name, 'output', key, str(source))
        except OSError:
            raise ApiError(400, 'FILE_UNREADABLE', '无法读取需要交付的文件') from None

    def references(self, turn: dict) -> list[dict]:
        references = []
        for fid in turn.get('attachment_ids', []):
            item, path = self.get(turn['conversation_id'], fid)
            references.append(item | {'path': str(path.resolve())})
        return references

    def preview(self, cid: str, fid: str) -> dict:
        item, path = self.get(cid, fid)
        suffix = Path(item['name']).suffix.lower()
        if suffix == '.pdf':
            return {'kind': 'pdf', 'truncated': False}
        if suffix in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:
            return {'kind': 'image', 'truncated': False}
        if suffix == '.xlsx':
            return self._spreadsheet_preview(path)
        if suffix in {'.txt', '.md', '.csv', '.json', '.yaml', '.yml', '.py', '.js', '.ts', '.html', '.xml', '.log', '.rst'}:
            with path.open('rb') as stream:
                raw = stream.read(512 * 1024 + 1)
            return {'kind': 'text', 'text': raw[:512 * 1024].decode('utf-8-sig', errors='replace'), 'truncated': len(raw) > 512 * 1024}
        return {'kind': 'download', 'truncated': False}

    @staticmethod
    def _spreadsheet_preview(path: Path) -> dict:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
        try:
            with ZipFile(path) as archive:
                if len(archive.infolist()) > 2048 or sum(i.file_size for i in archive.infolist()) > 64 * 1024 * 1024:
                    return {'kind': 'download', 'truncated': False, 'message': '表格较大，请下载后打开完整内容。'}
            book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
            try:
                sheets = []
                truncated, formulas = len(book.worksheets) > 8, False
                for sheet in book.worksheets[:8]:
                    truncated |= (sheet.max_row or 0) > 100 or (sheet.max_column or 0) > 24
                    rows = []
                    for cells in sheet.iter_rows(max_row=min(sheet.max_row or 100, 100), max_col=min(sheet.max_column or 24, 24)):
                        row = []
                        for cell in cells:
                            value = cell.value
                            if isinstance(value, (date, datetime, time)):
                                value = value.isoformat()
                            elif isinstance(value, timedelta):
                                value = str(value)
                            if isinstance(value, str) and len(value) > 2000:
                                value = value[:2000] + '…'
                                truncated = True
                            formulas |= cell.data_type == 'f'
                            row.append(value)
                        rows.append(row)
                    sheets.append({'name': sheet.title, 'rows': rows})
                return {'kind': 'spreadsheet', 'sheets': sheets, 'truncated': bool(truncated), 'has_formulas': bool(formulas)}
            finally:
                book.close()
        except (BadZipFile, OSError, ValueError, KeyError, ParseError, InvalidFileException):
            raise ApiError(400, 'FILE_PREVIEW_FAILED', '无法读取此 Excel 文件，请确认文件格式或下载后打开') from None
