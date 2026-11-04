'use strict';
const $ = (s) => document.querySelector(s);
let session = crypto.randomUUID();
let currentState;
let platformLabels = {};
let catalogVersion = '';
let displayedReplyMode = null;
let aiSettingsLoaded = false;
let aiSettingsRecovery = false;
let aiSettingsBusy = '';
let aiFormVersion = 0;
let aiTestRequest = 0;
function renderCatalog(catalog) {
  const version = JSON.stringify(catalog);
  if (version === catalogVersion) return;
  catalogVersion = version;
  platformLabels = Object.fromEntries(catalog.map(p => [p.id, p.name]));
  for (const selector of ['select[name="platform"]', '#preview-platform']) {
    const select = $(selector), previous = select.value;
    select.replaceChildren();
    if (selector !== '#preview-platform') {
      const option = document.createElement('option'); option.value = ''; option.textContent = '所有平台共用'; select.append(option);
    }
    for (const platform of catalog) {
      const option = document.createElement('option'); option.value = platform.id; option.textContent = platform.name; select.append(option);
    }
    if ([...select.options].some(o => o.value === previous)) select.value = previous;
  }
}
function notice(text, error = false) { $('#notice').textContent = text; $('#notice').className = error ? 'error' : ''; }
async function api(path, method = 'GET', body) {
  const res = await fetch('/api/' + path, {method, headers: {'Content-Type': 'application/json', 'X-Requested-With': 'merchant-console'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const data = await res.json();
  if (!res.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
    error.safeMessage = typeof data.detail === 'string' ? data.detail : '';
    error.status = res.status;
    throw error;
  }
  return data;
}
async function action(button, fn) { button.disabled = true; try { await fn(); } catch (e) { notice(e.message, true); } finally { button.disabled = false; if (currentState && button.classList.contains('mode-choice')) renderReplyMode(currentState); } }
function setProfile(profile) {
  $('#profile-json').value = JSON.stringify(profile, null, 2);
  const f = $('#profile-form'); const product = profile.products[0] || {};
  for (const key of ['merchant_name', 'tone']) f.elements[key].value = profile[key] || '';
  for (const key of ['id','name','price','min_price','stock','description','shipping','returns']) f.elements[key].value = product[key] ?? '';
  f.elements.platform.value = product.platform || '';
  f.elements.bargain_enabled.value = String(product.bargain_enabled ?? true);
  f.elements.conditions.value = profile.custom_instructions || '';
  f.elements.handoff_keywords.value = (profile.handoff_keywords || []).join(',');
  f.elements.faq.value = (product.faq || []).map(x => x.question + ' | ' + x.answer).join('\n');
  $('#preview-product').replaceChildren();
  for (const item of profile.products) { const opt = document.createElement('option'); opt.value = JSON.stringify([item.platform||'',item.id]); opt.textContent = item.name + (item.platform ? ' · '+(platformLabels[item.platform] || item.platform) : ''); $('#preview-product').append(opt); }
}
function renderReplyMode(data) {
  const mode = data.reply_mode || data.profile.reply_mode || 'custom';
  const isDefault = mode === 'default';
  $('#use-default').setAttribute('aria-pressed', String(isDefault));
  $('#use-custom').setAttribute('aria-pressed', String(!isDefault));
  $('#use-default').disabled = data.running || isDefault;
  $('#use-custom').disabled = data.running || !isDefault;
  $('#custom-profile').hidden = isDefault;
  $('#default-replies').hidden = !isDefault;
  $('#preview-product-label').hidden = isDefault;
  $('#mode-status').textContent = (isDefault ? '当前使用默认客服。已有的商品资料会保留。' : '当前按商品资料回复。') + (data.running ? ' 切换前请先停止机器人。' : '');
  if (displayedReplyMode !== null && displayedReplyMode !== mode) newSession();
  displayedReplyMode = mode;
}
function renderState(data) {
  currentState = data;
  renderCatalog(data.catalog || []);
  renderReplyMode(data);
  $('#status').textContent = data.running ? (data.mode === 'live' ? '自动回复中' : '观察中 · 不发送') : '本机工作台';
  $('#runtime-label').textContent = data.running ? (data.mode === 'live' ? '自动回复已启动' : '正在观察新消息') : '机器人未启动';
  $('#runtime-description').textContent = data.reply_mode === 'default' ? '默认客服已准备好。连接店铺后，先观察再开启回复。' : (data.profile.products.length ? '已保存商品规则。平台连接情况见“平台接入”。' : '先填写自己的商品资料，再试聊。');
  const aiStatus = data.ai_status || (data.ai_configured ? 'enabled' : 'unconfigured');
  const aiMessages = {
    enabled: 'AI 辅助已开启，配置已读取。连接情况以实际试聊为准；调用失败时使用内置话术。',
    disabled: 'AI 已关闭，当前使用内置话术。开启后可以让回复更自然。',
    unconfigured: '未配置 AI，当前使用内置话术，已经可以使用。接上 AI 后表达更自然。'
  };
  $('#default-description').textContent = data.default_skill?.description || '默认客服可直接用。接上 AI 后表达更自然，调用失败时继续使用内置话术。';
  $('#default-ai-status').textContent = aiMessages[aiStatus] || aiMessages.unconfigured;
  if (data.reply_mode === 'default') {
    const preview = aiStatus === 'enabled' ? 'AI 辅助已开启，失败时回退内置话术。' : (aiStatus === 'disabled' ? 'AI 已关闭，正在试用内置话术。' : '未配置 AI，正在试用内置话术。');
    $('#preview-note').textContent = preview + '试聊不会发给真实买家。';
  } else {
    const preview = aiStatus === 'enabled' ? 'AI 可辅助识别问法，回复仍按商品资料。' : (aiStatus === 'disabled' ? 'AI 已关闭，按商品资料和规则回复。' : '未配置 AI，按商品资料和规则回复。');
    $('#preview-note').textContent = preview + '试聊不会发给真实买家。';
  }
  $('#platform-list').replaceChildren();
  for (const [key, cfg] of Object.entries(data.connections.platforms)) {
    const row = document.createElement('div'); row.className = 'platform-row';
    const title = document.createElement('strong'); title.textContent = cfg.name;
    const desc = document.createElement('small'); const s = data.platforms[key];
    const meta = (data.catalog || []).find(p => p.id === key);
    const verification = meta?.verification_label || '待实店验证';
    desc.textContent = verification + ' · ' + (s ? `${s.status}${s.error ? ' · ' + s.error : ''}` : (cfg.enabled ? '已启用，尚未连接' : '未启用'));
    row.append(title, desc); $('#platform-list').append(row);
  }
}
async function load() { const data = await api('state'); renderState(data); setProfile(data.profile); $('#connections-json').value = JSON.stringify(data.connections, null, 2); }
$('#profile-form').addEventListener('submit', e => { e.preventDefault(); action(e.submitter, async () => {
  const f = Object.fromEntries(new FormData(e.target));
  const product = {platform:f.platform,bargain_enabled:f.bargain_enabled==='true',id:f.id, name:f.name, description:f.description, shipping:f.shipping, returns:f.returns, faq: f.faq.split('\n').filter(x=>x.trim()).map(line=>{const i=line.indexOf('|'); if(i<1) throw Error('每条问答需要用 | 分隔问题和答案'); return {question:line.slice(0,i).trim(),answer:line.slice(i+1).trim()};})};
  for(const key of ['price','min_price','stock']) if(f[key] !== '') product[key] = Number(f[key]);
  const saved = currentState?.profile?.products || [];
  const products = saved.filter(p=>(p.id !== product.id || (p.platform||'') !== product.platform));
  products.push(product);
  const profile = await api('profile/generate','POST',{reply_mode:'custom',merchant_name:f.merchant_name,tone:f.tone,conditions:f.conditions,handoff_keywords:f.handoff_keywords.split(/[,，]/).map(x=>x.trim()).filter(Boolean),products});
  await api('profile','PUT',profile); await load(); newSession(); notice('已生成并保存。可以在右侧测试回复。');
}); });
for (const mode of ['default', 'custom']) $('#use-'+mode).onclick=e=>action(e.currentTarget, async()=>{
  await api('reply-mode', 'PUT', {mode}); await load(); newSession();
  notice(mode === 'default' ? '已切换到默认客服，商品资料仍然保留。' : '已切换到按商品定制，请填写或检查商品资料。');
});
$('#default-connect').onclick=()=>document.querySelector('[data-tab="connections"]').click();
$('#default-ai-settings').onclick=()=>document.querySelector('[data-tab="ai-settings"]').click();
function aiFeedback(message, status = '') {
  $('#ai-feedback').textContent = message;
  $('#ai-feedback').dataset.status = status;
}
function updateAIControls() {
  const clearing = $('#ai-clear-key').checked;
  $('#ai-settings-fields').disabled = !aiSettingsLoaded || aiSettingsBusy === 'load' || aiSettingsBusy === 'save';
  $('#ai-api-key').disabled = clearing;
  $('#ai-enabled').disabled = clearing;
  $('#ai-test').disabled = !aiSettingsLoaded || Boolean(aiSettingsBusy) || clearing;
  $('#ai-save').disabled = !aiSettingsLoaded || Boolean(aiSettingsBusy);
  $('#ai-test').textContent = aiSettingsBusy === 'test' ? '测试中…' : '测试连接';
  $('#ai-save').textContent = aiSettingsBusy === 'save' ? '保存中…' : '保存设置';
}
function fillAISettings(data) {
  aiSettingsRecovery = false;
  $('#ai-enabled').checked = Boolean(data.enabled);
  $('#ai-base-url').value = data.base_url || '';
  $('#ai-model').value = data.model || '';
  $('#ai-timeout').value = data.timeout_seconds;
  $('#ai-api-key').value = '';
  $('#ai-api-key').placeholder = data.has_api_key ? '已保存密钥；留空保持不变，填写可替换' : '粘贴服务商提供的密钥';
  $('#ai-key-status').textContent = data.has_api_key ? '已保存密钥，不会回显。更换服务商地址时，请重新填写对应密钥。' : '尚未保存密钥。密钥只保存在本机，不会在页面回显。';
  $('#ai-clear-key').checked = false;
  $('#ai-clear-key-row').hidden = !data.has_api_key;
  $('#ai-clear-key-label').textContent = '删除已保存的密钥，并关闭 AI';
  $('#ai-saved-status').textContent = data.has_api_key
    ? (data.enabled ? '已保存：AI 辅助已开启。是否能连接，请点“测试连接”。' : '已保存：AI 已关闭，密钥已保留。')
    : '已保存：尚未填写密钥，当前使用内置话术。';
  aiFormVersion += 1;
}
function recoverAISettings() {
  aiSettingsLoaded = true;
  aiSettingsRecovery = true;
  $('#ai-enabled').checked = false;
  $('#ai-base-url').value = '';
  $('#ai-model').value = '';
  $('#ai-timeout').value = 12;
  $('#ai-api-key').value = '';
  $('#ai-api-key').placeholder = '请重新填写密钥';
  $('#ai-key-status').textContent = '原设置无法读取。请填写新密钥，或勾选下方“不保留原密钥，并关闭 AI”。';
  $('#ai-clear-key').checked = false;
  $('#ai-clear-key-row').hidden = false;
  $('#ai-clear-key-label').textContent = '不保留原密钥，并关闭 AI';
  $('#ai-saved-status').textContent = '原设置无法读取。请重新填写接口地址、模型和密钥后保存。';
  aiFormVersion += 1;
}
async function loadAISettings() {
  if (aiSettingsLoaded || aiSettingsBusy) return;
  aiSettingsBusy = 'load';
  $('#ai-load-retry').hidden = true;
  updateAIControls();
  try {
    fillAISettings(await api('ai-settings'));
    aiSettingsLoaded = true;
    aiFeedback('填写后可以先测试，再保存。');
  } catch (error) {
    if (error.status === 400) {
      recoverAISettings();
      aiFeedback(error.safeMessage || '原设置有误，请在这里重新填写后保存。', 'error');
    } else {
      $('#ai-saved-status').textContent = '尚未读取到已保存的设置。';
      aiFeedback(error.safeMessage || '暂时无法读取 AI 设置，请稍后重试。', 'error');
      $('#ai-load-retry').hidden = false;
    }
  } finally {
    aiSettingsBusy = '';
    updateAIControls();
  }
}
function aiPayload() {
  return {
    enabled: $('#ai-enabled').checked,
    base_url: $('#ai-base-url').value.trim(),
    model: $('#ai-model').value.trim(),
    timeout_seconds: Number($('#ai-timeout').value),
    api_key: $('#ai-api-key').value.trim(),
    clear_api_key: $('#ai-clear-key').checked
  };
}
function aiFormChanged(event) {
  if (event.target === $('#ai-clear-key') && event.target.checked) {
    $('#ai-api-key').value = '';
    $('#ai-enabled').checked = false;
  }
  aiFormVersion += 1;
  aiFeedback($('#ai-clear-key').checked ? '点“保存设置”后，将删除密钥并关闭 AI。' : '设置已修改，保存后才会生效。可以先测试连接。');
  updateAIControls();
}
$('#ai-settings-form').addEventListener('input', aiFormChanged);
$('#ai-settings-form').addEventListener('change', aiFormChanged);
$('#ai-load-retry').onclick = loadAISettings;
$('#ai-test').onclick = async () => {
  if (aiSettingsBusy || !$('#ai-settings-form').reportValidity()) return;
  if (aiSettingsRecovery && !$('#ai-api-key').value.trim()) {
    aiFeedback('原设置无法读取，请先填写新的密钥再测试。', 'error');
    return;
  }
  const version = aiFormVersion;
  const request = ++aiTestRequest;
  aiSettingsBusy = 'test';
  aiFeedback('正在测试当前填写的设置…');
  updateAIControls();
  try {
    const result = await api('ai-settings/test', 'POST', aiPayload());
    if (version !== aiFormVersion || request !== aiTestRequest) return;
    const timing = Number.isFinite(result.latency_ms) ? ` 用时 ${(result.latency_ms / 1000).toFixed(1)} 秒。` : '';
    aiFeedback((result.message || (result.ok ? '连接测试通过。' : '连接测试未通过，请检查填写的信息。')) + (result.ok ? timing + ' 本次测试没有保存设置，也没有改变 AI 开关。' : ''), result.ok ? 'success' : 'error');
  } catch (error) {
    if (version !== aiFormVersion || request !== aiTestRequest) return;
    aiFeedback(error.safeMessage || '暂时无法完成测试，请检查工作台是否仍在运行，再试一次。', 'error');
  } finally {
    aiSettingsBusy = '';
    updateAIControls();
  }
};
$('#ai-settings-form').onsubmit = async event => {
  event.preventDefault();
  if (aiSettingsBusy) return;
  // Read the form before disabling its controls. Never retain the key in UI state.
  const payload = aiPayload();
  if (aiSettingsRecovery && !payload.api_key && !payload.clear_api_key) {
    aiFeedback('请填写新的密钥，或勾选“不保留原密钥，并关闭 AI”后保存。', 'error');
    return;
  }
  aiSettingsBusy = 'save';
  aiFeedback('正在保存设置…');
  updateAIControls();
  try {
    const saved = await api('ai-settings', 'PUT', payload);
    fillAISettings(saved);
    aiFeedback('设置已保存，接下来的回复会使用新设置，无需重启。', 'success');
    api('state').then(renderState).catch(() => {});
  } catch (error) {
    aiFeedback(error.safeMessage || '暂时无法保存设置，请检查工作台是否仍在运行，再试一次。', 'error');
  } finally {
    aiSettingsBusy = '';
    updateAIControls();
  }
};
$('#save-json').onclick = e => action(e.target, async()=>{await api('profile','PUT',JSON.parse($('#profile-json').value)); await load(); newSession(); notice('完整商品配置已保存。');});
$('#save-connections').onclick = e => action(e.target, async()=>{await api('connections','PUT',JSON.parse($('#connections-json').value)); await load(); notice('接入配置已保存。可以先开始观察。');});
function bubble(text, kind, label='') { const node=document.createElement('div');node.className='bubble '+kind;node.textContent=text;if(label){const small=document.createElement('small');small.textContent=label;node.append(small);}$('#chat').append(node);$('#chat').scrollTop=$('#chat').scrollHeight; }
async function sendChat(text){
  const requestSession = session;
  bubble(text,'user'); $('#chat-input').value='';
  const selected = currentState?.reply_mode === 'custom' && $('#preview-product').value ? JSON.parse($('#preview-product').value) : ['', ''];
  let r;
  try { r=await api('preview','POST',{text,session_id:requestSession,platform:selected[0]||$('#preview-platform').value,item_id:selected[1]}); }
  catch (error) { if (session !== requestSession) return; throw error; }
  if (session !== requestSession) return;
  let label = '本地试聊';
  if (r.action === 'handoff') label = '转人工，后续自动回复暂停';
  else if (r.reply_mode === 'default' && r.reply) {
    if (r.reply_source === 'ai') label = 'AI 回复 · 本地试聊';
    else if (r.reply_source === 'builtin') label = '内置话术 · 本地试聊';
  }
  bubble(r.reply || '此会话已暂停自动回复，请重新开始会话继续测试。','assistant',label);
}
$('#chat-form').onsubmit=e=>{e.preventDefault();action(e.submitter,()=>sendChat($('#chat-input').value));};
document.querySelectorAll('[data-prompt]').forEach(button=>button.onclick=()=>action(button,()=>sendChat(button.dataset.prompt)));
function newSession(){session=crypto.randomUUID();$('#chat').replaceChildren();bubble('新的试聊会话已准备好。','assistant');}
$('#new-session').onclick=newSession;
$('#preview-product').onchange=newSession; $('#preview-platform').onchange=newSession;
for(const mode of ['observe','live']) $('#'+mode).onclick=e=>action(e.target,async()=>{if(mode==='live' && !confirm('将按当前配置向真实买家发送回复。请确认已通过观察模式核对店铺、商品与消息方向。'))return;await api('start','POST',{mode});renderState(await api('state'));notice(mode==='live'?'自动回复已启动，请查看平台连接状态。':'观察模式已启动，不会发送回复。');});
$('#stop').onclick=e=>action(e.target,async()=>{await api('stop','POST',{});renderState(await api('state'));notice('所有平台已停止。');});
const statuses={generated:'已生成，尚未确认发送',sent:'已发送',failed:'发送失败 / 结果待核实',ignored:'已忽略',handoff:'已转人工',processing:'处理中'};
async function events(){const data=await api('events');$('#events').replaceChildren();for(const item of data.events){const row=document.createElement('tr');for(const value of [String(item.platform||'')+' / '+String(item.conversation_id||item.conversation_key||''),item.text||item.user_text||'',item.reply||'',statuses[item.status]||item.status||'']){const cell=document.createElement('td');cell.textContent=value;row.append(cell);}$('#events').append(row);}if(!data.events.length){const row=document.createElement('tr');const cell=document.createElement('td');cell.colSpan=4;cell.textContent='暂无店铺消息。右侧的本地试聊使用独立记录。';row.append(cell);$('#events').append(row);}$('#handoffs').replaceChildren();for(const handoff of data.handoffs){const row=document.createElement('div');row.className='handoff';const label=document.createElement('span');label.textContent=`已接管：${handoff.platform} / ${handoff.conversation_id}`;const button=document.createElement('button');button.className='secondary';button.textContent='恢复自动回复';button.onclick=()=>action(button,async()=>{await api('handoff','POST',{...handoff,paused:false});await events();});row.append(label,button);$('#handoffs').append(row);}}
$('#refresh-events').onclick=e=>action(e.target,events);
// Order delivery is separate from chat login and the customer-service runtime.
let fulfillmentData = null;
let fulfillmentBusy = false;
let fulfillmentLoading = false;
let fulfillmentJobFilter = 'all';
let fulfillmentJobOffset = 0;
let fulfillmentJobsPage = {jobs:[],counts:{},offset:0,limit:50,total:0,has_more:false};
const deliveryTypes = {card:'卡密',link:'资料链接',physical:'实物物流'};
const deliveryStatuses = {ready:'可交付（试运行）',preview:'已核对（试运行）',reserved:'待发货',sending:'发送中',sent:'已交付',unknown:'待核实',needs_review:'待核实',blocked:'已阻止',awaiting_shipment:'待发货',processed:'本次处理完成',unchanged:'状态未变'};
const deliveryErrors = {
  rule_missing:'尚未配置这件商品的发货规则',rule_disabled:'这件商品的发货规则已停用',rule_changed:'发货规则已变化，请人工核对',
  price_mismatch:'实付金额未达到规则要求',insufficient_inventory:'卡密库存不足，请补充库存',order_unpaid:'订单尚未确认付款',
  order_cancelled:'订单已取消',order_refunded:'订单已退款或正在退款',order_fulfilled:'订单已由原系统完成交付',order_changed:'订单内容已变化，请人工核对',
  verification_failed:'未能核实订单，请检查订单来源',delivery_failed:'交付失败，可核验订单后重试',delivery_uncertain:'发送结果不明确，请到原订单核对',
  interrupted:'处理曾被中断，请到原订单核对',source_disabled:'订单来源未启用',tracking_required:'请先登记物流信息'
};
function fulfillmentFeedback(message, status='') {
  $('#fulfillment-feedback').textContent = message;
  $('#fulfillment-feedback').dataset.status = status;
}
function fulfillmentElement(tag, text, className='') {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}
function fulfillmentPlatform(id) {
  return (fulfillmentData?.platforms || []).find(p=>p.id===id)?.name || platformLabels[id] || id || '未指定平台';
}
function fulfillmentOptions(selector, values, emptyLabel) {
  const select = $(selector), previous = select.value;
  select.replaceChildren();
  if (!values.length) { const option=fulfillmentElement('option',emptyLabel);option.value='';select.append(option); }
  for (const value of values) { const option=fulfillmentElement('option',value.label);option.value=value.id;select.append(option); }
  if ([...select.options].some(option=>option.value===previous)) select.value=previous;
}
function selectedFulfillmentSource() {
  return fulfillmentData?.sources.find(source=>source.id===$('#fulfillment-sync-source').value);
}
function updateFulfillmentControls() {
  const unavailable = !fulfillmentData || fulfillmentBusy || fulfillmentLoading;
  const running = Boolean(fulfillmentData?.runtime?.running);
  $('#fulfillment').querySelectorAll('button').forEach(button=>button.disabled=unavailable);
  $('#fulfillment-refresh').disabled = fulfillmentBusy || fulfillmentLoading;
  for (const id of ['source','rule']) $('#fulfillment-'+id+'-fields').disabled = unavailable || running;
  $('#fulfillment-cards-fields').disabled = unavailable || running || !fulfillmentData?.rules.some(rule=>rule.type==='card');
  $('#fulfillment').querySelectorAll('[data-fulfillment-edit]').forEach(button=>button.disabled=unavailable||running);
  $('#fulfillment-start').disabled = unavailable || running || !fulfillmentData?.sources.some(source=>source.enabled&&source.auto_process);
  $('#fulfillment-stop').disabled = unavailable || !running;
  const source=selectedFulfillmentSource();
  for (const id of ['dry-sync','order-dry']) $('#fulfillment-'+id).disabled=unavailable||!source;
  for (const id of ['live-sync','order-live']) $('#fulfillment-'+id).disabled=unavailable||!source?.enabled;
  $('#fulfillment-sync-source').disabled=unavailable||!fulfillmentData?.sources.length;
  $('#fulfillment-order-id').disabled=unavailable;
  $('#fulfillment-job-filter').disabled=unavailable;
  $('#fulfillment-prev-page').disabled=unavailable||fulfillmentJobsPage.offset===0;
  $('#fulfillment-next-page').disabled=unavailable||!fulfillmentJobsPage.has_more;
  $('#fulfillment-tracking-form').querySelectorAll('input').forEach(input=>input.disabled=unavailable);
  $('#fulfillment-running-note').hidden=!running;
}
async function fulfillmentAction(fn) {
  if (fulfillmentBusy || fulfillmentLoading) return;
  fulfillmentBusy=true;updateFulfillmentControls();
  try { await fn(); }
  catch(error) { fulfillmentFeedback(error.safeMessage || '操作未完成，请检查本机工作台和订单服务后重试。','error'); }
  finally { fulfillmentBusy=false;updateFulfillmentControls(); }
}
async function loadFulfillment() {
  if (fulfillmentLoading) return;
  fulfillmentLoading=true;updateFulfillmentControls();
  try {
    const jobPath=offset=>'fulfillment/jobs?status='+encodeURIComponent(fulfillmentJobFilter)+'&offset='+offset+'&limit=50';
    const [data,initialPage]=await Promise.all([api('fulfillment'),api(jobPath(fulfillmentJobOffset))]);
    let page=initialPage;
    if(fulfillmentJobOffset>0&&fulfillmentJobOffset>=page.total){fulfillmentJobOffset=page.total>0?Math.floor((page.total-1)/50)*50:0;page=await api(jobPath(fulfillmentJobOffset));}
    fulfillmentJobsPage=page;fulfillmentJobOffset=page.offset;
    fulfillmentData={rules:data.rules||[],sources:data.sources||[],runtime:data.runtime||{},platforms:data.platforms||[]};
    renderFulfillment();
    if ($('#fulfillment-feedback').textContent==='正在读取订单设置。') fulfillmentFeedback('先接入订单来源，再试运行。试运行不会扣库存或实际发货。');
    return true;
  } catch(error) {
    fulfillmentFeedback(error.safeMessage || '暂时无法读取订单设置，请点“刷新订单与设置”重试。','error');
    return false;
  } finally { fulfillmentLoading=false;updateFulfillmentControls(); }
}
function fulfillmentButton(text, fn, editing=false) {
  const button=fulfillmentElement('button',text,'secondary');button.type='button';button.onclick=fn;
  if (editing) button.dataset.fulfillmentEdit='true';
  return button;
}
function renderFulfillment() {
  const data=fulfillmentData;
  const platforms=data.platforms.map(platform=>({id:platform.id,label:platform.name}));
  for (const selector of ['#fulfillment-source-platform','#fulfillment-rule-platform']) fulfillmentOptions(selector,platforms,'暂无可用平台');
  fulfillmentOptions('#fulfillment-sync-source',data.sources.map(source=>({id:source.id,label:source.name+' · '+fulfillmentPlatform(source.platform)+(source.enabled?'':'（未启用，可试运行）')})),'先添加订单来源');
  fulfillmentOptions('#fulfillment-card-rule',data.rules.filter(rule=>rule.type==='card').map(rule=>({id:rule.id,label:fulfillmentPlatform(rule.platform)+' / '+rule.shop_id+' / '+rule.item_id+(rule.sku_id?' / '+rule.sku_id:'')+' · 可用 '+rule.inventory_available+' 条'})),'先保存一条卡密发货规则');
  $('#fulfillment-platforms').replaceChildren();
  for (const platform of data.platforms) $('#fulfillment-platforms').append(fulfillmentElement('span',platform.name+' · '+(data.sources.some(source=>source.platform===platform.id)?'已配置来源':'待接入')));
  $('#fulfillment-sources').replaceChildren();
  for (const source of data.sources) {
    const row=fulfillmentElement('div','','fulfillment-list-item');
    row.append(fulfillmentElement('strong',source.name));
    row.append(fulfillmentElement('p',fulfillmentPlatform(source.platform)+' / '+source.shop_id+' · '+(source.enabled?'已启用':'未启用')+' · '+(source.auto_process?'允许自动处理':'仅手动核对')));
    const actions=fulfillmentElement('div','','actions');
    actions.append(fulfillmentButton('测试连接',()=>fulfillmentAction(async()=>{
      const result=await api('fulfillment/sources/'+encodeURIComponent(source.id)+'/test','POST',{});
      fulfillmentFeedback(source.name+'：'+(result.ok?'连接测试通过，仅确认接口可用，尚未执行真实发货。':(result.message||'连接测试未通过，请检查订单服务。')),result.ok?'success':'error');
    })),fulfillmentButton('编辑',()=>editFulfillmentSource(source),true));
    row.append(actions);$('#fulfillment-sources').append(row);
  }
  if (!data.sources.length) $('#fulfillment-sources').append(fulfillmentElement('p','还没有订单来源。聊天平台登录不会自动接通订单，请先填写下方信息。','fulfillment-empty'));
  $('#fulfillment-rules').replaceChildren();
  for (const rule of data.rules) {
    const row=fulfillmentElement('div','','fulfillment-list-item');
    row.append(fulfillmentElement('strong',fulfillmentPlatform(rule.platform)+' / '+rule.item_id+(rule.sku_id?' / '+rule.sku_id:'')));
    row.append(fulfillmentElement('p','店铺 '+rule.shop_id+' · '+(deliveryTypes[rule.type]||'待核对')+' · '+(rule.enabled?'规则已启用':'规则已停用')));
    const detail=rule.type==='card'?'可用 '+rule.inventory_available+' / 已导入 '+rule.inventory_total+' 条':rule.type==='link'?(rule.has_delivery_content?'已保存交付正文，不回显':'尚未保存交付正文'):'登记物流后，核验订单再回填';
    row.append(fulfillmentElement('p',detail+' · 最低实付单价 '+rule.min_unit_amount+' '+rule.currency));
    const actions=fulfillmentElement('div','','actions');actions.append(fulfillmentButton('编辑规则',()=>editFulfillmentRule(rule),true));
    if(rule.type==='card') actions.append(fulfillmentButton('导入卡密',()=>{$('#fulfillment-card-rule').value=rule.id;$('#fulfillment-cards-details').open=true;$('#fulfillment-cards').focus();},true));
    row.append(actions);$('#fulfillment-rules').append(row);
  }
  if (!data.rules.length) $('#fulfillment-rules').append(fulfillmentElement('p','还没有发货规则。按商品设置卡密、资料链接或实物物流。','fulfillment-empty'));
  const running=Boolean(data.runtime.running);
  $('#fulfillment-runtime-label').textContent=running?'自动发货运行中':'自动发货未开启';
  let lastSync='尚未同步订单';
  if(data.runtime.last_sync){const date=new Date(data.runtime.last_sync);lastSync=Number.isNaN(date.getTime())?'已有同步记录':'最近同步：'+date.toLocaleString('zh-CN');}
  $('#fulfillment-runtime-detail').textContent=(running?'正在处理已启用且允许自动发货的订单来源。 ':'先试运行，核对无误后再开启。 ')+lastSync+(data.runtime.last_error?' · '+data.runtime.last_error:'');
  renderFulfillmentJobs();updateFulfillmentControls();
}
function resetFulfillmentSource() {
  const form=$('#fulfillment-source-form');form.reset();$('#fulfillment-source-id').value='';form.elements.enabled.checked=false;form.elements.auto_process.checked=false;form.elements.secret.value='';
  $('#fulfillment-source-secret-note').textContent='只保存在本机。保存后不回显。首次接入请填写订单服务提供的密钥。';
  form.elements.secret.placeholder='首次接入请填写密钥';form.elements.secret.required=true;
  $('#fulfillment-source-summary').textContent='添加订单来源';$('#fulfillment-source-details').open=true;
}
function editFulfillmentSource(source) {
  const form=$('#fulfillment-source-form');$('#fulfillment-source-id').value=source.id;
  for(const key of ['name','platform','shop_id','base_url']) form.elements[key].value=source[key]||'';
  form.elements.enabled.checked=Boolean(source.enabled);form.elements.auto_process.checked=Boolean(source.auto_process);form.elements.secret.value='';form.elements.secret.required=!source.has_secret;
  form.elements.secret.placeholder=source.has_secret?'已保存密钥；留空保留，填写可替换':'请填写订单服务密钥';
  $('#fulfillment-source-secret-note').textContent=source.has_secret?'密钥已保存，不回显。更换服务地址时，请重新填写对应密钥。':'尚未保存密钥。';
  $('#fulfillment-source-summary').textContent='编辑订单来源';$('#fulfillment-source-details').open=true;form.elements.name.focus();
}
function updateFulfillmentRuleType() {
  const form=$('#fulfillment-rule-form'),type=form.elements.type.value;
  const saved=fulfillmentData?.rules.find(rule=>rule.id===$('#fulfillment-rule-id').value);
  $('#fulfillment-link-field').hidden=type!=='link';$('#fulfillment-link-content').disabled=type!=='link';
  $('#fulfillment-link-content').required=type==='link'&&!saved?.has_delivery_content;
  $('#fulfillment-link-note').textContent=saved?.has_delivery_content?'已有正文已保存。留空保持不变；填写后会替换，保存后清空输入。':'保存后清空输入，不回显正文。';
  $('#fulfillment-type-note').textContent=type==='card'?'卡密规则保存后，在下方导入库存，每行一条。':type==='physical'?'实物不会自动购买面单或安排快递。订单进入待发货后，填写承运商与物流单号，再核验回填。':'资料链接和说明只发给符合此规则的已付款订单。';
  if(type!=='link') $('#fulfillment-link-content').value='';
}
function resetFulfillmentRule() {
  $('#fulfillment-rule-form').reset();$('#fulfillment-rule-id').value='';$('#fulfillment-link-content').value='';$('#fulfillment-rule-summary').textContent='添加发货规则';$('#fulfillment-rule-details').open=true;updateFulfillmentRuleType();
}
function editFulfillmentRule(rule) {
  const form=$('#fulfillment-rule-form');$('#fulfillment-rule-id').value=rule.id;
  for(const key of ['platform','shop_id','item_id','sku_id','type','min_unit_amount','currency']) form.elements[key].value=rule[key]??'';
  form.elements.enabled.checked=Boolean(rule.enabled);$('#fulfillment-link-content').value='';
  $('#fulfillment-rule-summary').textContent='编辑发货规则';$('#fulfillment-rule-details').open=true;updateFulfillmentRuleType();
}
function deliveryExplanation(code) { return code?(deliveryErrors[code]||'待人工核对，请检查原订单和发货设置'):''; }
function renderFulfillmentResults(data, dryRun) {
  const container=$('#fulfillment-run-results');container.replaceChildren();
  container.append(fulfillmentElement('h4',dryRun?'本次试运行结果 · 未扣库存、未实际交付':'本次实际处理结果'));
  const results=Array.isArray(data.results)?data.results:[data];
  if(!results.length) container.append(fulfillmentElement('p','这次没有取得待处理订单。'));
  for(const result of results) {
    const order=result.reference?.order_id||result.job?.order_id||'';
    const jobs=result.jobs||(result.job?[result.job]:[]);
    const row=fulfillmentElement('div','','fulfillment-result');
    row.append(fulfillmentElement('strong',(order?'订单 '+order+' · ':'')+(deliveryStatuses[result.status]||'请查看订单记录')));
    if(result.error_code) row.append(fulfillmentElement('p',deliveryExplanation(result.error_code)));
    for(const job of jobs) row.append(fulfillmentElement('p',(job.item_id?'商品 '+job.item_id+' · ':'')+(deliveryTypes[job.type]||'待核对')+' · '+(deliveryStatuses[job.status]||'待人工核对')+(job.error_code?' · '+deliveryExplanation(job.error_code):'')));
    container.append(row);
  }
  if(data.has_more) container.append(fulfillmentElement('p','还有后续订单，本次只展示已取得的这一批。'));
}
function renderFulfillmentJobs() {
  const body=$('#fulfillment-jobs');body.replaceChildren();
  const labels={all:'全部',pending:'待发货',unknown:'待核实',sent:'已交付',blocked:'已阻止'};
  for(const option of $('#fulfillment-job-filter').options) option.textContent=labels[option.value]+'（'+(fulfillmentJobsPage.counts[option.value]||0)+'）';
  $('#fulfillment-job-filter').value=fulfillmentJobFilter;
  const pages=Math.max(1,Math.ceil(fulfillmentJobsPage.total/fulfillmentJobsPage.limit));
  $('#fulfillment-page-info').textContent='共 '+fulfillmentJobsPage.total+' 条 · 第 '+(Math.floor(fulfillmentJobsPage.offset/fulfillmentJobsPage.limit)+1)+' / '+pages+' 页';
  for(const job of fulfillmentJobsPage.jobs) {
    const row=document.createElement('tr'),identity=document.createElement('td'),delivery=document.createElement('td'),status=document.createElement('td'),next=document.createElement('td');
    identity.append(fulfillmentElement('strong',job.order_id||'待核对订单'),fulfillmentElement('small',fulfillmentPlatform(job.platform)+' / '+job.shop_id),fulfillmentElement('small','商品 '+job.item_id+(job.sku_id?' / '+job.sku_id:'')));
    delivery.append(fulfillmentElement('span',(deliveryTypes[job.type]||'待核对')+' × '+job.quantity),fulfillmentElement('small',job.amount+' '+job.currency));
    if(job.tracking_number) delivery.append(fulfillmentElement('small',(job.carrier||'')+' '+job.tracking_number));
    const badge=fulfillmentElement('span',deliveryStatuses[job.status]||'待核实','delivery-status');badge.dataset.status=job.status;status.append(badge);
    if(job.error_code) status.append(fulfillmentElement('small',deliveryExplanation(job.error_code)));
    if(job.status==='unknown'||job.status==='sending') next.append(fulfillmentElement('p',job.status==='unknown'?'请到原订单核对发送结果，此处不提供重发。':'处理中，请等待结果；若长时间不变，请人工核对。'));
    else if(job.status==='sent') next.append(fulfillmentElement('p',job.type==='physical'?'物流已回填':'已确认交付，不会重复发放'));
    else if(['awaiting_shipment','blocked','reserved'].includes(job.status)) {
      const actions=fulfillmentElement('div','','actions');
      if(job.type==='physical') actions.append(fulfillmentButton(job.tracking_number?'修改物流':'填写物流',()=>openFulfillmentTracking(job)));
      if(job.type!=='physical'||(job.carrier&&job.tracking_number)) actions.append(fulfillmentButton(job.type==='physical'?'核验并回填':'核验并重试',()=>fulfillmentAction(async()=>{
        if(!confirm(job.type==='physical'?'将重新核验这笔订单，并把登记的物流信息回填到订单服务。继续吗？':'将重新核验这笔订单，并尝试完成原来的交付。请先确认原订单没有已经发货。继续吗？'))return;
        const result=await api('fulfillment/jobs/'+encodeURIComponent(job.id)+'/retry','POST',{dry_run:false});renderFulfillmentResults(result,false);await loadFulfillment();fulfillmentFeedback('核验处理已结束，请查看订单状态。');
      })));
      next.append(actions);
    } else next.append(fulfillmentElement('p','请人工核对原订单'));
    row.append(identity,delivery,status,next);body.append(row);
  }
  if(!fulfillmentJobsPage.jobs.length){const row=document.createElement('tr'),cell=fulfillmentElement('td',fulfillmentJobFilter==='all'?'暂无实际处理记录。先试运行核对订单；试运行结果显示在上方，不会扣库存。':'当前筛选下没有订单记录，可切换其他状态查看。','fulfillment-empty');cell.colSpan=4;row.append(cell);body.append(row);}
}
function openFulfillmentTracking(job) {
  $('#fulfillment-tracking-job').value=job.id;$('#fulfillment-carrier').value=job.carrier||'';$('#fulfillment-tracking-number').value=job.tracking_number||'';
  $('#fulfillment-tracking-order').textContent='订单 '+job.order_id+' · 商品 '+job.item_id;
  $('#fulfillment-tracking-editor').hidden=false;$('#fulfillment-carrier').focus();
}
async function runFulfillment(dryRun, single=false) {
  const source=selectedFulfillmentSource();if(!source)return;
  if(single&&!$('#fulfillment-order-id').value.trim()){fulfillmentFeedback('请填写要核对的订单号。','error');return;}
  if(!dryRun&&!confirm('将实际处理“'+source.name+'”的'+(single?'这笔订单':'本批订单')+'：符合规则的卡密和资料会发送给买家。实物只进入待发货，需单独登记并核验回填物流。确认已核对试运行结果并继续吗？'))return;
  const payload={source_id:source.id,dry_run:dryRun};if(single)payload.order_id=$('#fulfillment-order-id').value.trim();
  const result=await api('fulfillment/'+(single?'process':'sync'),'POST',payload);renderFulfillmentResults(result,dryRun);await loadFulfillment();
  fulfillmentFeedback(dryRun?'试运行完成，没有扣库存、发送或回填。请查看每笔订单的结果。':'本次处理已结束，请以每笔订单的状态为准。');
}
$('#fulfillment-source-form').onsubmit=event=>{event.preventDefault();fulfillmentAction(async()=>{
  const form=event.target,payload={};for(const key of ['name','platform','shop_id','base_url','secret'])payload[key]=form.elements[key].value.trim();
  payload.enabled=form.elements.enabled.checked;payload.auto_process=form.elements.auto_process.checked;if($('#fulfillment-source-id').value)payload.id=$('#fulfillment-source-id').value;
  const result=await api('fulfillment/sources','PUT',payload);form.elements.secret.value='';await loadFulfillment();editFulfillmentSource(result.source);$('#fulfillment-source-details').open=false;fulfillmentFeedback('订单来源已保存。请先测试连接，再试运行核对订单。','success');
});};
$('#fulfillment-source-new').onclick=resetFulfillmentSource;
$('#fulfillment-source-form').addEventListener('input',()=>fulfillmentFeedback('订单来源有未保存的修改。请先保存，再测试已保存的连接。'));
$('#fulfillment-rule-form').onsubmit=event=>{event.preventDefault();fulfillmentAction(async()=>{
  const form=event.target,payload={};for(const key of ['platform','shop_id','item_id','sku_id','type','min_unit_amount','currency'])payload[key]=form.elements[key].value.trim();
  payload.enabled=form.elements.enabled.checked;if($('#fulfillment-rule-id').value)payload.id=$('#fulfillment-rule-id').value;
  if(payload.type==='link')payload.delivery_content=$('#fulfillment-link-content').value.trim();
  const result=await api('fulfillment/rules','PUT',payload);$('#fulfillment-link-content').value='';await loadFulfillment();editFulfillmentRule(result.rule);$('#fulfillment-rule-details').open=false;fulfillmentFeedback('发货规则已保存。卡密商品请继续导入库存，其余商品可以先试运行。','success');
});};
$('#fulfillment-rule-new').onclick=resetFulfillmentRule;
$('#fulfillment-rule-type').onchange=updateFulfillmentRuleType;
$('#fulfillment-cards-form').onsubmit=event=>{event.preventDefault();fulfillmentAction(async()=>{
  const cards=$('#fulfillment-cards').value.split(/\r?\n/).map(card=>card.trim()).filter(Boolean);
  if(!cards.length){fulfillmentFeedback('请至少填写一条卡密。','error');return;}
  const result=await api('fulfillment/cards','POST',{rule_id:$('#fulfillment-card-rule').value,cards});$('#fulfillment-cards').value='';await loadFulfillment();fulfillmentFeedback('已导入 '+result.added+' 条，跳过重复 '+result.duplicates+' 条，目前可用 '+result.available+' 条。','success');
});};
$('#fulfillment-tracking-form').onsubmit=event=>{event.preventDefault();fulfillmentAction(async()=>{
  const id=$('#fulfillment-tracking-job').value;
  await api('fulfillment/jobs/'+encodeURIComponent(id)+'/tracking','POST',{carrier:$('#fulfillment-carrier').value.trim(),tracking_number:$('#fulfillment-tracking-number').value.trim()});
  $('#fulfillment-tracking-form').reset();$('#fulfillment-tracking-editor').hidden=true;await loadFulfillment();fulfillmentFeedback('物流信息已登记，尚未回填。核对后点击这笔订单的“核验并回填”。','success');
});};
$('#fulfillment-tracking-cancel').onclick=()=>{$('#fulfillment-tracking-form').reset();$('#fulfillment-tracking-editor').hidden=true;};
$('#fulfillment-refresh').onclick=async()=>{if(await loadFulfillment())fulfillmentFeedback('订单和发货状态已刷新。');};
$('#fulfillment-sync-source').onchange=()=>{updateFulfillmentControls();$('#fulfillment-run-results').replaceChildren();};
$('#fulfillment-job-filter').onchange=()=>{fulfillmentJobFilter=$('#fulfillment-job-filter').value;fulfillmentJobOffset=0;loadFulfillment();};
$('#fulfillment-prev-page').onclick=()=>{fulfillmentJobOffset=Math.max(0,fulfillmentJobsPage.offset-fulfillmentJobsPage.limit);loadFulfillment();};
$('#fulfillment-next-page').onclick=()=>{if(fulfillmentJobsPage.has_more){fulfillmentJobOffset=fulfillmentJobsPage.offset+fulfillmentJobsPage.limit;loadFulfillment();}};
$('#fulfillment-dry-sync').onclick=()=>fulfillmentAction(()=>runFulfillment(true));
$('#fulfillment-live-sync').onclick=()=>fulfillmentAction(()=>runFulfillment(false));
$('#fulfillment-order-form').onsubmit=event=>{event.preventDefault();fulfillmentAction(()=>runFulfillment(true,true));};
$('#fulfillment-order-live').onclick=()=>{if($('#fulfillment-order-form').reportValidity())fulfillmentAction(()=>runFulfillment(false,true));};
$('#fulfillment-start').onclick=()=>fulfillmentAction(async()=>{
  if(!confirm('开启后，会持续读取已启用且允许自动处理的订单来源，并实际交付符合规则的卡密、资料。实物进入待发货后，仍需单独登记并核验回填物流。确认已核对订单来源、商品和库存，并开启吗？'))return;
  await api('fulfillment/start','POST',{});await loadFulfillment();fulfillmentFeedback('自动发货已开启，请留意订单记录中的待核实和已阻止项。','success');
});
$('#fulfillment-stop').onclick=()=>fulfillmentAction(async()=>{await api('fulfillment/stop','POST',{});await loadFulfillment();fulfillmentFeedback('自动发货已停止。已开始发送的订单请按记录核对。');});
resetFulfillmentSource();updateFulfillmentRuleType();updateFulfillmentControls();
setInterval(()=>{if(!$('#fulfillment').hidden&&!fulfillmentBusy&&!fulfillmentLoading)loadFulfillment();},10000);

document.querySelectorAll('.tab').forEach(button=>button.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===button));document.querySelectorAll('.tab-panel').forEach(x=>x.hidden=x.id!==button.dataset.tab);if(button.dataset.tab==='activity')events().catch(e=>notice(e.message,true));if(button.dataset.tab==='ai-settings')loadAISettings();if(button.dataset.tab==='fulfillment')loadFulfillment();});
load().catch(e=>notice(e.message,true));
setInterval(()=>api('state').then(renderState).catch(()=>{}),5000);
