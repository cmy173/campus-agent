"use strict";
let collegeNoticeGeneration = 0, collegeNoticeBusy = false;

function clearCollegeNotices() {
  collegeNoticeGeneration++;
  collegeNoticeBusy = false;
  $('college-notices-list').replaceChildren();
  $('college-notices-status').textContent = '根据当前账号已获取的学院信息匹配通知来源。';
  $('college-notices-sync').disabled = false;
}

function renderCollegeNotices(result) {
  const selection = result.selection || {};
  const root = $('college-notices-list');
  root.replaceChildren();
  const states = {
    missing: '账号尚无学院信息，请先在“我的学业”更新基本信息。',
    unsupported: '当前学院尚未接入经过验证的通知来源。',
    ambiguous: '学院信息或来源匹配存在冲突，请更新基本信息后重试。'
  };
  const messages = [];
  if (selection.college) messages.push('账号学院：' + selection.college);
  if (selection.status !== 'matched') {
    messages.push(states[selection.status] || '需要登录并获取学院信息。');
  } else {
    messages.push('已根据账号学院自动匹配');
    if (selection.profile_fetched_at) messages.push('学院信息获取于 ' + date(selection.profile_fetched_at));
    if (selection.profile_stale) messages.push('使用缓存中的学院信息，如有变更请更新基本信息');
    messages.push(result.fetched_at ? '通知上次获取：' + date(result.fetched_at) : '尚无通知缓存，点击“同步学院通知”读取公开正文');
    if (result.freshness === 'stale') messages.push('包含旧缓存，请结合原文核对');
  }
  if (result.error && selection.status === 'matched') messages.push(result.error.message);
  for (const warning of result.warnings || []) messages.push(warning.message);
  $('college-notices-status').textContent = messages.join(' · ');
  $('college-notices-sync').disabled = collegeNoticeBusy || selection.status !== 'matched';
  const records = [...(result.records || [])].sort((a,b) => b.published_at.localeCompare(a.published_at));
  for (const record of records) {
    const card = el('article', undefined, 'task-card');
    card.append(el('h3', record.title));
    card.append(el('p', record.college + ' · 发布于 ' + record.published_at + (record.stale ? ' · 旧缓存' : ''), 'footnote'));
    const source = el('a', '查看官方原文 ↗', 'text-button');
    source.href = record.source;
    source.target = '_blank';
    source.rel = 'noopener noreferrer';
    const details = el('details');
    details.append(el('summary', '展开通知正文'));
    const body = el('div', record.body, 'college-notice-body');
    details.append(body);
    for (const attachment of record.attachments || []) {
      const link = el('a', attachment.name + ' ↗', 'text-button');
      link.href = attachment.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      const item = el('p');
      item.append(link);
      details.append(item);
    }
    if (record.has_unread_content) details.append(el('p', '附件和图片只保存名称与官方链接，未下载、未解析；请在官方原文中核对完整要求。', 'footnote'));
    const save = el('button', '保存到资料库并整理', 'secondary');
    save.type = 'button';
    save.onclick = async () => {
      const epoch = sessionEpoch;
      save.disabled = true;
      try {
        // Only the record ID leaves the browser; source, attachments and owner stay server-side.
        const result = await api('/api/college-notices/import', {method:'POST', json:{record_id: record.id}});
        if (epoch !== sessionEpoch) return;
        await refresh();
        if (typeof loadNoticeAnalyses === 'function') await loadNoticeAnalyses();
        toast(result.duplicate
          ? '这条通知已在资料库中，未重复导入'
          : '已保存通知正文与 ' + result.attachments + ' 个附件链接；正在离线提取候选事项');
      } catch (error) {
        if (epoch === sessionEpoch) toast(error.message);
      } finally { if (epoch === sessionEpoch) save.disabled = false; }
    };
    card.append(source, details, save);
    root.append(card);
  }
}

async function loadCollegeNotices(sync = false) {
  if (!loggedIn || collegeNoticeBusy) return;
  const generation = ++collegeNoticeGeneration, epoch = sessionEpoch;
  collegeNoticeBusy = true;
  $('college-notices-sync').disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 55000);
  try {
    if (sync) $('college-notices-status').textContent = '正在读取学院公开通知正文…';
    const path = '/api/campus/' + (sync ? 'sync/' : 'query/') + 'college_notices';
    const result = await api(path, sync ? {method:'POST', json:{query:{}}, signal:controller.signal} : {signal:controller.signal});
    if (epoch !== sessionEpoch || generation !== collegeNoticeGeneration) return;
    collegeNoticeBusy = false;
    renderCollegeNotices(result);
  } catch (error) {
    if (epoch === sessionEpoch && generation === collegeNoticeGeneration) {
      $('college-notices-status').textContent = error.name === 'AbortError' ? '读取等待超时；已显示的缓存仍可查看，请稍后重新读取。' : error.message;
      $('college-notices-sync').disabled = false;
    }
  } finally {
    clearTimeout(timeout);
    if (epoch === sessionEpoch && generation === collegeNoticeGeneration) {
      collegeNoticeBusy = false;
    }
  }
}

$('college-notices-sync').onclick = () => loadCollegeNotices(true);
$('college-notices-profile').onclick = () => showView('campus');
