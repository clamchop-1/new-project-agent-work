// Authenticated browser acceptance using the installed Edge and Node's built-in WebSocket.
// This creates only disposable data under LocalCodingAgent/.local/tests.
import {spawn, execFileSync} from 'node:child_process';
import {createServer} from 'node:net';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import crypto from 'node:crypto';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const py = path.join(root, '.venv', 'Scripts', 'python.exe');
const serverScript = path.join(root, 'tests', 'manual_project_browser_server.py');
const edge = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const state = path.join(root, '.local', 'tests', 'project-browser-' + crypto.randomUUID().replaceAll('-', ''));
await mkdir(state, {recursive: true});
const result = {state_root: state, browser: 'Microsoft Edge headless, isolated user-data directory', checks: {},
  browser_verified: false, live_models: [], console_errors: [], browser_network: []};
let serverProc, edgeProc, ws, cdpSession, pageCommand;

function startServer() {
  return new Promise((resolve, reject) => {
    const child = spawn(py, ['-B', serverScript, '--state-root', state], {cwd: root, stdio: ['pipe', 'pipe', 'pipe']});
    let buffer = '', stderr = '';
    const timer = setTimeout(() => reject(new Error('isolated dashboard startup timed out')), 180000);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      buffer += chunk;
      const lines = buffer.split('\n'); buffer = lines.pop();
      for (const line of lines) {
        try { const row = JSON.parse(line); if (row.ready) { clearTimeout(timer); resolve({child, row}); } }
        catch { /* Ignore non-JSON runtime chatter; never print the bearer token. */ }
      }
    });
    child.stderr.setEncoding('utf8'); child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4000); });
    child.once('exit', code => { clearTimeout(timer); if (code !== 0) reject(new Error('isolated dashboard exited: ' + stderr.slice(-500))); });
    child.once('error', reject);
  });
}
function stopProcess(child) {
  return new Promise(resolve => {
    if (!child || child.exitCode !== null) return resolve();
    const timer = setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 10000);
    child.once('exit', () => { clearTimeout(timer); resolve(); });
    if(child.stdin?.writable) child.stdin.end('STOP\n');
    else child.kill('SIGTERM');
  });
}
async function freePort() {
  const listener = createServer();
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  return port;
}
async function waitHttp(url, timeout = 20000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    try { const response = await fetch(url); if (response.ok) return await response.json(); } catch {}
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error('timed out waiting for local Edge DevTools');
}
async function connectEdge() {
  const port = await freePort();
  const profile = path.join(state, 'edge-profile');
  await mkdir(profile, {recursive: true});
  edgeProc = spawn(edge, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + port,
    '--user-data-dir=' + profile, 'about:blank'], {stdio: ['ignore', 'ignore', 'ignore'], windowsHide: true});
  const version = await waitHttp('http://127.0.0.1:' + port + '/json/version');
  ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, {once: true}); ws.addEventListener('error', reject, {once: true}); });
  let nextId = 0; const pending = new Map();
  const requests=new Map();
  ws.addEventListener('message', event => {
    let msg; try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.method === 'Runtime.exceptionThrown') result.console_errors.push(String(msg.params.exceptionDetails?.text || 'page exception').slice(0, 300));
    if(msg.method==='Network.requestWillBeSent'){
      const row={method:msg.params.request.method,url:msg.params.request.url.split('?')[0]};
      requests.set(msg.params.requestId,row);
      if(row.url.includes('/api/projects')||row.url.includes('/api/knowledge/')) result.browser_network.push(row);
    }
    if(msg.method==='Network.responseReceived'&&requests.has(msg.params.requestId)){
      const row=requests.get(msg.params.requestId);if(row.url.includes('/api/projects')||row.url.includes('/api/knowledge/'))row.status=msg.params.response.status;
    }
    if(msg.method==='Network.loadingFailed'&&requests.has(msg.params.requestId)){
      const row=requests.get(msg.params.requestId);if(row.url.includes('/api/projects')||row.url.includes('/api/knowledge/'))row.error=msg.params.errorText;
    }
    if (msg.id && pending.has(msg.id)) { const p = pending.get(msg.id); pending.delete(msg.id); msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result); }
  });
  const send = (method, params = {}, sessionId = cdpSession) => new Promise((resolve, reject) => {
    const id = ++nextId; pending.set(id, {resolve, reject});
    ws.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error('DevTools command timed out: ' + method)); } }, 15000);
  });
  const target = await send('Target.createTarget', {url: 'about:blank'}, null);
  cdpSession = (await send('Target.attachToTarget', {targetId: target.targetId, flatten: true}, null)).sessionId;
  await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable');
  return {port, send};
}
const nap = ms => new Promise(resolve => setTimeout(resolve, ms));
async function evaluate(send, expression) {
  const response = await send('Runtime.evaluate', {expression: `(async()=>(${expression}))()`, returnByValue: true, awaitPromise: true, userGesture: true});
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text || 'page evaluation failed');
  return response.result?.value;
}
async function waitFor(send, expression, timeout = 20000, description = 'browser condition') {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await evaluate(send, expression)) return; await nap(100); }
  let diagnostic='';
  try { diagnostic=JSON.stringify(await evaluate(send,`(()=>({status:document.querySelector('#projectStatus')?.textContent||'',rows:document.querySelector('#projectRows')?.innerText||'',knowledge:document.querySelector('#knowledgeStatus')?.textContent||''}))()`)); } catch {}
  throw new Error('timed out waiting for ' + description + (diagnostic?'; UI='+diagnostic:''));
}
async function click(send, selector) {
  const ok = await evaluate(send, `(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return false;e.click();return true})()`);
  if (!ok) throw new Error('missing browser control ' + selector);
}
async function setValue(send, selector, value) {
  const ok = await evaluate(send, `(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return false;e.value=${JSON.stringify(value)};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error('missing browser field ' + selector);
}
async function chooseModel(send, key) {
  await setValue(send, '#model', key);
  return evaluate(send, `document.querySelector('#model').selectedOptions[0]?.textContent||''`);
}
async function sendPrompt(send, prompt) {
  await setValue(send, '#prompt', prompt);
  await click(send, '#send');
}
async function assistantText(send) {
  return evaluate(send, `(()=>{const all=[...document.querySelectorAll('.message.assistant')];if(!all.length)return '';const t=all.at(-1).innerText;return t.startsWith('Local assistant')?t.slice('Local assistant'.length).trimStart():t})()`);
}
async function waitComplete(send, timeout = 180000) {
  await waitFor(send, `document.querySelector('#stop').classList.contains('hidden')&&document.querySelector('#send').classList.contains('hidden')===false`, timeout, 'chat generation completion');
  const content=await assistantText(send);
  const saved=await evaluate(send,`await api('/api/chat/conversations/'+conversationId)`);
  const last=saved.messages.at(-1);
  if(last?.role!=='assistant'||!['complete','cancelled'].includes(last.stream_status))
    throw new Error('Browser stream ended without a complete assistant response (status '+String(last?.stream_status||'missing')+').');
  if(!content.trim()) throw new Error('Completed assistant response was not rendered in the active conversation.');
  return content;
}
async function clickProjectAction(send, projectName, label) {
  await click(send, '#projectsButton');
  await waitFor(send, `document.querySelector('#projectsDialog').open`, 10000, 'Projects dialog');
  const expression = `(()=>{const card=[...document.querySelectorAll('#projectRows .knowledge-row')].find(e=>e.querySelector('strong')?.textContent===${JSON.stringify(projectName)});const b=card&&[...card.querySelectorAll('button')].find(e=>e.textContent===${JSON.stringify(label)});if(!b)return false;b.click();return true})()`;
  if (!await evaluate(send, expression)) throw new Error(`missing ${label} action for ${projectName}`);
}
async function registerProject(send, projectName, repoPath) {
  await click(send, '#projectsButton');
  await waitFor(send, `document.querySelector('#projectsDialog').open`, 10000, 'Projects dialog');
  const present = await evaluate(send, `document.querySelector('#projectRows').innerText.includes(${JSON.stringify(projectName)})`);
  if (!present) {
    await setValue(send, '#projectRoot', repoPath);
    await click(send, '#registerProject');
    await waitFor(send, `document.querySelector('#projectRows').innerText.includes(${JSON.stringify(projectName)})`, 30000, projectName + ' registration');
  }
  await click(send, '#projectsDialog form button');
  await waitFor(send, `!document.querySelector('#projectsDialog').open`, 5000, 'Projects dialog close');
}
async function openNewProjectConversation(send, projectId) {
  const previous = await evaluate(send,'conversationId||""');
  await setValue(send, '#activeProject', projectId);
  await click(send, '#newChat');
  await waitFor(send, `conversationId&&conversationId!==${JSON.stringify(previous)}&&document.querySelector('#activeProject').value===${JSON.stringify(projectId)}`, 15000, 'new project chat creation');
  const id=await evaluate(send,'conversationId');
  await waitFor(send, `(await api('/api/chat/conversations/'+${JSON.stringify(id)})).project_id===${JSON.stringify(projectId)}`, 10000, 'persisted project conversation binding');
  return id;
}
async function selectConversation(send, id) {
  if(await evaluate(send,'conversationId||""')===id) return;
  const title = await evaluate(send, `(items.find(x=>x.id===${JSON.stringify(id)})||{}).title||''`);
  const clicked = await evaluate(send, `(()=>{const e=[...document.querySelectorAll('.conversation')].find(x=>x.textContent===${JSON.stringify(title)});if(!e)return false;e.click();return true})()`);
  if (!clicked) throw new Error('conversation not present in browser history');
  await waitFor(send, `conversationId===${JSON.stringify(id)}&&document.querySelector('#chatTitle').textContent.length>0`, 10000, 'conversation switch');
}
async function queryVisible(send, prompt, timeout = 180000) {
  await evaluate(send, `(()=>{window.__streamSamples=[];const target=document.querySelector('#messages');const observer=new MutationObserver(()=>{const a=[...document.querySelectorAll('.message.assistant')].at(-1);if(a){const t=a.innerText.replace(/^Local assistant\\s*/,'');if(t)window.__streamSamples.push({t:performance.now(),n:t.length})}});observer.observe(target,{childList:true,subtree:true,characterData:true});window.__streamObserver=observer})()`);
  const started = Date.now();
  await sendPrompt(send, prompt);
  await waitFor(send, `!document.querySelector('#stop').classList.contains('hidden')`, 5000, 'Stop button / generating state');
  const progressLatency = Date.now() - started;
  await waitFor(send, `(()=>{const a=[...document.querySelectorAll('.message.assistant')].at(-1);return !!a&&a.children.length>1})()`, timeout, 'first visible response text');
  const firstVisibleLatency = Date.now() - started;
  const content = await waitComplete(send, timeout);
  const samples = await evaluate(send, `window.__streamSamples||[]`);
  const increases = samples.map(x=>x.n).filter((n,i,a)=>n>0&&(i===0||n>a.slice(0,i).reduce((m,v)=>Math.max(m,v),0)));
  return {content, progress_latency_ms: progressLatency, first_visible_latency_ms: firstVisibleLatency,
    stream_snapshot_count: samples.length, distinct_progressive_lengths: [...new Set(increases)].length,
    completion_observed: true};
}
async function directReadAcceptance(send, server, projectName, conversationId, relativePath, expectedText) {
  const response=await queryVisible(send,`Read file ${relativePath}`,180000);
  const pattern=new RegExp(projectName.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+':'
    +relativePath.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+' lines (\\d+)-(\\d+)','g');
  const source=await readFile(path.join(projectName==='fixture-alpha'?server.alpha:server.beta,relativePath),'utf8');
  let citation=null, excerpt='';
  for(const match of response.content.matchAll(pattern)){
    const candidate=source.split(/\r?\n/).slice(Number(match[1])-1,Number(match[2])).join('\n');
    if(expectedText.every(term=>candidate.includes(term))){citation=match;excerpt=candidate;break}
  }
  const auditPath=path.join(server.state_root,'app','.local','chat','conversations','capability-audit.jsonl');
  const audit=(await readFile(auditPath,'utf8')).split(/\r?\n/).filter(Boolean).map(JSON.parse);
  const executed=audit.some(row=>row.conversation_id===conversationId
    &&row.capability_id==='project.read'&&row.state==='completed');
  if(!citation||!response.content.includes('Project passages retrieved')||!executed)
    throw new Error(`${server.models.find(x=>x.host_id=== (projectName==='fixture-alpha'?'mac_studio':'windows'))?.host_id||'Model'} natural-language file read lacked a completed trusted action and source-matching citation.`);
  return {path:relativePath,lines:[Number(citation[1]),Number(citation[2])],
    citation_matches_source:true,action_audit:'completed',stream_samples:response.stream_snapshot_count,
    supporting_excerpt:excerpt};
}
async function dialogOpen(send, accept) {
  await send('Page.handleJavaScriptDialog', {accept});
}

try {
  await mkdir(path.dirname(state), {recursive: true});
  ({child: serverProc, row: result.server} = await startServer());
  result.checks.live_models_available = result.server.models.map(x=>x.host_id+':'+x.model_id);
  const {send} = await connectEdge();
  pageCommand=send;
  await send('Page.navigate', {url: result.server.url});
  await waitFor(send, `document.readyState==='complete'&&document.querySelector('#token')`, 20000, 'dashboard login page');
  await setValue(send, '#token', result.server.token);
  await click(send, '#connect');
  await waitFor(send, `document.querySelector('#login').classList.contains('hidden')`, 20000, 'authenticated dashboard');
  result.checks.authenticated = true;
  result.build_id = await evaluate(send, `document.querySelector('#buildId')?.textContent||''`);
  result.checks.current_build_id = Boolean(result.build_id);

  await registerProject(send, 'fixture-alpha', result.server.alpha);
  await clickProjectAction(send, 'fixture-alpha', 'Refresh');
  await waitFor(send, `document.querySelector('#projectStatus').textContent.includes('Indexed')`, 120000, 'alpha indexing');
  const alphaRows = await evaluate(send, `await api('/api/projects')`);
  const alpha = alphaRows.projects.find(x=>x.display_name==='fixture-alpha');
  if(!alpha) throw new Error('alpha project registration missing from authenticated API');
  const modelForNaturalActions = result.server.models.find(x=>x.host_id==='mac_studio'&&x.model_id==='qwen3-coder:30b');
  await chooseModel(send, modelForNaturalActions.model_key);
  const registeredList = await queryVisible(send, 'What projects do I have registered?', 180000);
  if(!registeredList.content.includes('fixture-alpha'))
    throw new Error('Natural-language project listing did not display the registered disposable repository.');
  const openReply = await queryVisible(send, 'Open project fixture-alpha', 180000);
  const openedConversation = await evaluate(send, `await api('/api/chat/conversations/'+conversationId)`);
  if(openedConversation.project_id!==alpha.project_id)
    throw new Error('Natural-language project selection did not bind the active conversation.');
  result.checks.conversational_project_list_and_selection={
    listed:registeredList.content.includes('fixture-alpha'), selected:openedConversation.project_id===alpha.project_id,
    open_action_completed:openReply.completion_observed};
  const alphaConversation = await openNewProjectConversation(send, alpha.project_id);
  const mac = result.server.models.find(x=>x.host_id==='mac_studio'&&x.model_id==='qwen3-coder:30b');
  const macOption = await chooseModel(send, mac.model_key);
  const first = await queryVisible(send,
    'Using only this registered project, explain Aurora Audio Toolkit and its processing order. What does normalize_capture return, and how does run_pipeline use that value? Keep the answer concise and do not guess.', 180000);
  result.live_models.push({host:mac.host_id,model:mac.model_id,selection:macOption,...first});
  if(!/pipeline\.py lines \d+-\d+/.test(first.content)||!first.content.includes('fixture-alpha:'))
    throw new Error('Mac browser response did not display the trusted project citation.');
  const alphaSource = await readFile(path.join(result.server.alpha,'pipeline.py'),'utf8');
  const citeMatch = first.content.match(/fixture-alpha:pipeline\.py lines (\d+)-(\d+)/);
  if(!citeMatch) throw new Error('Mac citation line range not found in displayed answer.');
  const citedText = alphaSource.split(/\r?\n/).slice(Number(citeMatch[1])-1,Number(citeMatch[2])).join('\n');
  if(!citedText.includes('normalize_capture')&&!citedText.includes('run_pipeline'))
    throw new Error('Mac citation lines do not match the cited source content.');
  result.checks.mac_citation_matches_source = {path:'pipeline.py',lines:citeMatch.slice(1).map(Number),excerpt:citedText};
  result.checks.mac_browser_streaming = {progress_latency_ms:first.progress_latency_ms,
    first_visible_latency_ms:first.first_visible_latency_ms,stream_snapshot_count:first.stream_snapshot_count,
    distinct_progressive_lengths:first.distinct_progressive_lengths};
  result.checks.mac_conversational_file_read=await directReadAcceptance(send,result.server,
    'fixture-alpha',alphaConversation,'pipeline.py',['normalize_capture','run_pipeline']);

  const alphaSourcePath=path.join(result.server.alpha,'pipeline.py');
  await writeFile(alphaSourcePath, alphaSource + '\ndef revised_capture_gain(samples):\n    """Apply the updated capture gain."""\n    return [sample * 0.73 for sample in samples]\n', 'utf8');
  execFileSync('git',['-C',result.server.alpha,'add','pipeline.py']);
  execFileSync('git',['-C',result.server.alpha,'commit','-m','Update capture gain fixture']);
  await clickProjectAction(send,'fixture-alpha','Refresh');
  await waitFor(send, `document.querySelector('#projectStatus').textContent.includes('Indexed')`,120000,'updated alpha indexing');
  await selectConversation(send,alphaConversation);
  const updated=await queryVisible(send,'After the project refresh, which routine applies the new capture gain, and what multiplier does it use?',180000);
  result.live_models.push({host:mac.host_id,model:mac.model_id,query:'updated-source',...updated});
  if(!updated.content.includes('revised_capture_gain')||!updated.content.includes('0.73'))
    throw new Error('Refreshed source update was not recognized by the live model.');
  result.checks.refresh_recognizes_update=true;

  await registerProject(send,'fixture-beta',result.server.beta);
  await clickProjectAction(send,'fixture-beta','Refresh');
  await waitFor(send,`document.querySelector('#projectStatus').textContent.includes('Indexed')`,120000,'beta indexing');
  const projects=(await evaluate(send,`await api('/api/projects')`)).projects;
  const beta=projects.find(x=>x.display_name==='fixture-beta');
  const betaConversation=await openNewProjectConversation(send,beta.project_id);
  const windows=result.server.models.find(x=>x.host_id==='windows'&&x.model_id==='qwen3:8b');
  const windowsOption=await chooseModel(send,windows.model_key);
  const betaReply=await queryVisible(send,'Explain the purpose of this photo-indexing package and the label returned by classify_collection.',180000);
  result.live_models.push({host:windows.host_id,model:windows.model_id,selection:windowsOption,...betaReply});
  if(!betaReply.content.includes('fixture-beta:')||betaReply.content.includes('fixture-alpha:'))
    throw new Error('Windows model response did not cite only the selected beta project.');
  const betaCitation=betaReply.content.match(/fixture-beta:pipeline\.py lines (\d+)-(\d+)/);
  if(!betaCitation) throw new Error('Windows answer did not include a source line citation.');
  const betaSource=await readFile(path.join(result.server.beta,'pipeline.py'),'utf8');
  const betaCitedText=betaSource.split(/\r?\n/).slice(Number(betaCitation[1])-1,Number(betaCitation[2])).join('\n');
  if(!betaCitedText.includes('classify_collection')||!betaCitedText.includes('BLUEBIRD_PRIVATE_TAG'))
    throw new Error('Windows citation lines do not match the actual beta source.');
  result.checks.windows_citation_matches_source={path:'pipeline.py',lines:betaCitation.slice(1).map(Number),excerpt:betaCitedText};
  result.checks.windows_project_citation=true;
  result.checks.windows_conversational_file_read=await directReadAcceptance(send,result.server,
    'fixture-beta',betaConversation,'pipeline.py',['classify_collection','BLUEBIRD_PRIVATE_TAG']);
  await selectConversation(send,betaConversation);
  const isolation=await queryVisible(send,'Does this registered project implement normalize_capture? Answer only if this project source supports it.',180000);
  if(isolation.content.includes('fixture-alpha:')) throw new Error('Alpha project citation leaked into beta conversation.');
  result.checks.two_project_isolation=true;

  await setValue(send,'#activeProject','');
  const beforeGeneral=await evaluate(send,'conversationId');
  await click(send,'#newChat');
  await waitFor(send,`conversationId!==${JSON.stringify(beforeGeneral)}&&document.querySelector('#activeProject').value===''`,15000,'general conversation creation');
  const generalId=await evaluate(send,'conversationId');
  await waitFor(send,`(await api('/api/chat/conversations/'+${JSON.stringify(generalId)})).project_id===null`,10000,'unbound general conversation');
  result.checks.general_conversation_isolation=true;
  await chooseModel(send,mac.model_key);
  const generalReply=await queryVisible(send,
    'Repeat the exact private label from the Bluebird Photo Index source. If that source is not part of this conversation, say you do not know.',180000);
  if(generalReply.content.includes('BLUEBIRD_PRIVATE_TAG')||generalReply.content.includes('fixture-beta:'))
    throw new Error('General conversation received or repeated beta-only project context.');
  result.checks.general_chat_has_no_project_context={conversation_id:generalId,
    answer_excerpt:generalReply.content.slice(0,240)};

  // Exercise knowledge library through the authenticated browser using a disposable text attachment.
  const knowledgeFile=path.join(state,'browser-knowledge.txt');
  await writeFile(knowledgeFile,'The browser acceptance fixture states that the violet gateway uses port 7319 for its local test listener.\n','utf8');
  await click(send,'#knowledgeButton');
  await waitFor(send,`document.querySelector('#knowledgeDialog').open`,10000,'Knowledge Library dialog');
  await evaluate(send,`document.querySelector('#knowledgeScope').value='global'`);
  const doc=await send('DOM.getDocument',{});
  const fileNode=await send('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'#knowledgeFile'});
  await send('DOM.setFileInputFiles',{nodeId:fileNode.nodeId,files:[knowledgeFile]});
  await waitFor(send,`document.querySelector('#knowledgeFile').files.length===1`,10000,'selected Knowledge Library file');
  await evaluate(send,`(()=>{const i=document.querySelector('#knowledgeFile');i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  await waitFor(send,`(await api('/api/knowledge/documents')).documents?.some(d=>d.title.includes('browser-knowledge'))`,60000,'knowledge document persistence');
  const knowledgeInventory=await evaluate(send,`await api('/api/knowledge/documents')`);
  const uploadedKnowledge=knowledgeInventory.documents?.find(d=>d.title.includes('browser-knowledge'));
  if(!uploadedKnowledge)
    throw new Error('Authenticated Knowledge Library did not retain the uploaded fixture.');
  result.checks.knowledge_library_document=uploadedKnowledge;
  result.checks.knowledge_upload_ready=true;
  await click(send,'#knowledgeDialog form button');
  await click(send,'#newChat');
  await waitFor(send,`conversationId!==${JSON.stringify(generalId)}`,10000,'knowledge QA chat');
  const knowledgeReply=await queryVisible(send,'What port does the violet gateway use? Answer from my local knowledge library.',180000);
  if(!knowledgeReply.content.includes('7319')||!knowledgeReply.content.includes('Knowledge Library'))
    throw new Error('Knowledge Library did not provide a source-backed browser answer.');
  result.checks.knowledge_library_browser_retrieval=true;
  result.checks.research_mode_ui=await evaluate(send,`!!document.querySelector('#researchMode')&&[...document.querySelector('#researchMode').options].length>=3`);

  // Start a deliberately longer live request, switch to the general chat and back, then stop it.
  await selectConversation(send,betaConversation);
  await chooseModel(send,windows.model_key);
  const stopPrompt='Write a detailed, 1200-word architecture explanation of the selected project and continue until stopped. Begin with a short introduction.';
  await evaluate(send,`(()=>{window.__streamSamples=[];const o=new MutationObserver(()=>{const a=[...document.querySelectorAll('.message.assistant')].at(-1);if(a)window.__streamSamples.push(a.innerText.length)});o.observe(document.querySelector('#messages'),{childList:true,subtree:true,characterData:true});window.__streamObserver=o;return true})()`);
  const longStarted=Date.now(); await sendPrompt(send,stopPrompt);
  await waitFor(send,`!document.querySelector('#stop').classList.contains('hidden')`,5000,'live stop button');
  const longId=await evaluate(send,'conversationId');
  await waitFor(send,`(()=>{const a=[...document.querySelectorAll('.message.assistant')].at(-1);return (a?.querySelector('.live-assistant-text')?.innerText.trim().length||0)>=30})()`,90000,'actual model text before switch');
  const partialBeforeSwitch=await assistantText(send);
  const modelTextBeforeSwitch=await evaluate(send,`[...document.querySelectorAll('.message.assistant')].at(-1)?.querySelector('.live-assistant-text')?.innerText||''`);
  await selectConversation(send,generalId);
  await waitFor(send,`conversationId===${JSON.stringify(generalId)}&&!document.querySelector('#stop').classList.contains('hidden')&&document.querySelector('#status').textContent.includes('another conversation')`,5000,'switch away while background response remains active');
  result.checks.switch_to_other_conversation_during_inference={conversation_selected:generalId,
    status:await evaluate(send,`document.querySelector('#status').textContent`),background_stop_available:true};
  await selectConversation(send,longId);
  await waitFor(send,`conversationId===${JSON.stringify(longId)}&&!document.querySelector('#stop').classList.contains('hidden')`,10000,'return to generating chat');
  const stopVisible=!(await evaluate(send,`document.querySelector('#stop').classList.contains('hidden')`));
  if(!stopVisible) throw new Error('Stop control was unavailable after switching back.');
  await click(send,'#stop');
  await waitFor(send,`document.querySelector('#stop').classList.contains('hidden')`,45000,'live stop completion');
  const stoppedRow=await evaluate(send,`await api('/api/chat/conversations/'+${JSON.stringify(longId)})`);
  result.checks.stop_preserves_partial={clicked:true,partial_before_switch:partialBeforeSwitch.length,
    actual_model_text_before_switch:modelTextBeforeSwitch.length,
    final_status:stoppedRow.messages.at(-1)?.stream_status||null};
  if(modelTextBeforeSwitch.length<30||stoppedRow.messages.at(-1)?.stream_status!=='cancelled')
    throw new Error('Stop did not persist the live partial response as cancelled.');
  result.checks.no_response_misrouting=true;

  // Restart only the isolated dashboard process; Edge remains in its private profile.
  await stopProcess(serverProc); serverProc=null;
  ({child: serverProc, row: result.restarted_server}=await startServer());
  await send('Page.navigate',{url:result.restarted_server.url});
  await waitFor(send,`document.readyState==='complete'&&document.querySelector('#token')`,20000,'restarted dashboard login');
  await setValue(send,'#token',result.restarted_server.token);await click(send,'#connect');
  await waitFor(send,`document.querySelector('#login').classList.contains('hidden')`,20000,'re-authentication after restart');
  const persisted=await evaluate(send,`await api('/api/projects')`);
  const persistedConv=await evaluate(send,`await api('/api/chat/conversations')`);
  result.checks.restart_persistence={projects:persisted.projects.map(p=>p.display_name),
    conversations:persistedConv.conversations.length,
    alpha_conversation_exists:persistedConv.conversations.some(c=>c.id===alphaConversation),
    beta_conversation_exists:persistedConv.conversations.some(c=>c.id===betaConversation)};
  if(!result.checks.restart_persistence.alpha_conversation_exists||!result.checks.restart_persistence.beta_conversation_exists)
    throw new Error('Project conversations did not persist across isolated app restart.');

  await click(send,'#projectsButton');await waitFor(send,`document.querySelector('#projectsDialog').open`,10000,'Projects dialog after restart');
  await waitFor(send,`document.querySelector('#projectRows').innerText.includes('fixture-alpha')&&document.querySelector('#projectRows').innerText.includes('fixture-beta')`,15000,'registered projects reloaded after restart');
  const oldConfirm=await send('Runtime.evaluate',{expression:'window.confirm=()=>true'});
  const removed=await evaluate(send,`(()=>{const card=[...document.querySelectorAll('#projectRows .knowledge-row')].find(e=>e.querySelector('strong')?.textContent==='fixture-alpha');const b=card&&[...card.querySelectorAll('button')].find(x=>x.textContent==='Remove');if(!b)return false;b.click();return true})()`);
  if(!removed) throw new Error('Could not remove alpha registration through authenticated browser UI.');
  await waitFor(send,`!document.querySelector('#projectRows').innerText.includes('fixture-alpha')`,15000,'alpha unregistration');
  const alphaPresent=await readFile(path.join(result.restarted_server.alpha,'pipeline.py'),'utf8');
  if(!alphaPresent.includes('normalize_capture')) throw new Error('Unregister unexpectedly deleted disposable source files.');
  result.checks.unregister_preserves_repository=true;

  const screenshot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
  await writeFile(path.join(state,'browser-final.png'),Buffer.from(screenshot.data,'base64'));
  result.checks.console_errors=result.console_errors;
  if(result.console_errors.length) throw new Error('Browser JavaScript console reported exceptions.');
  result.browser_verified = true;
  result.status='passed';
} catch(error) {
  result.status='failed'; result.error=String(error?.stack||error).slice(0,3000);
  if(pageCommand) try {
    result.browser_diagnostic=await evaluate(pageCommand,`(()=>{const b=document.querySelector('#registerProject'),i=document.querySelector('#projectRoot');return {url:location.href,title:document.querySelector('#chatTitle')?.textContent||'',status:document.querySelector('#status')?.textContent||'',projectStatus:document.querySelector('#projectStatus')?.textContent||'',projectRows:document.querySelector('#projectRows')?.innerText||'',projectInput:i?.value||'',registerButton:b?{disabled:b.disabled,text:b.textContent,handler:b.onclick?.toString().slice(0,500)||'',bounds:(()=>{const r=b.getBoundingClientRect();return [r.x,r.y,r.width,r.height]})()}:null,knowledgeStatus:document.querySelector('#knowledgeStatus')?.textContent||'',stopHidden:document.querySelector('#stop')?.classList.contains('hidden'),sendHidden:document.querySelector('#send')?.classList.contains('hidden'),conversation:typeof conversationId==='undefined'?'':conversationId,latest:[...document.querySelectorAll('.message.assistant')].slice(-2).map(x=>x.innerText.slice(0,500))}})()`);
  } catch {}
} finally {
  for (const key of ['server','restarted_server']) if(result[key]?.token) delete result[key].token;
  if(ws) { try { await new Promise(resolve=>{ws.addEventListener('close',resolve,{once:true});ws.close();setTimeout(resolve,500)}) } catch {} }
  await stopProcess(edgeProc); await stopProcess(serverProc);
  try { result.inference_diagnostics=(await readFile(path.join(state,'inference-diagnostics.jsonl'),'utf8')).trim().split('\n').filter(Boolean).map(x=>JSON.parse(x)); } catch {}
  await writeFile(path.join(state,'browser-result.json'),JSON.stringify(result,null,2)+'\n','utf8');
}
console.log(JSON.stringify(result,null,2));
if(result.status!=='passed') process.exitCode=1;
