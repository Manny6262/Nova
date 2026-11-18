const {test} = require('node:test');
const assert = require('node:assert/strict');
const {NovaVoiceSession} = require('../static/voice-session.js');
const later = () => new Promise(resolve => setImmediate(resolve));
function setup() {
  const calls={turns:[],approvals:[],closed:0,cancelled:0,errors:[]};
  let approval=null, resolveTurn;
  const options={transport:{close(){}},state(){},echo:()=>false,busy:()=>false,
    speaking:()=>false,human(){},barge(){},error:e=>calls.errors.push(e),
    approval:()=>approval,decision:t=>t==='yeah go for it'?true:null,
    approve:async a=>calls.approvals.push(a),stopCommand:t=>t==='stop listening',
    cancelCommand:t=>t==='cancel the task',cancel:()=>calls.cancelled++,
    close:()=>{calls.closed++;session.stop()},accept:async b=>b.novaTranscript,
    turn:async t=>{calls.turns.push(t);await new Promise(r=>resolveTurn=r)},now:()=>100};
  const session=new NovaVoiceSession(options); session.active=true; session.epoch=1;
  return {session,calls,options,approval:a=>approval=a,finish:()=>resolveTurn()};
}
test('input arrives while the current turn is unfinished; no task is cancelled',async()=>{
  const x=setup(); await x.session.accept({verified:true,text:'first request'},null);
  await x.session.accept({verified:true,text:'another thought'},null);
  assert.equal(x.calls.cancelled,0); assert.deepEqual(x.session.queue,['another thought']);
  x.finish(); await later(); assert.deepEqual(x.calls.turns,['first request','another thought']);
  x.finish(); await later(); x.session.stop();
});
test('approval requires verified speech and the exact captured token',async()=>{
  const x=setup(); x.approval({token:'new'});
  await x.session.accept({verified:true,text:'yeah go for it',verified:true},'old');
  await x.session.accept({verified:true,text:'yeah go for it',verified:false},'new');
  assert.equal(x.calls.approvals.length,0);
  await x.session.accept({verified:true,text:'yeah go for it',verified:true},'new');
  assert.deepEqual(x.calls.approvals,[true]);
});
test('expired approval is not interpreted as a new task',async()=>{
  const x=setup(); await x.session.accept({verified:true,text:'yeah go for it',verified:true},'old');
  assert.equal(x.calls.turns.length,0);
});
test('stop listening closes the microphone without cancelling work',async()=>{
  const x=setup(); await x.session.accept({verified:true,text:'stop listening'},null);
  assert.equal(x.calls.closed,1);assert.equal(x.calls.cancelled,0);
});
test('explicit cancellation clears pending input',async()=>{
  const x=setup(); x.session.queue=['old']; await x.session.accept({verified:true,text:'cancel the task'},null);
  assert.equal(x.calls.cancelled,1);assert.equal(x.session.queue.length,0);
});
test('echo and empty recognition never create a turn',async()=>{
  const x=setup();x.options.echo=()=>true;
  await x.session.accept({verified:true,text:'speaker echo'},null);
  x.session.receive({novaTranscript:{text:''}}); await later();
  assert.equal(x.calls.turns.length,0);
});
test('late verification cannot resurrect an ended session',async()=>{
  const x=setup();let release;x.options.accept=()=>new Promise(r=>release=r);
  x.session.receive({});await later();x.session.stop();release({verified:true,text:'late response'});
  await later();assert.equal(x.calls.turns.length,0);
});
test('pending requests are bounded and overflow is visible',async()=>{
  const x=setup();x.options.busy=()=>true;
  for(let i=0;i<5;i++)await x.session.accept({verified:true,text:'task '+i},null);
  assert.equal(x.session.queue.length,3);assert.equal(x.calls.errors.length,2);
});
test('privacy idle timeout closes a session',()=>{
  const x=setup();x.session.started=0;x.session.lastHuman=0;x.session.now=()=>120001;
  x.session.tick();assert.equal(x.calls.closed,1);
});

test('attention is an exact whole phrase, never a question or command prefix',()=>{
  for(const t of ['Hey Nova','hey, nova!','Nova.','Hello Nova?']) assert.equal(NovaVoiceSession.isAttention(t),true,t);
  for(const t of ['Hey Nova open YouTube','Nova, how are you?','approve','hello','supernova','nova delete it','hey nova\nopen files'])
    assert.equal(NovaVoiceSession.isAttention(t),false,t);
});

test('verified attention is local, deduplicated, and does not cancel an active task',async()=>{
  const x=setup();let cues=0,barges=0;x.options.attention=()=>cues++;x.options.barge=()=>barges++;
  x.options.busy=()=>true;
  await x.session.accept({text:'Hey Nova',verified:true},null);
  await x.session.accept({text:'Hey Nova',verified:true},null);
  assert.equal(cues,1);assert.equal(barges,1);assert.equal(x.calls.cancelled,0);
  assert.equal(x.calls.turns.length,0);assert.equal(x.session.queue.length,0);
});

test('attention never shortcuts pending approval, expired approval, or unverified speech',async()=>{
  const x=setup();let cues=0;x.options.attention=()=>cues++;
  await x.session.accept({text:'Nova',verified:false},null);
  await x.session.accept({text:'Nova',verified:true},'expired');
  x.approval({token:'pending'});
  await x.session.accept({text:'Hey Nova',verified:true},null);
  await x.session.accept({text:'Hey Nova',verified:true},'pending');
  assert.equal(cues,0);assert.deepEqual(x.calls.approvals,[]);
});

test('attention followed by a request uses the normal model route',async()=>{
  const x=setup();x.options.attention=()=>{throw Error('must use model')};
  await x.session.accept({text:'Hey Nova, tell me about the moon',verified:true},null);
  assert.deepEqual(x.calls.turns,['Hey Nova, tell me about the moon']);x.finish();await later();x.session.stop();
});
