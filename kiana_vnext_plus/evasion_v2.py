"""Advanced Evasion v2 — WebRTC, Canvas, AudioContext, WebGL fingerprint spoofing"""
# ═══ 最新浏览器指纹反制脚本 ═══

WEBRTC_LEAK_PREVENT = """
// WebRTC IP leak prevention — blocks RTCPeerConnection
(function(){
    const origRTCPeerConnection = window.RTCPeerConnection || window.webkitRTCPeerConnection || window.mozRTCPeerConnection;
    if(origRTCPeerConnection){
        const blockIP = (conn) => {
            // [v6] 擦除规则**只写一份**：SDP 路径与候选事件路径共用它。
            // 两条路径各写一份正则，迟早会"擦一半留一半"（判据不一致）。
            const scrub = (s) => s.replace(/(\\d{1,3}\\.){3}\\d{1,3}/g, '0.0.0.0')
                                 .replace(/[0-9a-f]{1,4}(:[0-9a-f]{1,4}){7}/gi, '::1');
            const origCreateOffer = conn.createOffer.bind(conn);
            conn.createOffer = function(...args){
                return origCreateOffer(...args).then(desc => {
                    if(desc && desc.sdp){
                        desc.sdp = scrub(desc.sdp);
                    }
                    return desc;
                });
            };
            // [v6 修复] **候选事件路径同样要擦**。
            // 上面只擦了 createOffer 的 SDP —— 但 ICE 候选也会通过 onicecandidate
            // 逐条送给页面，`event.candidate.candidate` 里就带着真实地址。
            // 只擦一边 = "看起来有防护、实际有"（本工程最贵的一类）。
            // RTCIceCandidate 字段**只读**，故这里**替换事件对象**而非改写它：
            // Object.create(ev) 保住原型链（instanceof 仍成立）；
            // 任何异常一律回退原始事件——宁可不擦，也不能把 WebRTC 弄坏。
            // （擦除规则 `scrub` 已在函数开头定义，两条路径共用——**不要在这里再定义一次**，
            //   同作用域重复 `const` 是语法错误，会让整个 IIFE 直接失效。）
            try {
                const d = Object.getOwnPropertyDescriptor(origRTCPeerConnection.prototype, 'onicecandidate');
                if (d && d.set) {
                    let cb = null;
                    Object.defineProperty(conn, 'onicecandidate', {
                        configurable: true,
                        get: () => cb,
                        set: (fn) => {
                            cb = fn;
                            d.set.call(conn, (ev) => {
                                try {
                                    const c = ev && ev.candidate && ev.candidate.candidate;
                                    if (typeof c === 'string' && scrub(c) !== c
                                        && typeof RTCIceCandidate === 'function') {
                                        const nc = new RTCIceCandidate({
                                            candidate: scrub(c),
                                            sdpMid: ev.candidate.sdpMid,
                                            sdpMLineIndex: ev.candidate.sdpMLineIndex,
                                            usernameFragment: ev.candidate.usernameFragment,
                                        });
                                        const ev2 = Object.create(ev);
                                        Object.defineProperty(ev2, 'candidate',
                                            { value: nc, enumerable: true });
                                        if (cb) cb(ev2);
                                        return;
                                    }
                                } catch (e) { /* 回退原始事件 */ }
                                if (cb) cb(ev);
                            });
                        },
                    });
                }
            } catch (e) { /* 拿不到描述符就不加这层，保持原行为 */ }
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
    Object.defineProperty(navigator, 'webdriver', {get: () => false, configurable: true});
    // Remove chrome runtime
    if(window.chrome && window.chrome.runtime){
        window.chrome.runtime = undefined;
    }
    // Fake plugins —— [v6 修复] **只在"报 0 / 缺失"时才伪造**。
    // 原实现**无条件**用 3 项**普通数组**覆盖 navigator.plugins，而本脚本是
    // `add_init_script` 里排在 injection_scripts 之后的（solver_engine 先注入 55 维链、
    // 再注入 evasion_v2），于是它会把前者刚装好的完整 PluginArray 又换回"一眼假"的普通数组：
    //   · `navigator.plugins instanceof PluginArray` → false
    //   · length/item/namedItem/refresh 全成了**实例自有**属性（真接口在原型上）
    //   · 它**不碰 mimeTypes**，plugins=3 与 mimeTypes=2 的数量对应关系也就错了
    // 加守卫后：正常报数的浏览器一律不动；只有真报 0 的环境才由这里兜底。
    if (!navigator.plugins || navigator.plugins.length === 0) {
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
    }
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
