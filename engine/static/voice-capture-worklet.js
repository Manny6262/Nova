/* Capture outside the UI thread; transfer small PCM blocks without recording. */
class NovaVoiceCapture extends AudioWorkletProcessor {
    constructor() {
        super();
        this.block = new Float32Array(1024);
        this.offset = 0;
    }
    process(inputs) {
        const channel = inputs[0]?.[0];
        if (channel) {
            for (let i = 0; i < channel.length; i++) {
                this.block[this.offset++] = channel[i];
                if (this.offset === this.block.length) {
                    this.port.postMessage(this.block.buffer, [this.block.buffer]);
                    this.block = new Float32Array(1024);
                    this.offset = 0;
                }
            }
        }
        return true;
    }
}
registerProcessor('nova-voice-capture', NovaVoiceCapture);
