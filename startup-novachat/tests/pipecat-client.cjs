const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
function client() {
  const ctx = vm.createContext({Blob, Uint8Array, atob, performance, setTimeout, clearTimeout,
    Promise, Map, JSON, Error});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/pipecat-client.js'),'utf8')+
    '\nthis.Client=NovaPipecat;', ctx);
  let token = 'original';
  const instance = new ctx.Client({onPartial:()=>{},onFailure:()=>{},approvalToken:()=>token});
  instance.ready=true;
  return {instance, changeToken:()=>{token='different'}};
}
test('speech captured while no consumer is ready survives transcription delay', async()=>{
  const {instance}=client();
  instance.receive({type:'speech_start'});
  instance.receive({type:'utterance',wav:'AAAA',text:'check this',words:2,confidence:1});
  const clip=await instance.listen().promise;
  assert.equal(clip.novaTranscript.text,'check this');
});
test('approval retains the action token from speech start, not delivery', async()=>{
  const {instance,changeToken}=client();
  instance.receive({type:'speech_start'});
  changeToken();
  instance.receive({type:'utterance',wav:'AAAA',text:'go ahead'});
  const clip=await instance.listen().promise;
  assert.equal(clip.novaApprovalToken,'original');
});
test('disconnect releases pending listener and audio requests', async()=>{
  const {instance}=client();
  const listener=instance.listen();
  let audioReleased=false;
  instance.speech.set('clip', value=>{audioReleased=value===null});
  instance.close();
  assert.equal(await listener.promise,null);
  assert.equal(audioReleased,true);
  assert.equal(instance.ready,false);
});
test('continuous input queue is bounded', ()=>{
  const {instance}=client();
  for(let i=0;i<30;i++) instance.receive({type:'utterance',wav:'AAAA',text:String(i)});
  assert.equal(instance.queue.length,4);
});
test('audio worklet preserves samples across render blocks', ()=>{
  let Processor;
  const blocks=[];
  const ctx=vm.createContext({Float32Array,
    AudioWorkletProcessor:class {constructor(){this.port={postMessage:b=>blocks.push(new Float32Array(b))}}},
    registerProcessor:(_name,implementation)=>{Processor=implementation}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/voice-capture-worklet.js'),'utf8'),ctx);
  const worklet=new Processor();
  for(let n=0;n<16;n++) worklet.process([[Float32Array.from({length:128},(_,i)=>n*128+i)]]);
  assert.equal(blocks.length,2);
  assert.equal(blocks[0][0],0);
  assert.equal(blocks[0][1023],1023);
  assert.equal(blocks[1][0],1024);
  assert.equal(blocks[1][1023],2047);
});
