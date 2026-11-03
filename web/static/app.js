'use strict';
const $ = (s) => document.querySelector(s);
let session = crypto.randomUUID();
let currentState;
let platformLabels = {};
let catalogVersion = '';
let displayedReplyMode = null;
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
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
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
document.querySelectorAll('.tab').forEach(button=>button.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===button));document.querySelectorAll('.tab-panel').forEach(x=>x.hidden=x.id!==button.dataset.tab);if(button.dataset.tab==='activity')events().catch(e=>notice(e.message,true));});
load().catch(e=>notice(e.message,true));
setInterval(()=>api('state').then(renderState).catch(()=>{}),5000);
