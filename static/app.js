(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const defaults = {model:{protocol:'openai',base_url:'http://127.0.0.1:8080/v1',model:'local-model',api_key:'',timeout_seconds:180,max_tokens:2048,temperature:0.1,concurrency:1,enabled:false},opencli:{executable:'opencli',profile:'',timeout_seconds:90},limits:{max_candidates:50}};
  const state = {token:'',settings:defaults,runs:[],run:null,candidates:[],total:0,selectedId:null,detail:null,detailKey:'',detailTab:'evidence',filter:'',query:'',offset:0,pageSize:50,checked:new Set(),listSequence:0,runEpoch:0,polling:false,modelTest:null,reviewBusy:false,batchDecision:null,bootstrapDone:false};
  const decisions = {unreviewed:'未审核',keep:'保留',exclude:'排除',needs_info:'补查',watch:'观察'};
  const statuses = {created:'待运行',running:'运行中',pause_requested:'正在暂停',paused:'已暂停',completed:'已完成',completed_partial:'部分完成',failed:'运行失败',interrupted:'已中断'};
  const sources = {demo:'演示体验',import:'导入记录',urls:'网页链接',opencli:'平台搜索',comments:'指定帖子评论'};
  const roles = {buyer:'采购方',seller:'供应方',consumer:'消费者',discussion:'讨论',unknown:'身份待核'};
  const evidenceLabels = {complete:'证据完整',partial:'证据部分可用',unverified:'尚未核实',demo:'演示证据'};
  const reviewerLabels = {human:'人工审核',codex:'Codex 审核',dsh:'harness 审核'};
  const fieldLabels = {brand_name:'品牌名称',company_website:'品牌网站',country:'国家或市场',product:'商品',quantity:'数量',destination:'交付目的地',deadline:'需求截止时间',posted_at:'发布日期',author_handle:'作者账号',budget:'预算',property_type:'户型',area:'面积',renovation_stage:'装修阶段',start_date:'开工时间'};
  const platformDefaults = [
    {id:'douyin',label:'抖音',market:'cn',search_supported:true,limitations:'搜索摘要，视频内容需补查'},
    {id:'xiaohongshu',label:'小红书',market:'cn',search_supported:true,limitations:'笔记摘要，需核对完整原文'},
    {id:'weibo',label:'微博',market:'cn',search_supported:true,limitations:'微博搜索，需核对上下文'},
    {id:'bilibili',label:'哔哩哔哩',market:'cn',search_supported:true,limitations:'视频搜索，标题不代表采购需求'},
    {id:'zhihu',label:'知乎',market:'cn',search_supported:true,limitations:'讨论内容，需核实作者需求'},
    {id:'tieba',label:'百度贴吧',market:'cn',search_supported:true,limitations:'搜索结果，需核对完整帖子'},
    {id:'kuaishou',label:'快手',market:'cn',search_supported:false,limitations:'暂未接入自动搜索，可打开搜索页或导入原文',search_url_template:'https://www.kuaishou.com/search/video?searchKey={query}'},
    {id:'tiktok',label:'TikTok',market:'global',search_supported:true,limitations:'视频描述可能截短'},
    {id:'instagram',label:'Instagram',market:'global',search_supported:true,limitations:'搜索主要返回账号资料'}
  ];
  state.platformCatalog=platformDefaults;state.currentMarket='cn';state.targetingDrafts={};state.searchPreview=null;state.searchRevision=0;
  const icons = {
    refresh:'<path d="M20 7v5h-5"/><path d="M4 17v-5h5"/><path d="M6.1 8a7 7 0 0 1 11.6-2L20 9M4 15l2.3 3A7 7 0 0 0 18 16"/>',
    close:'<path d="m6 6 12 12M18 6 6 18"/>',menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',
    search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
    external:'<path d="M14 3h7v7M21 3l-11 11"/><path d="M10 4H4v16h16v-6"/>',
    document:'<path d="M5 3h9l5 5v13H5Z"/><path d="M14 3v6h5M9 13h6M9 17h6"/>',
    alert:'<path d="m12 3 10 18H2Z"/><path d="M12 9v5M12 17h.01"/>',
    arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',back:'<path d="M19 12H5m5-5-5 5 5 5"/>',
    pause:'<path d="M8 5v14M16 5v14"/>',play:'<path d="m7 4 13 8-13 8Z"/>',
    inbox:'<path d="M4 4h16v16H4Z"/><path d="M4 14h5l1.5 3h3L15 14h5M8 8h8"/>',
    check:'<path d="m5 12 4 4L19 6"/>',history:'<path d="M3 5v5h5"/><path d="M4 10a8 8 0 1 1 1 8M12 7v5l3 2"/>',
    download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>'
  };
  function icon(name) { return `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.document}</svg>`; }
  function uid() { return globalThis.crypto?.randomUUID?.() || `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
  function date(value, short=false) { if (!value) return '时间未知'; const d = new Date(value); if (Number.isNaN(d.getTime())) return String(value); return d.toLocaleString('zh-CN',short?{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}:{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); }
  function safeUrl(value) { try { const u = new URL(String(value)); return ['http:','https:'].includes(u.protocol) ? u.href : null; } catch { return null; } }
  function link(value,label) { const url=safeUrl(value); return url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}${icon('external')}</a>`:`<span class="muted">${esc(label)}（无可打开网址）</span>`; }
  function display(value, empty='待确认') { if(value===null||value===undefined||value==='') return empty; return typeof value==='object'?JSON.stringify(value):String(value); }
  function isDomesticTarget(candidate) { return (candidate?.analysis?.targeting?.market||state.run?.config?.targeting?.market)==='cn'; }
  function isHousingTarget(candidate) { const industry=candidate?.analysis?.targeting?.industry||state.run?.config?.targeting?.industry||'';return isDomesticTarget(candidate)&&/家装|装修|装饰|整装|全屋|房产/.test(industry); }
  function factLabel(field,candidate) { if(isDomesticTarget(candidate)){if(field==='product')return isHousingTarget(candidate)?'服务':'服务 / 产品';if(!isHousingTarget(candidate)&&(field==='start_date'||field==='deadline'))return '期望时间';}return fieldLabels[field]||field; }
  function visibleUnknownFields(candidate,fields) { const values=Array.isArray(fields)?fields:[];if(!isDomesticTarget(candidate))return values;const allowed=new Set(isHousingTarget(candidate)?['budget','property_type','area','renovation_stage','start_date','author_handle']:['budget','start_date','deadline','author_handle']);return values.filter(field=>typeof field==='string'&&allowed.has(field)); }
  function evidenceBudget(candidate) {
    if(candidate.budget!==null&&candidate.budget!==undefined&&candidate.budget!=='')return candidate.budget;
    const custom=candidate.analysis?.target_match?.custom_evidence?.['预算']??candidate.target_match?.custom_evidence?.['预算'];
    return custom&&typeof custom==='object'&&custom.quote&&custom.value!==null&&custom.value!==undefined&&custom.value!==''?custom.value:null;
  }
  function isCommentCandidate(candidate) { return candidate?.kind==='comment'||candidate?.evidence?.some(e=>e.kind==='comment')===true; }
  function localAnalysisSkipped(candidate) { return candidate?.analysis?.analysis_status==='not_requested'||candidate?.analysis_mode==='collect_only'||(!candidate?.analysis&&processingMode(state.run)==='collect_only'); }
  function candidateTitle(candidate) { return isCommentCandidate(candidate)?candidate.author_handle?`评论者：${candidate.author_handle}`:'评论者待核实':candidate.brand_name||candidate.author_handle||candidate.product||'未识别主体'; }
  function candidateExcerpt(candidate) {
    if(isCommentCandidate(candidate)||localAnalysisSkipped(candidate))return candidate.content_excerpt||candidate.evidence?.[0]?.content||'原文已保存，打开后查看；尚未进行本地模型初筛。';
    if(isDomesticTarget(candidate)) {
      const parts=[];if(candidate.product)parts.push(candidate.product);
      const budget=evidenceBudget(candidate);if(budget!==null)parts.push(`预算 ${display(budget)}`);
      return parts.length?parts.join(' · '):candidate.local_reason||'等待查看原文与分析结果';
    }
    return candidate.product?`${candidate.product}${candidate.quantity?` · ${candidate.quantity}`:''}`:candidate.local_reason||'等待查看原文与分析结果';
  }
  function candidateLocation(candidate) { return isDomesticTarget(candidate)?candidate.analysis?.target_match?.matched_region||candidate.target_match?.matched_region||candidate.matched_region||'地区待核实':candidate.country||'国家待确认'; }
  function detailFactPairs(candidate) {
    if(isHousingTarget(candidate))return [['地区',candidate.analysis?.target_match?.matched_region],['服务',candidate.product],['预算',evidenceBudget(candidate)],['户型',candidate.property_type],['面积',candidate.area],['装修阶段',candidate.renovation_stage],['开工时间',candidate.start_date],['账号身份',roles[candidate.role]||'待确认']];
    if(isDomesticTarget(candidate))return [['地区',candidate.analysis?.target_match?.matched_region],['服务 / 产品',candidate.product],['预算',evidenceBudget(candidate)],['期望时间',candidate.start_date||candidate.deadline],['账号身份',roles[candidate.role]||'待确认']];
    return [['国家 / 市场',candidate.country],['商品',candidate.product],['数量',candidate.quantity],['目的地',candidate.destination],['需求截止',candidate.deadline],['账号身份',roles[candidate.role]||'待确认']];
  }
  function badge(value, label) { return `<span class="badge ${esc(value || 'unreviewed')}">${esc(label || decisions[value] || value || '待确认')}</span>`; }
  function processingMode(run) { return run?.analysis_mode||run?.config?.analysis_mode; }
  function runProcessingHTML(run) {
    if(run.is_demo)return '';
    const mode=processingMode(run),collection=run.config?.collection||run.collection,scope=collection?.scope;
    if(!mode&&!scope)return '';
    const words=Array.isArray(collection?.keywords)?collection.keywords:[];
    return `<div class="run-processing"><strong>${esc(mode==='collect_only'?'只采集 · 待人工 / Codex 审核':mode==='local'?'采集后进行本地模型初筛':'采集记录')}</strong>${scope&&scope!=='posts'?`<span>${scope==='comments'?'评论':'帖子和评论'}：先定位帖子，再筛选评论</span><span>最多 ${esc(collection.max_posts)} 帖，每帖 ${esc(collection.max_comments_per_post)} 条评论</span>${words.length?`<span>${collection.match_mode==='all'?'全部命中':'任意命中'}：${esc(words.join('、'))}</span>`:'<span>未设置评论关键词</span>'}`:''}</div>`;
  }
  function collectionSummaryHTML(run) {
    const summary=run?.progress?.collection_summary;if(!summary||typeof summary!=='object')return '';
    const count=key=>Math.max(0,Number(summary[key])||0);
    return `<div class="collection-summary"><span>评论采样：已读 <strong>${count('comments_read')}</strong> 条，关键词匹配 <strong>${count('comments_matched')}</strong> 条，返回 <strong>${count('comments_returned')}</strong> 条。</span><span>已处理 ${count('posts_succeeded')} / ${count('posts_attempted')} 个帖子，范围限于本轮帖子。</span>${count('comments_matched')===0?'<small>本次没有命中只代表这批采样结果，不代表全平台没有需求。</small>':''}</div>`;
  }
  function errorMessage(error) { return error?.message || String(error || '操作失败'); }
  async function api(path, options={}) {
    const method=options.method||'GET'; const controller=new AbortController(); const timeout=setTimeout(()=>controller.abort(),options.timeout||300000);
    const headers={Accept:'application/json'}; if(options.body!==undefined) headers['Content-Type']='application/json'; if(method!=='GET') headers['X-App-Token']=state.token;
    try {
      const response=await fetch(path,{method,headers,body:options.body!==undefined?JSON.stringify(options.body):undefined,signal:controller.signal,cache:'no-store'});
      const text=await response.text(); let payload; try { payload=JSON.parse(text); } catch { throw new Error(`本地服务返回了无法识别的响应（HTTP ${response.status}）。请检查服务是否正常运行。`); }
      if(!response.ok||payload.ok===false) { const info=payload.error||{}; const err=new Error(info.message||`请求失败（HTTP ${response.status}）`); err.code=info.code;err.details=info.details;err.status=response.status;throw err; }
      return payload.data;
    } catch(error) { if(error.name==='AbortError') throw new Error('请求超时。请检查本地服务或模型状态后重试。'); if(error instanceof TypeError) throw new Error('无法连接本地服务。请确认程序仍在运行，然后刷新重试。'); throw error; } finally {clearTimeout(timeout);}
  }
  function toast(message,isError=false) { const node=document.createElement('div');node.className=`toast${isError?' error':''}`;node.textContent=message;$('toast-region').appendChild(node);setTimeout(()=>node.remove(),isError?8000:4000); }
  function setError(id,message) { $(id).textContent=message||''; }
  function globalError(message) { const box=$('global-error');box.hidden=!message;box.innerHTML=message?`<span>${esc(message)}</span><button class="text-button" data-action="retry">重新连接</button>`:''; }
  async function busy(button,fn,label='处理中…') { if(!button||button.disabled)return;const before=button.innerHTML;button.disabled=true;button.innerHTML=`<span class="spinner"></span>${esc(label)}`;try{return await fn();}finally{if(button.isConnected){button.disabled=false;button.innerHTML=before;}} }
  function openDialog(id) { const dialog=$(id);if(!dialog.open)dialog.showModal(); }
  function closeDialog(dialog) { dialog?.close(); }
  function mergeSettings(data) { return { ...defaults,...data,model:{...defaults.model,...data?.model},opencli:{...defaults.opencli,...data?.opencli},limits:{...defaults.limits,...data?.limits} }; }
  function setModelIndicator() { const m=$('model-indicator');const tested=state.modelTest?.reachable===true;m.className=`model-indicator${tested?' tested':''}`;m.innerHTML=`<i></i>${esc(tested?'模型接口已响应':state.settings.model.enabled?'默认本地初筛 · 未测试':'本地模型可选 · 默认只采集')}`;m.title=tested?'仅表示本次测试接口可达；实际分析结果见候选详情。':'每批次都可选择只采集，无需启动模型。设置地址不代表模型已就绪。'; }
  async function initialize() {
    globalError('');
    try { const b=await api('/api/bootstrap');state.token=b.token;state.settings=mergeSettings(b.settings);if(Array.isArray(b.capabilities?.platforms)&&b.capabilities.platforms.length)state.platformCatalog=b.capabilities.platforms;state.bootstrapDone=true;$('app-version').textContent=b.app_version?`v${b.app_version}`:'';setModelIndicator();await loadRuns();const remembered=localStorage.getItem('leadworkbench-run');const initial=state.runs.find(r=>r.id===remembered)||state.runs[0];if(initial)await selectRun(initial.id);else renderWelcome(); }
    catch(error){globalError(errorMessage(error));$('workspace').innerHTML=`<div class="detail-placeholder">${icon('alert')}<h2>本地工作台尚未就绪</h2><p>${esc(errorMessage(error))}</p><button class="button secondary" data-action="retry" style="margin-top:18px">重新连接</button></div>`;}
  }
  async function loadRuns() { const data=await api('/api/runs');state.runs=Array.isArray(data)?data:(data?.items||[]);renderRuns(); }
  function renderRuns() {
    const key=JSON.stringify([state.runs.map(r=>[r.id,r.name,r.status,r.counts?.total,r.is_demo]),state.run?.id]);if($('runs-list')._renderKey===key)return;$('runs-list')._renderKey=key;
    $('runs-list').innerHTML=state.runs.length?state.runs.map(r=>`<button class="run-item${state.run?.id===r.id?' active':''}" data-run="${esc(r.id)}" ${state.run?.id===r.id?'aria-current="true"':''}><span class="run-name">${esc(r.name||'未命名批次')}</span><span class="run-meta"><i class="status-dot ${esc(r.status)}"></i>${esc(statuses[r.status]||r.status)}<span> ${Number(r.counts?.total)||0} 条</span>${r.is_demo?'<span>演示</span>':''}</span></button>`).join(''):'<div class="nav-empty">还没有批次。<br>新建任务，或先体验演示流程。</div>';
  }
  function renderWelcome() {
    state.run=null;$('workspace').classList.remove('is-workbench');
    $('workspace').innerHTML=`<div class="welcome"><h1>找到值得跟进的人。</h1><p>保存真实来源，看清需求，再做决定。</p><div class="welcome-actions"><button class="button primary" data-action="new">新建任务</button><button class="button secondary" data-action="demo">体验演示</button></div><div class="welcome-sources"><button class="welcome-source" data-new-source="opencli"><strong>搜索平台</strong><span>按行业、地区和需求找到帖子。</span>${icon('arrow')}</button><button class="welcome-source" data-new-source="comments"><strong>筛选评论</strong><span>从指定帖子下的评论寻找需求信号。</span>${icon('arrow')}</button><button class="welcome-source" data-new-source="urls"><strong>读取链接</strong><span>保存公开网页的内容与来源。</span>${icon('arrow')}</button><button class="welcome-source" data-new-source="import"><strong>导入记录</strong><span>从 JSON、CSV 或 JSONL 开始审核。</span>${icon('arrow')}</button></div><p class="welcome-footnote">数据仅保存在本机。本地模型可选，演示内容始终标注。</p></div>`;renderRuns();
  }
  async function selectRun(id) {
    const epoch=++state.runEpoch;state.run=state.runs.find(r=>r.id===id)||{id,name:'正在读取批次…',counts:{}};state.candidates=[];state.total=0;state.selectedId=null;state.detail=null;state.detailKey='';state.offset=0;state.checked.clear();state.query='';state.filter='';renderRunShell();renderRuns();$('app').classList.remove('nav-open');try{localStorage.setItem('leadworkbench-run',id);}catch{}
    try {const run=await api(`/api/runs/${encodeURIComponent(id)}`);if(epoch!==state.runEpoch)return;state.run=run;renderBatchHeader();await loadCandidates();}catch(error){if(epoch!==state.runEpoch)return;globalError(errorMessage(error));}
  }
  function renderRunShell() {
    $('workspace').classList.add('is-workbench');
    $('workspace').innerHTML=`<div id="batch-top"></div><div class="review-toolbar"><div id="filters" class="filter-group" aria-label="审核状态筛选"></div><label class="search-field">${icon('search')}<input id="candidate-search" type="search" placeholder="搜索线索" aria-label="搜索候选" value="${esc(state.query)}"></label></div><div class="workbench" id="workbench"><section class="candidate-pane" aria-label="候选线索"><div class="pane-heading"><strong id="candidate-count">候选线索</strong><div id="selection-tools" class="selection-tools"></div></div><div class="candidate-list" id="candidate-list"><div class="empty-list"><span class="spinner"></span><p>正在读取候选…</p></div></div><div id="list-footer" class="list-footer"></div></section><section class="detail-pane" id="detail-pane" aria-label="线索详情"></section></div>`;
    renderBatchHeader();renderDetailPlaceholder();renderFilters();$('candidate-search').addEventListener('input',debounce(()=>{state.query=$('candidate-search').value;state.offset=0;state.checked.clear();loadCandidates().catch(e=>toast(errorMessage(e),true));},280));
  }
  function renderBatchHeader() {
    const top=$('batch-top');if(!state.run||!top)return;const r=state.run,progress=r.progress||{};const pct=progress.total?Math.min(100,(Number(progress.current)||0)/Number(progress.total)*100):0;let control='';
    if(r.status==='running')control=`<button class="button secondary" data-header-focus="control" data-run-control="pause">${icon('pause')}暂停</button>`;
    else if(r.status==='pause_requested')control='<button class="button secondary" disabled>正在暂停</button>';
    else if(['paused','interrupted'].includes(r.status))control=`<button class="button primary" data-header-focus="control" data-run-control="resume">${icon('play')}继续</button>`;
    else if(['created','failed'].includes(r.status))control=`<button class="button primary" data-header-focus="control" data-run-control="start">${icon('play')}${r.status==='failed'?'重试':'开始'}</button>`;
    const html=`<div class="batch-header"><div class="batch-heading"><h1 title="${esc(r.name)}">${esc(r.name||'未命名任务')}</h1>${badge(r.status,statuses[r.status]||r.status)}</div><div class="batch-actions">${control}<details class="batch-more"><summary data-header-focus="menu" aria-label="更多任务操作" title="更多操作">⋯</summary><div class="batch-menu"><button data-header-focus="history" data-action="history">${icon('history')}审核历史</button><button data-header-focus="review-import" data-action="review-import">导入 Codex 审核</button><a data-header-focus="export-json" href="/api/runs/${encodeURIComponent(r.id)}/export?format=json" download>导出 JSON</a><a data-header-focus="export-csv" href="/api/runs/${encodeURIComponent(r.id)}/export?format=csv" download>导出 CSV</a></div></details></div></div><details class="batch-context"><summary data-header-focus="context">本轮条件与运行信息</summary><div class="batch-context-content"><p class="batch-description">${esc(r.goal||'未补充目标')}</p>${runProcessingHTML(r)}${targetingSummaryHTML(r.config?.targeting,r.config?.platforms||[])}<div class="batch-meta"><span>来源：${esc(sources[r.source]||r.source)}</span><span>创建：${esc(date(r.created_at))}</span><code>${esc(r.id)}</code></div></div></details>${r.is_demo?'<div class="batch-banner">合成演示 · 非实际客户，不调用本地模型。</div>':''}${r.error?`<div class="batch-banner error">${icon('alert')}<span>${esc(typeof r.error==='object'?r.error.message||JSON.stringify(r.error):r.error)}</span></div>`:''}${['running','pause_requested','paused','completed_partial'].includes(r.status)?`<div class="progress-strip"><div class="progress-bar"><span style="width:${pct}%"></span></div><span>${esc(progress.message||statuses[r.status])}</span><span>${Number(progress.current)||0} / ${Number(progress.total)||0}</span></div>`:''}${collectionSummaryHTML(r)}`;
    // Polling should leave native menus, keyboard focus and expanded conditions in place.
    if(top._renderKey!==html){
      const sameRun=top._renderRunId===r.id;
      const details=sameRun?['.batch-more','.batch-context'].map(selector=>({selector,open:!!top.querySelector(selector)?.open})):[];
      const active=document.activeElement;
      const focusKey=sameRun&&active&&top.contains(active)?active.dataset?.headerFocus:null;
      const contextScroll=sameRun?top.querySelector('.batch-context-content')?.scrollTop:0;
      top.innerHTML=html;top._renderKey=html;top._renderRunId=r.id;
      for(const item of details){const detail=top.querySelector(item.selector);if(detail)detail.open=item.open;}
      const context=top.querySelector('.batch-context-content');if(context)context.scrollTop=contextScroll||0;
      if(focusKey)top.querySelector(`[data-header-focus="${focusKey}"]`)?.focus({preventScroll:true});
    }
    renderFilters();
  }
  function renderFilters() { if(!$('filters'))return;const counts=state.run?.counts||{};const key=JSON.stringify([counts,state.filter]);if($('filters')._renderKey===key)return;$('filters')._renderKey=key;$('filters').innerHTML=[['','全部'],['unreviewed','未审核'],['keep','保留'],['needs_info','补查'],['watch','观察'],['exclude','排除']].map(([key,label])=>`<button class="filter-button${state.filter===key?' active':''}" data-filter="${key}" aria-pressed="${state.filter===key}">${label}<small>${Number(key?counts[key]:counts.total)||0}</small></button>`).join(''); }
  async function fetchDetail(id) { const d=await api(`/api/candidates/${encodeURIComponent(id)}`);try{const history=await api(`/api/reviews?run_id=${encodeURIComponent(d.run_id)}`);d._historyReviews=(history.items||[]).filter(r=>(r.changes||[]).some(c=>c.candidate_id===id));}catch{d._historyError=true;}return d; }
  async function loadCandidates() { if(!state.run)return;const runId=state.run.id;const sequence=++state.listSequence;const params=new URLSearchParams({run_id:runId,q:state.query,decision:state.filter,limit:state.pageSize,offset:state.offset});const data=await api(`/api/candidates?${params}`);if(sequence!==state.listSequence||state.run?.id!==runId)return;state.candidates=data?.items||[];state.total=Number(data?.total)||0;renderCandidates();if(state.selectedId){const item=state.candidates.find(c=>c.id===state.selectedId);if(item&&state.detail&&item.version!==state.detail.version){const id=state.selectedId;const detail=await fetchDetail(id);if(state.selectedId===id&&state.run?.id===runId){state.detail=detail;renderDetail();}}}}
  function renderCandidates() {
    if(!$('candidate-list'))return;$('candidate-count').textContent=`候选线索 · ${state.total} 条`;
    const renderKey=JSON.stringify([state.candidates,state.selectedId,[...state.checked],state.query,state.filter,state.run?.status]);
    if($('candidate-list')._renderKey!==renderKey){$('candidate-list')._renderKey=renderKey;
    $('candidate-list').innerHTML=state.candidates.length?state.candidates.map(c=>`<div class="candidate-row${c.id===state.selectedId?' active':''}"><input class="candidate-checkbox" type="checkbox" data-check="${esc(c.id)}" aria-label="选择 ${esc(candidateTitle(c))}" ${state.checked.has(c.id)?'checked':''}><button class="candidate-body" data-candidate="${esc(c.id)}" aria-label="查看 ${esc(candidateTitle(c))}" ${c.id===state.selectedId?'aria-current="true"':''}><div class="candidate-top"><span class="candidate-title">${esc(candidateTitle(c))}</span><div class="candidate-labels">${c.is_demo?badge('demo','演示'):''}${isCommentCandidate(c)?badge('comment','评论'):''}${c.priority&&c.priority!=='unknown'?`<span class="badge priority-${esc(String(c.priority).toLowerCase())}">${esc(c.priority)}</span>`:''}</div></div><div class="candidate-subline"><span>${esc(platformLabel(c.platform||'网页'))}</span><span class="separator">/</span><span>${esc(candidateLocation(c))}</span><span class="separator">/</span><span>${esc(roles[c.role]||'身份待核')}</span></div><p class="candidate-excerpt">${esc(candidateExcerpt(c))}</p><div class="candidate-bottom">${badge(c.effective_decision||'unreviewed')}<span>${esc(c.posted_at?date(c.posted_at,true):'发布日期待确认')}</span></div></button></div>`).join(''):`<div class="empty-list"><strong>${state.query||state.filter?'没有匹配的候选':'这批还没有候选'}</strong>${state.query||state.filter?'调整关键词或切回“全部”查看。':state.run?.status==='created'?'点击“开始运行”，程序会把结果逐条放到这里。':['running','pause_requested'].includes(state.run?.status)?'程序正在采集与分析，结果会自动出现。':'可查看批次状态与错误信息，或新建任务缩小范围。'}</div>`;}
    renderSelection();const from=state.total?state.offset+1:0,to=Math.min(state.offset+state.pageSize,state.total);$('list-footer').innerHTML=`<span>${from}–${to} / ${state.total} 条</span><div><button class="text-button" data-page="prev" ${state.offset<=0?'disabled':''}>上一页</button><button class="text-button" data-page="next" ${state.offset+state.pageSize>=state.total?'disabled':''}>下一页</button></div>`;
  }
  function renderSelection() { const n=state.checked.size;if(!$('selection-tools'))return;$('selection-tools').innerHTML=n?`<span>已选 ${n}</span><button class="text-button" data-batch="keep">保留</button><button class="text-button" data-batch="needs_info">补查</button><button class="text-button" data-batch="exclude">排除</button><button class="text-button" data-action="clear-checks" aria-label="取消选择">×</button>`:state.candidates.length?'<button class="text-button" data-action="select-page">选择本页</button>':''; }
  function renderDetailPlaceholder() { if(!$('detail-pane'))return;$('detail-pane').innerHTML=`<div class="detail-placeholder">${icon('document')}<h2>选一条线索，核对证据</h2><p>原始内容、可选本地初筛与审核意见分别保留，每次修改都能追溯。</p></div>`; }
  function commentContextHTML(candidate,evidence) {
    if(evidence?.kind!=='comment'&&!isCommentCandidate(candidate))return '';
    const m=evidence?.metadata||{},parent=candidate.parent_source_url||m.parent_source_url;
    const matches=Array.isArray(m.keyword_matches)?m.keyword_matches:[];
    return `<div class="comment-context"><div><strong>评论者</strong><span>${esc(candidate.author_handle||'待核实')}</span></div>${m.parent_title?`<div><strong>父帖标题</strong><span>${esc(m.parent_title)}</span></div>`:''}${parent?`<div class="comment-parent-link">${link(parent,'打开父帖核查上下文')}</div>`:''}${m.parent_comment_id?'<p>这是一条回复；完整对话仍需结合上下文核实。</p>':''}${matches.length?`<p>关键词命中：${esc(matches.join('、'))}（原文匹配，非模型判断）</p>`:''}${m.comment_permalink_available===false?'<p>平台未提供可独立定位的评论链接，已保留评论 ID 和父帖来源。</p>':''}</div>`;
  }
  function evidenceHTML(candidate,evidence,index) {
    const isComment=evidence.kind==='comment',m=evidence.metadata||{};
    const source=isComment&&m.comment_permalink_available===false?candidate.parent_source_url||m.parent_source_url||evidence.source_url:evidence.source_url;
    const sourceLabel=isComment?(m.comment_permalink_available===true?'评论位置':'父帖来源'):'查看来源';
    const facts=detailFactPairs(candidate);
    return `<article class="evidence-item"><div class="evidence-label"><strong>${esc(({post:'帖子原文',website:'网页原文',comment:'评论原文',import:'导入原文'})[evidence.kind]||'来源原文')} ${index+1}</strong><span>${esc(evidence.posted_at?date(evidence.posted_at,true):'发布日期待核实')}</span></div><blockquote class="evidence-quote">${esc(evidence.content||evidence.quote||'尚未取得可核查原文。')}</blockquote><div class="detail-links">${link(source,sourceLabel)}</div>${isComment?`<details><summary>评论上下文与关键词</summary>${commentContextHTML(candidate,evidence)}</details>`:''}${evidence.quote&&evidence.content!==evidence.quote?`<details><summary>模型引用的片段</summary><blockquote class="evidence-quote">${esc(evidence.quote)}</blockquote></details>`:''}<details class="record-details"><summary>记录信息</summary><dl class="detail-facts">${facts.map(([k,v])=>`<div class="mini-fact"><dt>${esc(factLabel(k,candidate))}</dt><dd>${esc(display(v))}</dd></div>`).join('')}</dl><div class="evidence-meta">证据 ID：${esc(evidence.id)}<br>候选版本：${esc(candidate.version)}${isComment&&(candidate.comment_id||m.comment_id)?`<br>评论 ID：${esc(candidate.comment_id||m.comment_id)}`:''}<br>采集于 ${esc(date(evidence.retrieved_at))}${evidence.content_sha256?`<br>内容指纹：${esc(evidence.content_sha256)}`:''}${evidence.is_demo?'<br>合成演示内容。':''}</div></details></article>`;
  }
  function localAnalysisHTML(candidate) {
    const a=candidate.analysis||{};
    if(localAnalysisSkipped(candidate)){const reviewed=candidate.effective_decision&&candidate.effective_decision!=='unreviewed';return `<section class="layer skipped-analysis"><div class="layer-header"><span>本地初筛</span><small>未使用本地模型</small></div><p>${reviewed?'本轮仅采集原文，已有外部审核，见下方意见。':'本轮仅采集原文，等待你或 Codex 审核。'}</p><details class="layer-meta"><summary>处理说明</summary>关键词属于原文匹配，没有生成本地模型结论。</details></section>`;}
    const facts=a.facts&&typeof a.facts==='object'?Object.entries(a.facts):[],unknown=visibleUnknownFields(candidate,a.unknown_fields);
    return `${inputWindowHTML(a.input_window)}<section class="layer"><div class="layer-header"><span>本地初筛</span>${a.decision||candidate.local_decision?badge(a.decision||candidate.local_decision):'<small>尚无结论</small>'}</div><p>${esc(a.reason||candidate.local_reason||'尚无分析结果。')}</p>${targetMatchHTML(a.target_match)}${facts.length||unknown.length?`<details><summary>提取信息与待核实项</summary>${facts.length?`<dl class="facts-list">${facts.map(([k,v])=>`<dt>${esc(factLabel(k,candidate))}</dt><dd>${esc(display(v))}</dd>`).join('')}</dl>`:''}${unknown.length?`<div class="unknown-list">${unknown.map(u=>`<span>${esc(typeof u==='object'?JSON.stringify(u):factLabel(u,candidate))}待确认</span>`).join('')}</div>`:''}</details>`:''}${a.citations?.length?`<details class="layer-meta"><summary>引用依据</summary><pre>${esc(JSON.stringify(a.citations,null,2))}</pre></details>`:''}<details class="layer-meta"><summary>运行信息</summary><div>模型：${esc(a.analysis_status==='synthetic_demo'?'演示规则（未调用模型）':a.model||'未记录')}<br>状态：${esc(a.analysis_status||'未记录')}<br>提示词：${esc(a.prompt_version||'未记录')}${a.protocol?`<br>协议：${esc(a.protocol)}`:''}</div></details></section>`;
  }
  async function loadDetail(id) {
    state.selectedId=id;state.detailTab='evidence';state.detailKey='';renderCandidates();$('workbench')?.classList.add('has-detail');$('detail-pane').innerHTML='<div class="detail-placeholder"><span class="spinner"></span><p>正在读取证据…</p></div>';
    const runId=state.run?.id;try{const d=await fetchDetail(id);if(state.selectedId!==id||state.run?.id!==runId)return;state.detail=d;renderDetail();}catch(error){if(state.selectedId!==id)return;$('detail-pane').innerHTML=`<div class="detail-placeholder">${icon('alert')}<h2>无法读取这条线索</h2><p>${esc(errorMessage(error))}</p><button class="button secondary" data-action="retry-detail" style="margin-top:15px">重试</button></div>`;}
  }
  function reviewEntries(d) { return (d._historyReviews||d.reviews||[]).flatMap(r=>{if(Array.isArray(r.changes))return r.changes.filter(c=>!c.candidate_id||c.candidate_id===d.id).map(c=>({...c,reviewer:r.reviewer,created_at:r.created_at,status:r.status,changeset_id:r.id}));return [{...r,changeset_id:r.changeset_id||r.id}];}).sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||''))); }
  function renderDetail() {
    const d=state.detail;if(!d||!$('detail-pane'))return;const evidence=d.evidence||[],existingReason=$('human-reason')?.value||'';
    const known=detailFactPairs(d).filter(([k,v])=>v!==null&&v!==undefined&&v!==''&&!['待确认','身份待核','unknown'].includes(v)).slice(0,3);
    $('detail-pane').innerHTML=`<div class="detail-header"><button class="icon-button mobile-back" data-action="back-list">${icon('back')}返回列表</button><div class="detail-kicker"><span>${esc(platformLabel(d.platform||'来源'))}</span>${badge(d.evidence_status||'unverified',evidenceLabels[d.evidence_status]||'待核实')}${isCommentCandidate(d)?badge('comment','评论'):''}</div><div class="detail-title-row"><h2 title="${esc(candidateTitle(d))}">${esc(candidateTitle(d))}</h2><div class="detail-links">${link(d.parent_source_url||d.source_url,'来源')}</div></div>${known.length?`<div class="known-facts">${known.map(([k,v])=>`<span><strong>${esc(factLabel(k,d))}</strong>${esc(display(v))}</span>`).join('')}</div>`:''}</div><div class="detail-tabs" role="tablist" aria-label="线索详情视图"><button class="detail-tab${state.detailTab==='evidence'?' active':''}" role="tab" aria-selected="${state.detailTab==='evidence'}" data-detail-tab="evidence">原文</button><button class="detail-tab${state.detailTab==='analysis'?' active':''}" role="tab" aria-selected="${state.detailTab==='analysis'}" data-detail-tab="analysis">判断</button><button class="detail-tab${state.detailTab==='reviews'?' active':''}" role="tab" aria-selected="${state.detailTab==='reviews'}" data-detail-tab="reviews">记录 ${reviewEntries(d).length}</button></div><div class="detail-content" id="detail-content" role="tabpanel"></div><form class="human-review" id="human-review-form"><div class="human-review-title"><span>你的审核</span>${badge(d.human_decision||'unreviewed')}</div><textarea id="human-reason" rows="1" placeholder="记录审核依据（选填）" aria-label="人工审核理由">${esc(existingReason)}</textarea><div class="human-actions"><button class="button positive" type="button" data-review="keep">保留</button><button class="button secondary" type="button" data-review="needs_info">补查</button><button class="button secondary" type="button" data-review="watch">观察</button><button class="button danger" type="button" data-review="exclude">排除</button></div><div class="review-feedback" id="review-feedback" role="alert"></div></form>`;
    renderDetailBody();state.detailKey=JSON.stringify(d);
  }
  function renderDetailBody() {
    const d=state.detail;if(!d||!$('detail-content'))return;document.querySelectorAll('[data-detail-tab]').forEach(b=>{b.classList.toggle('active',b.dataset.detailTab===state.detailTab);b.setAttribute('aria-selected',String(b.dataset.detailTab===state.detailTab));});
    if(state.detailTab==='evidence') {
      $('detail-content').innerHTML=(d.evidence||[]).length?(d.evidence||[]).map((e,i)=>evidenceHTML(d,e,i)).join(''):'<div class="empty-list"><strong>尚未取得原始证据</strong>这条线索需要补查来源与原文。</div>';
    } else if(state.detailTab==='analysis') {
      const entries=reviewEntries(d);const codex=entries.find(r=>r.reviewer==='codex'&&r.status!=='reverted');const human=entries.find(r=>r.reviewer==='human'&&r.status!=='reverted');
      $('detail-content').innerHTML=`${localAnalysisHTML(d)}<section class="layer"><div class="layer-header"><span>Codex 复核</span>${d.codex_decision?badge(d.codex_decision):'<small>等待导入</small>'}</div><p>${esc(codex?.reason||'导出本批 JSON 交给 Codex，完成第一轮筛选后导入审核变更。')}</p>${codex?`<div class="layer-meta">${esc(date(codex.created_at))}</div>`:''}</section><section class="layer"><div class="layer-header"><span>人工决定</span>${d.human_decision?badge(d.human_decision):'<small>尚未审核</small>'}</div><p>${esc(human?.reason||'核对原始证据后，使用下方操作保留、排除、观察或补查。')}</p>${human?`<div class="layer-meta">${esc(date(human.created_at))}</div>`:''}</section>`;
    } else {
      const entries=reviewEntries(d);$('detail-content').innerHTML=entries.length?entries.map(r=>`<article class="review-entry"><div class="review-entry-head"><strong>${esc(reviewerLabels[r.reviewer]||r.reviewer||'审核')}</strong>${badge(r.decision||'watch')}${r.status==='reverted'?'<span class="badge">已撤销</span>':''}<time>${esc(date(r.created_at))}</time></div><p>${esc(r.reason||'未填写理由')}</p><div class="layer-meta">变更集 ${esc(r.changeset_id||'未记录')}</div></article>`).join(''):'<div class="empty-list"><strong>这条还没有审核记录</strong>人工操作或导入 Codex 审核后，记录会保存在这里。</div>';
    }
  }
  async function submitReview(decision,button) {
    if(!state.detail||state.reviewBusy)return;state.reviewBusy=true;const d=state.detail;const reason=$('human-reason').value.trim()||`人工标记为${decisions[decision]}。`;setError('review-feedback','');
    try {await busy(button,async()=>{await api('/api/reviews',{method:'POST',body:{run_id:d.run_id,reviewer:'human',operation_id:uid(),changes:[{candidate_id:d.id,base_version:d.version,decision,reason,evidence_ids:(d.evidence||[]).map(e=>e.id),tags:[]}]}});toast(`已${decisions[decision]}，审核记录已保存`);await refreshAfterReview(d.id);},'保存中…');}
    catch(error){if($('review-feedback'))setError('review-feedback',error.status===409?`${errorMessage(error)} 请重新读取当前版本后再审核。`:errorMessage(error));else toast(errorMessage(error),true);if(error.status===409)await refreshAfterReview(d.id);}
    finally{state.reviewBusy=false;}
  }
  async function refreshAfterReview(detailId) { const runId=state.run?.id;if(!runId)return;await loadRuns();state.run=await api(`/api/runs/${encodeURIComponent(runId)}`);renderBatchHeader();await loadCandidates();if(detailId&&state.selectedId===detailId){state.detail=await fetchDetail(detailId);if($('human-reason'))$('human-reason').value='';renderDetail();} }
  async function controlRun(action,button) { const id=state.run?.id;if(!id||state.controlBusy)return;state.controlBusy=true;try{await busy(button,async()=>{const run=await api(`/api/runs/${encodeURIComponent(id)}/${action}`,{method:'POST',body:{}});if(state.run?.id===id){state.run=run;renderBatchHeader();}await loadRuns();toast(action==='pause'?'已请求暂停，当前步骤结束后停止':action==='resume'?'已继续运行':'任务已启动');});}finally{state.controlBusy=false;} }
  function splitTerms(value) { return [...new Set(String(value||'').split(/[,，;；\r\n]+/).map(s=>s.trim().replace(/^#+/,'')).filter(Boolean))]; }
  function currentCustomRows() { return [...$('target-custom-fields').querySelectorAll('.custom-field-row')].map(row=>({key:row.querySelector('[data-custom-key]').value,value:row.querySelector('[data-custom-value]').value})); }
  function addCustomField(key='',value='') {
    const holder=$('target-custom-fields');if(holder.children.length>=10){toast('一轮最多添加 10 项补充条件。',true);return;}
    const row=document.createElement('div');row.className='custom-field-row';
    row.innerHTML=`<input data-custom-key maxlength="40" aria-label="补充条件名称" placeholder="条件名称，例如：装修阶段" value="${esc(key)}"><input data-custom-value maxlength="500" aria-label="补充条件内容" placeholder="例如：尚未确定施工团队" value="${esc(value)}"><button class="icon-button" type="button" data-remove-custom aria-label="移除补充条件">${icon('close')}</button>`;
    holder.appendChild(row);
  }
  function targetingPayload() {
    const f=$('new-form'),custom={};
    for(const row of currentCustomRows()) {
      const key=row.key.trim(),value=row.value.trim();if(!key&&!value)continue;
      if(!key||!value)throw new Error('补充条件需要同时填写名称和内容，或移除空行。');
      if(Object.hasOwn(custom,key))throw new Error(`补充条件“${key}”重复，请合并后再创建。`);
      Object.defineProperty(custom,key,{value,enumerable:true,configurable:true,writable:true});
    }
    return {market:f.elements.market.value,industry:f.elements.industry.value.trim(),regions:splitTerms(f.elements.regions.value),include_tags:splitTerms(f.elements.include_tags.value),exclude_tags:splitTerms(f.elements.exclude_tags.value),customer_type:f.elements.customer_type.value,needs:f.elements.needs.value.trim(),custom_fields:custom};
  }
  function selectedPlatforms() { return [...$('search-platforms').querySelectorAll('input[data-search-platform]:checked')].filter(input=>!input.disabled).map(input=>input.value); }
  function catalogForMarket(market) { return (state.platformCatalog||platformDefaults).filter(p=>p.market===market); }
  function platformLabel(id) { return (state.platformCatalog||platformDefaults).find(p=>p.id===id)?.label||id; }
  function targetingSummaryHTML(t,platforms=[]) {
    if(!t||typeof t!=='object')return '';
    const customer={consumer:'个人消费者 / 业主',business:'商家 / 企业',any:'顾客类型不限'}[t.customer_type]||'按证据判断';
    const terms=[t.market==='cn'?'国内':'海外',t.industry,customer,...(t.regions||[])].filter(Boolean);
    const tags=(t.include_tags||[]).map(tag=>`<span class="target-tag">${esc(tag)}</span>`).join('');
    const extra=Object.entries(t.custom_fields||{});
    return `<div class="run-targeting"><div class="run-targeting-summary"><strong>定向条件</strong><span>${esc(terms.join(' / '))}</span>${platforms.length?`<span class="run-targeting-platforms">${esc(platforms.map(platformLabel).join('、'))}</span>`:''}</div>${tags?`<div class="target-tag-preview">${tags}</div>`:''}${t.needs||extra.length||(t.exclude_tags||[]).length?`<details><summary>查看完整筛选条件</summary>${t.needs?`<p>${esc(t.needs)}</p>`:''}${(t.exclude_tags||[]).length?`<p>排除类型与信号：${esc(t.exclude_tags.join('、'))}</p>`:''}${extra.length?`<dl>${extra.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`:''}<p class="muted">筛选条件不等于客户事实；地区、预算和计划均以原文为准。</p></details>`:''}</div>`;
  }
  function targetMatchHTML(match) {
    if(!match||typeof match!=='object'||!match.status)return '';
    const label={matched:'初筛符合定向',excluded:'初筛不符合定向',needs_info:'定向信息待补查'}[match.status]||'定向匹配待核实';
    const tags=Array.isArray(match.matched_tags)?match.matched_tags:[];
    const missing=Array.isArray(match.missing_fields)?match.missing_fields:[];
    const custom=match.custom_evidence&&typeof match.custom_evidence==='object'?Object.entries(match.custom_evidence):[];
    return `<details class="target-match ${esc(match.status)}"><summary>定向匹配 · ${esc(label)}</summary><p>${esc(match.reason||'请核对原文中的需求、地区和作者身份。')}</p>${tags.length?`<div class="target-tag-preview">${tags.map(tag=>`<span class="target-tag">${esc(typeof tag==='string'?tag:JSON.stringify(tag))}</span>`).join('')}</div>`:''}${match.region_evidence?`<p class="region-evidence">地区依据${match.matched_region?`（${esc(match.matched_region)}）`:''}：${esc(display(match.region_evidence))}</p>`:''}${custom.length?`<details><summary>补充条件依据</summary><dl class="facts-list">${custom.map(([key,value])=>`<dt>${esc(key)}</dt><dd>${esc(value&&typeof value==='object'?value.quote||JSON.stringify(value):display(value))}</dd>`).join('')}</dl></details>`:''}${missing.length?`<small>仍需确认：${esc(missing.map(field=>fieldLabels[field]||field).join('、'))}</small>`:''}</details>`;
  }
  function inputWindowHTML(window) { return window?.truncated?`<div class="input-window-notice">本地模型仅分析了前 ${esc(window.analyzed_chars||'部分')} 字原文${window.original_chars?`（原文共 ${esc(window.original_chars)} 字）`:''}，后段需求可能尚未判断。完整采集原文仍保存在“证据原文”，请补查后再决定。</div>`:''; }
  function commentsSupported(platform) { return typeof platform.comments_supported==='boolean'?platform.comments_supported:platform.comment_supported===true; }
  function collectingComments() { const f=$('new-form');return f.elements.source.value==='comments'||(f.elements.source.value==='opencli'&&f.elements.collection_scope.value!=='posts'); }
  function collectionPayload() {
    const f=$('new-form'),scope=f.elements.source.value==='comments'?'comments':f.elements.collection_scope.value||'posts';
    if(scope==='posts')return {scope:'posts',keywords:[],match_mode:'any',max_posts:3,max_comments_per_post:20,include_replies:false};
    if(!['comments','posts_and_comments'].includes(scope))throw new Error('请选择有效的采集范围。');
    const keywords=[...new Set(String(f.elements.comment_keywords.value||'').split(/[,，;；\r\n]+/).map(s=>s.trim()).filter(Boolean))];
    if(keywords.length>20||keywords.some(word=>word.length>100))throw new Error('评论关键词最多 20 个，每个不超过 100 字。');
    const max_posts=Number(f.elements.max_posts.value),max_comments_per_post=Number(f.elements.max_comments_per_post.value);
    if(!Number.isInteger(max_posts)||max_posts<1||max_posts>20)throw new Error('最多读取帖子数应为 1–20。');
    if(!Number.isInteger(max_comments_per_post)||max_comments_per_post<1||max_comments_per_post>100)throw new Error('每帖最多评论数应为 1–100。');
    return {scope,keywords,match_mode:f.elements.comment_match_mode.value,max_posts,max_comments_per_post,include_replies:f.elements.include_replies.checked};
  }
  function updateCommentCapabilityHint() {
    const catalog=$('new-form').elements.source.value==='comments'?state.platformCatalog:catalogForMarket($('new-form').elements.market.value);
    const labels=catalog.filter(commentsSupported).map(p=>p.id==='zhihu'?`${p.label}回答`:p.label);
    $('comment-capability-hint').textContent=labels.length?`已接入评论读取：${labels.join('、')}，仍需浏览器连接和相应登录。其他平台可导入评论原文。回复支持与完整性按实际读取结果标记。`:'当前范围尚未接入自动评论读取，可切换平台或导入已有评论原文。';
  }
  function updateCollectionOptions(refreshPlatforms=true) {
    const f=$('new-form'),source=f.elements.source.value;if(source==='comments')f.elements.collection_scope.value='comments';
    const visible=['opencli','comments'].includes(source),comments=visible&&collectingComments();
    $('collection-options').hidden=!visible;$('collection-scope').hidden=source==='comments';$('comment-options').hidden=!comments;
    for(const key of ['comment_keywords','comment_match_mode','max_posts','max_comments_per_post','include_replies'])f.elements[key].disabled=!comments;
    if(source==='opencli'&&refreshPlatforms)renderPlatformOptions(selectedPlatforms());
    updateCommentCapabilityHint();
  }
  function updateAnalysisModeHint() {
    const f=$('new-form'),local=f.elements.analysis_mode.value==='local';
    $('analysis-mode-hint').textContent=local?`初筛将调用 ${state.settings.model.base_url}。请用你现有控制器启动服务；模型离线时也可改为只采集。`:'本轮不会调用本地模型。平台连接正常后即可采集，完成后可交给人工 / Codex 审核。';
  }
  function prefillController() {
    const f=$('settings-form');f.elements.protocol.value='openai';f.elements.base_url.value='http://127.0.0.1:8080/v1';f.elements.model.value='local-model';
    $('model-test-result').hidden=true;state.modelTest=null;setModelIndicator();toast('已填入本地服务示例，请填写实际模型 ID；尚未保存，也未启动模型。');
  }
  function renderPlatformOptions(selected=[]) {
    const catalog=catalogForMarket($('new-form').elements.market.value);
    const comments=collectingComments();
    $('search-platforms').innerHTML=catalog.map(p=>{const available=p.search_supported&&(!comments||commentsSupported(p));return `<label class="platform-option${available?'':' manual-only'}${p.experimental?' experimental':''}"><input type="checkbox" data-search-platform value="${esc(p.id)}" ${available&&selected.includes(p.id)?'checked':''} ${available?'':'disabled'}><span><strong>${esc(p.label)}</strong><small>${available?(comments?'帖子搜索 + 评论读取 · 需登录':p.experimental?'实验性只读搜索 · 需登录实测':'只读搜索 · 需浏览器连接'):comments&&!commentsSupported(p)?'评论读取暂未接入':'自动搜索暂未接入'}</small><span class="platform-limit">${esc(Array.isArray(p.limitations)?p.limitations.join('；'):p.limitations||'')}</span></span></label>`;}).join('');
    renderManualLinks();updateCommentCapabilityHint();
  }
  function renderTagPreview() {
    const f=$('new-form');$('target-tag-preview').innerHTML=[...splitTerms(f.elements.include_tags.value).map(tag=>`<span class="target-tag">${esc(tag)}</span>`),...splitTerms(f.elements.exclude_tags.value).map(tag=>`<span class="target-tag excluded">排除：${esc(tag)}</span>`)].join('');
  }
  function manualQuery() { const f=$('new-form');return f.elements.queries.value.split(/\r?\n/).map(s=>s.trim()).find(Boolean)||[splitTerms(f.elements.regions.value)[0],f.elements.industry.value.trim(),splitTerms(f.elements.include_tags.value)[0]].filter(Boolean).join(' '); }
  function renderManualLinks(links) {
    const query=manualQuery();const fallback=catalogForMarket($('new-form').elements.market.value).filter(p=>(!p.search_supported||p.experimental)&&p.search_url_template).map(p=>({platform:p.id,label:p.label,url:String(p.search_url_template).replace(/\{query\}/g,encodeURIComponent(query))}));
    const manual=[...(Array.isArray(links)?links:[]),...fallback],seen=new Set();
    const usable=manual.filter(item=>{const url=safeUrl(item.url);if(!url||seen.has(url))return false;seen.add(url);return true;});
    $('manual-platform-links').innerHTML=usable.length?`<div><strong>补充平台</strong><p>实验性适配器或暂未接入的平台，也可先打开公开搜索页；找到帖子后，用“网页链接”或“导入记录”保留原文。</p></div><div class="manual-link-list">${usable.map(item=>link(item.url,`${item.label||platformLabel(item.platform)} 搜索页`)).join('')}</div>`:'';
  }
  function invalidateSearchPreview() { state.searchRevision++;state.searchPreview=null;const box=$('search-plan-preview');box.hidden=true;renderTagPreview();renderManualLinks(); }
  function scopeDraft() { const f=$('new-form');return {industry:f.elements.industry.value,customer_type:f.elements.customer_type.value,regions:f.elements.regions.value,include_tags:f.elements.include_tags.value,exclude_tags:f.elements.exclude_tags.value,needs:f.elements.needs.value,custom:currentCustomRows(),queries:f.elements.queries.value,platforms:selectedPlatforms(),goal:f.elements.goal.value}; }
  function setMarket(market,reset=false) {
    const f=$('new-form');if(!reset)state.targetingDrafts[state.currentMarket]=scopeDraft();
    const draft=state.targetingDrafts[market]||(market==='cn'?{industry:'家装',customer_type:'consumer',regions:'',include_tags:'准备装修，旧房改造，找施工队',exclude_tags:'同行广告，招商，招聘',needs:'寻找有明确装修计划、正在询价或求推荐施工团队的业主。',custom:[{key:'',value:''}],queries:'',platforms:['douyin','xiaohongshu'],goal:''}:{industry:'',customer_type:'business',regions:'',include_tags:'',exclude_tags:'',needs:'',custom:[{key:'',value:''}],queries:'',platforms:['tiktok'],goal:''});
    f.elements.market.value=market;state.currentMarket=market;
    for(const key of ['industry','customer_type','regions','include_tags','exclude_tags','needs','queries','goal'])f.elements[key].value=draft[key]||'';
    $('target-custom-fields').replaceChildren();for(const row of draft.custom||[])addCustomField(row.key,row.value);
    $('home-preset').hidden=market!=='cn';renderPlatformOptions(draft.platforms||[]);invalidateSearchPreview();
    f.elements.name.placeholder=market==='cn'?'例如：苏州家装 · 近期准备装修的业主':'例如：澳洲小型烘焙店 · 包装需求';
  }
  async function generateSearchPreview(button,replaceQueries=true) {
    return busy(button,async()=>{
      const f=$('new-form'),revision=state.searchRevision;const targeting=targetingPayload();
      const body={targeting,queries:replaceQueries?[]:f.elements.queries.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),platforms:selectedPlatforms()};
      if(!body.platforms.length)throw new Error('请至少选择一个已接入自动搜索的平台。快手可使用下方搜索页入口。');
      const result=await api('/api/search/preview',{method:'POST',body});
      if(revision!==state.searchRevision){toast('条件已变化，请按当前条件重新生成方案。',true);return null;}
      const queries=Array.isArray(result.queries)?result.queries.filter(q=>typeof q==='string'&&q.trim()):[];
      if(!queries.length)throw new Error('没有生成可用搜索词，请补充行业、地区或需求标签。');
      state.searchPreview={...result,queries};if(replaceQueries||!f.elements.queries.value.trim())f.elements.queries.value=queries.join('\n');
      const box=$('search-plan-preview');box.hidden=false;box.innerHTML=`<strong>本轮搜索方案</strong><p>${esc(result.summary||'按当前定向条件生成，搜索后仍需核对原文。')}</p><div class="preview-queries">${queries.map(q=>`<span>${esc(q)}</span>`).join('')}</div>${(result.warnings||[]).length?`<ul>${result.warnings.map(w=>`<li>${esc(typeof w==='string'?w:JSON.stringify(w))}</li>`).join('')}</ul>`:''}<small>上方搜索词可继续修改。查询条件不会自动成为候选事实。</small>`;
      renderManualLinks(result.manual_links);return state.searchPreview;
    },'生成中…');
  }
  function showNew(source='opencli') { const form=$('new-form');form.reset();state.targetingDrafts={};state.currentMarket='cn';$('create-submit').textContent='创建并运行';form.elements.source.value=source;form.elements.collection_scope.value='posts';form.elements.comment_match_mode.value='any';form.elements.max_posts.value='3';form.elements.max_comments_per_post.value='20';form.elements.analysis_mode.value=state.settings.model.enabled?'local':'collect_only';setMarket(source==='demo'?'global':'cn',true);form.elements.name.value=source==='demo'?'演示批次 · 小品牌需求核查':'';form.elements.limit.max=String(state.settings.limits?.max_candidates||50);setError('new-error','');setError('import-validation','');showSource(source);updateAnalysisModeHint();state.wizardStep=0;state.wizardMaxVisited=0;setWizardStep(0,false);openDialog('new-dialog');form.elements.name.focus(); }
  function wizardFieldError(field,message) { const error=new Error(message);error.field=field;throw error; }
  function wizardSummary() {
    const f=$('new-form'),platforms=selectedPlatforms(),target=f.elements.source.value==='demo'?null:targetingPayload();
    $('wizard-summary').innerHTML=`<p><strong>${esc(f.elements.name.value.trim()||'未命名任务')}</strong></p><p>${esc(sources[f.elements.source.value]||'请选择来源')}${platforms.length&&f.elements.source.value==='opencli'?` / ${esc(platforms.map(platformLabel).join('、'))}`:''}</p>${target?`<p>${esc([target.market==='cn'?'国内':'海外',target.industry,...target.regions].filter(Boolean).join(' / '))}</p>`:''}`;
  }
  function setWizardStep(step,focus=true) {
    state.wizardStep=Math.max(0,Math.min(2,step));state.wizardMaxVisited=Math.max(state.wizardMaxVisited||0,state.wizardStep);
    document.querySelectorAll('[data-wizard-panel]').forEach(panel=>{panel.hidden=Number(panel.dataset.wizardPanel)!==state.wizardStep;});
    document.querySelectorAll('[data-wizard-step]').forEach(button=>{const number=Number(button.dataset.wizardStep);button.disabled=number>state.wizardMaxVisited;button.classList.toggle('completed',number<state.wizardStep);if(number===state.wizardStep)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current');});
    $('wizard-back').hidden=state.wizardStep===0;$('wizard-next').hidden=state.wizardStep===2;$('create-submit').hidden=state.wizardStep!==2;
    $('wizard-subtitle').textContent=['先确定目标，再选择来源。','选择来源与采集范围。','确认处理方式后创建。'][state.wizardStep];
    if(state.wizardStep===2)wizardSummary();$('wizard-body').scrollTop=0;
    if(focus){const heading=document.querySelector(`[data-wizard-panel="${state.wizardStep}"] h3`);if(heading){heading.tabIndex=-1;heading.focus({preventScroll:true});}}
  }
  function presentWizardError(error) {
    setError('new-error',errorMessage(error));const f=$('new-form');let field=error.field;
    if(!field&&String(error.message).includes('补充条件'))field=$('target-custom-fields').querySelector('input');
    if(!field&&String(error.message).includes('帖子数'))field=f.elements.max_posts;
    if(!field&&String(error.message).includes('每帖'))field=f.elements.max_comments_per_post;
    if(field){let details=field.closest?.('details');while(details){details.open=true;details=details.parentElement?.closest('details');}field.focus?.({preventScroll:false});}
  }
  async function validateWizardStep(step) {
    const f=$('new-form'),source=f.elements.source.value;
    if(step===0){if(!f.elements.name.value.trim())wizardFieldError(f.elements.name,'请先为任务起个名字。');if(source!=='demo')targetingPayload();return;}
    if(step===1){
      if(source==='import'){const records=await validateRecords();if(!records.length)wizardFieldError(f.elements.records,'没有可导入的记录，请检查来源与原文。');}
      if(source==='urls'||source==='comments'){const urls=f.elements.urls.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);if(!urls.length)wizardFieldError(f.elements.urls,'请至少填写一个来源链接。');if(urls.some(url=>!safeUrl(url)))wizardFieldError(f.elements.urls,'请使用完整的 http:// 或 https:// 链接。');}
      if(source==='opencli'){if(!selectedPlatforms().length)throw new Error('请至少选择一个可用平台。');if(!f.elements.queries.value.trim()){const plan=await generateSearchPreview($('preview-search'),false);if(!plan)throw new Error('条件已变化，请重新生成搜索词。');}}
      if(['comments','opencli'].includes(source))collectionPayload();return;
    }
    const limit=Number(f.elements.limit.value),maximum=Number(f.elements.limit.max)||1000;
    if(!Number.isInteger(limit)||limit<1||limit>maximum)wizardFieldError(f.elements.limit,`本轮最多条数应为 1–${maximum}。`);
  }
  async function goWizardStep(step) {
    let checking=state.wizardStep;try{setError('new-error','');if(step>state.wizardStep){for(let current=state.wizardStep;current<step;current++){checking=current;await validateWizardStep(current);}}setWizardStep(step);}catch(error){if(checking!==state.wizardStep)setWizardStep(checking,false);presentWizardError(error);}
  }
  async function wizardSubmit(event) {
    const form=event.currentTarget;event.preventDefault();if(state.wizardStep<2){await busy($('wizard-next'),()=>goWizardStep(state.wizardStep+1),'检查中…');return;}
    try{await validateWizardStep(2);await createRun({preventDefault(){},currentTarget:form});}catch(error){presentWizardError(error);}
  }
  function showSource(source) { document.querySelectorAll('.source-panel').forEach(p=>p.hidden=!p.dataset.source.split(' ').includes(source));$('targeting-builder').hidden=source==='demo';$('market-fieldset').hidden=source==='demo';$('analysis-mode-fieldset').hidden=source==='demo';$('source-url-label').textContent=source==='comments'?'待筛选评论的帖子 / 回答链接':'网页链接';$('source-url-help').textContent=source==='comments'?'每行一个帖子链接；先读取这些帖子下的评论，再按关键词筛选。知乎请使用回答链接。不是全站评论索引。':'读取公开网页。需要登录、验证码或纯动态内容的页面可能无法采集。';updateCollectionOptions(); }
  function parseRecords(text) {
    const input=String(text||'').replace(/^\uFEFF/,'').trim();if(!input)throw new Error('请先选择文件或粘贴记录。');
    if(input.startsWith('[')||input.startsWith('{')){try{const data=JSON.parse(input);if(Array.isArray(data))return data;if(Array.isArray(data.records))return data.records;if(data.source_url||data.content)return[data];throw new Error('JSON 应为记录数组或含 records 数组的对象。');}catch(error){const lines=input.split(/\r?\n/).filter(l=>l.trim());if(lines.length>1){try{return lines.map(l=>JSON.parse(l));}catch{}}throw new Error(`无法读取 JSON：${error.message}`);}}
    const first=input.split(/\r?\n/)[0];const delimiter=first.includes('\t')?'\t':',';const rows=[];let row=[],cell='',quoted=false;
    for(let i=0;i<input.length;i++){const c=input[i];if(c==='"'){if(quoted&&input[i+1]==='"'){cell+='"';i++;}else quoted=!quoted;}else if(c===delimiter&&!quoted){row.push(cell);cell='';}else if((c==='\n'||c==='\r')&&!quoted){if(c==='\r'&&input[i+1]==='\n')i++;row.push(cell);if(row.some(v=>v.trim()))rows.push(row);row=[];cell='';}else cell+=c;}if(quoted)throw new Error('CSV 存在未闭合的引号。');row.push(cell);if(row.some(v=>v.trim()))rows.push(row);if(rows.length<2)throw new Error('CSV 需要表头和至少一条记录。');
    const aliases={'来源网址':'source_url','来源链接':'source_url','网址':'source_url','链接':'source_url','url':'source_url','原文':'content','内容':'content','平台':'platform','品牌':'brand_name','品牌名称':'brand_name','发布日期':'posted_at','官网':'company_website','国家':'country','作者':'author_handle'};const headers=rows.shift().map(v=>aliases[v.trim()]||v.trim());return rows.map(values=>Object.fromEntries(headers.map((key,i)=>[key,values[i]||''])));
  }
  async function validateRecords() { const records=parseRecords($('records-input').value);const result=await api('/api/import/validate',{method:'POST',body:{records}});const normalized=Array.isArray(result)?result:(result.records||result.normalized_records||[]);const warnings=result.warnings||[];$('import-validation').className='form-message success';$('import-validation').textContent=`可导入 ${normalized.length} 条记录。${warnings.length?'\n'+warnings.map(w=>typeof w==='string'?w:JSON.stringify(w)).join('\n'):''}`;return normalized; }
  async function createRun(event) {
    event.preventDefault();setError('new-error','');const form=event.currentTarget;
    await busy($('create-submit'),async()=>{
      try {
        const source=form.elements.source.value;
        const body={name:form.elements.name.value.trim(),goal:form.elements.goal.value.trim(),source,limit:Number(form.elements.limit.value),queries:[],platforms:[],urls:[],records:[],targeting:source==='demo'?undefined:targetingPayload(),analysis_mode:source==='demo'?undefined:form.elements.analysis_mode.value,collection:['opencli','comments'].includes(source)?collectionPayload():undefined};
        if(!body.name)throw new Error('请填写批次名称。');
        if(!body.goal&&body.targeting){const t=body.targeting;body.goal=[t.market==='cn'?'国内定向获客':'海外商机',t.industry,t.regions.join('、'),t.needs].filter(Boolean).join(' · ');}
        if(source==='import'){body.records=await validateRecords();if(!body.records.length)throw new Error('没有可导入的记录，请检查来源网址和原文。');}
        if(source==='urls'||source==='comments'){
          body.urls=form.elements.urls.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
          if(!body.urls.length)throw new Error('请至少填写一个网页链接。');
          if(body.urls.some(u=>!safeUrl(u)))throw new Error('网页链接必须是完整的 http:// 或 https:// 地址。');
        }
        if(source==='opencli'){
          body.queries=form.elements.queries.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
          body.platforms=selectedPlatforms();
          if(!body.platforms.length)throw new Error('请至少选择一个已接入自动搜索的平台。');
          if(collectingComments()&&body.platforms.some(id=>!commentsSupported(state.platformCatalog.find(p=>p.id===id)||{})))throw new Error('所选平台尚未接入评论读取，请调整平台或改为读取帖子。');
          if(!body.queries.length){const plan=await generateSearchPreview($('preview-search'),false);if(!plan)throw new Error('请重新生成当前条件的搜索词。');body.queries=plan.queries;}
        }
        const run=await api('/api/runs',{method:'POST',body});
        const startNow=form.elements.start_now.checked;let startError=null;
        if(startNow){try{await api(`/api/runs/${encodeURIComponent(run.id)}/start`,{method:'POST',body:{}});}catch(error){startError=error;}}
        closeDialog($('new-dialog'));await loadRuns();await selectRun(run.id);
        toast(startError?`批次已创建，启动失败：${errorMessage(startError)}`:startNow?'批次已创建并启动':'批次已创建，随时可以开始运行',!!startError);
      } catch(error) { if($('new-dialog').open)setError('new-error',errorMessage(error));else toast(errorMessage(error),true); }
    },'创建中…');
  }
  async function showHistory() { openDialog('history-dialog');$('history-content').innerHTML='<div class="empty-list"><span class="spinner"></span><p>正在读取审核历史…</p></div>';try{const data=await api(`/api/reviews?run_id=${encodeURIComponent(state.run.id)}`);const items=Array.isArray(data)?data:(data.items||[]);$('history-content').innerHTML=items.length?items.map(r=>`<article class="history-item"><div class="history-item-head"><div><strong>${esc(reviewerLabels[r.reviewer]||r.reviewer)}</strong><span>${(r.changes||[]).length} 条修改</span><time>${esc(date(r.created_at))}</time>${r.status==='reverted'?'<span class="badge">已撤销</span>':''}</div>${r.status!=='reverted'?`<button class="button secondary small" data-revert="${esc(r.id)}">撤销本次</button>`:''}</div><div class="history-changes">${(r.changes||[]).map(c=>`<div class="history-change">${badge(c.decision)} <span>${esc(state.candidates.find(x=>x.id===c.candidate_id)?.brand_name||c.candidate_id)}</span><p>${esc(c.reason||'未填写理由')}</p></div>`).join('')}</div><div class="history-id">${esc(r.id)}${r.reverted_at?` · 撤销于 ${esc(date(r.reverted_at))}`:''}</div></article>`).join(''):'<div class="empty-list"><strong>本批还没有审核历史</strong>人工操作或导入审核后，可在这里查看与撤销。</div>';}catch(error){$('history-content').innerHTML=`<div class="empty-list"><strong>历史读取失败</strong>${esc(errorMessage(error))}<br><button class="button secondary small" data-action="history">重试</button></div>`;} }
  async function revertReview(id,button) {
    if(button.dataset.confirmRevert!=='yes'){
      const item=button.closest('.history-item');if(!item)return;
      const notice=document.createElement('div');notice.className='undo-confirmation';notice.setAttribute('role','status');notice.textContent='确认后将撤销本次审核的全部修改，恢复这次修改之前的审核意见；原始证据保持原样。如果任一候选此后已有新修改，程序会拒绝整批撤销，避免覆盖。';
      item.querySelector('.history-item-head').after(notice);
      button.dataset.confirmRevert='yes';button.textContent='确认撤销';button.classList.add('danger');
      const cancel=document.createElement('button');cancel.type='button';cancel.className='button secondary small';cancel.dataset.revertCancel=id;cancel.textContent='取消';button.after(cancel);
      return;
    }
    await busy(button,async()=>{await api(`/api/reviews/${encodeURIComponent(id)}/revert`,{method:'POST',body:{operation_id:uid()}});toast('已撤销本次审核');await refreshAfterReview(state.selectedId);await showHistory();},'撤销中…');
  }
  function cancelRevert(button) {
    const item=button.closest('.history-item');if(!item)return;
    const original=item.querySelector('[data-revert]');if(original){delete original.dataset.confirmRevert;original.textContent='撤销本次';original.classList.remove('danger');}
    item.querySelector('.undo-confirmation')?.remove();button.remove();
  }
  function showReviewImport() { $('review-import-form').reset();setError('review-import-error','');openDialog('review-import-dialog'); }
  function normalizeReview(data) { if(!data||typeof data!=='object')throw new Error('审核 JSON 应是对象或变更数组。');const raw=data.review||data.proposed_review||data.review_template||data;const changes=Array.isArray(raw)?raw:raw.changes;if(!Array.isArray(changes)||!changes.length)throw new Error('审核 JSON 需要非空 changes 数组。');const runId=raw.run_id||data.run_id||data.run?.id||state.run.id;if(runId!==state.run.id)throw new Error('审核文件属于其他批次，请切换到对应批次后导入。');const clean=changes.map((c,i)=>{if(!c||!c.candidate_id||!Number.isInteger(c.base_version)||c.base_version<1)throw new Error(`第 ${i+1} 条缺少 candidate_id 或有效 base_version。`);if(!['keep','exclude','needs_info','watch'].includes(c.decision))throw new Error(`第 ${i+1} 条 decision 不受支持。`);if(typeof c.reason!=='string'||!c.reason.trim())throw new Error(`第 ${i+1} 条请填写 reason 审核依据。`);return {candidate_id:c.candidate_id,base_version:c.base_version,decision:c.decision,reason:c.reason,evidence_ids:Array.isArray(c.evidence_ids)?c.evidence_ids:[],tags:Array.isArray(c.tags)?c.tags:[]};});return {run_id:runId,reviewer:'codex',operation_id:raw.operation_id||data.operation_id||uid(),changes:clean}; }
  async function importReview(event) { event.preventDefault();setError('review-import-error','');await busy($('review-import-submit'),async()=>{try{const body=normalizeReview(JSON.parse($('review-json').value));$('review-json').value=JSON.stringify(body,null,2);await api('/api/reviews',{method:'POST',body});closeDialog($('review-import-dialog'));toast(`已导入 ${body.changes.length} 条 Codex 审核意见`);await refreshAfterReview(state.selectedId);}catch(error){setError('review-import-error',error.status===409?`${errorMessage(error)} 请导出最新版本重新复核。`:errorMessage(error));}},'应用中…'); }
  function showBatchReview(decision) { state.batchDecision=decision;state.batchCandidates=state.candidates.filter(c=>state.checked.has(c.id)).map(c=>({...c}));$('batch-review-description').textContent=`将选中的 ${state.batchCandidates.length} 条候选标记为“${decisions[decision]}”，保留审核历史。`;$('batch-review-form').reset();setError('batch-review-error','');openDialog('batch-review-dialog'); }
  async function submitBatchReview(event) { event.preventDefault();setError('batch-review-error','');await busy($('batch-review-submit'),async()=>{try{const decision=state.batchDecision;const selected=state.batchCandidates||[];if(!selected.length)throw new Error('请先选择候选。');const reason=$('batch-review-reason').value.trim()||`人工批量标记为${decisions[decision]}。`;await api('/api/reviews',{method:'POST',body:{run_id:state.run.id,reviewer:'human',operation_id:uid(),changes:selected.map(c=>({candidate_id:c.id,base_version:c.version,decision,reason,evidence_ids:[],tags:[]}))}});state.checked.clear();closeDialog($('batch-review-dialog'));toast(`已审核 ${selected.length} 条候选`);await refreshAfterReview(state.selectedId);}catch(error){setError('batch-review-error',errorMessage(error));}},'保存中…'); }
  async function showSettings() { setError('settings-error','');$('model-test-result').hidden=true;try{state.settings=mergeSettings(await api('/api/settings'));}catch(error){toast(errorMessage(error),true);}const m=state.settings.model,o=state.settings.opencli,form=$('settings-form');for(const key of ['protocol','model','base_url','timeout_seconds','max_tokens','temperature'])if(form.elements[key])form.elements[key].value=m[key]??defaults.model[key];form.elements.enabled.checked=!!m.enabled;form.elements.api_key.value='';form.elements.api_key.placeholder=m.api_key||m.api_key_set||m.api_key_configured?'已保存密钥，留空保留':'服务未设密钥时可留空';form.elements.clear_key.checked=false;form.elements.executable.value=o.executable;form.elements.node_entry.value=o.node_entry||'';form.elements.profile.value=o.profile;form.elements.opencli_timeout.value=o.timeout_seconds;openDialog('settings-dialog'); }
  function settingsPayload() { const f=$('settings-form');const model={protocol:f.elements.protocol.value,base_url:f.elements.base_url.value.trim(),model:f.elements.model.value.trim(),enabled:f.elements.enabled.checked,timeout_seconds:Number(f.elements.timeout_seconds.value),max_tokens:Number(f.elements.max_tokens.value),temperature:Number(f.elements.temperature.value),concurrency:1};if(!safeUrl(model.base_url))throw new Error('请输入完整的 http:// 或 https:// 模型服务地址。');if(!model.model)throw new Error('请填写本地服务实际提供的模型名称或 alias。');if(f.elements.clear_key.checked)model.api_key='';else if(f.elements.api_key.value.trim())model.api_key=f.elements.api_key.value.trim();return {model,opencli:{executable:f.elements.executable.value.trim()||'opencli',node_entry:f.elements.node_entry.value.trim(),profile:f.elements.profile.value.trim(),timeout_seconds:Number(f.elements.opencli_timeout.value)}}; }
  async function saveSettings(event) { event.preventDefault();setError('settings-error','');await busy($('settings-submit'),async()=>{try{state.settings=mergeSettings(await api('/api/settings',{method:'PUT',body:settingsPayload()}));state.modelTest=null;setModelIndicator();closeDialog($('settings-dialog'));toast('设置已保存；连接状态以实际测试为准');}catch(error){setError('settings-error',errorMessage(error));}},'保存中…'); }
  async function testModel(button) { setError('settings-error','');await busy(button,async()=>{const box=$('model-test-result');box.hidden=false;box.className='test-result';box.innerHTML='<span class="spinner"></span> 正在请求模型接口，请保持本地服务运行…';try{const result=await api('/api/model/test',{method:'POST',body:settingsPayload().model,timeout:300000});state.modelTest=result;setModelIndicator();const models=(result.models||[]).map(m=>typeof m==='string'?m:m.id||m.name||'').filter(Boolean);box.className=`test-result ${result.reachable?'success':'error'}`;box.innerHTML=`<strong>${result.reachable?'接口可达':'连接未通过'}</strong>${result.latency_ms!==undefined?` · ${esc(result.latency_ms)} ms`:''}<div>${esc(result.error||display(result.health,result.reachable?'请继续通过真实批次验证分析输出。':'未取得可用响应。'))}</div>${result.model?`<div>测试模型：${esc(result.model)}</div>`:''}${models.length?`<label>接口返回的模型<select id="detected-model"><option value="">选择一个模型填入设置…</option>${models.map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('')}</select></label>`:''}`;}catch(error){state.modelTest=null;setModelIndicator();box.className='test-result error';box.textContent=errorMessage(error);}},'测试中…'); }
  function reviewExample() { const c=state.detail||state.candidates[0];return {run_id:state.run?.id||'run_批次ID',reviewer:'codex',operation_id:uid(),changes:[{candidate_id:c?.id||'c_候选ID',base_version:c?.version||1,decision:'needs_info',reason:'指出原文中已有的证据，以及仍需确认的采购条件。',evidence_ids:c?.evidence?.map(e=>e.id)||[],tags:[]}]}; }
  function showGuide() { $('review-example').textContent=JSON.stringify(reviewExample(),null,2);openDialog('guide-dialog'); }
  async function copyText(text) { try { await navigator.clipboard.writeText(text);toast('已复制到剪贴板'); } catch { const area=document.createElement('textarea');area.value=text;area.style.position='fixed';area.style.opacity='0';document.body.appendChild(area);area.select();let copied=false;try{copied=document.execCommand('copy');}catch{}area.remove();if(copied)toast('已复制到剪贴板');else toast('浏览器未允许复制，请手动选择内容。',true); } }
  function debounce(fn,ms) { let timer;return (...args)=>{clearTimeout(timer);timer=setTimeout(()=>fn(...args),ms);}; }
  async function poll() { if(!state.bootstrapDone||state.polling||state.reviewBusy||state.controlBusy||document.hidden||document.querySelector('dialog[open]'))return;state.polling=true;try{const id=state.run?.id;await loadRuns();if(id&&state.run?.id===id){const run=await api(`/api/runs/${encodeURIComponent(id)}`);if(state.run?.id!==id)return;state.run=run;renderBatchHeader();await loadCandidates();}globalError('');}catch(error){globalError(errorMessage(error));}finally{state.polling=false;} }
  document.addEventListener('click',async(event)=>{
    const b=event.target.closest('button,a');if(!b)return;
    if(b.classList.contains('close-dialog')){closeDialog(b.closest('dialog'));return;}
    try {
      if(b.dataset.run){await selectRun(b.dataset.run);return;}
      if(b.dataset.newSource){showNew(b.dataset.newSource);return;}
      if(b.dataset.candidate){await loadDetail(b.dataset.candidate);return;}
      if(b.hasAttribute('data-filter')){state.filter=b.dataset.filter;state.offset=0;state.checked.clear();renderFilters();await loadCandidates();return;}
      if(b.dataset.detailTab){state.detailTab=b.dataset.detailTab;renderDetailBody();return;}
      if(b.dataset.review){await submitReview(b.dataset.review,b);return;}
      if(b.dataset.batch){showBatchReview(b.dataset.batch);return;}
      if(b.dataset.runControl){await controlRun(b.dataset.runControl,b);return;}
      if(b.dataset.revertCancel){cancelRevert(b);return;}
      if(b.dataset.revert){await revertReview(b.dataset.revert,b);return;}
      if(b.dataset.page){state.offset=Math.max(0,state.offset+(b.dataset.page==='next'?state.pageSize:-state.pageSize));state.checked.clear();await loadCandidates();return;}
      switch(b.dataset.action){case'retry':await initialize();break;case'new':showNew();break;case'demo':showNew('demo');break;case'history':if(state.run)await showHistory();break;case'review-import':if(state.run)showReviewImport();break;case'clear-checks':state.checked.clear();renderCandidates();break;case'select-page':state.candidates.forEach(c=>state.checked.add(c.id));renderCandidates();break;case'back-list':$('workbench').classList.remove('has-detail');break;case'retry-detail':if(state.selectedId)await loadDetail(state.selectedId);break;}
    }catch(error){toast(errorMessage(error),true);}
  });
  document.addEventListener('change',(event)=>{if(event.target.dataset.check){const id=event.target.dataset.check;event.target.checked?state.checked.add(id):state.checked.delete(id);renderSelection();}if(event.target.id==='detected-model'){$('settings-form').elements.model.value=event.target.value||$('settings-form').elements.model.value;} });
  $('wizard-next').addEventListener('click',()=>busy($('wizard-next'),()=>goWizardStep(state.wizardStep+1),'检查中…'));
  $('wizard-back').addEventListener('click',()=>goWizardStep(state.wizardStep-1));
  document.querySelectorAll('[data-wizard-step]').forEach(button=>button.addEventListener('click',()=>goWizardStep(Number(button.dataset.wizardStep))));
  $('market-switch').addEventListener('change',e=>{if(e.target.name==='market')setMarket(e.target.value);});
  $('home-preset').addEventListener('click',()=>{delete state.targetingDrafts.cn;setMarket('cn',true);toast('已填入家装业主示例，请补充你服务的地区。');});
  $('add-custom-field').addEventListener('click',()=>{addCustomField();invalidateSearchPreview();});
  $('target-custom-fields').addEventListener('click',e=>{const button=e.target.closest('[data-remove-custom]');if(button){button.closest('.custom-field-row').remove();invalidateSearchPreview();}});
  $('new-form').addEventListener('input',e=>{if(e.target.closest('#targeting-builder')||e.target.name==='queries'||e.target.name==='goal')invalidateSearchPreview();});
  $('search-platforms').addEventListener('change',invalidateSearchPreview);
  $('collection-scope').addEventListener('change',()=>{updateCollectionOptions();invalidateSearchPreview();});
  $('analysis-mode-fieldset').addEventListener('change',updateAnalysisModeHint);
  $('use-controller').addEventListener('click',prefillController);
  $('preview-search').addEventListener('click',e=>generateSearchPreview(e.currentTarget,true).catch(error=>setError('new-error',errorMessage(error))));
  document.querySelectorAll('.close-dialog.icon-button').forEach(b=>b.innerHTML=icon('close'));$('refresh-runs').innerHTML=icon('refresh');$('toggle-nav').innerHTML=icon('menu');
  $('new-run').addEventListener('click',()=>showNew());$('refresh-runs').addEventListener('click',()=>busy($('refresh-runs'),poll,'').catch(e=>toast(errorMessage(e),true)));$('toggle-nav').addEventListener('click',()=>{$('app').classList.toggle('nav-open');});document.addEventListener('click',e=>{if($('app').classList.contains('nav-open')&&!e.target.closest('.sidebar')&&!e.target.closest('#toggle-nav'))$('app').classList.remove('nav-open');});
  $('brand-home').addEventListener('click',e=>{e.preventDefault();state.runEpoch++;state.selectedId=null;state.detail=null;renderWelcome();$('app').classList.remove('nav-open');});
  $('source-switch').addEventListener('change',e=>showSource(e.target.value));$('new-form').elements.start_now.addEventListener('change',e=>$('create-submit').textContent=e.target.checked?'创建并运行':'创建批次');$('new-form').addEventListener('submit',wizardSubmit);
  $('records-file').addEventListener('change',async(e)=>{try{const f=e.target.files[0];if(!f)return;if(f.size>3500000)throw new Error('文件超过 3.5 MB，请缩小本轮导入范围。');$('records-input').value=await f.text();setError('import-validation','文件已读取，可先检查导入内容。');}catch(error){setError('import-validation',errorMessage(error));}});
  $('validate-records').addEventListener('click',e=>busy(e.currentTarget,async()=>{try{await validateRecords();}catch(error){$('import-validation').className='form-message error';setError('import-validation',errorMessage(error));}},'检查中…'));
  $('open-settings').addEventListener('click',showSettings);$('settings-form').addEventListener('submit',saveSettings);$('test-model').addEventListener('click',e=>testModel(e.currentTarget));$('open-guide').addEventListener('click',showGuide);$('copy-review-example').addEventListener('click',()=>copyText($('review-example').textContent));$('review-import-form').addEventListener('submit',importReview);$('batch-review-form').addEventListener('submit',submitBatchReview);
  $('review-file').addEventListener('change',async(e)=>{try{const f=e.target.files[0];if(!f)return;if(f.size>3500000)throw new Error('审核文件超过 3.5 MB，请分批处理。');$('review-json').value=await f.text();}catch(error){setError('review-import-error',errorMessage(error));}});
  document.querySelectorAll('dialog').forEach(d=>d.addEventListener('click',e=>{if(e.target===d){const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close();}}));
  document.addEventListener('keydown',e=>{if(e.key==='Escape')$('app').classList.remove('nav-open');if((e.ctrlKey||e.metaKey)&&e.key==='k'&&!document.querySelector('dialog[open]')){e.preventDefault();$('candidate-search')?.focus();}});
  initialize();setInterval(poll,3000);
})();
