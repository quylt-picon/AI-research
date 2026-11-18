// Mic → 16 kHz 16-bit mono PCM chunks (100 ms), posted to the main thread.
// Downsamples by averaging, which is also a crude low-pass for speech.
class PCMRecorder extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.target = (options.processorOptions && options.processorOptions.targetRate) || 16000;
    this.ratio = sampleRate / this.target;
    this.chunk = Math.round(this.target / 10);
    this.out = new Int16Array(this.chunk);
    this.n = 0;
    this.pos = 0;
    this.sum = 0;
    this.cnt = 0;
    this.sq = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const ch = input[0];
    for (let i = 0; i < ch.length; i++) {
      this.sum += ch[i];
      this.cnt++;
      this.pos += 1;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.sum / this.cnt));
        this.sum = 0;
        this.cnt = 0;
        this.sq += v * v;
        this.out[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (this.n === this.chunk) {
          const level = Math.sqrt(this.sq / this.chunk);
          this.port.postMessage({ pcm: this.out.buffer.slice(0), level });
          this.n = 0;
          this.sq = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("pcm-recorder", PCMRecorder);
