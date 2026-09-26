"""Advanced Evasion v2 — WebRTC, Canvas, AudioContext, WebGL fingerprint spoofing"""
# ═══ 最新浏览器指纹反制脚本 ═══

WEBRTC_LEAK_PREVENT = """
// WebRTC IP leak prevention — blocks RTCPeerConnection
(function(){
    const origRTCPeerConnection = window.RTCPeerConnection || window.webkitRTCPeerConnection || window.mozRTCPeerConnection;
    if(origRTCPeerConnection){
        const blockIP = (conn) => {
            const origCreateOffer = conn.createOffer.bind(conn);
            conn.createOffer = function(...args){
                return origCreateOffer(...args).then(desc => {
                    if(desc && desc.sdp){
                        desc.sdp = desc.sdp.replace(/(\\d{1,3}\\.){3}\\d{1,3}/g, '0.0.0.0');
                        desc.sdp = desc.sdp.replace(/[0-9a-f]{1,4}(:[0-9a-f]{1,4}){7}/gi, '::1');
                    }
                    return desc;
                });
            };
        };
        window.RTCPeerConnection = function(...args){
            const conn = new origRTCPeerConnection(...args);
            blockIP(conn);
            return conn;
        };
        if(window.webkitRTCPeerConnection){
            window.webkitRTCPeerConnection = window.RTCPeerConnection;
        }
    }
})();
"""

CANVAS_FINGERPRINT_NOISE = """
// Canvas fingerprint noise injection
(function(){
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const origToBlob = HTMLCanvasElement.prototype.toBlob;
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    const noiseCanvas = () => {
        const canvas = document.createElement('canvas');
        canvas.width = 2; canvas.height = 2;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = 'rgba(' + Math.floor(Math.random()*3) + ',' + Math.floor(Math.random()*3) + ',' + Math.floor(Math.random()*3) + ',0.01)';
        ctx.fillRect(0,0,2,2);
        return ctx.getImageData(0,0,2,2);
    };
    let noiseData = noiseCanvas();
    setInterval(() => { noiseData = noiseCanvas(); }, 30000);
    HTMLCanvasElement.prototype.toDataURL = function(...args){
        try{
            const ctx = this.getContext('2d');
            if(ctx && this.width > 10 && this.height > 10){
                ctx.putImageData(noiseData, Math.floor(Math.random()*2), Math.floor(Math.random()*2));
            }
        }catch(e){}
        return origToDataURL.apply(this, args);
    };
    CanvasRenderingContext2D.prototype.getImageData = function(...args){
        const result = origGetImageData.apply(this, args);
        if(result && result.data && result.data.length > 100){
            const offset = (Math.floor(Math.random()*result.width) + Math.floor(Math.random()*result.height) * result.width) * 4;
            if(offset < result.data.length - 3){
                result.data[offset] = result.data[offset] ^ 1;
            }
        }
        return result;
    };
})();
"""

AUDIO_FINGERPRINT_SPOOF = """
// AudioContext fingerprint randomization
(function(){
    const origCreateOscillator = AudioContext.prototype.createOscillator;
    const origCreateAnalyser = AudioContext.prototype.createAnalyser;
    const origCreateDynamicsCompressor = AudioContext.prototype.createDynamicsCompressor;
    let noiseOffset = Math.random() * 0.0001;
    AudioContext.prototype.createDynamicsCompressor = function(){
        const comp = origCreateDynamicsCompressor.call(this);
        const origThreshold = Object.getOwnPropertyDescriptor(AudioParam.prototype, 'value');
        if(origThreshold && origThreshold.set){
            const thresholdParam = comp.threshold;
            const origSet = Object.getOwnPropertyDescriptor(AudioParam.prototype, 'value').set;
            noiseOffset += 0.00001;
        }
        return comp;
    };
    // Override sampleRate to common value
    const origDefine = Object.defineProperty;
    const origSampleRate = Object.getOwnPropertyDescriptor(BaseAudioContext.prototype, 'sampleRate');
    if(origSampleRate && origSampleRate.get){
        const origGet = origSampleRate.get;
        Object.defineProperty(BaseAudioContext.prototype, 'sampleRate', {
            get: function(){ return 44100; },
            configurable: true
        });
    }
})();
"""

WEBGL_FINGERPRINT_SPOOF = """
// WebGL fingerprint spoofing
(function(){
    const origGetParameter = WebGLRenderingContext.prototype.getParameter;
    const spoofedGL = {
        'RENDERER': 'ANGLE (Intel, Intel(R) Arc(TM) Graphics (0x00007D55) Direct3D11 vs_5_0 ps_5_0, D3D11)',
        'VENDOR': 'Google Inc. (Intel)',
        'VERSION': 'WebGL 2.0 (OpenGL ES 3.0 Chromium)',
        'SHADING_LANGUAGE_VERSION': 'WebGL GLSL ES 3.0 (OpenGL ES GLSL ES 3.0 Chromium)',
    };
    const paramMap = {
        0x1F01: 'RENDERER', 0x1F00: 'VENDOR', 0x1F02: 'VERSION',
        0x8B8C: 'SHADING_LANGUAGE_VERSION', 0x8B8D: 'SHADING_LANGUAGE_VERSION',
    };
    WebGLRenderingContext.prototype.getParameter = function(pname){
        const key = paramMap[pname];
        if(key) return spoofedGL[key] || origGetParameter.call(this, pname);
        return origGetParameter.call(this, pname);
    };
    if(window.WebGL2RenderingContext){
        WebGL2RenderingContext.prototype.getParameter = WebGLRenderingContext.prototype.getParameter;
    }
})();
"""

# ═══ CDP-level evasion (Chrome DevTools Protocol) ═══
CDP_EVASION_INJECT = """
// Disable automation detection via CDP
(function(){
    // Remove webdriver flag
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});
    // Remove chrome runtime
    if(window.chrome && window.chrome.runtime){
        window.chrome.runtime = undefined;
    }
    // Fake plugins
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
                {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
                {name: 'Native Client', filename: 'internal-nacl-plugin', description: ''},
            ];
            plugins.item = (i) => plugins[i] || null;
            plugins.namedItem = (n) => plugins.find(p => p.name === n) || null;
            plugins.refresh = () => {};
            Object.defineProperty(plugins, 'length', {value: plugins.length});
            return plugins;
        }
    });
    // Permissions API spoof
    if(navigator.permissions && navigator.permissions.query){
        const origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = function(desc){
            if(desc.name === 'notifications') return Promise.resolve({state: 'prompt'});
            return origQuery(desc);
        };
    }
})();
"""

ALL_EVASION_SCRIPTS = [
    WEBRTC_LEAK_PREVENT,
    CANVAS_FINGERPRINT_NOISE,
    AUDIO_FINGERPRINT_SPOOF,
    WEBGL_FINGERPRINT_SPOOF,
    CDP_EVASION_INJECT,
]
