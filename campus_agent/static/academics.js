"use strict";
let academicSnapshot=null, academicData=null, academicDay='', academicTerm='', academicRequest=0;
let gradeTerm='', gradePage=0, academicCalendarBusy=false;
let calendarAutoTried=new Set();
const academicWeekdays=['一','二','三','四','五','六','日'];
function academicISO(value=new Date()) {
  return new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(value);
}
function academicShift(day,days){return new Date(Date.parse(day+'T12:00:00+08:00')+days*864e5).toISOString().slice(0,10);}
function academicMetric(label,value,detail) {
  const card=el('article',undefined,'academic-stat');card.append(el('span',label),el('strong',value),el('small',detail));return card;
}
function resetAcademicViews() {
  academicSnapshot=academicData=null;academicDay=academicTerm=gradeTerm='';gradePage=0;academicRequest++;academicCalendarBusy=false;calendarAutoTried.clear();
  for(const id of ['campus-overview','campus-today-courses','campus-today-tasks','campus-calendar-state','campus-calendar-sources','campus-course-detail','campus-term'])$(id).replaceChildren();
  $('campus-course-title').textContent='课程详情';$('campus-week-title').textContent='';$('campus-today-date').textContent='';
  $('campus-grades-dialog').close();$('campus-course-dialog').close();$('campus-connection').open=false;
}
function setAcademicSnapshot(snapshot) {
  academicSnapshot=snapshot;
  loadAcademicViews().catch(e=>{$('campus-calendar-state').textContent=e.message;});
}
function academicTerms(){
  const section=academicSnapshot?.courses;
  return section?.terms||(section?.year?[{...section,term:section.year+'–'+(Number(section.year)+1)+' · '+({'3':'秋冬','12':'春夏','16':'短学期'}[section.semester]||section.semester)}]:[]);
}
async function loadAcademicViews() {
  const epoch=sessionEpoch,serial=++academicRequest;
  const params=new URLSearchParams();if(academicDay)params.set('day',academicDay);if(academicTerm)params.set('term',academicTerm);
  const [data,latestTasks]=await Promise.all([api('/api/campus/academics?'+params),api('/api/tasks')]);
  if(epoch!==sessionEpoch||serial!==academicRequest)return;
  academicData=data;tasks=latestTasks;
  const select=$('campus-term');select.replaceChildren(new Option('选择学期',''));
  const terms=academicTerms();
  terms.forEach(t=>select.append(new Option(t.term+(t.current?(t.range_source==='fallback'?' · 推定当前':' · 教务默认'):'')+(t.stale?' · 上次数据':''),t.year+'/'+t.semester)));
  if(academicTerm&&!terms.some(t=>t.year+'/'+t.semester===academicTerm))academicTerm='';
  select.value=academicTerm||data.agenda.term_key;
  $('campus-calendar-refresh').disabled=academicCalendarBusy||!select.value;
  renderAcademicToday();renderAcademicWeek();renderAcademicGrades();
  renderAcademicMetrics();
  const key=data.agenda.term_key,calendar=data.agenda.calendar;
  if(accountMode==='passport'&&calendar&&!calendarAutoTried.has(key)&&
     (calendar.source_status==='fallback'||calendar.stale)){
    calendarAutoTried.add(key);readAcademicCalendar(false);
  }
}
function renderAcademicMetrics() {
  const g=academicData.grades.overall,a=academicData.today_agenda;
  const todayEvents=a.events.filter(e=>e.date===a.today);
  const next=todayEvents.find(e=>Date.parse(e.end)>Date.now());
  const overview=$('campus-overview');overview.replaceChildren(
    academicMetric('今日课程',a.dated?String(todayEvents.length):'—',a.unplaced.length?a.unplaced.length+' 条安排未能定位':a.source_status==='fallback'?'教务课程 · 日期推算':a.source_status==='cache'?'教务课程 · 校历缓存':a.term||'暂无可用课表'),
    academicMetric(next&&Date.parse(next.start)<=Date.now()?'正在上课':'接下来',next?next.time:'—',next?next.name:'今天剩余安排见下方'),
    academicMetric('加权均绩',g.gpa==null?'—':g.gpa.toFixed(2),g.included+' 条参与 · 点击“成绩与均绩”查看口径'));
}
function renderAcademicToday() {
  if(!academicData)return;
  const a=academicData.today_agenda,root=$('campus-today-courses');root.replaceChildren();
  $('campus-today-date').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric',weekday:'long'}).format(new Date(a.today+'T12:00:00+08:00'));
  const list=a.events.filter(e=>e.date===a.today);
  if(!a.dated)root.append(el('p','尚无可用日期规则，请查看星期课表。','footnote'));
  else if(!list.length)root.append(el('p',a.unplaced.length?'部分课程时间未识别，请查看来源字段。':'当前课表安排中今天没有课程。','footnote'));
  if(a.source_status==='fallback')root.append(el('p','按教务课程和默认日期展示，日期为推算；停课、调课以最新通知为准。','footnote'));
  for(const event of list){
    const button=academicCourseButton(event,'academic-agenda-course');
    const state=Date.now()<Date.parse(event.start)?'未开始':Date.now()<Date.parse(event.end)?'上课中':'已结束';
    button.append(el('span',state+(event.conflict?' · 时间冲突':''),'badge '+(event.conflict?'amber':'')));
    root.append(button);
  }
  if(a.stale)root.append(el('p','正在使用已缓存课表'+(a.fetched_at?' · 最后成功获取：'+date(a.fetched_at):'')+'；临时调课以学校通知为准。','footnote'));
  if(a.unplaced.length)root.append(el('p',a.unplaced.length+' 条时间规则未识别，未计入今日课程。','error'));
  renderAcademicTasks();
}
function renderAcademicTasks() {
  const root=$('campus-today-tasks');root.replaceChildren();
  const today=academicISO(),pending=tasks.filter(t=>t.state==='pending');
  const list=pending.filter(t=>t.due&&academicISO(new Date(t.due))===today).sort((a,b)=>Date.parse(a.due)-Date.parse(b.due));
  if(!list.length)root.append(el('p','今天没有已标明截止日期的未完成事项。','footnote'));
  for(const task of list){
    const card=el('article',undefined,'academic-task');
    card.append(el('strong',task.title),el('small',date(task.due)+' · '+(task.reviewed?'已核对':'时间待核对'),'footnote'));
    const source=el('button',task.name+' · '+task.locator+' ↗','text-button');source.onclick=()=>openSource(task.document,task.chunk);card.append(source);root.append(card);
  }
  const unknown=pending.filter(t=>!t.due).length,overdue=pending.filter(t=>t.due&&academicISO(new Date(t.due))<today).length;
  const all=el('button','查看全部待办'+(overdue?' · '+overdue+' 项逾期':'')+(unknown?' · '+unknown+' 项时间待核对':''),'text-button');
  all.onclick=()=>showView('tasks');root.append(all);
}
function academicCourseButton(event,className='academic-course') {
  const button=el('button',undefined,className);
  button.setAttribute('aria-label',(event.date||('周'+academicWeekdays[Number(event.weekday)-1]))+' '+event.name+' '+(event.time||'节次待核对')+' '+(event.location||'地点未提供'));
  button.append(el('small',event.time||('第 '+(event.first_period||event.start_period||'?')+'–'+(event.last_period||'?')+' 节')),el('strong',event.name),el('span',event.location||'地点未提供'));
  if(event.provisional)button.append(el('small',event.stale?'上次课表':event.source_status==='fallback'?'日期推算':event.source_status==='cache'?'校历缓存':'部分规则按默认展示','academic-provisional'));
  if(event.conflict)button.classList.add('conflict');
  button.onclick=()=>{
    $('campus-course-title').textContent=event.name;const root=$('campus-course-detail');root.replaceChildren();
    campusTable(root,['字段','来源内容'],[['日期',event.date||'校历未核对'],['时间',event.time||'只提供节次'],['课程号',event.code],['教师',event.teacher],['地点',event.location],['星期',event.weekday],['节次',event.periods||event.start_period+' 起 / '+event.duration+' 节'],['周次',event.weeks],['单双周',{'0':'单周','1':'双周','2':'单双周'}[event.odd_even]||event.odd_even],['季学期',event.season],['教务获取时间',date(event.fetched_at||academicData.agenda.fetched_at)]]);
    if(event.teaching_date&&event.teaching_date!==event.date)root.append(el('p','按校方调课安排，补上 '+event.teaching_date+' 的课程。','footnote'));
    root.append(el('p','来源：本科教务网课表；'+(event.date?(event.source_status==='fallback'?'日期由默认规则推算。':event.source_status==='cache'?'日期使用同学期校历缓存。':'日期时间按校历映射。'):'尚未转换为具体日期。')+'课程为只读安排。','footnote'));
    for(const assumption of event.assumptions||[])root.append(el('p',assumption,'footnote'));
    $('campus-course-dialog').showModal();
  };return button;
}
function renderAcademicWeek() {
  const a=academicData.agenda,root=$('campus-courses');root.replaceChildren();
  $('campus-week-title').textContent=a.dated?a.week_start+' — '+academicShift(a.week_start,6):'星期课表 · 日期待核对';
  for(const id of ['campus-week-prev','campus-week-next','campus-week-today'])$(id).disabled=!a.dated;
  const state=$('campus-calendar-state');state.replaceChildren(el('p',a.message,'footnote'));
  if(a.stale)state.append(el('p','正在使用已缓存课表'+(a.fetched_at?' · 最后成功获取：'+date(a.fetched_at):'')+'；更新暂不可用时仍可正常查看。','footnote'));
  if(a.uncertain_dates?.length)state.append(el('p','以下日期暂按常规课表展示，活动或调课安排以通知为准：'+a.uncertain_dates.join('、'),'footnote'));
  if(!a.term_key){renderCalendarSources(null);return;}
  const term=academicTerms().find(t=>t.year+'/'+t.semester===a.term_key);
  let entries=a.events,unplaced=a.unplaced;
  if(!a.dated){
    entries=[];unplaced=[];
    for(const row of term.data){
      const span=academicPeriods(row);const weekday=Number(row.weekday);
      if(!span||!Number.isInteger(weekday)||weekday<1||weekday>7){unplaced.push(row);continue;}
      entries.push({...row,first_period:span[0],last_period:span[1],day_index:weekday-1,time:'第 '+span[0]+'–'+span[1]+' 节 · '+(row.season||'季学期待核对')});
    }
  }
  const wrap=el('div',undefined,'academic-calendar-wrap'),grid=el('div',undefined,'academic-calendar');
  const periodCount=Math.max(13,...entries.map(e=>e.last_period));
  grid.style.gridTemplateRows='42px repeat('+periodCount+',minmax(40px,auto))';
  const corner=el('div','节次','academic-calendar-head academic-calendar-corner');corner.style.gridColumn='1';corner.style.gridRow='1';grid.append(corner);
  for(let d=0;d<7;d++){
    const iso=academicShift(a.week_start,d),head=el('div',undefined,'academic-calendar-head'+(a.dated&&iso===a.today?' is-today':''));
    head.style.gridColumn=String(d+2);head.style.gridRow='1';
    head.append(el('strong','周'+academicWeekdays[d]));if(a.dated)head.append(el('small',iso.slice(5)+(a.uncertain_dates?.includes(iso)?' · 待核对':'')));grid.append(head);
  }
  for(let p=1;p<=periodCount;p++){
    const label=el('div',undefined,'academic-period');label.append(el('strong',String(p)));
    label.style.gridColumn='1';label.style.gridRow=String(p+1);
    const time=a.dated?a.calendar.period_times[p-1]?.[0]:null;if(time)label.append(el('small',time));grid.append(label);
    for(let d=0;d<7;d++){const cell=el('div',undefined,'academic-calendar-cell');cell.style.gridColumn=String(d+2);cell.style.gridRow=String(p+1);grid.append(cell);}
  }
  for(let d=0;d<7;d++){
    const dayEntries=entries.filter(e=>(a.dated?Math.round((Date.parse(e.date)-Date.parse(a.week_start))/864e5):e.day_index)===d).sort((x,y)=>x.first_period-y.first_period);
    const groups=[];
    for(const event of dayEntries){const last=groups.at(-1);if(last&&event.first_period<=last.end){last.events.push(event);last.end=Math.max(last.end,event.last_period);}else groups.push({start:event.first_period,end:event.last_period,events:[event]});}
    for(const group of groups){
      const box=el('div',undefined,'academic-course-block');box.style.gridColumn=String(d+2);box.style.gridRow=(group.start+1)+' / '+(group.end+2);
      group.events.forEach(event=>box.append(academicCourseButton(event)));grid.append(box);
    }
  }
  wrap.tabIndex=0;wrap.setAttribute('aria-label','周课表，可左右滚动查看整周');
  const hint=el('p','左右滑动查看整周，点击课程查看详情。','footnote academic-scroll-hint');
  wrap.append(grid);root.append(hint,wrap);const overflow=wrap.scrollWidth>wrap.clientWidth;hint.classList.toggle('hidden',!overflow);
  if(a.dated&&overflow){const index=Math.round((Date.parse(a.today)-Date.parse(a.week_start))/864e5);if(index>=0&&index<=6)wrap.scrollLeft=index*98;}
  if(!entries.length)root.append(el('p',unplaced.length?'部分课程未能定位，详见下方来源字段。':a.dated?'当前课表安排中本周没有课程。':'该学期没有返回课表记录。','footnote'));
  if(unplaced.length){const details=el('details',undefined,'academic-source-details');details.append(el('summary',unplaced.length+' 条安排无法定位，查看来源字段'));campusTable(details,['课程','星期','节次','周次','季学期'],unplaced.map(r=>[r.name,r.weekday,r.periods||r.start_period+'/'+r.duration,r.weeks,r.season]));root.append(details);}
  renderCalendarSources(a.calendar);
}
function academicPeriods(row){
  let first=Number(row.start_period),last=first+Number(row.duration)-1;
  if(!row.start_period||!row.duration){const m=/^\s*(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\s*(?:节)?\s*$/.exec(row.periods||'');if(!m)return null;first=Number(m[1]);last=Number(m[2]||m[1]);}
  return Number.isInteger(first)&&Number.isInteger(last)&&first>=1&&first<=last&&last<=20?[first,last]:null;
}
function renderCalendarSources(calendar) {
  const root=$('campus-calendar-sources');root.replaceChildren();
  if(!calendar){root.append(el('p','尚未读取此学期校历。点击上方按钮，由程序调取学校来源。','footnote'));return;}
  if(calendar.error)root.append(el('p','在线校历未更新，当前使用'+(calendar.usable||calendar.verified?'缓存或默认日期。':'星期课表。')+calendar.error.message,'footnote'));
  root.append(el('p','日期来源：'+(calendar.source_status==='fallback'?'默认推算':calendar.source_status==='cache'?'同学期缓存':calendar.source_status==='unavailable'?'暂无可用日期':'学校在线校历'),'footnote'));
  if(calendar.method)root.append(el('p',calendar.method,'footnote'));
  if(calendar.fetched_at)root.append(el('p','获取时间：'+date(calendar.fetched_at)+' · '+(calendar.verified?'核对通过（见适用范围）':'尚未通过核对'),'footnote'));
  if(calendar.scope)root.append(el('p',calendar.scope,'footnote'));
  for(const [index,source] of (calendar.sources||[]).entries()){const a=el('a',['校方校历 PDF','学校官网学期日期','教务停课调课通知'][index]||'校历来源');a.href=source.url;a.target='_blank';a.rel='noopener';const p=el('p');p.append(a);root.append(p);}
}
function renderAcademicGrades() {
  const root=$('campus-grades');root.replaceChildren();const data=academicData.grades,overall=data.overall;
  if(gradeTerm&&!data.terms.some(t=>t.term===gradeTerm)){gradeTerm='';gradePage=0;}
  const cards=el('div',undefined,'academic-stats');cards.append(academicMetric('加权均绩',overall.gpa==null?'—':overall.gpa.toFixed(2),'教务返回绩点的原始分制'),academicMetric('参与加权学分',String(overall.credits),overall.included+' 条记录参与'),academicMetric('未参与记录',String(overall.excluded),'缺失绩点或非正学分'));
  root.append(cards,el('p',data.method,'footnote'));
  if(data.fetched_at)root.append(el('p','获取时间：'+date(data.fetched_at)+(data.stale?' · 上次数据':'')+(data.complete?'':' · 成绩未获取完整，均绩仅基于已有记录'),'footnote'));
  const terms=el('div',undefined,'academic-grade-terms');
  for(const t of data.terms){const button=el('button',undefined,'academic-grade-term'+(gradeTerm===t.term?' active':''));button.append(el('span',t.term),el('strong',t.gpa==null?'—':t.gpa.toFixed(2)),el('small',t.included+' 条参与 · '+t.credits+' 学分'));button.onclick=()=>{gradeTerm=t.term;gradePage=0;renderAcademicGrades();};terms.append(button);}
  root.append(el('h3','按学期均绩'),terms,el('h3','课程成绩'));
  const label=el('label','查看学期 '),select=el('select');select.append(new Option('全部学期',''));data.terms.forEach(t=>select.append(new Option(t.term,t.term)));select.value=gradeTerm;select.onchange=()=>{gradeTerm=select.value;gradePage=0;renderAcademicGrades();};label.append(select);root.append(label);
  const rows=(academicSnapshot?.grades?.data||[]).filter(r=>!gradeTerm||(r.term||'未提供学期')===gradeTerm),pages=Math.max(1,Math.ceil(rows.length/10));gradePage=Math.min(gradePage,pages-1);
  if(rows.length)campusTable(root,['课程','学期','成绩','学分','返回绩点'],rows.slice(gradePage*10,gradePage*10+10).map(r=>[r.name,r.term,r.score,r.credits,r.gpa]));else root.append(el('p','尚未获取该范围的成绩。','empty'));
  const nav=el('div',undefined,'academic-week-nav'),prev=el('button','上一页','secondary'),next=el('button','下一页','secondary');prev.disabled=gradePage===0;next.disabled=gradePage>=pages-1;
  prev.onclick=()=>{gradePage--;renderAcademicGrades();};next.onclick=()=>{gradePage++;renderAcademicGrades();};nav.append(prev,el('span',(gradePage+1)+' / '+pages+' · '+rows.length+' 条'),next);root.append(nav);
}
$('campus-term').onchange=()=>{academicTerm=$('campus-term').value;loadAcademicViews().catch(e=>toast(e.message));};
for(const [id,days] of [['campus-week-prev',-7],['campus-week-next',7]])$(id).onclick=()=>{academicDay=academicShift(academicData.agenda.day,days);loadAcademicViews().catch(e=>toast(e.message));};
$('campus-week-today').onclick=()=>{academicDay='';academicTerm='';loadAcademicViews().catch(e=>toast(e.message));};
$('campus-grades-open').onclick=()=>{$('campus-grades-dialog').showModal();};
$('campus-grades-close').onclick=()=>{$('campus-grades-dialog').close();};
$('campus-course-close').onclick=()=>{$('campus-course-dialog').close();};
$('campus-connection-open').onclick=()=>{$('campus-connection').open=true;$('campus-connection').scrollIntoView({behavior:'smooth',block:'start'});};
async function readAcademicCalendar(refresh=true){
  if(academicCalendarBusy||!$('campus-term').value)return;
  const epoch=sessionEpoch,[year,semester]=$('campus-term').value.split('/');academicCalendarBusy=true;$('campus-calendar-refresh').disabled=true;
  $('campus-calendar-state').textContent='正在更新日期来源；当前课程仍可按缓存或默认日期查看。';
  try{const result=await api('/api/campus/calendar',{method:'POST',json:{year,semester,campus:'main',refresh}});if(epoch!==sessionEpoch)return;renderCampus(result);}
  catch(e){if(epoch===sessionEpoch)$('campus-calendar-state').textContent=e.message;}
  finally{if(epoch===sessionEpoch){academicCalendarBusy=false;$('campus-calendar-refresh').disabled=false;}}
}
$('campus-calendar-refresh').onclick=()=>readAcademicCalendar();
setInterval(()=>{
  if(!loggedIn||!academicData||$('view-campus').classList.contains('hidden'))return;
  const calendar=academicData.agenda.calendar;
  if(academicISO()!==academicData.today_agenda.today||calendar?.verified&&!calendar.stale&&Date.now()-Date.parse(calendar.fetched_at)>6*3600e3)loadAcademicViews().catch(e=>toast(e.message));
  else{renderAcademicToday();renderAcademicMetrics();}
},60000);
