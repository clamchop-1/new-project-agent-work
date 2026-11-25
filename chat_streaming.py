"""Live client-side response handling appended to the authenticated chat page."""

STREAMING_CLIENT_PATCH = r'''
// One active request owns its conversation, placeholder and event sequence.
let activeChatRequest=null;
const priorRenderMessages=renderMessages;
const priorShowActivity=showActivity;
function nearLatest(box){return box.scrollHeight-box.scrollTop-box.clientHeight<100}
function rebuildResearchSourcePanels(rows){
 const assistants=rows.filter(row=>row.role==='assistant'),articles=$('messages').querySelectorAll('.message.assistant');
 for(let i=0;i<Math.min(assistants.length,articles.length);i++){
  const row=assistants[i],article=articles[i];article.querySelectorAll('.research-sources').forEach(node=>node.remove());
  const refs=Array.isArray(row.sources)?row.sources.filter(source=>source&&/^[a-f0-9]{32}$/.test(source.source_id||'')&&/^[a-z0-9][a-z0-9_-]{0,63}$/.test(source.provider_id||'')&&typeof source.url==='string'&&source.url.startsWith('https://')):[];
  if(!refs.length)continue;
  const panel=document.createElement('div');panel.className='research-sources';panel.style.cssText='margin-top:12px;padding:10px;border-left:3px solid var(--accent);font-size:.88rem';
  const heading=document.createElement('strong');heading.textContent='Online sources';panel.append(heading);
  const sourceConversation=conversationId;
  for(const source of refs){
   const item=document.createElement('div');item.style.marginTop='8px';const link=document.createElement('a');link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=(source.cached?'Cached · ':'')+source.title+' · Retrieved '+source.retrieved_at;item.append(link);
   if(source.publication_date){const date=document.createElement('span');date.textContent=' · Published '+source.publication_date;item.append(date)}
   const save=document.createElement('button');save.textContent='Save to Knowledge Library';save.style.cssText='display:block;margin-top:4px';save.onclick=async()=>{try{await api('/api/research/sources/'+source.source_id+'/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conversation_id:sourceConversation})});save.textContent='Saved to Knowledge Library';save.disabled=true}catch(error){$('status').textContent=error.message}};
   item.append(save);panel.append(item);
  }
  article.append(panel);
 }
}
const latestButton=document.createElement('button');
latestButton.textContent='↓ Latest response';latestButton.className='plain hidden';
latestButton.style.cssText='position:absolute;right:24px;bottom:116px;z-index:2;background:var(--bg);border:1px solid var(--line);border-radius:18px;padding:7px 12px';
$('messages').after(latestButton);
latestButton.onclick=()=>{$('messages').scrollTop=$('messages').scrollHeight;latestButton.classList.add('hidden')};
$('messages').addEventListener('scroll',()=>{if(nearLatest($('messages')))latestButton.classList.add('hidden')});
showActivity=text=>{const box=$('messages'),follow=nearLatest(box),top=box.scrollTop;priorShowActivity(text);if(!follow)box.scrollTop=top};
renderMessages=rows=>{
 const box=$('messages'),follow=nearLatest(box),top=box.scrollTop,visible=rows.slice();
 if(activeChatRequest&&activeChatRequest.conversationId===conversationId){
  if(!activeChatRequest.serverRequestId){const generating=visible.find(row=>row.stream_status==='generating'&&row.stream_id);if(generating)activeChatRequest.serverRequestId=generating.stream_id}
  const found=activeChatRequest.serverRequestId&&visible.findIndex(row=>row.stream_id===activeChatRequest.serverRequestId);
  if(Number.isInteger(found)&&found>=0){if(activeChatRequest.assistant.content.length>=visible[found].content.length)visible[found].content=activeChatRequest.assistant.content;activeChatRequest.assistant=visible[found]}
  else if(!(visible.length&&visible[visible.length-1]===activeChatRequest.assistant))visible.push(activeChatRequest.assistant);
 }
 priorRenderMessages(visible);
 rebuildResearchSourcePanels(visible);
 const assistantRows=visible.filter(row=>row.role==='assistant'),assistantArticles=box.querySelectorAll('.message.assistant');
 for(let i=0;i<Math.min(assistantRows.length,assistantArticles.length);i++){
  const row=assistantRows[i],article=assistantArticles[i];
  if(!row.stream_status||row.stream_status==='complete'||(activeChatRequest&&activeChatRequest.conversationId===conversationId&&row.stream_status==='generating'))continue;
  if(article.querySelector('[data-stream-state]'))continue;
  const note=document.createElement('div');note.className='message-label';note.dataset.streamState=row.stream_status;
  note.textContent=row.stream_status==='cancelled'?'Response stopped · partial text saved':row.stream_status==='failed'?'Response failed · partial text saved':row.stream_status==='generating'?'Response is still generating':'Response interrupted · partial text saved';article.append(note);
 }
 if(activeChatRequest&&activeChatRequest.conversationId===conversationId){
  const articles=box.querySelectorAll('.message.assistant');activeChatRequest.article=articles.length?articles[articles.length-1]:null;
 }
 if(!follow){box.scrollTop=top;if(activeChatRequest&&activeChatRequest.conversationId===conversationId)latestButton.classList.remove('hidden')}
};
function paintLiveReply(request){
 if(!request||request.conversationId!==conversationId)return;
 const box=$('messages');let article=request.article;
 if(!article||!article.isConnected){renderMessages(request.rows);article=request.article}
 if(!article)return;
 const follow=nearLatest(box),top=box.scrollTop,label=article.querySelector(':scope > .message-label');
 const sources=article.querySelector(':scope > .research-sources');
 for(const child of [...article.children])if(child!==label&&child!==sources)child.remove();
 const body=document.createElement('div');body.className='live-assistant-text';
 renderMarkdownSafe(body,request.assistant.content);article.insertBefore(body,sources||null);
 const state=request.assistant.stream_status;
 if(state&&state!=='complete'){
  const note=document.createElement('div');note.className='message-label';
  note.textContent=state==='generating'?'Response in progress':state==='cancelled'?'Response stopped · partial text saved':state==='failed'?'Response failed · partial text saved':'Response interrupted · partial text saved';
  article.append(note);
 }
 if(follow)box.scrollTop=box.scrollHeight;else{box.scrollTop=top;latestButton.classList.remove('hidden')}
}
let livePaintPending=false;
function scheduleLivePaint(request){if(livePaintPending)return;livePaintPending=true;requestAnimationFrame(()=>{livePaintPending=false;paintLiveReply(request)})}
function appendImmediatePlaceholder(request,text){
 const box=$('messages'),follow=nearLatest(box);
 if(box.querySelector('.welcome'))box.replaceChildren();
 const user=document.createElement('article');user.className='message user';
 const userLabel=document.createElement('div');userLabel.className='message-label';userLabel.textContent='You';
 const userText=document.createElement('div');userText.textContent=text;user.append(userLabel,userText);
 const assistant=document.createElement('article');assistant.className='message assistant';
 const assistantLabel=document.createElement('div');assistantLabel.className='message-label';assistantLabel.textContent='Local assistant';assistant.append(assistantLabel);
 box.append(user,assistant);request.article=assistant;
 if(follow)box.scrollTop=box.scrollHeight;
}
const previousPageParse=parseStreamEvent;
parseStreamEvent=line=>{
 const event=previousPageParse(line);
 if(event.type==='sources'&&Array.isArray(event.sources)){
  const online=event.sources.length>0&&event.sources.every(source=>source&&typeof source.url==='string'&&typeof source.provider_id==='string'&&typeof source.source_id==='string');
  streamResearchSources=online?event.sources:[];
  streamResearchConversationId=online?(activeChatRequest?activeChatRequest.conversationId:conversationId):'';
 }
 if(event.type==='research_proposal'&&activeChatRequest){
  try{sessionStorage.setItem('researchProposal:'+activeChatRequest.conversationId,JSON.stringify(event))}catch{}
 }
 return event;
};
const oldConversationOpener=openConversation;
const streamWatchers=new Set();
async function watchActiveConversation(id){
 if(streamWatchers.has(id))return;streamWatchers.add(id);let previous='';
 try{while(conversationId===id){
  await new Promise(resolve=>setTimeout(resolve,700));
  const [state,row]=await Promise.all([api('/api/chat/conversations/'+id+'/status'),api('/api/chat/conversations/'+id)]);
  const snapshot=JSON.stringify(row.messages);
  if(conversationId===id&&snapshot!==previous){previous=snapshot;renderMessages(row.messages)}
  if(!state.active){await loadConversations();return}
 }}catch(error){if(conversationId===id)$('status').textContent='Reconnecting to the saved response… '+error.message}
 finally{streamWatchers.delete(id)}
}
openConversation=async id=>{
 try{
  const row=await api('/api/chat/conversations/'+id);conversationId=row.id;$('chatTitle').textContent=row.title;
  renderMessages(row.messages);renderConversations(items);
  const state=await api('/api/chat/conversations/'+id+'/status');
  if(state.active&&(!activeChatRequest||activeChatRequest.conversationId!==id))watchActiveConversation(id);
  else if(state.state==='interrupted')$('status').textContent='The previous dashboard stopped during generation. Any saved partial response is shown above.';
 }catch(error){$('status').textContent='Could not open this conversation: '+error.message}
};
newConversation=async()=>{
 const row=await api('/api/chat/conversations',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
 conversationId=row.id;$('chatTitle').textContent=row.title;renderMessages([]);await loadConversations();$('prompt').focus();return row;
};
$('newChat').onclick=()=>{newConversation().catch(error=>{$('status').textContent=error.message})};
 $('stop').textContent='Stop generating';$('stop').classList.add('hidden');$('stop').disabled=false;
$('stop').onclick=async()=>{
 const request=activeChatRequest;if(!request)return;
 $('stop').disabled=true;$('status').textContent='Stopping generation…';
 try{await api('/api/chat/conversations/'+request.conversationId+'/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})}
 catch(error){$('status').textContent='Could not send the stop request: '+error.message}
};
sendChat=async()=>{
 const text=$('prompt').value.trim();if(!text||sending)return;
 if(!models.length){$('status').textContent='No compatible authorized model is available. Refresh models or check the local assistant connection.';return}
 sending=true;$('send').disabled=true;$('send').classList.add('hidden');$('stop').disabled=false;$('stop').classList.remove('hidden');
 const selected=models.find(model=>model.model_key===$('model').value);
 const request={conversationId:conversationId,serverRequestId:null,lastSequence:0,rows:[],assistant:{role:'assistant',content:'',stream_status:'generating'},article:null,model:selected?selected.model_id:'selected model'};
 activeChatRequest=request;controller=new AbortController();showActivity('Preparing your response with '+request.model+'…');$('status').textContent='Preparing request…';
 const user={role:'user',content:text};request.rows=[user,request.assistant];appendImmediatePlaceholder(request,text);$('prompt').value='';
 let responseError=null;
 try{
  if(!request.conversationId){
   const created=await api('/api/chat/conversations',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
   request.conversationId=created.id;conversationId=created.id;$('chatTitle').textContent=created.title;
  }
  const activeId=request.conversationId,prior=await api('/api/chat/conversations/'+activeId);
  request.rows=[...prior.messages,user,request.assistant];request.afterCount=prior.messages.length+1;
  if(conversationId===activeId){renderMessages(request.rows);paintLiveReply(request)}
  const response=await fetch('/api/chat/conversations/'+activeId+'/messages',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({message:text,model:$('model').value}),signal:controller.signal});
  if(!response.ok){const detail=await response.json().catch(()=>({}));throw Error(detail.error||'The assistant could not respond ('+response.status+').')}
  if(!response.body)throw Error('The dashboard returned no response stream.');
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',receivedDone=false;
  const consume=line=>{
   if(!line.trim())return;let event;
   try{event=parseStreamEvent(line)}catch(error){throw Error('The response stream was interrupted or malformed. '+error.message)}
   if(event.request_id){if(request.serverRequestId&&request.serverRequestId!==event.request_id)throw Error('A response event belonged to another request.');request.serverRequestId=event.request_id}
   if(Number.isInteger(event.sequence)){if(event.sequence<=request.lastSequence)return;request.lastSequence=event.sequence}
   if(event.conversation_id&&event.conversation_id!==activeId)throw Error('A response event belonged to another conversation.');
   if(event.type==='status'){$('status').textContent=event.text;if(conversationId===activeId)showActivity(event.text)}
   else if(event.type==='model'){request.model=event.model_id||request.model;if(conversationId===activeId)showActivity('Generating with '+request.model+'…')}
   else if(event.type==='token'){clearActivity();request.assistant.content+=event.text;scheduleLivePaint(request)}
   else if(event.type==='sources'&&Array.isArray(event.sources)){const online=event.sources.length>0&&event.sources.every(source=>source&&typeof source.url==='string'&&typeof source.provider_id==='string'&&typeof source.source_id==='string');if(online){request.assistant.sources=event.sources;streamResearchSources=event.sources;streamResearchConversationId=activeId;if(conversationId===activeId)renderMessages(request.rows)}}
   else if(event.type==='research_proposal'){try{sessionStorage.setItem('researchProposal:'+activeId,JSON.stringify(event))}catch{}$('status').textContent='Waiting for your research approval.'}
   else if(event.type==='research_summary')$('status').textContent=event.retrieved_sources?'Research sources ready · preparing response':'Research completed without readable sources';
   else if(event.type==='error')responseError=event.message||'The local model could not complete this response.';
   else if(event.type==='done'){if(event.conversation_id&&event.conversation_id!==activeId)throw Error('Completion belonged to another conversation.');receivedDone=true;request.assistant.stream_status=event.cancelled?'cancelled':(responseError?'failed':'complete')}
  };
  while(true){const part=await reader.read();if(part.done)break;buffer+=decoder.decode(part.value,{stream:true});const lines=buffer.split('\n');buffer=lines.pop();for(const line of lines)consume(line)}
  buffer+=decoder.decode();if(buffer.trim())consume(buffer);
  if(!receivedDone)throw Error('The connection ended before the response was complete. Checking the saved conversation…');
  if(responseError)$('status').textContent=responseError;
  else if(request.assistant.stream_status==='cancelled')$('status').textContent='Stopped. Partial response saved.';
 }catch(error){$('status').textContent=error.name==='AbortError'?'Connection closed. Checking the saved response…':error.message;if(request.assistant.content)request.assistant.stream_status='interrupted'}
 finally{
  clearActivity();controller=null;sending=false;$('send').disabled=false;$('send').classList.remove('hidden');$('stop').disabled=false;$('stop').classList.add('hidden');
  try{
   await loadConversations();const state=await api('/api/chat/conversations/'+request.conversationId+'/status'),row=await api('/api/chat/conversations/'+request.conversationId);
   if(conversationId===request.conversationId){$('chatTitle').textContent=row.title;renderMessages(row.messages);const last=row.messages[row.messages.length-1];if(last&&last.role==='assistant'&&last.stream_status==='complete')$('status').textContent=''}
   if(state.active&&conversationId===request.conversationId)watchActiveConversation(request.conversationId);
  }catch(error){if(!$('status').textContent)$('status').textContent='Could not refresh the saved conversation. Reopen it to reconcile the response.'}
  activeChatRequest=null;
 }
};
async function guardedSendChat(){try{await sendChat()}catch(error){clearActivity();controller=null;sending=false;activeChatRequest=null;$('send').disabled=false;$('send').classList.remove('hidden');$('stop').disabled=false;$('stop').classList.add('hidden');$('status').textContent='Could not start this response: '+(error&&error.message?error.message:'unexpected chat error')}}
$('send').onclick=guardedSendChat;$('prompt').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();guardedSendChat()}};
'''
