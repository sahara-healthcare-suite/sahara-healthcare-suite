/**
 * Sahara Healthcare Suite - Audio Worklet PCM Processor
 * Performs anti-aliased downsampling from browser-native microphone rates
 * (48 kHz / 44.1 kHz) to the strict 16 kHz mono PCM required by the Intron API.
 * Includes a 5-second circular ring buffer to survive temporary WebSocket outages.
 */

class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();

    const sourceSampleRate = Number(globalThis.sampleRate) || 48000;
    this.targetSampleRate = 16000;
    this.filterLength = 64;
    this.filterKernel = this.createLowPassKernel(
      this.filterLength,
      Math.min(0.48, Math.max(0.2, 15000 / sourceSampleRate))
    );

    this.bufferSize = 4096;
    this.buffer = new Float32Array(this.bufferSize);
    this.bufferIndex = 0;
    this.isPaused = false;

    this.ringBufferSize = 80000;
    this.ringBuffer = new Float32Array(this.ringBufferSize);
    this.ringWriteIndex = 0;
    this.ringSampleCount = 0;
    this.sequenceId = 0;
    this.lastAckTimestamp = 0;
    this.packetQueue = [];

    this.port.onmessage = (event) => {
      if (event.data.command === 'PAUSE') {
        this.isPaused = true;
      } else if (event.data.command === 'RESUME') {
        this.isPaused = false;
      } else if (event.data.command === 'FLUSH') {
        this.flushBuffer();
        this.port.postMessage({ eventType: 'flushed' });
      } else if (event.data.command === 'ACK') {
        this.lastAckTimestamp = Number(event.data.ackTimestamp || 0);
        this.replayBufferedPackets();
      }
    };
  }

  createLowPassKernel(length, cutoffNormalized) {
    const kernel = new Float32Array(length);
    let gain = 0;

    const center = (length - 1) / 2;
    for (let tap = 0; tap < length; tap++) {
      const x = tap - center;
      const sinc = Math.abs(x) < 1e-8
        ? 2 * cutoffNormalized
        : Math.sin(2 * Math.PI * cutoffNormalized * x) / (Math.PI * x);
      const window = 0.54 - 0.46 * Math.cos((2 * Math.PI * tap) / (length - 1));
      const value = sinc * window;
      kernel[tap] = value;
      gain += value;
    }

    if (gain !== 0) {
      for (let tap = 0; tap < length; tap++) {
        kernel[tap] /= gain;
      }
    }

    return kernel;
  }

  downsampleTo16k(channelData) {
    const sourceSampleRate = Number(globalThis.sampleRate) || 48000;
    const ratio = sourceSampleRate / this.targetSampleRate;
    const outputLength = Math.max(1, Math.ceil(channelData.length / ratio));
    const resampled = new Float32Array(outputLength);

    for (let outIndex = 0; outIndex < outputLength; outIndex++) {
      const sourcePosition = outIndex * ratio;
      const center = Math.floor(sourcePosition);
      const start = center - (this.filterKernel.length >> 1);
      let sum = 0;

      for (let tap = 0; tap < this.filterKernel.length; tap++) {
        const sourceIndex = start + tap;
        const sample = sourceIndex >= 0 && sourceIndex < channelData.length
          ? channelData[sourceIndex]
          : 0;
        sum += this.filterKernel[tap] * sample;
      }

      resampled[outIndex] = sum;
    }

    return resampled;
  }

  pushToRingBuffer(samples, packetTimestamp) {
    for (let i = 0; i < samples.length; i++) {
      this.ringBuffer[this.ringWriteIndex] = samples[i];
      this.ringWriteIndex = (this.ringWriteIndex + 1) % this.ringBufferSize;
    }

    this.ringSampleCount = Math.min(this.ringBufferSize, this.ringSampleCount + samples.length);

    this.packetQueue.push({
      sequence: ++this.sequenceId,
      timestamp: packetTimestamp,
      samples: samples.slice()
    });

    while (this.packetQueue.length > 100) {
      this.packetQueue.shift();
    }
  }

  replayBufferedPackets() {
    if (!this.packetQueue.length) return;

    const replayPackets = this.packetQueue.filter((packet) => packet.timestamp > this.lastAckTimestamp);
    if (!replayPackets.length) return;

    for (const packet of replayPackets) {
      const pcm16 = new Int16Array(packet.samples.length);
      for (let i = 0; i < packet.samples.length; i++) {
        const sample = Math.max(-1, Math.min(1, packet.samples[i]));
        pcm16[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      }
      this.port.postMessage({
        eventType: 'pcmdata',
        sequence: packet.sequence,
        timestamp: packet.timestamp,
        pcmBuffer: pcm16.buffer
      }, [pcm16.buffer]);
    }
  }

  process(inputs, outputs, parameters) {
    if (this.isPaused) return true;

    const input = inputs[0];
    if (input && input.length > 0) {
      const channelData = input[0];
      const downsampled = this.downsampleTo16k(channelData);
      const packetTimestamp = Date.now();
      this.pushToRingBuffer(downsampled, packetTimestamp);

      for (let i = 0; i < downsampled.length; i++) {
        this.buffer[this.bufferIndex++] = downsampled[i];

        if (this.bufferIndex >= this.bufferSize) {
          this.flushBuffer();
        }
      }
    }
    return true;
  }

  flushBuffer() {
    if (this.bufferIndex === 0) return;

    const pcm16 = new Int16Array(this.bufferIndex);
    for (let i = 0; i < this.bufferIndex; i++) {
      const sample = Math.max(-1, Math.min(1, this.buffer[i]));
      pcm16[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }

    const packetTimestamp = Date.now();
    const packetSamples = new Float32Array(this.bufferIndex);
    for (let i = 0; i < this.bufferIndex; i++) {
      packetSamples[i] = this.buffer[i];
    }

    this.pushToRingBuffer(packetSamples, packetTimestamp);

    this.port.postMessage(
      {
        eventType: 'pcmdata',
        sequence: this.sequenceId,
        timestamp: packetTimestamp,
        pcmBuffer: pcm16.buffer
      },
      [pcm16.buffer]
    );

    this.buffer = new Float32Array(this.bufferSize);
    this.bufferIndex = 0;
  }
}

registerProcessor('pcm-processor', PCMProcessor);
