const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {NovaVoiceSession} = require('../static/voice-session.js');
const html = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
function extract(name) {
  const start = html.search(new RegExp('(?:async )?function ' + name + '\\('));
  const end = html.indexOf('\n}', start) + 2;
  assert.ok(start >= 0 && end > start);
  return html.slice(start, end);
}
function context() {
  return vm.createContext({pendingVoiceApproval:null,voiceTransEl:{},showToast(){},setVoiceState(){}});
}
test('all inline scripts parse',()=>{
  for(const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
});
test('voice streams each complete sentence without waiting for a 170 character batch',()=>{
  const spoken=[];
  const c=vm.createContext({ttsOn:true,voiceActive:true,firstSpoken:false,spokenUpTo:0,speakBuffer:'',SPEAK_CHUNK:170,enqueueSpeak:t=>spoken.push(t)});
  vm.runInContext(extract('speakStreaming'),c);
  c.speakStreaming('Hello there.',false);
  c.speakStreaming('Hello there. I can help.',false);
  assert.equal(spoken.length,2); assert.equal(spoken.join(''),'Hello there. I can help.');
});
test('approval parser accepts recognizer punctuation and surrounding whitespace',()=>{
  const c=vm.createContext({}); vm.runInContext(extract('spokenApproval'),c);
  for(const phrase of ['Approve.',' APPROVE! ','Yes.','Go ahead.','Approve, please.']) assert.equal(c.spokenApproval(phrase),true,phrase);
  for(const phrase of ['Deny.',' No! ','Decline.','Do not.']) assert.equal(c.spokenApproval(phrase),false,phrase);
  for(const phrase of ['Do not approve.','Should I approve?','Approve and delete everything.','', '...']) assert.equal(c.spokenApproval(phrase),null,phrase);
});
test('natural replies stay conservative about conditions and negation',()=>{
  const c=vm.createContext({}); vm.runInContext(extract('spokenApproval'),c);
  for(const phrase of ['Yeah, go for it.','Sure, do it.','Yes go ahead','Sounds good','Absolutely.']) assert.equal(c.spokenApproval(phrase),true,phrase);
  for(const phrase of ["No, don't do that.",'Not now.',"Don't do it.",'Nah.']) assert.equal(c.spokenApproval(phrase),false,phrase);
  for(const phrase of ['Yeah go for it if it is safe','Yes but wait','She said approve','Sure?',"Yeah don't do it"]) assert.equal(c.spokenApproval(phrase),null,phrase);
});
test('delivery failure keeps approval pending and never announces success',async()=>{
  const c=context([]); const pending={info:{token:'test-only'}}; c.pendingVoiceApproval=pending;
  c.fetch=async()=>({ok:false}); c.showToast=()=>{};
  c.markVoiceApprovalDecision=()=>{throw Error('must preserve failed approval')};
  vm.runInContext(extract('submitVoiceApproval'),c);
  assert.equal(await c.submitVoiceApproval(true),false); assert.equal(c.pendingVoiceApproval,pending);
});
test('typed confirmation cannot be replaced by voice approval',async()=>{
  const c=context([]); c.pendingVoiceApproval={info:{token:'test-only',require_type:true}};
  c.fetch=()=>{throw Error('must not submit')}; vm.runInContext(extract('submitVoiceApproval'),c);
  assert.equal(await c.submitVoiceApproval(true),false);
});
test('continuous confident speech bypasses the upload',async()=>{
 const c=vm.createContext({NovaVoiceSession,performance,voiceMetrics:{},looksLikeNovaEcho:()=>false,updateVoiceMetrics(){},fetch(){throw Error('no upload')}});
 vm.runInContext(extract('acceptContinuousSpeech'),c);
 const result=await c.acceptContinuousSpeech({novaTranscript:{text:'hello nova',confidence:.96,words:2}});
 assert.equal(result.text,'hello nova');assert.equal(result.verified,true);
});

test('confident Nova alone can acknowledge attention, not an approval',async()=>{
 const c=vm.createContext({NovaVoiceSession,performance,voiceMetrics:{},looksLikeNovaEcho:()=>false,updateVoiceMetrics(){},
   AbortController,setTimeout,clearTimeout,FormData,fetch:async()=>({ok:true,json:async()=>({speech_verified:false})})});
 vm.runInContext(extract('acceptContinuousSpeech'),c);
 const blob=new Blob(['fixture']);blob.novaTranscript={text:'Nova',confidence:.99,words:1};
 assert.equal((await c.acceptContinuousSpeech(blob)).verified,true);
 blob.novaApprovalToken='pending';
 assert.equal(await c.acceptContinuousSpeech(blob),null);
 delete blob.novaApprovalToken;blob.novaTranscript.confidence=.7;
 assert.equal(await c.acceptContinuousSpeech(blob),null);
 blob.novaTranscript={text:'approve',confidence:1,words:1};
 assert.equal(await c.acceptContinuousSpeech(blob),null);
});
test('old voice mode settings and legacy loop are gone',()=>{
 assert.ok(!html.includes('id="voiceAgentMode"'));assert.ok(!html.includes('function voiceLoop('));
 assert.ok(html.includes('voice_version: voiceActive ? 2'));
});
