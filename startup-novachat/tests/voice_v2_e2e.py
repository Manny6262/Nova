"""Real synthetic speech -> live recognizer -> cloud -> Kokoro -> browser playback.
No physical microphone or computer actions. Consumes a short existing cloud turn.
"""

import base64
import argparse
import io
import json
import wave
import requests
from playwright.sync_api import sync_playwright

base = "http://127.0.0.1:5000"
parser = argparse.ArgumentParser()
parser.add_argument('--phrase', default='Hello Nova, how are you today?')
parser.add_argument('--turns', type=int, default=2)
parser.add_argument('--attention', action='store_true')
args = parser.parse_args()
http = requests.Session()
http.trust_env = False
sid = http.post(base + "/sessions", json={}).json()["id"]
try:
    response = http.post(
        base + "/speak", json={"text": args.phrase}, timeout=40
    )
    response.raise_for_status()
    with wave.open(io.BytesIO(response.content), "rb") as wav:
        audio = {
            "rate": wav.getframerate(),
            "pcm": base64.b64encode(wav.readframes(wav.getnframes())).decode(),
        }
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge",
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        page = browser.new_page()
        chats = []
        page.on('request', lambda request: chats.append(request.url) if request.url.endswith('/chat') else None)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""Object.defineProperty(navigator.mediaDevices,'getUserMedia',{value:async()=>{
          const context=new AudioContext();return context.createMediaStreamDestination().stream;
        }});""")
        page.goto(base, wait_until="networkidle")
        page.evaluate(
            """async sid=>{
          activeSessionId=sid;brainMode='codex';screenOn=false;capWake.checked=false;
          capWrite.checked=false;capExec.checked=false;capWeb.checked=false;capControl.checked=true;
          await enterVoiceMode();novaVoiceSession.tap.port.onmessage=null;
        }""",
            sid,
        )
        assert page.evaluate("voiceActive && pipecatVoice.ready")
        page.evaluate("""()=>{
          window.testTrace=[];
          const receive=pipecatVoice.receive.bind(pipecatVoice);
          pipecatVoice.receive=data=>{
            if(['speech_start','partial','utterance','audio','audio_error'].includes(data.type))
              testTrace.push({at:performance.now(),type:data.type,text:data.text,confidence:data.confidence,words:data.words});
            receive(data);
          };
          const accept=novaVoiceSession.o.accept;
          novaVoiceSession.o.accept=async blob=>{
            const result=await accept(blob);testTrace.push({at:performance.now(),type:'accepted',result});return result;
          };
        }""")
        for turn_number in range(1, args.turns+1):
            page.evaluate("voiceMetrics.spokeAt=0")
            page.evaluate(
                """async data=>{
              const bytes=Uint8Array.from(atob(data.pcm),c=>c.charCodeAt(0));
              const raw=new Int16Array(bytes.buffer),floats=Float32Array.from(raw,x=>x/32768);
              const step=Math.round(data.rate*.032);
              for(let i=0;i<floats.length;i+=step){
                pipecatVoice.sendAudio(pcm16Chunk(floats.slice(i,i+step),data.rate));
                await new Promise(r=>setTimeout(r,32));
              }
              window.testSpeechEnded=performance.now();
              for(let i=0;i<65;i++){
                pipecatVoice.sendAudio(new Int16Array(512).buffer);
                await new Promise(r=>setTimeout(r,32));
              }
            }""",
                audio,
            )
            try:
                page.wait_for_function("voiceMetrics.spokeAt > 0", timeout=20000)
            except Exception:
                print(json.dumps(page.evaluate("""()=>({trace:testTrace,metrics:voiceMetrics,
                  state:voiceStateEl.textContent,transcript:voiceTransEl.textContent,
                  busy,active:voiceActive,reply:document.getElementById('voiceReply').textContent})""")), flush=True)
                raise
            metrics = page.evaluate("""()=>({provider:voiceMetrics.provider,
              live_transcript_ms:voiceMetrics.stt,model_first_text_ms:Math.round(voiceMetrics.ai),
              first_text_to_audio_ms:Math.round(voiceMetrics.tts),
              end_of_fixture_to_audio_ms:Math.round(voiceMetrics.spokeAt-window.testSpeechEnded)})""")
            assert metrics["provider"] == ('Local attention' if args.attention else 'Codex agent'), metrics
            if args.attention:
                assert len(chats) == 0, chats
                assert 0 < metrics['end_of_fixture_to_audio_ms'] < 1800, metrics
            page.wait_for_function("!busy && !speaking", timeout=30000)
            assert not errors, errors
            assert page.locator("#voiceReply").inner_text().strip()
            print(json.dumps(dict(metrics, turn=turn_number)), flush=True)
        page.evaluate("exitVoiceMode()")
        assert page.evaluate("!novaVoiceSession.stream && !pipecatVoice.ready")
        browser.close()
finally:
    http.delete(base + f"/sessions/{sid}").raise_for_status()
