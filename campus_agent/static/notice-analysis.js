"use strict";
// Label maps are shared with app.js, which is loaded first.
let noticeDocs = [], noticeGeneration = 0, noticeModel = {configured:false}, noticePoll = null;

function clearNoticeAnalyses() {
  noticeGeneration++;
  clearTimeout(noticePoll);
  noticeDocs = [];
  $('notice-analysis-list').replaceChildren();
  const consent = $('notice-model-consent');
  consent.checked = false;
  consent.disabled = true;
  $('notice-model-provider').textContent = '未配置模型';
  $('notice-analysis-status').textContent = '保存通知后，可离线规则提取，或在明确同意后使用模型整理事件。';
}

function link(url, label, className) {
  const node = el('a', label + ' ↗', className || 'text-button');
  node.href = url;
  node.target = '_blank';
  node.rel = 'noopener noreferrer';
  return node;
}

function renderSource(card, doc) {
  const source = doc.source;
  if (!source) {
    card.append(el('p', '手动上传的资料：没有官方来源记录，附件清单不可用。', 'footnote'));
    return;
  }
  const meta = el('p', source.college + ' · 发布于 ' + source.published_at + ' · 记录 ' + source.record_id +
    ' · 正文版本 ' + source.body_sha256.slice(0, 12), 'footnote');
  card.append(meta, link(source.origin, '查看官方原文'));
  if (!source.attachments.length) {
    card.append(el('p', '这条通知没有发现附件。', 'footnote'));
    return;
  }
  const box = el('div', undefined, 'notice-attachments');
  box.append(el('strong', '附件（' + source.attachments.length + ' 个，均未下载、未解析）'));
  for (const item of source.attachments) {
    const row = el('p');
    row.append(link(item.url, item.name), el('span', '未解析', 'badge amber'),
      el('small', ' ' + item.ref, 'footnote'));
    box.append(row);
  }
  if (source.has_unread_content) box.append(el('p', '正文还可能包含未解析的图片或外部链接。', 'footnote'));
  card.append(box);
}

function renderTime(row, item) {
  row.append(el('span', TIME_KIND_LABELS[item.kind] || item.kind, 'badge'),
    el('span', item.value ? date(item.value) : '时间待核对'),
    el('small', '原文：' + item.raw + ' · ' + (TIME_PRECISION_LABELS[item.precision] || item.precision), 'footnote'));
  if (item.derived) row.append(el('small', '推导：' + item.derivation + '（待核对）', 'footnote'));
  if (item.conflict) row.append(el('span', '与其他时间矛盾，未自动消解', 'error'));
}

function renderEvent(card, doc, event, attachments) {
  const box = el('article', undefined, 'notice-event');
  box.append(el('h4', event.title));
  if (event.summary) box.append(el('p', event.summary));
  if (event.audience.length) box.append(el('p', '适用对象：' + event.audience.join('、'), 'footnote'));
  if (event.actions.length) {
    const list = el('ul', undefined, 'notice-actions');
    for (const action of event.actions) list.append(el('li', action));
    box.append(list);
  }
  for (const item of event.times) {
    const row = el('p', undefined, 'notice-time');
    renderTime(row, item);
    box.append(row);
  }
  if (event.location) box.append(el('p', '地点：' + event.location));
  else box.append(el('p', event.location_needs_attachment ? '地点：需查看附件，原文未给出' : '地点：原文未给出', 'footnote'));
  if (event.attachment_refs.length) {
    const names = event.attachment_refs.map(ref => {
      const found = attachments.find(a => a.ref === ref);
      return found ? found.name + '（未解析）' : ref;
    });
    box.append(el('p', '关联附件：' + names.join('、'), 'footnote'));
  }
  for (const question of event.open_questions) box.append(el('p', '待核对：' + question, 'error'));
  const evidence = el('div', undefined, 'notice-evidence');
  evidence.append(el('strong', '原文依据（引用已定位，语义仍需人工核对）'));
  for (const item of event.evidence) {
    const button = el('button', undefined, 'citation');
    button.append(el('strong', item.ref + ' ↗'), el('span', item.quote));
    const chunk = item.chunk;
    button.onclick = () => openSource(doc.document, chunk);
    evidence.append(button);
  }
  box.append(evidence);
  card.append(box);
}

function renderAnalysis(card, doc, analysis) {
  const model = analysis.extractor !== 'rules';
  const block = el('section', undefined, 'analysis-block');
  const head = el('div', undefined, 'task-meta');
  head.append(el('strong', model ? '模型整理 · ' + analysis.model : '离线规则提取'),
    el('span', ANALYSIS_STATUS_LABELS[analysis.status] || analysis.status,
      'badge ' + (analysis.status === 'failed' ? 'red' : analysis.status === 'ready' ? '' : 'amber')));
  if (analysis.result_at) head.append(el('small', '结果时间 ' + date(analysis.result_at), 'footnote'));
  if (analysis.usage && analysis.usage.total_tokens) {
    head.append(el('small', 'tokens ' + analysis.usage.total_tokens + ' · ' +
      (analysis.usage.latency_ms || 0) + ' ms', 'footnote'));
  }
  block.append(head);
  if (analysis.error) block.append(el('p', analysis.error.message + '（' + analysis.error.code + '）', 'error'));
  const payload = analysis.payload || {};
  if (analysis.status === 'failed' && analysis.result_at) {
    block.append(el('p', '本次整理失败，下方仍显示上次成功的结果。', 'footnote'));
  }
  if (payload.coverage_note) block.append(el('p', payload.coverage_note, 'footnote'));
  if (payload.notice_summary) block.append(el('p', payload.notice_summary));
  const attachments = (doc.source && doc.source.attachments) || [];
  for (const event of payload.events || []) renderEvent(block, doc, event, attachments);
  if (model && ['ready', 'partial'].includes(analysis.status) && !(payload.events || []).length) {
    block.append(el('p', '模型没有从已分析的正文中识别出可确认的事件。', 'empty'));
  }
  for (const question of payload.open_questions || []) block.append(el('p', '待核对：' + question, 'error'));
  if (payload.evidence_corrected) {
    block.append(el('p', '有 ' + payload.evidence_corrected + ' 条依据的段落归属由服务端更正。', 'footnote'));
  }
  card.append(block);
}

const DOCUMENT_STATUS = {queued:'等待解析', processing:'正在解析', ready:'已整理', failed:'解析失败'};

function renderNoticeAnalyses() {
  const root = $('notice-analysis-list');
  root.replaceChildren();
  if (!noticeDocs.length) {
    root.append(el('div', '还没有通知类资料。可在上方同步学院通知后保存，或从“我的资料”上传通知。', 'empty'));
    return;
  }
  for (const doc of noticeDocs) {
    const card = el('article', undefined, 'task-card');
    card.append(el('h3', doc.name));
    const running = doc.analyses.some(a => a.status === 'running' || a.status === 'queued');
    card.append(el('p', running ? '正在整理，请稍候…'
      : '资料状态：' + (DOCUMENT_STATUS[doc.status] || doc.status), 'footnote'));
    renderSource(card, doc);
    for (const analysis of doc.analyses) renderAnalysis(card, doc, analysis);
    const actions = el('div', undefined, 'row-actions');
    const rules = el('button', '离线重新提取', 'text-button');
    rules.type = 'button';
    rules.disabled = running;
    rules.onclick = () => analyze(doc.document, 'rules', rules);
    actions.append(rules);
    if (noticeModel.configured) {
      const button = el('button', running ? '整理中…' : '用模型整理事件', 'secondary');
      button.type = 'button';
      button.disabled = running || !$('notice-model-consent').checked;
      button.onclick = () => analyze(doc.document, 'model', button);
      actions.append(button);
    }
    const view = el('button', '查看原文', 'text-button');
    view.type = 'button';
    view.onclick = () => openSource(doc.document);
    actions.append(view);
    card.append(actions);
    if (noticeModel.configured && !running) {
      card.append(el('p', $('notice-model-consent').checked
        ? '已同意发送：将把通知标题、正文段落与附件名称发送给 ' + noticeModel.provider_label + '。'
        : '勾选上方同意项后，才能把通知内容发送给 ' + noticeModel.provider_label + ' 整理。', 'footnote'));
    }
    root.append(card);
  }
}

async function analyze(docId, mode, button) {
  const epoch = sessionEpoch, generation = noticeGeneration;
  if (button) button.disabled = true;
  try {
    await api('/api/documents/' + docId + '/analysis',
      {method:'POST', json: mode === 'model' ? {mode, consent:true} : {mode}});
    if (epoch !== sessionEpoch || generation !== noticeGeneration) return;
    await loadNoticeAnalyses();
    await refresh();
    if (mode === 'model') toast('已提交模型整理；结果需人工核对');
  } catch (error) {
    if (epoch === sessionEpoch && generation === noticeGeneration) toast(error.message);
  }
}

async function loadNoticeAnalyses() {
  if (!loggedIn) return;
  const generation = ++noticeGeneration, epoch = sessionEpoch;
  try {
    const result = await api('/api/notice-analyses');
    if (epoch !== sessionEpoch || generation !== noticeGeneration) return;
    noticeDocs = result.documents || [];
    noticeModel = result.model || {configured:false};
    const consent = $('notice-model-consent');
    consent.disabled = !noticeModel.configured;
    if (!noticeModel.configured) consent.checked = false;
    $('notice-model-provider').textContent = noticeModel.configured
      ? noticeModel.provider_label + ' · ' + noticeModel.model
      : '未配置模型（DEEPSEEK_API_KEY）';
    $('notice-analysis-status').textContent = noticeModel.configured
      ? '模型整理会发送：' + (noticeModel.sends || '通知正文与附件名称') + '。密钥不经过浏览器。'
      : '未配置模型；仍可保存通知并使用离线规则提取。';
    renderNoticeAnalyses();
    clearTimeout(noticePoll);
    if (noticeDocs.some(d => d.analyses.some(a => a.status === 'running' || a.status === 'queued'))) {
      noticePoll = setTimeout(() => { if (sessionEpoch === epoch) loadNoticeAnalyses().catch(() => {}); }, 2000);
    }
  } catch (error) {
    if (epoch === sessionEpoch && generation === noticeGeneration) {
      $('notice-analysis-status').textContent = error.message;
    }
  }
}

$('notice-model-consent').onchange = () => renderNoticeAnalyses();
