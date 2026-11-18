/* Local PCM transport for Pipecat. Audio is never persisted by this client. */
class NovaPipecat {
    constructor({onPartial, onFailure, approvalToken}) {
        this.onPartial = onPartial;
        this.onFailure = onFailure;
        this.approvalToken = approvalToken;
        this.socket = null;
        this.ready = false;
        this.queue = [];
        this.waiter = null;
        this.speech = new Map();
        this.sequence = 0;
        this.captureToken = null;
        this.epoch = 0;
        this.startRequest = null;
        this.cancelConnect = null;
    }
    static blob(encoded) {
        return new Blob([Uint8Array.from(atob(encoded), c => c.charCodeAt(0))], {type:'audio/wav'});
    }
    async connect(pause) {
        this.close();
        const epoch = this.epoch;
        const aborter = new AbortController();
        this.startRequest = aborter;
        const startupTimer = setTimeout(() => aborter.abort(), 30000);
        try {
            const response = await fetch('/voice/pipecat', {method:'POST', signal:aborter.signal});
            const config = await response.json();
            if (epoch !== this.epoch || aborter.signal.aborted) throw Error('Voice startup cancelled');
            if (!response.ok || !config.available) throw Error(config.error || 'Voice service unavailable');
            const socket = new WebSocket(config.url);
            this.socket = socket;
            await new Promise((resolve, reject) => {
                const fail = message => { clearTimeout(timer); this.cancelConnect = null; reject(Error(message)); };
                const timer = setTimeout(() => fail('Voice connection timed out'), 15000);
                this.cancelConnect = () => fail('Voice startup cancelled');
                socket.onmessage = event => {
                    if (epoch !== this.epoch) return;
                    try {
                        const data = JSON.parse(event.data);
                        if (data.type === 'ready') {
                            clearTimeout(timer); this.cancelConnect = null; this.ready = true;
                            socket.send(JSON.stringify({type:'settings', pause})); resolve();
                        } else this.receive(data);
                    } catch {
                        const ready = this.ready;
                        this.close();
                        if (ready) this.onFailure();
                    }
                };
                socket.onerror = () => { if (!this.ready) fail('Voice connection failed'); };
                socket.onclose = () => {
                    if (epoch !== this.epoch) return;
                    const wasReady = this.ready;
                    fail('Voice connection closed');
                    this.close();
                    if (wasReady) this.onFailure();
                };
            });
        } catch (error) {
            if (epoch === this.epoch) this.close();
            throw error;
        } finally {
            clearTimeout(startupTimer);
            if (this.startRequest === aborter) this.startRequest = null;
        }
    }
    sendAudio(buffer) {
        if (!this.ready) return;
        if (this.socket.bufferedAmount > 128000) {
            this.close(); this.onFailure(); return;
        }
        this.socket.send(buffer);
    }
    receive(data) {
        if (data.type === 'speech_start') this.captureToken = this.approvalToken();
        if (data.type === 'partial') this.onPartial(data.text);
        if (data.type === 'utterance') {
            const blob = NovaPipecat.blob(data.wav);
            blob.novaTranscript = data;
            blob.novaApprovalToken = this.captureToken;
            blob.novaCapturedAt = performance.now();
            if (this.onUtterance) this.onUtterance(blob);
            else if (this.waiter) this.waiter(blob);
            else {
                this.queue.push(blob);
                if (this.queue.length > 4) this.queue.shift();
            }
        }
        if (data.type === 'audio' || data.type === 'audio_error') {
            const finish = this.speech.get(data.id);
            if (finish) finish(data.type === 'audio' ? {blob:NovaPipecat.blob(data.wav), prepMs:data.ms} : null);
        }
        if (data.type === 'error') { this.close(); this.onFailure(); }
    }
    listen(timeoutMs = 30000) {
        if (this.waiter) this.waiter(null);
        let settle;
        const promise = new Promise(resolve => {
            const timer = timeoutMs ? setTimeout(() => finish(null), timeoutMs) : null;
            const finish = value => {
                clearTimeout(timer);
                if (this.waiter === finish) this.waiter = null;
                resolve(value);
            };
            settle = finish;
            while (this.queue.length && performance.now() - this.queue[0].novaCapturedAt > 15000) this.queue.shift();
            if (this.queue.length) finish(this.queue.shift());
            else if (!this.ready) finish(null);
            else this.waiter = finish;
        });
        return {promise, cancel:() => settle(null), manual:() => this.finishTurn()};
    }
    finishTurn() {
        if (this.ready) this.socket.send(JSON.stringify({type:'finish'}));
    }
    synthesize(text, signal) {
        if (!this.ready || signal.aborted) return Promise.resolve(null);
        const id = 'tts-' + (++this.sequence);
        return new Promise(resolve => {
            const finish = value => {
                clearTimeout(timer); signal.removeEventListener('abort', abort);
                this.speech.delete(id); resolve(value);
            };
            const abort = () => {
                if (this.ready) this.socket.send(JSON.stringify({type:'cancel_speech', id}));
                finish(null);
            };
            const timer = setTimeout(abort, 38000);
            this.speech.set(id, finish);
            signal.addEventListener('abort', abort, {once:true});
            this.socket.send(JSON.stringify({type:'speak', id, text}));
        });
    }
    close() {
        this.epoch++;
        if (this.startRequest) { this.startRequest.abort(); this.startRequest = null; }
        if (this.cancelConnect) { this.cancelConnect(); this.cancelConnect = null; }
        this.ready = false;
        if (this.socket) {
            this.socket.onclose = null; this.socket.onmessage = null; this.socket.onerror = null;
            this.socket.close(); this.socket = null;
        }
        if (this.waiter) this.waiter(null);
        for (const finish of [...this.speech.values()]) finish(null);
        this.speech.clear();
        this.queue = [];
    }
}
