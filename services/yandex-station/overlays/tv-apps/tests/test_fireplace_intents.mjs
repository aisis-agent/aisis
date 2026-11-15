import assert from 'node:assert/strict';
import { test } from 'node:test';
import { pathToFileURL } from 'node:url';
const api=await import(pathToFileURL(new URL('../src/backend/tv-app-intents.ts',import.meta.url).pathname).href);
const title='Cozy mountain cabin fireplace — 4K';
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
function ready(requestId,overrides={}){
 const now=new Date().toISOString();
 return {state:'playing',attributes:{owned:true,file_matches:true,repeat:'one',progressing:true,
  observed_at:now,launch_started_at:now,request_id:requestId,source_kind:'youtube',
  source_id:'fireplace-video-a',title,...overrides}};
}
function parse(text){return api.parseFireplaceIntent(text)}
test('explicit fireplace imperatives select only the projector script',()=>{
 for(const text of ['открой камин на проекторе','включи камин','запусти камин на проекторе','Алиса, пожалуйста, открой камин на проекторе!','открой на проекторе камин','включи, пожалуйста, камин']){
  assert.deepEqual(parse(text),{scriptEntity:'script.living_room_fireplace',label:'камин'},text);
  assert.equal(api.parseTvAppIntent(text),null);
 }
});
test('questions, negation, physical heaters, music and other targets cannot launch the fireplace',()=>{
 for(const text of ['как открыть камин на проекторе','не включай камин','почему камин не работает','открой камин?','открой камин на телевизоре','открой камин на телике','включи камин на колонке','включи песню камин','включи отопление камина','включи электрический камин','зажги камин','открой камин в студии','выключи камин','открой https://example.test/fireplace','открой камин и выключи телевизор','камин',null,{},'']){
  assert.equal(parse(text),null,JSON.stringify(text));
 }
});
test('each command sends a new request ID and accepts the actual selected title',async()=>{
 const ids=[];
 for(const [selectedTitle,sourceKind] of [[title,'youtube'],['Crackling Oak Fire | 8 hours','youtube'],['Licensed fallback fireplace','local']]){
  const calls=[];let reads=0,proofs=0,requestId;
  const result=await api.executeFireplaceIntent(parse('открой камин на проекторе'),{
   callScript:async(entity,variables)=>{calls.push(entity);requestId=variables?.request_id;assert.match(requestId,uuid);ids.push(requestId)},
   readNative:async()=>({state:++reads===1?'idle':'playing',attributes:{media_title:selectedTitle}}),
   readReadiness:async()=>{proofs++;return ready(requestId,{title:selectedTitle,source_kind:sourceKind})},sleep:async()=>{}
  });
  assert.deepEqual(calls,['script.living_room_fireplace']);assert.equal(result.status,'confirmed');assert.equal(reads,2);assert.equal(proofs,2);
 }
 assert.equal(new Set(ids).size,ids.length,'a later request must not reuse an earlier launch receipt');
});
test('old playback with a newly refreshed observation cannot confirm a new command',async()=>{
 let calls=0,requestId;
 const result=await api.executeFireplaceIntent(parse('включи камин'),{
  callScript:async(_entity,variables)=>{calls++;requestId=variables?.request_id},
  readNative:async()=>({state:'playing',attributes:{media_title:title}}),
  readReadiness:async()=>ready('b153f6bc-f3e7-4e17-a138-ff15a8bcaf98'),sleep:async()=>{}
 });
 assert.equal(calls,1);assert.match(requestId,uuid);assert.equal(result.status,'pending');
});
test('two concurrent commands cannot claim the same launch receipt',async()=>{
 const sent=[];let activeRequest;
 const actions={
  callScript:async(_entity,variables)=>{sent.push(variables?.request_id);activeRequest=variables?.request_id},
  readNative:async()=>({state:'playing',attributes:{media_title:title}}),
  readReadiness:async()=>ready(activeRequest),sleep:async()=>{}
 };
 const results=await Promise.all([api.executeFireplaceIntent(parse('включи камин'),actions),api.executeFireplaceIntent(parse('включи камин'),actions)]);
 assert.equal(sent.length,2);sent.forEach(id=>assert.match(id,uuid));assert.notEqual(sent[0],sent[1]);
 assert.deepEqual(results.map(result=>result.status),['pending','confirmed']);
});
test('power, another title, cached identifiers and absent Kodi state cannot confirm playback',async()=>{
 for(const native of [
  {state:'on',attributes:{media_title:title}},{state:'off',attributes:{media_title:title}},
  {state:'paused',attributes:{media_title:title}},{state:'buffering',attributes:{media_title:title}},
  {state:'playing',attributes:{media_title:'Another unrelated video'}},
  {state:'playing',attributes:{media_content_id:'fireplace-video-a'}},
  {state:'playing',attributes:{app_id:'com.google.android.youtube.tv'}},undefined]){
  let calls=0,reads=0,requestId;
  const result=await api.executeFireplaceIntent(parse('включи камин'),{
   callScript:async(_entity,variables)=>{calls++;requestId=variables?.request_id},
   readNative:async()=>{reads++;return native},readReadiness:async()=>ready(requestId),sleep:async()=>{}
  });
  assert.equal(result.status,'pending');assert.equal(calls,1);assert.ok(reads>0&&reads<=12);
 }
});
test('unowned, foreign, stopped, malformed or nonlooping receipts cannot confirm playback',async()=>{
 const invalid=[
  {owned:false},{file_matches:false},{repeat:'off'},{repeat:'all'},{progressing:false},{progressing:undefined},
  {request_id:undefined},{source_id:undefined},{source_id:''},{source_kind:'unknown'},
  {title:undefined},{title:''},{title:17},{observed_at:'not-a-date'},{launch_started_at:'not-a-date'},
  {launch_started_at:'2000-01-01T00:00:00Z'},{observed_at:'2000-01-01T00:00:00Z'},
  {observed_at:'2099-01-01T00:00:00Z'},{launch_started_at:'2099-01-01T00:00:00Z'},
  {launch_started_at:new Date(Date.now()+1000).toISOString(),observed_at:new Date(Date.now()).toISOString()}
 ];
 for(const overrides of invalid){
  let calls=0,requestId;
  const result=await api.executeFireplaceIntent(parse('включи камин'),{
   callScript:async(_entity,variables)=>{calls++;requestId=variables?.request_id},
   readNative:async()=>({state:'playing',attributes:{media_title:title}}),
   readReadiness:async()=>ready(requestId,overrides),sleep:async()=>{}
  });
  assert.equal(result.status,'pending',JSON.stringify(overrides));assert.equal(calls,1);
 }
});
test('missing, unavailable and stale-phase sensor state remains pending',async()=>{
 for(const state of [undefined,'idle','unavailable','paused']){
  let requestId;
  const result=await api.executeFireplaceIntent(parse('включи камин'),{
   callScript:async(_entity,variables)=>{requestId=variables?.request_id},
   readNative:async()=>({state:'playing',attributes:{media_title:title}}),
   readReadiness:async()=>state===undefined?undefined:{...ready(requestId),state},sleep:async()=>{}
  });
  assert.equal(result.status,'pending');
 }
});
test('an explicit launch rejection is not retried and lost readback stays pending',async()=>{
 let writes=0,reads=0;
 await assert.rejects(()=>api.executeFireplaceIntent(parse('включи камин'),{
  callScript:async()=>{writes++;throw Error('launch rejected')},
  readNative:async()=>{reads++;return undefined},readReadiness:async()=>undefined
 }),/launch rejected/);
 assert.equal(writes,1);assert.equal(reads,0);
 const pending=await api.executeFireplaceIntent(parse('включи камин'),{
  callScript:async()=>{writes++},readNative:async()=>{throw Error('read failed')},readReadiness:async()=>undefined
 });
 assert.equal(writes,2);assert.equal(pending.status,'pending');
});
async function flush(){for(let i=0;i<15;i++)await Promise.resolve()}
test('a stalled readiness read cannot exceed the existing four-second voice budget',async(t)=>{
 t.mock.timers.enable({apis:['Date','setTimeout'],now:2000000000000});
 let result,writes=0,release;
 const pendingProof=new Promise(resolve=>{release=resolve});
 const task=api.executeFireplaceIntent(parse('включи камин'),{
  callScript:async()=>{writes++},
  readNative:async()=>({state:'playing',attributes:{media_title:title}}),
  readReadiness:async()=>await pendingProof
 }).then(value=>{result=value});
 await flush();t.mock.timers.tick(3999);await flush();assert.equal(result,undefined);
 t.mock.timers.tick(1);await flush();
 try{assert.equal(result?.status,'pending');assert.equal(writes,1)}
 finally{release(undefined);await flush();await task}
});

test('same-request starting, failure and uncertain open have distinct outcomes',async()=>{
 for(const [state,error,wanted] of [['starting',null,'starting'],['error','source_unavailable','failed'],['unknown','open_unconfirmed','pending']]){
  let requestId,writes=0;
  const result=await api.executeFireplaceIntent(parse('включи камин'),{
   callScript:async(_entity,variables)=>{writes++;requestId=variables?.request_id},
   readNative:async()=>({state:'playing',attributes:{media_title:title}}),
   readReadiness:async()=>({...ready(requestId,{observed_at:null,owned:false,progressing:false,error}),state}),
   sleep:async()=>{}
  });
  assert.equal(writes,1);assert.equal(result.status,wanted,state);
 }
});
test('starting or failure from a different command cannot describe this launch',async()=>{
 for(const state of ['starting','error']){
  const result=await api.executeFireplaceIntent(parse('включи камин'),{
   callScript:async()=>{},
   readNative:async()=>({state:'playing',attributes:{media_title:title}}),
   readReadiness:async()=>({...ready('b153f6bc-f3e7-4e17-a138-ff15a8bcaf98',{error:state==='error'?'source_unavailable':null}),state}),
   sleep:async()=>{}
  });
  assert.equal(result.status,'pending');
 }
});
