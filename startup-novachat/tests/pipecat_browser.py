"""Dashboard -> real Pipecat -> mocked task -> real Kokoro. No real microphone."""
import base64
import io
import wave
import requests
from playwright.sync_api import sync_playwright


def audio(text):
    response = requests.post('http://127.0.0.1:5000/speak', json={'text':text}, timeout=40)
    response.raise_for_status()
    with wave.open(io.BytesIO(response.content), 'rb') as reader:
        return {'rate':reader.getframerate(),
                'pcm':base64.b64encode(reader.readframes(reader.getnframes())).decode()}


def stream(page, text):
    page.evaluate('''async data => {
      const bytes=Uint8Array.from(atob(data.pcm), c=>c.charCodeAt(0));
      const raw=new Int16Array(bytes.buffer);
      const floats=Float32Array.from(raw, x=>x/32768);
      for(let offset=0;offset<floats.length;offset+=Math.round(data.rate*.032)) {
        pipecatVoice.sendAudio(pcm16Chunk(floats.slice(offset,offset+Math.round(data.rate*.032)),data.rate));
        await new Promise(r=>setTimeout(r,32));
      }
      for(let i=0;i<65 && pipecatVoice.ready;i++) {
        pipecatVoice.sendAudio(new Int16Array(512).buffer);
        await new Promise(r=>setTimeout(r,32));
      }
    }''', audio(text))


with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True,
        args=['--autoplay-policy=no-user-gesture-required'])
    page = browser.new_page()
    errors, chats, confirms = [], [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.add_init_script('''
      // A silent generated MediaStream; never opens a physical microphone.
      Object.defineProperty(navigator.mediaDevices,'getUserMedia',{value:async()=>{
        const context=new AudioContext();
        window.testMicContext=context;
        return context.createMediaStreamDestination().stream;
      }});
    ''')

    def chat(route):
        chats.append(route.request.post_data_json)
        route.fulfill(status=200,content_type='application/x-ndjson',
                      body='{"token":"I am ready to help."}\n{"done":true}\n')

    def confirm(route):
        confirms.append(route.request.post_data_json)
        route.fulfill(status=200,content_type='application/json',body='{"ok":true}')

    page.route('**/chat', chat)
    page.route('**/confirm', confirm)
    page.goto('http://127.0.0.1:5000/', wait_until='networkidle')
    page.evaluate('''async()=>{
      capWake.checked=false;
      await enterVoiceMode();
      window.synthCount=0;
      const original=pipecatVoice.synthesize.bind(pipecatVoice);
      pipecatVoice.synthesize=(...args)=>{window.synthCount++;return original(...args)};
      // Disable only the test stream's PCM callback; inject fixture audio instead.
      novaVoiceSession.tap.port.onmessage=null;
    }''')
    assert page.evaluate('pipecatVoice.ready && voiceActive')
    stream(page, 'Hello Nova, please check the project.')
    page.wait_for_function('window.synthCount > 0', timeout=15000)
    page.wait_for_function('!speaking && !busy', timeout=15000)
    assert len(chats) == 1, chats
    assert 'project' in chats[0]['message'].lower()
    print('PASS: dashboard voice -> Pipecat -> task routing -> Pipecat Kokoro playback')
    stream(page, 'Stop listening.')
    page.wait_for_function('!voiceActive && !pipecatVoice.ready', timeout=10000)
    print('PASS: spoken stop listening closes microphone and Pipecat')

    # Exercise approval through the actual speech verifier, without executing an action.
    page.evaluate('''async()=>{
      await new Promise(r=>setTimeout(r,1200));
      await enterVoiceMode();
      novaVoiceSession.tap.port.onmessage=null;
      voiceConfirm({id:'test-action',token:'test-approval',name:'test_action',summary:'Synthetic test only',require_type:false});
    }''')
    stream(page, 'Yeah, go for it.')
    page.wait_for_function('pendingVoiceApproval === null', timeout=15000)
    assert len(confirms) == 1 and confirms[0]['approved'] is True, confirms
    page.evaluate('exitVoiceMode()')
    print('PASS: natural spoken approval reaches the correct pending action')
    page.evaluate('''async()=>{
      await new Promise(r=>setTimeout(r,1200));
      await enterVoiceMode();
      novaVoiceSession.tap.port.onmessage=null;
      const originalFetch=window.fetch.bind(window);
      window.testAborts=0;
      window.fetch=(url,options)=>{
        if(url!=='/chat') return originalFetch(url,options);
        options.signal.addEventListener('abort',()=>window.testAborts++);
        return Promise.resolve(new Response(new ReadableStream({start(controller){
          window.finishTestTask=()=>controller.close();
          controller.enqueue(new TextEncoder().encode('{"provider_label":"Synthetic pending task"}\\n'));
        }}),{headers:{'Content-Type':'application/x-ndjson'}}));
      };
    }''')
    stream(page, 'Please tell me about the weather.')
    page.wait_for_function('busy && !!window.finishTestTask', timeout=10000)
    page.evaluate('pipecatVoice.socket.close()')
    page.wait_for_function('!pipecatVoice.ready && !voiceActive && busy', timeout=5000)
    page.wait_for_timeout(500)
    assert page.evaluate('window.testAborts') == 0
    page.evaluate('window.finishTestTask()')
    page.wait_for_function('!busy', timeout=5000)
    page.evaluate('exitVoiceMode()')
    print('PASS: connection loss closes microphone without cancelling pending task')
    page.evaluate('''async()=>{
      await enterVoiceMode();
    }''')
    assert page.evaluate('voiceActive && pipecatVoice.ready')
    page.evaluate('exitVoiceMode()')
    assert not errors, errors
    browser.close()
    print('PASS: new session reconnects; no JavaScript runtime errors')
