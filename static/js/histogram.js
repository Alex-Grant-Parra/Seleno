(function () {
    class HistogramPanel {
        constructor(config) {
            this.canvas = document.getElementById(config.canvasId);
            this.controlsRoot = document.getElementById(config.controlsRootId);
            this.statsElement = config.statsId ? document.getElementById(config.statsId) : null;
            this.emptyMessage = config.emptyMessage || _('Waiting for image data...');

            this.sourceImage = null;
            this.onImageLoad = null;
            this.pendingFrame = null;

            this.offscreenCanvas = document.createElement('canvas');
            this.offscreenCtx = this.offscreenCanvas.getContext('2d', { willReadFrequently: true });

            if (!this.canvas || !this.controlsRoot) {
                return;
            }

            this.ctx = this.canvas.getContext('2d');
            this.controls = {
                mode: this.controlsRoot.querySelector('[data-hist="mode"]'),
                bins: this.controlsRoot.querySelector('[data-hist="bins"]'),
                binsValue: this.controlsRoot.querySelector('[data-hist="bins-value"]'),
                blackPoint: this.controlsRoot.querySelector('[data-hist="black-point"]'),
                blackPointValue: this.controlsRoot.querySelector('[data-hist="black-point-value"]'),
                whitePoint: this.controlsRoot.querySelector('[data-hist="white-point"]'),
                whitePointValue: this.controlsRoot.querySelector('[data-hist="white-point-value"]'),
                sampleStep: this.controlsRoot.querySelector('[data-hist="sample-step"]'),
                sampleStepValue: this.controlsRoot.querySelector('[data-hist="sample-step-value"]'),
                logScale: this.controlsRoot.querySelector('[data-hist="log-scale"]'),
                smooth: this.controlsRoot.querySelector('[data-hist="smooth"]')
            };

            this.settings = {
                mode: 'luminance',
                bins: 128,
                blackPoint: 0,
                whitePoint: 100,
                sampleStep: 2,
                logScale: false,
                smooth: false
            };

            this.bindControls();
            this.syncSettings();
            this.drawPlaceholder(this.emptyMessage);
        }

        bindControls() {
            Object.values(this.controls).forEach((control) => {
                if (!control || typeof control.addEventListener !== 'function') {
                    return;
                }
                const eventName = control.type === 'checkbox' || control.tagName === 'SELECT' ? 'change' : 'input';
                control.addEventListener(eventName, () => {
                    this.syncSettings();
                    this.refresh();
                });
            });
        }

        syncSettings() {
            if (!this.canvas || !this.controlsRoot) {
                return;
            }

            const parsedBins = parseInt(this.controls.bins ? this.controls.bins.value : this.settings.bins, 10);
            const parsedBlack = parseInt(this.controls.blackPoint ? this.controls.blackPoint.value : this.settings.blackPoint, 10);
            const parsedWhite = parseInt(this.controls.whitePoint ? this.controls.whitePoint.value : this.settings.whitePoint, 10);
            const parsedSample = parseInt(this.controls.sampleStep ? this.controls.sampleStep.value : this.settings.sampleStep, 10);

            this.settings.mode = this.controls.mode ? this.controls.mode.value : this.settings.mode;
            this.settings.bins = Number.isFinite(parsedBins) ? Math.max(16, Math.min(512, parsedBins)) : 128;
            this.settings.blackPoint = Number.isFinite(parsedBlack) ? Math.max(0, Math.min(95, parsedBlack)) : 0;
            this.settings.whitePoint = Number.isFinite(parsedWhite) ? Math.max(5, Math.min(100, parsedWhite)) : 100;
            this.settings.sampleStep = Number.isFinite(parsedSample) ? Math.max(1, Math.min(10, parsedSample)) : 2;
            this.settings.logScale = !!(this.controls.logScale && this.controls.logScale.checked);
            this.settings.smooth = !!(this.controls.smooth && this.controls.smooth.checked);

            if (this.settings.whitePoint <= this.settings.blackPoint + 1) {
                this.settings.whitePoint = Math.min(100, this.settings.blackPoint + 1);
                if (this.controls.whitePoint) {
                    this.controls.whitePoint.value = String(this.settings.whitePoint);
                }
            }

            if (this.controls.binsValue) {
                this.controls.binsValue.textContent = String(this.settings.bins);
            }
            if (this.controls.blackPointValue) {
                this.controls.blackPointValue.textContent = String(this.settings.blackPoint) + '%';
            }
            if (this.controls.whitePointValue) {
                this.controls.whitePointValue.textContent = String(this.settings.whitePoint) + '%';
            }
            if (this.controls.sampleStepValue) {
                this.controls.sampleStepValue.textContent = String(this.settings.sampleStep) + ' px';
            }
        }

        setSourceImage(imageElement) {
            if (!this.canvas || !this.controlsRoot) {
                return;
            }

            if (this.sourceImage && this.onImageLoad) {
                this.sourceImage.removeEventListener('load', this.onImageLoad);
                this.sourceImage.removeEventListener('liveframe', this.onImageLoad);
            }

            this.sourceImage = imageElement;
            this.onImageLoad = () => this.refresh();

            if (this.sourceImage) {
                // A canvas (the live view player) announces each new frame with 'liveframe'
                this.sourceImage.addEventListener('load', this.onImageLoad);
                this.sourceImage.addEventListener('liveframe', this.onImageLoad);
                if (this.sourceReady()) {
                    this.refresh();
                } else {
                    this.drawPlaceholder(this.emptyMessage);
                }
            } else {
                this.drawPlaceholder(this.emptyMessage);
            }
        }

        clear(message) {
            this.drawPlaceholder(message || this.emptyMessage);
            if (this.statsElement) {
                this.statsElement.textContent = _('No image loaded.');
            }
        }

        sourceReady() {
            const source = this.sourceImage;
            if (!source) {
                return false;
            }
            if (typeof HTMLCanvasElement !== 'undefined' && source instanceof HTMLCanvasElement) {
                return source.width > 0 && source.height > 0;
            }
            return source.complete && source.naturalWidth > 0;
        }

        refresh() {
            if (!this.canvas || !this.controlsRoot) {
                return;
            }
            if (!this.sourceReady()) {
                this.drawPlaceholder(this.emptyMessage);
                return;
            }

            if (this.pendingFrame) {
                cancelAnimationFrame(this.pendingFrame);
            }

            this.pendingFrame = requestAnimationFrame(() => {
                const histogramData = this.buildHistogram();
                if (!histogramData) {
                    this.drawPlaceholder(_('Unable to compute histogram.'));
                    if (this.statsElement) {
                        this.statsElement.textContent = _('Unable to read image pixel data.');
                    }
                    return;
                }
                this.drawHistogram(histogramData);
                this.updateStats(histogramData);
            });
        }

        buildHistogram() {
            const width = this.sourceImage.naturalWidth || this.sourceImage.width;
            const height = this.sourceImage.naturalHeight || this.sourceImage.height;
            if (!width || !height || !this.offscreenCtx) {
                return null;
            }

            const maxDimension = 512;
            const scale = Math.min(1, maxDimension / Math.max(width, height));
            const sampleWidth = Math.max(1, Math.floor(width * scale));
            const sampleHeight = Math.max(1, Math.floor(height * scale));

            this.offscreenCanvas.width = sampleWidth;
            this.offscreenCanvas.height = sampleHeight;
            this.offscreenCtx.clearRect(0, 0, sampleWidth, sampleHeight);
            this.offscreenCtx.drawImage(this.sourceImage, 0, 0, sampleWidth, sampleHeight);

            let imageData;
            try {
                imageData = this.offscreenCtx.getImageData(0, 0, sampleWidth, sampleHeight).data;
            } catch (error) {
                return null;
            }
            const bins = this.settings.bins;

            const histogram = {
                red: new Array(bins).fill(0),
                green: new Array(bins).fill(0),
                blue: new Array(bins).fill(0),
                luminance: new Array(bins).fill(0),
                stats: {
                    samples: 0,
                    meanLuminance: 0,
                    peakBin: 0,
                    peakCount: 0
                }
            };

            const sampleStep = this.settings.sampleStep;
            const blackNorm = this.settings.blackPoint / 100;
            const whiteNorm = this.settings.whitePoint / 100;
            const spanNorm = Math.max(0.0001, whiteNorm - blackNorm);

            let luminanceAccumulator = 0;

            for (let y = 0; y < sampleHeight; y += sampleStep) {
                for (let x = 0; x < sampleWidth; x += sampleStep) {
                    const idx = (y * sampleWidth + x) * 4;
                    const r = imageData[idx];
                    const g = imageData[idx + 1];
                    const b = imageData[idx + 2];

                    const lum = Math.round(0.2126 * r + 0.7152 * g + 0.0722 * b);
                    luminanceAccumulator += lum;

                    this.accumulateBin(histogram.red, r, bins, blackNorm, spanNorm);
                    this.accumulateBin(histogram.green, g, bins, blackNorm, spanNorm);
                    this.accumulateBin(histogram.blue, b, bins, blackNorm, spanNorm);
                    this.accumulateBin(histogram.luminance, lum, bins, blackNorm, spanNorm);
                    histogram.stats.samples += 1;
                }
            }

            if (histogram.stats.samples > 0) {
                histogram.stats.meanLuminance = luminanceAccumulator / histogram.stats.samples;
            }

            let peakCount = 0;
            let peakBin = 0;
            histogram.luminance.forEach((value, index) => {
                if (value > peakCount) {
                    peakCount = value;
                    peakBin = index;
                }
            });

            histogram.stats.peakCount = peakCount;
            histogram.stats.peakBin = peakBin;

            return histogram;
        }

        accumulateBin(targetHistogram, pixelValue, bins, blackNorm, spanNorm) {
            const normalized = (pixelValue / 255 - blackNorm) / spanNorm;
            const clamped = Math.max(0, Math.min(1, normalized));
            const bin = Math.min(bins - 1, Math.floor(clamped * (bins - 1)));
            targetHistogram[bin] += 1;
        }

        smoothSeries(values) {
            if (!this.settings.smooth) {
                return values;
            }

            const smoothed = new Array(values.length).fill(0);
            for (let i = 0; i < values.length; i += 1) {
                const a = values[Math.max(0, i - 1)];
                const b = values[i];
                const c = values[Math.min(values.length - 1, i + 1)];
                smoothed[i] = (a + b + c) / 3;
            }
            return smoothed;
        }

        drawHistogram(histogramData) {
            const ctx = this.ctx;
            const width = this.canvas.width;
            const height = this.canvas.height;
            ctx.clearRect(0, 0, width, height);

            ctx.fillStyle = '#0f172a';
            ctx.fillRect(0, 0, width, height);

            const paddingLeft = 28;
            const paddingRight = 10;
            const paddingTop = 10;
            const paddingBottom = 20;

            const plotWidth = width - paddingLeft - paddingRight;
            const plotHeight = height - paddingTop - paddingBottom;

            ctx.strokeStyle = 'rgba(148, 163, 184, 0.35)';
            ctx.lineWidth = 1;

            for (let i = 0; i <= 4; i += 1) {
                const y = paddingTop + (plotHeight * i) / 4;
                ctx.beginPath();
                ctx.moveTo(paddingLeft, y);
                ctx.lineTo(width - paddingRight, y);
                ctx.stroke();
            }

            const mode = this.settings.mode;
            const seriesToDraw = [];

            if (mode === 'rgb') {
                seriesToDraw.push({ color: '#ef4444', values: this.smoothSeries(histogramData.red) });
                seriesToDraw.push({ color: '#22c55e', values: this.smoothSeries(histogramData.green) });
                seriesToDraw.push({ color: '#3b82f6', values: this.smoothSeries(histogramData.blue) });
            } else if (mode === 'red') {
                seriesToDraw.push({ color: '#ef4444', values: this.smoothSeries(histogramData.red) });
            } else if (mode === 'green') {
                seriesToDraw.push({ color: '#22c55e', values: this.smoothSeries(histogramData.green) });
            } else if (mode === 'blue') {
                seriesToDraw.push({ color: '#3b82f6', values: this.smoothSeries(histogramData.blue) });
            } else {
                seriesToDraw.push({ color: '#f8fafc', values: this.smoothSeries(histogramData.luminance) });
            }

            let maxValue = 1;
            seriesToDraw.forEach((series) => {
                const localMax = Math.max(...series.values, 1);
                if (localMax > maxValue) {
                    maxValue = localMax;
                }
            });

            seriesToDraw.forEach((series) => {
                ctx.beginPath();
                ctx.lineWidth = 1.5;
                ctx.strokeStyle = series.color;

                series.values.forEach((value, index) => {
                    const x = paddingLeft + (index / Math.max(1, series.values.length - 1)) * plotWidth;
                    const scaled = this.settings.logScale
                        ? Math.log1p(value) / Math.log1p(maxValue)
                        : value / maxValue;
                    const y = paddingTop + (1 - scaled) * plotHeight;
                    if (index === 0) {
                        ctx.moveTo(x, y);
                    } else {
                        ctx.lineTo(x, y);
                    }
                });

                ctx.stroke();
            });

            ctx.fillStyle = '#cbd5e1';
            ctx.font = '10px sans-serif';
            ctx.fillText('0', paddingLeft - 8, height - 8);
            ctx.fillText('255', width - paddingRight - 18, height - 8);
        }

        drawPlaceholder(message) {
            if (!this.ctx || !this.canvas) {
                return;
            }

            const ctx = this.ctx;
            ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
            ctx.fillStyle = '#0f172a';
            ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

            ctx.fillStyle = '#94a3b8';
            ctx.font = '12px sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(message, this.canvas.width / 2, this.canvas.height / 2);
            ctx.textAlign = 'left';
            ctx.textBaseline = 'alphabetic';
        }

        updateStats(histogramData) {
            if (!this.statsElement) {
                return;
            }

            const mean = histogramData.stats.meanLuminance.toFixed(1);
            const peak = histogramData.stats.peakBin;
            const sampleCount = histogramData.stats.samples.toLocaleString();
            this.statsElement.textContent = _('Mean: {mean} | Peak bin: {peak} | Samples: {sampleCount}', { mean, peak, sampleCount });
        }
    }

    window.HistogramPanel = HistogramPanel;
})();
