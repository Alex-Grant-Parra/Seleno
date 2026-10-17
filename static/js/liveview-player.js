// Live view player: watches a telescope over the live-view socket and draws it on a canvas.
// H.264 is decoded in the browser with WebCodecs; JPEG frames (the poster before the
// first keyframe, and telescopes that only send JPEG) are drawn directly.
(function () {
    'use strict';

    const HEADER_BYTES = 24;
    const KIND_JPEG = 1;
    const KIND_H264 = 2;
    const FLAG_KEYFRAME = 0x01;
    const MAX_DECODE_QUEUE = 10;     // beyond this, skip to the next keyframe to keep the delay low
    const PROBE_CODEC = 'avc1.42E01F';
    const RECONNECT_MS = 3000;
    const FRAME_EVENT_MS = 500;      // how often to tell listeners (the histogram) about a new frame

    let h264Support = null;

    function canDecodeH264() {
        if (h264Support === null) {
            h264Support = (async () => {
                if (typeof window.VideoDecoder !== 'function') return false;
                try {
                    const support = await window.VideoDecoder.isConfigSupported({ codec: PROBE_CODEC });
                    return !!(support && support.supported);
                } catch (error) {
                    return false;
                }
            })();
        }
        return h264Support;
    }

    class LiveViewPlayer {
        constructor(canvas, options = {}) {
            this.canvas = canvas;
            this.ctx = canvas.getContext('2d');
            this.onState = options.onState || (() => {});
            this.telescopeId = null;
            this.wanted = false;
            this.ws = null;
            this.decoder = null;
            this.stream = null;
            this.needKeyframe = true;
            this.playingH264 = false;
            this.reconnectTimer = null;
            this.lastFrameEvent = 0;
            this.drawnAnything = false;
            this.generation = 0;
        }

        async start(telescopeId) {
            this.stop();
            this.telescopeId = telescopeId;
            this.wanted = true;
            await this._connect(++this.generation);
        }

        stop() {
            this.wanted = false;
            this.generation++;
            if (this.reconnectTimer) {
                clearTimeout(this.reconnectTimer);
                this.reconnectTimer = null;
            }
            if (this.ws) {
                const ws = this.ws;
                this.ws = null;
                try { ws.close(); } catch (error) { /* already closed */ }
            }
            this._closeDecoder();
            this.stream = null;
            this.playingH264 = false;
        }

        get isRunning() {
            return this.wanted;
        }

        async _connect(generation) {
            if (!this.wanted || generation !== this.generation) return;
            this.onState('connecting');
            let data;
            try {
                const resp = await fetch(`/interface/liveview_ticket?telescope_id=${encodeURIComponent(this.telescopeId)}`, {
                    credentials: 'same-origin',
                    cache: 'no-store'
                });
                data = await resp.json();
                if (!resp.ok || data.status !== 'success') {
                    this.onState('error', data.message || 'Could not open live view');
                    return;
                }
            } catch (error) {
                this.onState('error', 'Could not reach the server');
                this._scheduleReconnect(generation);
                return;
            }
            if (!this.wanted || generation !== this.generation) return;

            const h264 = await canDecodeH264();
            const ws = new WebSocket(data.url);
            ws.binaryType = 'arraybuffer';
            this.ws = ws;
            this.h264 = h264;

            ws.onopen = () => {
                ws.send(JSON.stringify({ type: 'hello', decode: h264 ? ['h264', 'jpeg'] : ['jpeg'] }));
            };
            ws.onmessage = (event) => {
                if (ws !== this.ws) return;
                if (typeof event.data === 'string') {
                    let message;
                    try { message = JSON.parse(event.data); } catch (error) { return; }
                    this._onControl(message);
                } else {
                    this._onFrame(event.data);
                }
            };
            ws.onclose = (event) => {
                if (ws !== this.ws) return;
                this.ws = null;
                this._closeDecoder();
                if (event.code === 4003 || event.code === 4001) {
                    this.onState('error', event.reason || 'Live view refused');
                }
                this._scheduleReconnect(generation);
            };
        }

        _scheduleReconnect(generation) {
            if (!this.wanted || generation !== this.generation) return;
            // A fresh ticket each time: they last a minute
            this.reconnectTimer = setTimeout(() => this._connect(generation), RECONNECT_MS);
        }

        _onControl(message) {
            if (message.type === 'telescope') {
                this.onState(message.connected ? 'waiting' : 'offline');
                if (!message.connected) {
                    this._closeDecoder();
                    this.stream = null;
                    this.playingH264 = false;
                }
            } else if (message.type === 'stream') {
                // A new stream: the encoder restarted (quality, focus, zoom, codec, reconnect)
                this.stream = message;
                this._closeDecoder();
                this.playingH264 = false;
                if (message.codec && message.codec !== 'jpeg' && this.h264) {
                    this._configureDecoder();
                }
            }
        }

        _configureDecoder() {
            const stream = this.stream;
            try {
                this.decoder = new window.VideoDecoder({
                    output: (frame) => this._drawVideoFrame(frame),
                    error: (error) => this._onDecoderError(error)
                });
                // No description: the decoder reads Annex B (start codes, SPS/PPS in band)
                this.decoder.configure({
                    codec: stream.codec,
                    codedWidth: stream.width,
                    codedHeight: stream.height,
                    optimizeForLatency: true
                });
                this.needKeyframe = true;
            } catch (error) {
                console.warn('Live view: could not configure the H.264 decoder', error);
                this.decoder = null;
            }
        }

        _closeDecoder() {
            if (this.decoder) {
                try {
                    if (this.decoder.state !== 'closed') this.decoder.close();
                } catch (error) { /* ignore */ }
                this.decoder = null;
            }
            this.needKeyframe = true;
        }

        _onDecoderError(error) {
            console.warn('Live view decoder error', error);
            // Reset, ask for a keyframe and wait for it
            this._closeDecoder();
            this.playingH264 = false;
            if (this.stream && this.stream.codec !== 'jpeg' && this.h264) {
                this._configureDecoder();
            }
            this._send({ type: 'keyframe' });
        }

        _send(message) {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(JSON.stringify(message));
            }
        }

        _onFrame(buffer) {
            if (buffer.byteLength < HEADER_BYTES) return;
            const view = new DataView(buffer);
            const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
            const length = view.getUint32(20);
            if (magic !== 'SLV2' || length !== buffer.byteLength - HEADER_BYTES) return;

            const kind = view.getUint8(4);
            const keyframe = (view.getUint8(5) & FLAG_KEYFRAME) !== 0;
            const timestamp = Number(view.getBigUint64(12));
            const payload = new Uint8Array(buffer, HEADER_BYTES, length);

            if (kind === KIND_JPEG) {
                // The poster before the first keyframe, or a JPEG-only telescope
                if (!this.playingH264) this._drawJpeg(payload);
            } else if (kind === KIND_H264) {
                this._decode(payload, keyframe, timestamp);
            }
        }

        _decode(payload, keyframe, timestamp) {
            const decoder = this.decoder;
            if (!decoder || decoder.state !== 'configured') return;
            if (this.needKeyframe && !keyframe) return;
            if (!keyframe && decoder.decodeQueueSize > MAX_DECODE_QUEUE) {
                this.needKeyframe = true;
                return;
            }
            this.needKeyframe = false;
            try {
                decoder.decode(new window.EncodedVideoChunk({
                    type: keyframe ? 'key' : 'delta',
                    timestamp,
                    data: payload
                }));
            } catch (error) {
                this._onDecoderError(error);
            }
        }

        _fitCanvas(width, height) {
            if (width && height && (this.canvas.width !== width || this.canvas.height !== height)) {
                this.canvas.width = width;
                this.canvas.height = height;
            }
        }

        _drawVideoFrame(frame) {
            try {
                this._fitCanvas(frame.displayWidth, frame.displayHeight);
                this.ctx.drawImage(frame, 0, 0, this.canvas.width, this.canvas.height);
                this.playingH264 = true;
                this._frameDrawn();
            } finally {
                frame.close();
            }
        }

        async _drawJpeg(payload) {
            let bitmap;
            try {
                bitmap = await createImageBitmap(new Blob([payload], { type: 'image/jpeg' }));
            } catch (error) {
                return;
            }
            if (!this.playingH264) {
                this._fitCanvas(bitmap.width, bitmap.height);
                this.ctx.drawImage(bitmap, 0, 0, this.canvas.width, this.canvas.height);
                this._frameDrawn();
            }
            bitmap.close();
        }

        _frameDrawn() {
            if (!this.drawnAnything) {
                this.drawnAnything = true;
            }
            this.onState('playing', this.playingH264 ? 'H.264' : 'JPEG');
            const now = performance.now();
            if (now - this.lastFrameEvent >= FRAME_EVENT_MS) {
                this.lastFrameEvent = now;
                this.canvas.dispatchEvent(new Event('liveframe'));
            }
        }
    }

    window.LiveViewPlayer = LiveViewPlayer;
})();
