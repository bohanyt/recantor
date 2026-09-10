class RecantorRealtimePcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frameSamples = Math.max(1, Math.round(sampleRate * 0.02));
    this.pending = new Float32Array(this.frameSamples);
    this.pendingLength = 0;
    this.sampleOffset = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0 || input[0].length === 0) return true;

    const frameLength = input[0].length;
    for (let index = 0; index < frameLength; index += 1) {
      let sample = 0;
      for (let channel = 0; channel < input.length; channel += 1) {
        sample += input[channel][index] || 0;
      }
      sample /= input.length;
      this.pending[this.pendingLength] = sample;
      this.pendingLength += 1;

      if (this.pendingLength === this.frameSamples) {
        const samples = this.pending;
        const sampleOffset = this.sampleOffset;
        this.sampleOffset += samples.length;
        this.pending = new Float32Array(this.frameSamples);
        this.pendingLength = 0;
        this.port.postMessage({ sampleOffset, samples }, [samples.buffer]);
      }
    }
    return true;
  }
}

registerProcessor('recantor-realtime-pcm', RecantorRealtimePcmProcessor);
