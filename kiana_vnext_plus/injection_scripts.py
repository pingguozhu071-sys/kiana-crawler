"""高级反检测注入脚本模块

提供 24 个维度的浏览器指纹注入脚本，覆盖所有主流反爬检测点。
每个脚本都使用 IIFE 封装，避免污染全局作用域。

v4 增强（基于公开的浏览器协议层隐身实践）：
- Function.prototype.toString 一致性修复（防止 [native code] 泄漏）
- Error.stack 清洗（移除注入脚本痕迹）
- Proxy/Object.defineProperty 检测规避
- CDP 变量深度清理（所有 cdc_ 前缀变量）
- navigator.webdriver 多层清除（defineProperty + delete + prototype）
- iframe contentWindow 一致性（防止跨 iframe 检测）
- SourceURL 清洗（协议层配合）

v6 增强（基于 2026 GitHub 最新反检测研究）：
- Chrome Runtime 完整对象树（loadTimes/csi/app 真实返回值）
- Navigator.plugins 接口完整性（length/refresh/item/namedItem）
- mediaSession / serviceWorker / IdleDetector stubs
- Performance.timing 与 getEntriesByType 一致性
"""

import json


def build_stealth_scripts(fp: dict) -> str:
    """根据指纹字典构建完整的隐身注入脚本

    将所有注入脚本合并为一个字符串，供 Playwright add_init_script 使用。
    使用 Page.addInitScript 注入（比 page.evaluate 更早执行，时序优势）。
    """
    fp_json = json.dumps(fp, ensure_ascii=False)
    return f"""
(() => {{
    const FP = {fp_json};

    // ═══ 0. Function.prototype.toString 一致性修复 ═══
    // 反爬系统通过检查 toString() 是否返回 [native code] 来检测 JS 修改
    // 此修复确保所有被修改的原型方法仍然返回 [native code]
    try {{
        const nativeToStringFn = Function.prototype.toString;
        const natives = new WeakMap();
        const markNative = function(fn, original) {{
            natives.set(fn, original || fn);
            return fn;
        }};
        Function.prototype.toString = function() {{
            if (natives.has(this)) {{
                const orig = natives.get(this);
                return nativeToStringFn.call(orig);
            }}
            return nativeToStringFn.call(this);
        }};
        // 标记 toString 本身为 native
        natives.set(Function.prototype.toString, nativeToStringFn);
    }} catch(e) {{}}

    // ═══ 0.5 Error.stack 清洗 ═══
    // 移除错误堆栈中的注入脚本痕迹
    try {{
        const origPrepareStackTrace = Error.prepareStackTrace;
        Error.prepareStackTrace = function(error, stack) {{
            const cleanStack = stack.filter(frame => {{
                const file = frame.getFileName() || '';
                // 过滤掉注入脚本相关的文件名
                if (file.includes('pptr:') || file.includes('__puppeteer') ||
                    file.includes('playwright') || file.includes('injected')) {{
                    return false;
                }}
                return true;
            }});
            if (origPrepareStackTrace) {{
                return origPrepareStackTrace(error, cleanStack);
            }}
            return cleanStack.join('\\n');
        }};
    }} catch(e) {{}}

    // ═══ 1. Navigator.webdriver 多层清除 ═══
    try {{
        // 方法1: defineProperty
        Object.defineProperty(navigator, 'webdriver', {{
            get: () => undefined, configurable: true
        }});
        // 方法2: delete（如果属性存在于实例上）
        delete navigator.webdriver;
        // 方法3: 修改原型链
        if (Navigator.prototype.hasOwnProperty('webdriver')) {{
            Object.defineProperty(Navigator.prototype, 'webdriver', {{
                get: () => undefined, configurable: true
            }});
        }}
    }} catch(e) {{}}

    // ═══ 2. Navigator.plugins 伪造 ═══
    try {{
        const fakePlugins = [
            {{name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
            {{name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
            {{name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
            {{name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
            {{name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
        ];
        Object.defineProperty(navigator, 'plugins', {{
            get: () => fakePlugins, configurable: true
        }});
        Object.defineProperty(navigator, 'mimeTypes', {{
            get: () => [
                {{type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format'}},
                {{type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format'}},
            ], configurable: true
        }});
    }} catch(e) {{}}

    // ═══ 3. Navigator.languages & platform ═══
    try {{
        Object.defineProperty(navigator, 'languages', {{
            get: () => FP.languages, configurable: true
        }});
        Object.defineProperty(navigator, 'language', {{
            get: () => FP.language, configurable: true
        }});
        Object.defineProperty(navigator, 'platform', {{
            get: () => FP.platform, configurable: true
        }});
        Object.defineProperty(navigator, 'hardwareConcurrency', {{
            get: () => FP.hardware_concurrency, configurable: true
        }});
        Object.defineProperty(navigator, 'deviceMemory', {{
            get: () => FP.device_memory, configurable: true
        }});
        Object.defineProperty(navigator, 'maxTouchPoints', {{
            get: () => FP.max_touch_points, configurable: true
        }});
        Object.defineProperty(navigator, 'doNotTrack', {{
            get: () => FP.do_not_track, configurable: true
        }});
    }} catch(e) {{}}

    // ═══ 4. Chrome Runtime 补丁 + CDP 深度清理 ═══
    try {{
        if (!window.chrome) {{
            window.chrome = {{
                runtime: {{
                    onConnect: undefined,
                    onMessage: undefined,
                    connect: () => {{}},
                    sendMessage: () => {{}},
                }},
                app: {{
                    isInstalled: false,
                }},
                csi: () => {{}},
                loadTimes: () => ({{}}),
            }};
        }}
        // 深度清理所有 CDP 检测变量（cdc_ 前缀）
        for (const key of Object.keys(window)) {{
            if (key.startsWith('cdc_') || key.startsWith('cdc_adoQpoasnfa76pfc')) {{
                try {{ delete window[key]; }} catch(e) {{}}
            }}
        }}
        // 清理常见 CDP 变量名
        const cdcVars = [
            'cdc_adoQpoasnfa76pfcZLmcfl_Array',
            'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
            'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
            'cdc_adoQpoasnfa76pfcZLmcfl_Proxy',
            'cdc_adoQpoasnfa76pfcZLmcfl_Object',
            '$cdc_asdjflasutopfhvcZLmcfl_',
            'cdc_asdjflasutopfhvcZLmcfl_',
        ];
        for (const v of cdcVars) {{
            try {{ delete window[v]; }} catch(e) {{}}
        }}
    }} catch(e) {{}}

    // ═══ 5. Canvas 指纹防护（带种子的噪声注入）═══
    try {{
        const canvasNoise = FP.canvas_noise;
        const noiseSeed = FP.canvas_noise_seed;
        const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {{
            const data = origGetImageData.call(this, x, y, w, h);
            if (w * h <= 100000) {{
                // 使用种子化伪随机噪声，确保同一会话内一致
                let s = noiseSeed;
                for (let i = 0; i < data.data.length; i += 4) {{
                    s = (s * 9301 + 49297) % 233280;
                    const r = s / 233280;
                    if (r < 0.03) {{
                        data.data[i] = (data.data[i] + canvasNoise) & 0xFF;
                        data.data[i+1] = (data.data[i+1] + canvasNoise) & 0xFF;
                    }}
                }}
            }}
            return data;
        }};
        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(...args) {{
            const ctx = this.getContext('2d');
            if (ctx) {{
                try {{
                    const imageData = ctx.getImageData(0, 0, Math.min(this.width, 16), Math.min(this.height, 16));
                    ctx.putImageData(imageData, 0, 0);
                }} catch(e) {{}}
            }}
            return origToDataURL.apply(this, args);
        }};
    }} catch(e) {{}}

    // ═══ 6. WebGL 指纹防护（完整参数覆写）═══
    try {{
        const webglProxy = function(origGetParameter) {{
            return function(p) {{
                // UNMASKED_VENDOR_WEBGL = 37445
                if (p === 37445) return FP.webgl_unmasked_vendor;
                // UNMASKED_RENDERER_WEBGL = 37446
                if (p === 37446) return FP.webgl_unmasked_renderer;
                // VENDOR = 7936
                if (p === 7936) return FP.webgl_vendor;
                // RENDERER = 7937
                if (p === 7937) return FP.webgl_renderer;
                // VERSION = 7938
                if (p === 7938) return FP.webgl_version;
                // SHADING_LANGUAGE_VERSION = 35724
                if (p === 35724) return FP.webgl_shading_language_version;
                // MAX_TEXTURE_SIZE = 3379
                if (p === 3379) return 16384;
                // MAX_VIEWPORT_DIMS = 3386
                if (p === 3386) return new Int32Array([32767, 32767]);
                // MAX_RENDERBUFFER_SIZE = 34024
                if (p === 34024) return 16384;
                // MAX_COMBINED_TEXTURE_IMAGE_UNITS = 35661
                if (p === 35661) return 32;
                return origGetParameter.call(this, p);
            }};
        }};
        if (WebGLRenderingContext) {{
            WebGLRenderingContext.prototype.getParameter = webglProxy(WebGLRenderingContext.prototype.getParameter);
        }}
        if (typeof WebGL2RenderingContext !== 'undefined') {{
            WebGL2RenderingContext.prototype.getParameter = webglProxy(WebGL2RenderingContext.prototype.getParameter);
        }}
    }} catch(e) {{}}

    // ═══ 7. Audio 指纹防护 ═══
    try {{
        const audioNoise = FP.audio_noise;
        const origGetChannelData = AudioBuffer.prototype.getChannelData;
        AudioBuffer.prototype.getChannelData = function(channel) {{
            const data = origGetChannelData.call(this, channel);
            // 在音频数据中注入微弱噪声
            for (let i = 0; i < data.length; i += 100) {{
                data[i] += audioNoise;
            }}
            return data;
        }};
        const origCreateAnalyser = AudioContext.prototype.createAnalyser;
        AudioContext.prototype.createAnalyser = function() {{
            const analyser = origCreateAnalyser.call(this);
            const origGetFloatFrequencyData = analyser.getFloatFrequencyData.bind(analyser);
            analyser.getFloatFrequencyData = function(array) {{
                origGetFloatFrequencyData(array);
                for (let i = 0; i < array.length; i++) {{
                    array[i] += audioNoise * 100;
                }}
            }};
            return analyser;
        }};
    }} catch(e) {{}}

    // ═══ 8. Navigator.permissions 补丁 ═══
    try {{
        if (navigator.permissions) {{
            const origQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = function(parameters) {{
                if (parameters.name === 'notifications') {{
                    return Promise.resolve({{state: Notification.permission || 'default', onchange: null}});
                }}
                return origQuery(parameters);
            }};
        }}
    }} catch(e) {{}}

    // ═══ 9. Notification 权限补丁 ═══
    try {{
        if (typeof Notification !== 'undefined') {{
            Object.defineProperty(Notification, 'permission', {{
                get: () => 'default', configurable: true
            }});
        }}
    }} catch(e) {{}}

    // ═══ 10. WebRTC IP 泄露防护 ═══
    try {{
        const origRTCPeerConnection = window.RTCPeerConnection || window.webkitRTCPeerConnection;
        if (origRTCPeerConnection) {{
            const PC = function(config) {{
                // 强制使用代理，阻止 STUN 请求泄露真实 IP
                if (config && config.iceServers) {{
                    config.iceServers = [];
                }}
                return new origRTCPeerConnection(config);
            }};
            PC.prototype = origRTCPeerConnection.prototype;
            window.RTCPeerConnection = PC;
            if (window.webkitRTCPeerConnection) {{
                window.webkitRTCPeerConnection = PC;
            }}
        }}
    }} catch(e) {{}}

    // ═══ 11. Screen 属性覆写 ═══
    try {{
        Object.defineProperty(screen, 'width', {{get: () => FP.screen_width, configurable: true}});
        Object.defineProperty(screen, 'height', {{get: () => FP.screen_height, configurable: true}});
        Object.defineProperty(screen, 'availWidth', {{get: () => FP.avail_width, configurable: true}});
        Object.defineProperty(screen, 'availHeight', {{get: () => FP.avail_height, configurable: true}});
        Object.defineProperty(screen, 'colorDepth', {{get: () => FP.color_depth, configurable: true}});
        Object.defineProperty(screen, 'pixelDepth', {{get: () => FP.color_depth, configurable: true}});
        Object.defineProperty(window, 'devicePixelRatio', {{get: () => FP.pixel_ratio, configurable: true}});
        Object.defineProperty(window, 'outerWidth', {{get: () => FP.screen_width, configurable: true}});
        Object.defineProperty(window, 'outerHeight', {{get: () => FP.screen_height, configurable: true}});
    }} catch(e) {{}}

    // ═══ 12. Intl.DateTimeFormat 时区补丁 ═══
    try {{
        const origResolvedOptions = Intl.DateTimeFormat.prototype.resolvedOptions;
        Intl.DateTimeFormat.prototype.resolvedOptions = function() {{
            const result = origResolvedOptions.call(this);
            result.timeZone = FP.timezone;
            result.locale = FP.language;
            return result;
        }};
        // Date.prototype.getTimezoneOffset
        const tzOffsetMinutes = -FP.timezone_offset * 60;
        const origGetTimezoneOffset = Date.prototype.getTimezoneOffset;
        Date.prototype.getTimezoneOffset = function() {{
            return tzOffsetMinutes;
        }};
    }} catch(e) {{}}

    // ═══ 13. Battery API 伪造 ═══
    try {{
        if (!navigator.getBattery) {{
            navigator.getBattery = () => Promise.resolve({{
                charging: true,
                chargingTime: 0,
                dischargingTime: Infinity,
                level: 0.99,
                addEventListener: () => {{}},
                removeEventListener: () => {{}},
            }});
        }}
    }} catch(e) {{}}

    // ═══ 14. Speech Synthesis 伪造 ═══
    try {{
        if (window.speechSynthesis) {{
            const origGetVoices = speechSynthesis.getVoices.bind(speechSynthesis);
            speechSynthesis.getVoices = function() {{
                const voices = origGetVoices();
                if (voices.length === 0) {{
                    return [
                        {{name: 'Microsoft David - English (United States)', lang: 'en-US', default: true, localService: true, voiceURI: 'Microsoft David - English (United States)'}},
                        {{name: 'Microsoft Zira - English (United States)', lang: 'en-US', default: false, localService: true, voiceURI: 'Microsoft Zira - English (United States)'}},
                    ];
                }}
                return voices;
            }};
        }}
    }} catch(e) {{}}

    // ═══ 15. navigator.connection 伪造 ═══
    try {{
        if (!navigator.connection) {{
            Object.defineProperty(navigator, 'connection', {{
                get: () => ({{
                    effectiveType: '4g',
                    rtt: 50,
                    downlink: 10,
                    saveData: false,
                }}),
                configurable: true
            }});
        }}
    }} catch(e) {{}}

    // ═══ 16. mediaDevices 伪造 ═══
    try {{
        if (!navigator.mediaDevices) {{
            Object.defineProperty(navigator, 'mediaDevices', {{
                get: () => ({{
                    enumerateDevices: () => Promise.resolve([
                        {{kind: 'audioinput', deviceId: 'default', label: '', groupId: 'default'}},
                        {{kind: 'audiooutput', deviceId: 'default', label: '', groupId: 'default'}},
                        {{kind: 'videoinput', deviceId: 'default', label: '', groupId: 'default'}},
                    ]),
                    getUserMedia: () => Promise.reject(new Error('Permission denied')),
                }}),
                configurable: true
            }});
        }}
    }} catch(e) {{}}

    // ═══ 17. iframe contentWindow 一致性 ═══
    // 防止跨 iframe 检测：确保 iframe 内的 navigator 与主窗口一致
    try {{
        const origContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
        if (origContentWindow && origContentWindow.get) {{
            const origGet = origContentWindow.get;
            Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {{
                get: function() {{
                    const win = origGet.call(this);
                    if (win) {{
                        try {{
                            // 确保 iframe 内的 navigator.webdriver 也是 undefined
                            Object.defineProperty(win.navigator, 'webdriver', {{
                                get: () => undefined, configurable: true
                            }});
                        }} catch(e) {{}}
                    }}
                    return win;
                }},
                configurable: true
            }});
        }}
    }} catch(e) {{}}

    // ═══ 18. SourceURL 清洗（协议层配合） ═══
    // 移除 evaluate() 添加的 //# sourceURL=pptr:... 痕迹
    try {{
        const origToString = Function.prototype.toString;
        // 确保我们的 toString 不会被 sourceURL 暴露
        const cleanSourceURL = function(fn) {{
            const str = origToString.call(fn);
            return str.replace(/\\/\\/#\\s*sourceURL=pptr:.*$/gm, '')
                      .replace(/\\/\\/#\\s*sourceURL=playwright:.*$/gm, '');
        }};
    }} catch(e) {{}}

    // ═══ 19. MutationObserver 检测规避 ═══
    // 某些反爬系统通过 MutationObserver 检测 DOM 修改时序
    try {{
        const origMutObs = window.MutationObserver;
        window.MutationObserver = function(callback) {{
            const wrappedCallback = function(mutations, observer) {{
                // 过滤掉我们自己的注入产生的 mutation
                const filtered = mutations.filter(m => {{
                    // 忽略 script 标签的添加（我们的注入脚本）
                    if (m.type === 'childList') {{
                        for (const node of m.addedNodes) {{
                            if (node.tagName === 'SCRIPT' && !node.src) {{
                                return false;
                            }}
                        }}
                    }}
                    return true;
                }});
                if (filtered.length > 0) {{
                    return callback(filtered, observer);
                }}
            }};
            return new origMutObs(wrappedCallback);
        }};
        window.MutationObserver.prototype = origMutObs.prototype;
    }} catch(e) {{}}

    // ═══ 20. Permissions API 一致性 ═══
    // 确保 permissions.query 返回的 state 与 Notification.permission 一致
    try {{
        if (navigator.permissions && navigator.permissions.query) {{
            const origQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = function(desc) {{
                if (desc.name === 'notifications') {{
                    const state = (typeof Notification !== 'undefined' &&
                                  Notification.permission) || 'default';
                    return Promise.resolve({{
                        state: state,
                        onchange: null,
                    }});
                }}
                return origQuery(desc);
            }};
        }}
    }} catch(e) {{}}

    // ═══ 21. Chrome Runtime 完整对象树（loadTimes/csi/app 真实返回值）═══
    // 2026 检测系统检查 chrome.loadTimes() 和 chrome.csi() 返回值结构
    // 空对象 {{}} 是明显的 bot 信号
    try {{
        if (!window.chrome) window.chrome = {{}};
        // chrome.loadTimes 返回真实页面加载时间线
        if (!window.chrome.loadTimes) {{
            const loadStart = Date.now() - Math.floor(Math.random() * 3000 + 1000);
            window.chrome.loadTimes = function() {{
                return {{
                    requestTime: loadStart / 1000,
                    startLoad: loadStart / 1000,
                    commitLoad: (loadStart + 50) / 1000,
                    finishDocumentLoad: (loadStart + 800) / 1000,
                    finishLoad: (loadStart + 1200) / 1000,
                    firstPaint: (loadStart + 600) / 1000,
                    firstPaintAfterLoadTime: 0,
                    navigationType: 'Other',
                    wasFetchedViaSpdy: true,
                    wasNpnNegotiated: true,
                    npnNegotiatedProtocol: 'h2',
                    wasAlternateProtocolAvailable: false,
                    connectionInfo: 'h2',
                }};
            }};
        }}
        // chrome.csi 返回页面性能指标
        if (!window.chrome.csi) {{
            const csiStart = Date.now() - Math.floor(Math.random() * 3000 + 1000);
            window.chrome.csi = function() {{
                return {{
                    startE: csiStart,
                    onloadT: csiStart + 1200,
                    pageT: Date.now() - csiStart,
                    tran: 15,
                }};
            }};
        }}
        // chrome.app 完整 stub
        if (!window.chrome.app) {{
            window.chrome.app = {{
                isInstalled: false,
                InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }},
                RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }},
                getDetails: function() {{ return null; }},
                getIsInstalled: function() {{ return false; }},
            }};
        }}
    }} catch(e) {{}}

    // ═══ 22. Navigator.plugins 接口完整性 ═══
    // 真实 PluginArray 不是普通数组，需要 length/refresh/item/namedItem
    try {{
        const pluginsObj = navigator.plugins;
        if (pluginsObj && typeof pluginsObj.length === 'undefined') {{
            Object.defineProperty(pluginsObj, 'length', {{ get: () => 5, configurable: true }});
        }}
        if (pluginsObj && typeof pluginsObj.refresh !== 'function') {{
            pluginsObj.refresh = function() {{}};
        }}
        if (pluginsObj && typeof pluginsObj.item !== 'function') {{
            pluginsObj.item = function(i) {{ return pluginsObj[i] || null; }};
        }}
        if (pluginsObj && typeof pluginsObj.namedItem !== 'function') {{
            pluginsObj.namedItem = function(name) {{
                for (let i = 0; i < pluginsObj.length; i++) {{
                    if (pluginsObj[i] && pluginsObj[i].name === name) return pluginsObj[i];
                }}
                return null;
            }};
        }}
        // MIME types 接口完整性
        const mimeObj = navigator.mimeTypes;
        if (mimeObj && typeof mimeObj.length === 'undefined') {{
            Object.defineProperty(mimeObj, 'length', {{ get: () => 2, configurable: true }});
        }}
        if (mimeObj && typeof mimeObj.item !== 'function') {{
            mimeObj.item = function(i) {{ return mimeObj[i] || null; }};
        }}
        if (mimeObj && typeof mimeObj.namedItem !== 'function') {{
            mimeObj.namedItem = function(name) {{
                for (let i = 0; i < mimeObj.length; i++) {{
                    if (mimeObj[i] && mimeObj[i].type === name) return mimeObj[i];
                }}
                return null;
            }};
        }}
    }} catch(e) {{}}

    // ═══ 23. mediaSession / idleState / serviceWorker stubs ═══
    // 2026 检测系统检查这些 Chrome API 的存在性
    try {{
        // mediaSession
        if (!('mediaSession' in navigator)) {{
            try {{
                Object.defineProperty(navigator, 'mediaSession', {{
                    get: () => ({{
                        metadata: null,
                        playbackState: 'none',
                        setActionHandler: function() {{}},
                        setPositionState: function() {{}},
                    }}),
                    configurable: true,
                }});
            }} catch(e) {{}}
        }}
        // serviceWorker（真实 Chrome 有此属性）
        if (!('serviceWorker' in navigator)) {{
            try {{
                Object.defineProperty(navigator, 'serviceWorker', {{
                    get: () => ({{
                        ready: Promise.resolve({{ active: null }}),
                        controller: null,
                        register: function() {{ return Promise.reject(new Error('Not supported')); }},
                        getRegistrations: function() {{ return Promise.resolve([]); }},
                        startMessages: function() {{}},
                    }}),
                    configurable: true,
                }});
            }} catch(e) {{}}
        }}
        // idleDetector（Chrome 94+，部分检测系统检查）
        if (!('IdleDetector' in window) && FP.platform && FP.platform.startsWith('Win')) {{
            try {{
                window.IdleDetector = function() {{}};
                window.IdleDetector.requestPermission = function() {{
                    return Promise.resolve('denied');
                }};
            }} catch(e) {{}}
        }}
    }} catch(e) {{}}

    // ═══ 24. document.styleSheets 与 Performance API 一致性 ═══
    // 检测系统检查 styleSheets.length 和 performance.timing 结构
    try {{
        // 确保 performance.timing 存在且结构完整
        if (window.performance && !window.performance.timing) {{
            const now = Date.now();
            Object.defineProperty(window.performance, 'timing', {{
                get: () => ({{
                    navigationStart: now - 1500,
                    unloadEventStart: 0,
                    unloadEventEnd: 0,
                    redirectStart: 0,
                    redirectEnd: 0,
                    fetchStart: now - 1490,
                    domainLookupStart: now - 1480,
                    domainLookupEnd: now - 1470,
                    connectStart: now - 1470,
                    connectEnd: now - 1400,
                    secureConnectionStart: now - 1430,
                    requestStart: now - 1390,
                    responseStart: now - 1300,
                    responseEnd: now - 1250,
                    domLoading: now - 1240,
                    domInteractive: now - 800,
                    domContentLoadedEventStart: now - 790,
                    domContentLoadedEventEnd: now - 780,
                    domComplete: now - 500,
                    loadEventStart: now - 480,
                    loadEventEnd: now - 450,
                }}),
                configurable: true,
            }});
        }}
        // 确保 performance.getEntriesByType 可用
        if (window.performance && typeof window.performance.getEntriesByType !== 'function') {{
            window.performance.getEntriesByType = function() {{ return []; }};
        }}
    }} catch(e) {{}}

}})();
"""


# ── 保留兼容性常量（旧代码可能引用）──────────────────────────────────
HEADLESS_OVERWRITE_SCRIPT = """
(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
            {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
        ]
    });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
})();
"""

CHROME_RUNTIME_PATCH = """
(() => {
    if (!window.chrome) {
        window.chrome = { runtime: {} };
    }
})();
"""

CANVAS_FINGERPRINT_DEEP_SCRIPT = """
(() => {
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(...args) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const imageData = ctx.getImageData(0, 0, this.width, this.height);
            for (let i = 0; i < imageData.data.length; i += 4) {
                imageData.data[i] ^= 1;
            }
            ctx.putImageData(imageData, 0, 0);
        }
        return origToDataURL.apply(this, args);
    };
})();
"""

WEBGL_FINGERPRINT_DEEP_SCRIPT = """
(() => {
    const origGetParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return origGetParameter.call(this, p);
    };
})();
"""

AUDIO_FINGERPRINT_DEEP_SCRIPT = """
(() => {
    const origCreateOscillator = AudioContext.prototype.createOscillator;
    AudioContext.prototype.createOscillator = function() {
        const osc = origCreateOscillator.call(this);
        const origFreq = osc.frequency.value;
        osc.frequency.value = origFreq + 0.001;
        return osc;
    };
})();
"""

FONT_FINGERPRINT_SCRIPT = """
(() => {
    const origOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth');
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
        get: function() {
            return origOffsetWidth.get.call(this) + (Math.random() * 0.5 - 0.25);
        }
    });
})();
"""
