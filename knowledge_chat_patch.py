"""Knowledge Library progress UI and large-file streaming upload interaction."""

PATCH = r"""
refreshKnowledge=async function(){
 const scopeQuery=conversationId?'?conversation_id='+encodeURIComponent(conversationId):'';
 try{
  const [documentData,jobData,libraryStatus]=await Promise.all([
   api('/api/knowledge/documents'+scopeQuery),api('/api/knowledge/jobs'+scopeQuery),api('/api/knowledge/status')
  ]);
  const jobs=new Map((jobData.jobs||[]).map(job=>[job.doc_id,job]));
  const documents=new Map((documentData.documents||[]).map(doc=>[doc.doc_id,doc]));
  for(const job of jobData.jobs||[])if(!documents.has(job.doc_id))documents.set(job.doc_id,job);
  const host=$('knowledgeDocuments');host.replaceChildren();
  const needsReindex=(libraryStatus.semantic_states?.requires_reindex||0)+(libraryStatus.semantic_states?.semantic_indexing||0);
  reindexSemantic.hidden=!needsReindex;
  for(const doc of [...documents.values()].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')))){
   const job=jobs.get(doc.doc_id),row=document.createElement('div');row.className='knowledge-row';row.style.alignItems='flex-start';
   const info=document.createElement('div');info.style.flex='1';info.style.minWidth='0';
   const title=document.createElement('strong');title.textContent=doc.title||doc.original_name;info.append(title);
   const state=job?.state||'legacy_indexed';
   const labels={queued:'Queued',extracting:'Extracting',indexing:'Indexing',embedding:'Embedding',ready:'Ready',partially_available:'Partially available',cancelled:'Cancelled',failed:'Failed',legacy_indexed:'Indexed · detailed processing history unavailable'};
   const line=document.createElement('div');line.className='hint';
   const size=Number(doc.byte_size||0),date=doc.created_at?new Date(doc.created_at).toLocaleString():'Upload date unavailable';
   const pages=job?.total_pages??doc.page_count,extracted=job?.extracted_pages;
   const chunks=job?.indexed_chunks??doc.indexed_chunks??0,embedded=job?.embedded_chunks??doc.embedded_chunks??0;
   const detail=[doc.original_name||'',(size/1048576).toFixed(2)+' MiB',date,
    pages?`Pages ${extracted===undefined?(doc.pages_with_chunks??'?'):extracted}/${pages}`:'Pages not applicable',
    `Chunks ${chunks}`,`Embeddings ${embedded}/${chunks}`,labels[state]||state,
    doc.scope==='global'?'Project library':'This conversation',
    chunks?'Search ready':'Not searchable yet'].filter(Boolean).join(' · ');
   line.textContent=detail;info.append(line);
   if(job&&job.processing_percent!==null&&job.processing_percent!==undefined&&job.state!=='ready'){
    const progress=document.createElement('progress');progress.max=100;progress.value=Math.max(0,Math.min(100,job.processing_percent));progress.setAttribute('aria-label','Document page processing progress');progress.style.width='min(100%,360px)';info.append(progress);
    const progressText=document.createElement('div');progressText.className='hint';progressText.textContent=`${job.processed_pages}/${job.total_pages} pages processed · ${job.stage}`;info.append(progressText);
   }else if(job&&['extracting','indexing','embedding','queued'].includes(job.state)){
    const stage=document.createElement('div');stage.className='hint';stage.textContent=`${job.stage} · ${job.processed_pages} pages checkpointed`;info.append(stage);
   }
   const warnings=job?.warnings||[];if(warnings.length||job?.error){const warning=document.createElement('div');warning.className='hint error';warning.textContent=job.error||warnings.map(item=>item.kind==='empty_pages'?`${item.count} pages had no extractable text`:item.kind==='extraction_failures'?`${item.count} page extraction failures`:item.kind==='semantic_index_incomplete'?'Semantic indexing incomplete':item.kind).join(' · ');info.append(warning)}
   row.append(info);
   if(job&&['queued','extracting','indexing','embedding'].includes(job.state)){
    const cancel=document.createElement('button');cancel.textContent='Cancel';cancel.onclick=async()=>{cancel.disabled=true;try{await api('/api/knowledge/jobs/'+job.job_id+'/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refreshKnowledge()}catch(error){$('knowledgeStatus').textContent=error.message;cancel.disabled=false}};row.append(cancel);
   }else if(job&&['cancelled','failed','partially_available'].includes(job.state)){
    const resume=document.createElement('button');resume.textContent='Resume';resume.onclick=async()=>{resume.disabled=true;try{await api('/api/knowledge/jobs/'+job.job_id+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refreshKnowledge()}catch(error){$('knowledgeStatus').textContent=error.message;resume.disabled=false}};row.append(resume);
   }else if(!job||doc.status==='current'){
    const remove=document.createElement('button');remove.textContent='Remove';remove.onclick=async()=>{try{await api('/api/knowledge/documents/'+doc.doc_id+(conversationId?'?conversation_id='+encodeURIComponent(conversationId):''),{method:'DELETE'});$('knowledgeStatus').textContent='Document and indexed passages removed.';await refreshKnowledge()}catch(error){$('knowledgeStatus').textContent=error.message}};row.append(remove);
   }
   host.append(row);
  }
  if(!host.childElementCount){const empty=document.createElement('p');empty.className='hint';empty.textContent='No documents in this scope yet.';host.append(empty)}
  if(libraryStatus.embedding_model)$('knowledgeStatus').textContent='Project library · '+(libraryStatus.document_count||0)+' documents · local semantic and keyword search available.';
  else $('knowledgeStatus').textContent='Project library · '+(libraryStatus.document_count||0)+' documents · keyword search available; local embeddings unavailable.';
 }catch(error){$('knowledgeStatus').textContent=error.message}
};
if($('knowledgeDrop')?.firstChild)$('knowledgeDrop').firstChild.textContent='Drop a TXT, Markdown, or text-based PDF · up to 512 MiB and 10,000 PDF pages\n';
function uploadKnowledge(file){
 if(!file)return;
 const scope=$('knowledgeScope').value;
 if(scope==='conversation'&&!conversationId){$('knowledgeStatus').textContent='Create or select a conversation first.';return}
 const query=new URLSearchParams({filename:file.name,scope});
 if(scope==='conversation')query.set('conversation_id',conversationId);
 const xhr=new XMLHttpRequest();xhr.open('POST','/api/knowledge/upload?'+query.toString());
 xhr.setRequestHeader('Authorization','Bearer '+token);xhr.setRequestHeader('Content-Type','application/octet-stream');
 $('knowledgeStatus').textContent='Uploading '+file.name+'…';
 xhr.upload.onprogress=event=>{if(event.lengthComputable)$('knowledgeStatus').textContent=`Uploading ${file.name} · ${Math.floor(100*event.loaded/event.total)}%`};
 xhr.onerror=()=>{$('knowledgeStatus').textContent='Upload connection failed. The library was not reported ready.'};
 xhr.onload=async()=>{let result={};try{result=JSON.parse(xhr.responseText)}catch{}if(xhr.status<200||xhr.status>=300){$('knowledgeStatus').textContent=result.error||`Upload failed (${xhr.status}).`;return}
  $('knowledgeFile').value='';$('knowledgeStatus').textContent=result.duplicate?'This exact document is already in the project library.':`Upload saved. ${result.title} is queued for extraction and indexing.`;await refreshKnowledge();
 };
 xhr.send(file);
}
const knowledgePoll=setInterval(()=>{if($('knowledgeDialog')?.open)refreshKnowledge()},1800);
"""
