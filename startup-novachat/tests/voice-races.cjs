const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const clientSource=fs.readFileSync(path.join(__dirname,'../static/pipecat-client.js'),'utf8');
test('closing voice during service startup cannot resurrect the socket',async()=>{
  let release;
  const created=[];
  class Socket {constructor(){created.push(this)} close(){} send(){}}
  const scope=vm.createContext({Blob,Uint8Array,atob,performance,setTimeout,clearTimeout,AbortController,
    fetch:()=>new Promise(r=>{release=r}),WebSocket:Socket});
  vm.runInContext(clientSource+'\nthis.Client=NovaPipecat',scope);
  const client=new scope.Client({onPartial:()=>{},onFailure:()=>{},approvalToken:()=>null});
  const connection=client.connect(.65).then(()=>true,()=>false);
  client.close();
  release({ok:true,json:async()=>({available:true,url:'ws://test'})});
  await new Promise(r=>setTimeout(r,0));
  if(created[0]) created[0].onmessage({data:'{"type":"ready"}'});
  assert.equal(await connection,false);
  assert.equal(client.ready,false);
  client.close();
});
test('replacing a listener releases the previous listener',async()=>{
  const scope=vm.createContext({Blob,Uint8Array,atob,performance,setTimeout,clearTimeout,AbortController});
  vm.runInContext(clientSource+'\nthis.Client=NovaPipecat',scope);
  const client=new scope.Client({onPartial:()=>{},onFailure:()=>{},approvalToken:()=>null});
  client.ready=true;
  const old=client.listen(0);
  const next=client.listen(0);
  assert.equal(await Promise.race([old.promise,new Promise(r=>setTimeout(()=>r('leaked'),50))]),null);
  next.cancel(); client.close();
});
test('cancelled audio pump cannot start playback from a newer reply queue',async()=>{
  const html=fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8');
  const start=html.indexOf('async function playNextSpeak(');
  const src=html.slice(start,html.indexOf('\n}',start)+2);
  let finish;
  const scope=vm.createContext({speakQueue:[{text:'old',generation:1}],ttsOn:true,speaking:false,
    speechGeneration:1,ttsBtn:{classList:{add(){},remove(){}}},prepareSpeech:()=>new Promise(r=>finish=r)});
  vm.runInContext(src,scope);
  const pending=scope.playNextSpeak();
  scope.speechGeneration=2;
  let restarted=0;
  scope.playNextSpeak=()=>restarted++;
  finish(null); await pending;
  assert.equal(restarted,0);
});
