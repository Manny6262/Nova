/* One continuous voice session. Capture, recognition, task work and playback
   have independent lifecycles. No preferences from the old voice engines. */
class NovaVoiceSession {
    static isAttention(text) {
        // A whole attention phrase only, never a prefix of a question/command.
        return /^(?:(?:hey|hi|hello)[\s,]+)?nova[.!?,]*$/i.test((text || '').trim());
    }
    constructor(options) {
        this.o = options;
        this.active = false;
        this.epoch = 0;
        this.queue = [];
        this.running = false;
        this.accepting = Promise.resolve();
        this.pendingRecognition = 0;
        this.now = options.now || (() => performance.now());
    }
    async start() {
        if (this.active) return;
        this.active = true;
        this.sessionKey = crypto.randomUUID();
        this.lastAttention = null;
        const epoch = ++this.epoch;
        this.started = this.lastHuman = this.now();
        this.o.state('thinking', 'Connecting voice…');
        this.o.transport.onUtterance = blob => this.receive(blob);
        this.o.transport.onPartial = text => {
            if (!this.active) return;
            this.o.partial(text);
            // Recognized words, not amplitude spikes, quiet the speaker.
            // They never cancel a task or authorize anything.
            if (text.trim().split(/\s+/).length >= 3 && !this.o.echo(text)) {
                this.o.duck?.();
                clearTimeout(this.duckTimer);
                this.duckTimer = setTimeout(() => this.o.restore?.(), 1200);
            }
        };
        this.o.transport.onFailure = () => this.fail('Voice connection lost. Microphone closed; your task is still running. Reopen voice to reconnect.');
        try {
            const mic = navigator.mediaDevices.getUserMedia({audio: {
                echoCancellation: {ideal:true}, noiseSuppression: {ideal:true},
                autoGainControl: {ideal:true}, channelCount: {ideal:1}, latency:{ideal:.01}
            }}).then(stream => {
                if (!this.active || epoch !== this.epoch) {
                    stream.getTracks().forEach(track => track.stop());
                    throw Error('Voice startup cancelled');
                }
                this.stream = stream;
                return stream;
            });
            let deadline;
            try {
                await Promise.race([
                    Promise.all([mic, this.o.transport.connect(.45)]),
                    new Promise((_, reject) => { deadline = setTimeout(() => reject(Error('Voice startup timed out')), 30000); })
                ]);
            } finally { clearTimeout(deadline); }
            if (!this.active || epoch !== this.epoch) return;
            const context = this.context = new AudioContext({latencyHint:'interactive'});
            await context.resume();
            await context.audioWorklet.addModule('/static/voice-capture-worklet.js');
            if (!this.active || epoch !== this.epoch) return;
            const source = this.source = context.createMediaStreamSource(this.stream);
            const tap = this.tap = new AudioWorkletNode(context, 'nova-voice-capture');
            const silent = this.silent = context.createGain();
            silent.gain.value = 0;
            tap.port.onmessage = event => {
                if (this.active && epoch === this.epoch)
                    this.o.transport.sendAudio(this.o.pcm(new Float32Array(event.data), context.sampleRate));
            };
            source.connect(tap); tap.connect(silent); silent.connect(context.destination);
            this.stream.getTracks().forEach(track => track.onended = () => {
                if (this.active) this.fail('Microphone disconnected. Reopen voice after reconnecting it.');
            });
            this.timer = setInterval(() => this.tick(), 1000);
            this.o.ready(this.stream.getAudioTracks()[0]?.getSettings?.() || {});
            this.o.state('listening', 'Listening — go ahead');
        } catch (error) {
            if (this.active && epoch === this.epoch) this.fail(error.message || 'Could not start voice');
        }
    }
    receive(blob) {
        const epoch = this.epoch;
        if (this.pendingRecognition >= 3) {
            this.o.error('Speech verification is catching up. Please pause briefly.');
            return;
        }
        this.pendingRecognition++;
        // Capture keeps running while a rare uncertain utterance is verified.
        this.accepting = this.accepting.then(async () => {
            if (!this.active || epoch !== this.epoch) return;
            const candidate = await this.o.accept(blob);
            if (!this.active || epoch !== this.epoch || !candidate?.text) return;
            await this.accept(candidate, blob.novaApprovalToken);
        }).catch(error => {
            if (this.active && epoch === this.epoch) this.o.error(error.message || 'Please say that again');
        }).finally(() => {
            if (epoch === this.epoch) this.pendingRecognition--;
        });
    }
    async accept(candidate, capturedApproval) {
        const text = candidate.text.trim();
        if (!this.active || !text || candidate.verified !== true || this.o.echo(text)) return;
        const approval = this.o.approval();
        if (approval) {
            if (capturedApproval !== approval.token || !candidate.verified) return;
            const decision = this.o.decision(text);
            if (decision !== null) {
                this.lastHuman = this.now();
                await this.o.approve(decision);
                return;
            }
        } else if (capturedApproval) return; // expired approval cannot become a task
        this.lastHuman = this.now();
        this.o.human(text);
        if (this.o.stopCommand(text)) { this.o.close(); return; }
        if (this.o.cancelCommand(text)) {
            this.queue = [];
            this.o.cancel();
            this.o.state('listening', 'Stopped — listening');
            return;
        }
        if (!approval && this.o.attention && NovaVoiceSession.isAttention(text)) {
            // Local acknowledgement cannot grant permission, cancel work, or
            // claim an action succeeded. Ordinary turns still use the model.
            if (!this.lastAttention || this.now() - this.lastAttention > 1800) {
                this.lastAttention = this.now();
                this.o.barge();
                this.o.attention(text);
            }
            return;
        }
        this.o.barge();
        if (this.queue.length >= 3) {
            this.o.error('Three requests are waiting. Please let the current task finish.');
            return;
        }
        this.queue.push(text);
        if (this.running || this.o.busy()) this.o.state('thinking', 'Heard you — queued after the current task');
        this.drain();
    }
    async drain() {
        if (!this.active || this.running || this.o.busy() || !this.queue.length) return;
        this.running = true;
        const epoch = this.epoch;
        const text = this.queue.shift();
        this.o.state('thinking', 'Working — you can keep talking');
        try { await this.o.turn(text); }
        catch (error) { if (this.active && epoch === this.epoch) this.o.error(error.message); }
        finally {
            if (epoch === this.epoch) {
                this.running = false;
                if (this.active) {
                    if (!this.o.speaking()) this.o.state('listening', 'Listening — go ahead');
                    this.drain();
                }
            }
        }
    }
    tick() {
        if (!this.active) return;
        if (this.now()-this.lastHuman > 120000 || this.now()-this.started > 900000) {
            this.o.error('Voice closed for privacy. Microphone off.');
            this.o.close(); return;
        }
        this.drain();
        if (!this.running && !this.o.busy() && !this.o.speaking() && !this.o.approval())
            this.o.state('listening', 'Listening — go ahead');
    }
    fail(message) { this.o.error(message); this.o.close(); }
    stop() {
        this.active = false; this.epoch++;
        clearInterval(this.timer);
        clearTimeout(this.duckTimer); this.o.restore?.();
        this.queue = []; this.running = false; this.accepting = Promise.resolve();
        this.pendingRecognition = 0;
        this.o.transport.onUtterance = null;
        this.o.transport.close();
        if (this.tap) { this.tap.port.onmessage = null; this.tap.port.close(); }
        for (const node of [this.source, this.tap, this.silent]) { try { node?.disconnect(); } catch {} }
        this.stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
        this.context?.close().catch(() => {});
        this.stream = this.context = this.tap = this.source = this.silent = null;
    }
}
if (typeof module !== 'undefined') module.exports = {NovaVoiceSession};
