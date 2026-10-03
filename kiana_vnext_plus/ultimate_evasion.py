"""终极绕过引擎（Ultimate Evasion Engine）

在 evasion_engine.py (35维) 和 behavioral_biometrics.py (43维) 之上，
追加 12 个极限绕过维度，覆盖最前沿反爬检测系统的全部检测面：

  44. WebRTC ICE Candidate 伪造 — 防止真实 IP 泄露
  45. Battery API 一致性 — navigator.getBattery() 合理化
  46. Network Information API — navigator.connection 一致性
  47. Font Metrics 指纹 — measureText() 结果一致性
  48. WebGL2 扩展列表伪装 — 扩展列表与 GPU 匹配
  49. Speech Synthesis API — voices 列表一致性
  50. performance.now() 精度控制 — 防止计时攻击
  51. Geolocation API mock — 地理位置与代理 IP 一致
  52. MediaCapabilities API — 媒体能力查询一致性
  53. Notification API permission — 通知权限一致性
  54. USB/Bluetooth/Gamepad API — 存在性模拟
  55. ResizeObserver/IntersectionObserver — 观察器行为一致性
"""

import json


def build_ultimate_evasion_scripts(fp: dict) -> str:
    """构建终极绕过注入脚本

    在 build_combined_biometrics 之后追加执行，覆盖最前沿的检测维度。
    所有覆写均使用 try-catch 包裹，确保单点失败不影响其他维度。

    Args:
        fp: 指纹字典（来自 fingerprint_consistency.compute_fingerprint_from_ip）

    Returns:
        可直接用于 add_init_script 的 JavaScript 字符串
    """
    fp_json = json.dumps(fp, ensure_ascii=False)
    # 生成一个地理位置坐标（基于地理区域代码）
    geo_code = fp.get("geo_code", "US")
    geo_coords = _get_geo_coords(geo_code)
    lat = geo_coords["lat"]
    lng = geo_coords["lng"]

    return f"""
(() => {{
    const FP = {fp_json};
    const GEO_LAT = {lat};
    const GEO_LNG = {lng};

    // ═══════════════════════════════════════════════════════════════
    // 44. WebRTC ICE Candidate 伪造
    // 防止 STUN 请求泄露真实 IP 地址
    // 反爬系统通过 WebRTC 获取 ICE candidates 中的真实公网 IP
    // ═══════════════════════════════════════════════════════════════
    try {{
        const origRTC = window.RTCPeerConnection;
        if (origRTC) {{
            const PatchedRTC = function(config, constraints) {{
                // 强制使用代理服务器配置
                if (config && config.iceServers) {{
                    // 过滤掉 STUN/TURN 服务器，防止泄露真实 IP
                    config.iceServers = config.iceServers.filter(s => {{
                        const url = (s.urls || '').toLowerCase();
                        return !url.includes('stun:') && !url.includes('turn:');
                    }});
                    // 如果全部被过滤，添加一个假的 STUN 服务器
                    if (config.iceServers.length === 0) {{
                        config.iceServers = [{{ urls: 'stun:stun.l.google.com:19302' }}];
                    }}
                }}
                const pc = new origRTC(config, constraints);

                // 拦截 addIceCandidate，过滤掉包含真实 IP 的 candidate
                const origAddIceCandidate = pc.addIceCandidate.bind(pc);
                pc.addIceCandidate = function(candidate) {{
                    if (candidate && candidate.candidate) {{
                        const candStr = candidate.candidate;
                        // 只允许 host candidate（本地）和 relay candidate（代理）
                        // 过滤 srflx candidate（STUN 反射地址 = 真实公网 IP）
                        if (candStr.includes('srflx')) {{
                            return Promise.resolve(); // 静默丢弃
                        }}
                    }}
                    return origAddIceCandidate(candidate);
                }};

                // 拦截 onicecandidate，过滤掉 srflx candidate
                const origSetOnIce = Object.getOwnPropertyDescriptor(
                    origRTC.prototype, 'onicecandidate'
                );
                if (origSetOnIce && origSetOnIce.set) {{
                    let userCallback = null;
                    Object.defineProperty(pc, 'onicecandidate', {{
                        get: () => userCallback,
                        set: function(cb) {{
                            userCallback = cb;
                            // 用包装函数替换
                            origSetOnIce.set.call(this, function(event) {{
                                if (event && event.candidate && event.candidate.candidate) {{
                                    if (event.candidate.candidate.includes('srflx')) {{
                                        return; // 静默丢弃
                                    }}
                                }}
                                if (userCallback) userCallback(event);
                            }});
                        }},
                        configurable: true,
                    }});
                }}

                // [v6 修复] **SDP 路径必须同样过滤 srflx**。
                // 只拦 onicecandidate 是不够的：ICE 候选**也写在 SDP 里**，
                // 页面读 `pc.localDescription.sdp` 就能拿到被事件过滤掉的那条 srflx，
                // 从而绕过整套过滤拿到真实公网 IP（本工程把 WebRTC 当泄漏面来防，
                // 所以这条属于"看起来有防护、实际有"的真缺口）。
                // 任何异常一律回退原描述——宁可不改，也不能把 WebRTC 弄坏。
                ['localDescription', 'remoteDescription'].forEach(function(prop) {{
                    const d = Object.getOwnPropertyDescriptor(origRTC.prototype, prop);
                    if (d && d.get) {{
                        Object.defineProperty(pc, prop, {{
                            configurable: true,
                            get: function() {{
                                let desc;
                                try {{ desc = d.get.call(pc); }} catch (e) {{ return null; }}
                                try {{
                                    if (desc && typeof desc.sdp === 'string'
                                        && desc.sdp.indexOf('srflx') >= 0) {{
                                        // [v6 修复] 这里的换行必须写成 JS 转义序列（两个字符），
                                        // 不能是**真实换行**：本函数返回的是 **f-string 模板**，
                                        // 直接写出单个反斜杠，会被 Python 先解释成**真实 CR/LF 字符**
                                        // 塞进 JS 字符串字面量里，生成的脚本直接语法错误 ——
                                        // 而它和 §1-§3 层是拼成**同一个** add_init_script 的，
                                        // 一处语法错误会让**整条 55 维链**一行都不执行
                                        // （真机表现：plugins=0 自证告警 ×5，而 webdriver 是干净的
                                        //  —— 那是另一条独立脚本 evasion_v2 遮的）。
                                        const sdp = desc.sdp.split('\\r\\n').filter(function(l) {{
                                            return !(l.indexOf('a=candidate:') === 0
                                                     && l.indexOf('srflx') >= 0);
                                        }}).join('\\r\\n');
                                        return new RTCSessionDescription(
                                            {{ type: desc.type, sdp: sdp }});
                                    }}
                                }} catch (e) {{ /* 回退原描述 */ }}
                                return desc;
                            }},
                        }});
                    }}
                }});

                return pc;
            }};
            PatchedRTC.prototype = origRTC.prototype;
            PatchedRTC.generateCertificate = origRTC.generateCertificate;
            try {{ window.RTCPeerConnection = PatchedRTC; }} catch(e) {{}}
            try {{ window.webkitRTCPeerConnection = PatchedRTC; }} catch(e) {{}}
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 45. Battery API 一致性
    // 反爬系统通过 navigator.getBattery() 检测设备是否真实
    // 自动化环境通常没有电池或返回异常数据
    // ═══════════════════════════════════════════════════════════════
    try {{
        if (!navigator.getBattery) {{
            const _batteryLevel = 0.35 + Math.random() * 0.6;
            const _charging = Math.random() > 0.5;
            const _chargingTime = _charging ? Math.floor(Math.random() * 7200) : Infinity;
            const _dischargingTime = !_charging ? Math.floor(Math.random() * 14400) : Infinity;

            const _batteryManager = {{
                charging: _charging,
                chargingTime: _chargingTime,
                dischargingTime: _dischargingTime,
                level: _batteryLevel,
                addEventListener: () => {{}},
                removeEventListener: () => {{}},
                onchargingchange: null,
                onchargingtimechange: null,
                ondischargingtimechange: null,
                onlevelchange: null,
            }};

            navigator.getBattery = function() {{
                return Promise.resolve(_batteryManager);
            }};
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 46. Network Information API
    // navigator.connection 返回网络信息（类型、下行速度、RTT）
    // 反爬系统检测这些值是否合理
    // ═══════════════════════════════════════════════════════════════
    try {{
        if (!navigator.connection) {{
            const _connectionTypes = ['wifi', 'ethernet', '4g'];
            const _connType = _connectionTypes[Math.floor(Math.random() * _connectionTypes.length)];
            const _effectiveTypes = ['4g', '4g', '3g'];

            Object.defineProperty(navigator, 'connection', {{
                get: () => ({{
                    effectiveType: _effectiveTypes[Math.floor(Math.random() * _effectiveTypes.length)],
                    rtt: Math.floor(50 + Math.random() * 100),
                    downlink: Math.round((5 + Math.random() * 15) * 10) / 10,
                    saveData: false,
                    type: _connType,
                    addEventListener: () => {{}},
                    removeEventListener: () => {{}},
                    onchange: null,
                }}),
                configurable: true,
            }});
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 47. Font Metrics 指纹
    // CanvasRenderingContext2D.measureText() 返回的度量值
    // 包含字体特定信息，反爬系统通过比较不同字体的度量值来检测指纹
    // ═══════════════════════════════════════════════════════════════
    try {{
        const origMeasureText = CanvasRenderingContext2D.prototype.measureText;
        const _fontMetricCache = new Map();

        CanvasRenderingContext2D.prototype.measureText = function(text) {{
            const result = origMeasureText.call(this, text);
            const ctxFont = this.font;

            // 缓存度量结果，确保同一字体+文本返回一致值
            const cacheKey = ctxFont + '|' + text;
            if (_fontMetricCache.has(cacheKey)) {{
                const cached = _fontMetricCache.get(cacheKey);
                return Object.assign(result, cached);
            }}

            // 注入微小噪声（模拟字体渲染差异）
            const noise = (Math.random() - 0.5) * 0.1;
            const metrics = {{
                width: result.width + noise,
                actualBoundingBoxLeft: (result.actualBoundingBoxLeft || 0) + noise * 0.5,
                actualBoundingBoxRight: (result.actualBoundingBoxRight || 0) + noise * 0.5,
                actualBoundingBoxAscent: (result.actualBoundingBoxAscent || 0) + noise * 0.3,
                actualBoundingBoxDescent: (result.actualBoundingBoxDescent || 0) + noise * 0.3,
                fontBoundingBoxAscent: result.fontBoundingBoxAscent || 0,
                fontBoundingBoxDescent: result.fontBoundingBoxDescent || 0,
            }};

            _fontMetricCache.set(cacheKey, metrics);
            return Object.assign(result, metrics);
        }};
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 48. WebGL2 扩展列表伪装
    // 确保 WebGL2 扩展列表与 GPU 配置匹配
    // 反爬系统通过扩展列表检测 GPU 信息是否被伪造
    // ═══════════════════════════════════════════════════════════════
    try {{
        if (window.WebGL2RenderingContext) {{
            const origGetExtensions = WebGL2RenderingContext.prototype.getSupportedExtensions;
            const _vendor = FP.webgl_vendor || '';
            const _isNVIDIA = /NVIDIA/i.test(_vendor);
            const _isIntel = /Intel/i.test(_vendor);
            const _isAMD = /AMD|ATI/i.test(_vendor);

            WebGL2RenderingContext.prototype.getSupportedExtensions = function() {{
                const exts = origGetExtensions.call(this) || [];

                // 根据 GPU 厂商调整扩展列表
                if (_isNVIDIA) {{
                    // NVIDIA 特有扩展
                    if (!exts.includes('GL_NV_primitive_restart')) exts.push('GL_NV_primitive_restart');
                }}
                if (_isIntel) {{
                    // Intel 特有扩展
                    if (!exts.includes('GL_INTEL_map_texture')) exts.push('GL_INTEL_map_texture');
                }}
                if (_isAMD) {{
                    // AMD 特有扩展
                    if (!exts.includes('GL_AMD_pinned_memory')) exts.push('GL_AMD_pinned_memory');
                }}

                return exts.sort();
            }};
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 49. Speech Synthesis API
    // speechSynthesis.getVoices() 返回语音合成声音列表
    // 反爬系统通过声音列表检测操作系统和浏览器真实性
    // ═══════════════════════════════════════════════════════════════
    try {{
        if (window.speechSynthesis) {{
            const _platform = FP.platform || 'Win32';
            const _lang = FP.language || 'en-US';

            // 根据平台生成声音列表
            let _voices = [];
            if (_platform === 'Win32') {{
                _voices = [
                    {{name: 'Microsoft David - English (United States)', lang: 'en-US', localService: true, default: true, voiceURI: 'Microsoft David - English (United States)'}},
                    {{name: 'Microsoft Zira - English (United States)', lang: 'en-US', localService: true, default: false, voiceURI: 'Microsoft Zira - English (United States)'}},
                    {{name: 'Microsoft Mark - English (United States)', lang: 'en-US', localService: true, default: false, voiceURI: 'Microsoft Mark - English (United States)'}},
                ];
            }} else if (_platform === 'MacIntel') {{
                _voices = [
                    {{name: 'Alex', lang: 'en-US', localService: true, default: true, voiceURI: 'Alex'}},
                    {{name: 'Daniel', lang: 'en-GB', localService: true, default: false, voiceURI: 'Daniel'}},
                    {{name: 'Samantha', lang: 'en-US', localService: true, default: false, voiceURI: 'Samantha'}},
                ];
            }} else {{
                _voices = [
                    {{name: 'Google US English', lang: 'en-US', localService: false, default: true, voiceURI: 'Google US English'}},
                    {{name: 'Google UK English Female', lang: 'en-GB', localService: false, default: false, voiceURI: 'Google UK English Female'}},
                ];
            }}

            // 如果语言不是英语，添加对应语言的声音
            if (_lang && !_lang.startsWith('en')) {{
                _voices.push({{
                    name: 'Google ' + _lang,
                    lang: _lang,
                    localService: false,
                    default: false,
                    voiceURI: 'Google ' + _lang,
                }});
            }}

            const origGetVoices = speechSynthesis.getVoices.bind(speechSynthesis);
            speechSynthesis.getVoices = function() {{
                const realVoices = origGetVoices();
                if (realVoices && realVoices.length > 0) {{
                    return realVoices;
                }}
                return _voices;
            }};

            // 触发 voiceschanged 事件
            setTimeout(function() {{
                try {{
                    speechSynthesis.dispatchEvent(new Event('voiceschanged'));
                }} catch(e) {{}}
            }}, 100);
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 50. performance.now() 精度控制
    // 降低时间精度防止 Spectre/Meltdown 类计时攻击
    // 同时防止反爬系统通过高精度计时检测自动化
    // Chrome 默认在跨域隔离环境下降低精度到 100μs
    // ═══════════════════════════════════════════════════════════════
    try {{
        const origNow = performance.now.bind(performance);
        let _perfOffset = 0;
        let _lastVal = origNow();

        performance.now = function() {{
            const realNow = origNow();
            // 确保时间单调递增
            if (realNow < _lastVal) _perfOffset += (_lastVal - realNow);
            _lastVal = realNow;

            // 降低精度到 100μs (0.1ms)
            const rounded = Math.round((realNow + _perfOffset) * 10) / 10;
            return rounded;
        }};

        // 同时处理 Date.now() 的精度一致性
        const origDateNow = Date.now;
        Date.now = function() {{
            return Math.floor(origDateNow.call(Date) / 1) * 1; // 保持整数精度
        }};
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 51. Geolocation API mock
    // 模拟地理位置与代理 IP 地理区域一致
    // 反爬系统通过 navigator.geolocation 检测真实位置
    // ═══════════════════════════════════════════════════════════════
    try {{
        if (navigator.geolocation) {{
            const origGetCurrentPosition = navigator.geolocation.getCurrentPosition.bind(navigator.geolocation);
            const origWatchPosition = navigator.geolocation.watchPosition.bind(navigator.geolocation);

            const _geoAccuracy = 50 + Math.floor(Math.random() * 100);
            const _geoAltitude = Math.floor(Math.random() * 200);
            const _geoHeading = Math.floor(Math.random() * 360);
            const _geoSpeed = Math.floor(Math.random() * 5);

            navigator.geolocation.getCurrentPosition = function(success, error, options) {{
                // 延迟返回（模拟 GPS 定位时间）
                const delay = 500 + Math.random() * 2000;
                setTimeout(function() {{
                    if (success) {{
                        success({{
                            coords: {{
                                latitude: GEO_LAT + (Math.random() - 0.5) * 0.01,
                                longitude: GEO_LNG + (Math.random() - 0.5) * 0.01,
                                accuracy: _geoAccuracy,
                                altitude: _geoAltitude,
                                altitudeAccuracy: _geoAccuracy * 0.5,
                                heading: _geoHeading,
                                speed: _geoSpeed,
                            }},
                            timestamp: Date.now(),
                        }});
                    }}
                }}, delay);
            }};

            navigator.geolocation.watchPosition = function(success, error, options) {{
                const watchId = Math.floor(Math.random() * 10000);
                // 定期返回位置更新
                const interval = setInterval(function() {{
                    if (success) {{
                        success({{
                            coords: {{
                                latitude: GEO_LAT + (Math.random() - 0.5) * 0.01,
                                longitude: GEO_LNG + (Math.random() - 0.5) * 0.01,
                                accuracy: _geoAccuracy,
                                altitude: _geoAltitude,
                                altitudeAccuracy: _geoAccuracy * 0.5,
                                heading: _geoHeading,
                                speed: _geoSpeed,
                            }},
                            timestamp: Date.now(),
                        }});
                    }}
                }}, 5000 + Math.random() * 10000);
                // 存储 interval 以便 clearWatch 可以清除
                navigator.geolocation._watchIntervals = navigator.geolocation._watchIntervals || {{}};
                navigator.geolocation._watchIntervals[watchId] = interval;
                return watchId;
            }};

            const origClearWatch = navigator.geolocation.clearWatch.bind(navigator.geolocation);
            navigator.geolocation.clearWatch = function(watchId) {{
                if (navigator.geolocation._watchIntervals && navigator.geolocation._watchIntervals[watchId]) {{
                    clearInterval(navigator.geolocation._watchIntervals[watchId]);
                    delete navigator.geolocation._watchIntervals[watchId];
                }}
                origClearWatch(watchId);
            }};
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 52. MediaCapabilities API
    // navigator.mediaCapabilities 返回媒体编解码能力
    // 反爬系统通过检查编解码支持来检测浏览器真实性
    // ═══════════════════════════════════════════════════════════════
    try {{
        if (!navigator.mediaCapabilities) {{
            Object.defineProperty(navigator, 'mediaCapabilities', {{
                get: () => ({{
                    decodingInfo: function(config) {{
                        const isVideo = config.video || (config.type === 'file' && config.video);
                        const isAudio = config.audio || (config.type === 'file' && config.audio);
                        return Promise.resolve({{
                            supported: true,
                            smooth: true,
                            powerEfficient: Math.random() > 0.3,
                        }});
                    }},
                    encodingInfo: function(config) {{
                        return Promise.resolve({{
                            supported: true,
                            smooth: true,
                            powerEfficient: Math.random() > 0.5,
                        }});
                    }},
                }}),
                configurable: true,
            }});
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 53. Notification API permission
    // 确保通知权限状态合理
    // ═══════════════════════════════════════════════════════════════
    try {{
        if (window.Notification) {{
            // 确保 permission 属性返回合理值
            const _validPermissions = ['default', 'granted', 'denied'];
            const _currentPermission = Notification.permission || 'default';

            // 如果当前权限值不合法，修正它
            if (!_validPermissions.includes(_currentPermission)) {{
                Object.defineProperty(Notification, 'permission', {{
                    get: () => 'default',
                    configurable: true,
                }});
            }}

            // 包装 requestPermission
            const origRequest = Notification.requestPermission;
            if (origRequest) {{
                Notification.requestPermission = function(callback) {{
                    // 大部分用户会拒绝通知
                    const result = Math.random() > 0.7 ? 'granted' : 'denied';
                    if (callback) callback(result);
                    return Promise.resolve(result);
                }};
            }}
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 54. USB / Bluetooth / Gamepad / Serial API 存在性模拟
    // 反爬系统检测这些 API 是否存在来判断浏览器环境
    // ═══════════════════════════════════════════════════════════════
    try {{
        // USB API
        if (!navigator.usb) {{
            Object.defineProperty(navigator, 'usb', {{
                get: () => ({{
                    getDevices: () => Promise.resolve([]),
                    requestDevice: () => Promise.reject(new DOMException('NotFoundError')),
                    addEventListener: () => {{}},
                    removeEventListener: () => {{}},
                    onconnect: null,
                    ondisconnect: null,
                }}),
                configurable: true,
            }});
        }}

        // Bluetooth API
        if (!navigator.bluetooth) {{
            Object.defineProperty(navigator, 'bluetooth', {{
                get: () => ({{
                    getAvailability: () => Promise.resolve(false),
                    requestDevice: () => Promise.reject(new DOMException('NotFoundError')),
                    getDevices: () => Promise.resolve([]),
                    addEventListener: () => {{}},
                    removeEventListener: () => {{}},
                    onavailabilitychanged: null,
                }}),
                configurable: true,
            }});
        }}

        // Gamepad API
        if (!navigator.getGamepads) {{
            navigator.getGamepads = function() {{
                return [null, null, null, null];
            }};
        }}

        // Serial API
        if (!navigator.serial) {{
            Object.defineProperty(navigator, 'serial', {{
                get: () => ({{
                    getPorts: () => Promise.resolve([]),
                    requestPort: () => Promise.reject(new DOMException('NotFoundError')),
                    addEventListener: () => {{}},
                    removeEventListener: () => {{}},
                    onconnect: null,
                    ondisconnect: null,
                }}),
                configurable: true,
            }});
        }}

        // HID API
        if (!navigator.hid) {{
            Object.defineProperty(navigator, 'hid', {{
                get: () => ({{
                    getDevices: () => Promise.resolve([]),
                    requestDevice: () => Promise.reject(new DOMException('NotFoundError')),
                    addEventListener: () => {{}},
                    removeEventListener: () => {{}},
                    onconnect: null,
                    ondisconnect: null,
                }}),
                configurable: true,
            }});
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 55. ResizeObserver / IntersectionObserver 行为一致性
    // 反爬系统通过观察器回调行为检测自动化环境
    // ═══════════════════════════════════════════════════════════════
    try {{
        // ResizeObserver: 确保回调有合理的延迟（真实环境有 rAF 延迟）
        if (window.ResizeObserver) {{
            const origResizeObserver = window.ResizeObserver;
            const _resizeCallbackDelays = new WeakMap();

            window.ResizeObserver = function(callback) {{
                const wrappedCallback = function(entries) {{
                    // 注入 4-16ms 延迟（模拟 requestAnimationFrame 节奏）
                    const delay = 4 + Math.floor(Math.random() * 12);
                    setTimeout(() => callback(entries), delay);
                }};
                return new origResizeObserver(wrappedCallback);
            }};
            window.ResizeObserver.prototype = origResizeObserver.prototype;
        }}

        // IntersectionObserver: 确保回调有合理的行为
        if (window.IntersectionObserver) {{
            const origIntersectionObserver = window.IntersectionObserver;

            window.IntersectionObserver = function(callback, options) {{
                const wrappedCallback = function(entries, observer) {{
                    // 确保阈值检查有合理的抖动
                    callback(entries, observer);
                }};

                const observer = new origIntersectionObserver(wrappedCallback, options);

                // 确保 thresholds 属性存在且合理
                if (!observer.thresholds) {{
                    Object.defineProperty(observer, 'thresholds', {{
                        get: () => [0],
                        configurable: true,
                    }});
                }}

                return observer;
            }};
            window.IntersectionObserver.prototype = origIntersectionObserver.prototype;
        }}

        // PerformanceObserver: 过滤可能暴露自动化的条目
        if (window.PerformanceObserver) {{
            const origPerfObserver = window.PerformanceObserver;
            window.PerformanceObserver = function(callback) {{
                const wrappedCallback = function(list, observer) {{
                    // 过滤掉可能暴露自动化的 PerformanceEntry
                    const entries = list.getEntries();
                    const filtered = entries.filter(e => {{
                        const name = (e.name || '').toLowerCase();
                        return !name.includes('puppeteer') &&
                               !name.includes('playwright') &&
                               !name.includes('devtools') &&
                               !name.includes('cdp');
                    }});
                    // 始终调用 callback；若部分条目被过滤则传递过滤后的列表
                    if (filtered.length === entries.length) {{
                        callback(list, observer);
                    }} else {{
                        callback({{getEntries: () => filtered, getEntriesByType: (t) => filtered.filter(e => e.entryType === t), getEntriesByName: (n, t) => filtered.filter(e => e.name === n && (!t || e.entryType === t))}}, observer);
                    }}
                }};
                return new origPerfObserver(wrappedCallback);
            }};
            window.PerformanceObserver.prototype = origPerfObserver.prototype;
        }}
    }} catch(e) {{}}

}})();
"""


def build_ultimate_combined(fp: dict) -> str:
    """构建终极组合隐身脚本：基础 16 维 + 极限 19 维 + 行为 8 维 + 终极 12 维 = 55 维全覆写

    [v2.19 标注] **当前生产链路零调用**（生产走 `build_ultimate_evasion_scripts`，
    本函数是其"四层合并"版本）。保留为能力储备并已加结构回归测试
    （tests/test_v219_evasion.py）——请勿当作死代码删除。

    将所有四个层的注入脚本合并为一个完整的字符串：
      1. injection_scripts.build_stealth_scripts (1-16)
      2. evasion_engine.build_evasion_scripts (17-35)
      3. behavioral_biometrics.build_biometrics_script (36-43)
      4. ultimate_evasion.build_ultimate_evasion_scripts (44-55)

    Args:
        fp: 指纹字典

    Returns:
        合并后的完整 JavaScript 注入字符串（55 维全覆写）
    """
    from .injection_scripts import build_stealth_scripts
    from .evasion_engine import build_evasion_scripts
    from .behavioral_biometrics import build_biometrics_script

    base = build_stealth_scripts(fp)
    evasion = build_evasion_scripts(fp)
    biometrics = build_biometrics_script(fp)
    ultimate = build_ultimate_evasion_scripts(fp)

    return base + "\n" + evasion + "\n" + biometrics + "\n" + ultimate


def get_ultimate_dimensions() -> list:
    """返回所有终极绕过维度的列表，用于文档和调试"""
    return [
        ("44", "WebRTC ICE Candidate 伪造", "防止 STUN 请求泄露真实 IP，过滤 srflx candidate"),
        ("45", "Battery API 一致性", "navigator.getBattery() 返回合理电池状态"),
        ("46", "Network Information API", "navigator.connection 网络类型与速度一致性"),
        ("47", "Font Metrics 指纹", "measureText() 度量值缓存与微噪声注入"),
        ("48", "WebGL2 扩展列表伪装", "扩展列表与 GPU 厂商匹配"),
        ("49", "Speech Synthesis API", "getVoices() 返回平台一致的声音列表"),
        ("50", "performance.now() 精度控制", "降低时间精度到 100μs 防止计时攻击"),
        ("51", "Geolocation API mock", "模拟地理位置与代理 IP 地理区域一致"),
        ("52", "MediaCapabilities API", "媒体编解码能力查询一致性"),
        ("53", "Notification API permission", "通知权限状态合理化"),
        ("54", "USB/Bluetooth/Gamepad/Serial API", "设备 API 存在性模拟"),
        ("55", "ResizeObserver/IntersectionObserver", "观察器回调行为一致性"),
    ]


# ── 地理坐标缓存 ──────────────────────────────────────────────
_GEO_COORDS_CACHE = {
    "US": {"lat": 37.7749, "lng": -122.4194},      # San Francisco
    "US_EAST": {"lat": 40.7128, "lng": -74.0060},   # New York
    "UK": {"lat": 51.5074, "lng": -0.1278},         # London
    "DE": {"lat": 52.5200, "lng": 13.4050},         # Berlin
    "FR": {"lat": 48.8566, "lng": 2.3522},          # Paris
    "JP": {"lat": 35.6762, "lng": 139.6503},        # Tokyo
    "SG": {"lat": 1.3521, "lng": 103.8198},         # Singapore
    "HK": {"lat": 22.3193, "lng": 114.1694},        # Hong Kong
    "KR": {"lat": 37.5665, "lng": 126.9780},        # Seoul
    "AU": {"lat": -33.8688, "lng": 151.2093},       # Sydney
    "CA": {"lat": 43.6532, "lng": -79.3832},        # Toronto
    "NL": {"lat": 52.3676, "lng": 4.9041},          # Amsterdam
}


def _get_geo_coords(geo_code: str) -> dict:
    """根据地理区域代码返回对应的坐标"""
    return _GEO_COORDS_CACHE.get(geo_code, {"lat": 37.7749, "lng": -122.4194})
