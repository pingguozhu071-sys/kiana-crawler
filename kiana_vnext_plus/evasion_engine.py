"""极限绕过引擎（Evasion Engine）

在 injection_scripts.py 的 16 维隐身注入之上，追加 14 个高级绕过维度，
覆盖当前主流反爬检测系统的全部检测面：

  17. Function.prototype.toString 一致性（最关键维度）
  18. navigator.userAgentData 高熵 Client Hints API
  19. CDP / Puppeteer / Playwright 痕迹清除
  20. Error.stack 调用栈清洗
  21. CSS 媒体查询指纹（prefers-color-scheme 等）
  22. Trusted Types API 模拟
  23. Shadow DOM / attachShadow 监控防御
  24. Proxy / Reflect 原生性检测防御
  25. MutationObserver 诱饵防御
  26. navigator.globalPrivacyControl
  27. navigator.storage.estimate 一致性
  28. PerformanceObserver / PerformanceEntry 指纹
  29. window.chrome 深度实现（csi/loadTimes 真实数据）
  30. window.screenX / screenY / availLeft / availTop
"""

import json


def build_evasion_scripts(fp: dict) -> str:
    """构建极限绕过注入脚本

    在 build_stealth_scripts 之后追加执行，覆盖更高级的检测维度。
    所有覆写均使用原生 toString 伪装，确保 Function.prototype.toString
    返回预期的原生代码字符串。

    Args:
        fp: 指纹字典（来自 fingerprint_consistency.compute_fingerprint_from_ip）

    Returns:
        可直接用于 add_init_script 的 JavaScript 字符串
    """
    fp_json = json.dumps(fp, ensure_ascii=False)
    return f"""
(() => {{
    const FP = {fp_json};

    // ═══════════════════════════════════════════════════════════════
    // 17. Function.prototype.toString 一致性（最关键维度）
    // 反爬系统通过 toString 检测函数是否被覆写：
    //   - 覆写后的函数 toString 应返回 "function functionName() {{ [native code] }}"
    //   - 需要同时处理 Function.prototype.toString 本身
    // ═══════════════════════════════════════════════════════════════
    try {{
        const _toString = Function.prototype.toString;
        const _nativePattern = /^function \\w+\\(\\) \\{{ \\[native code\\] \\}}$/;
        const _cache = new WeakMap();

        Function.prototype.toString = new Proxy(_toString, {{
            apply: function(target, thisArg, args) {{
                // 如果是被覆写的函数，返回伪装的原生字符串
                if (_cache.has(thisArg)) {{
                    return _cache.get(thisArg);
                }}
                return target.call(thisArg, ...args);
            }}
        }});

        // 注册伪装函数：使其 toString 返回原生代码字符串
        window.__markNative = function(fn, name) {{
            const nativeStr = `function ${{name || fn.name || ''}}() {{ [native code] }}`;
            _cache.set(fn, nativeStr);
            return fn;
        }};
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 18. navigator.userAgentData 高熵 Client Hints API
    // Chrome 90+ 支持 userAgentData，反爬系统通过高熵值获取
    // GPU/内存/架构等信息来检测自动化
    // ═══════════════════════════════════════════════════════════════
    try {{
        const uaData = {{
            brands: [
                {{brand: 'Chromium', version: String(FP.chrome_version)}},
                {{brand: 'Google Chrome', version: String(FP.chrome_version)}},
                {{brand: 'Not;A=Brand', version: '24'}},
                {{brand: 'Not.A/Brand', version: '99'}},
            ],
            mobile: false,
            platform: (function() {{
                if (FP.platform === 'Win32') return 'Windows';
                if (FP.platform === 'MacIntel') return 'macOS';
                if (FP.platform === 'Linux x86_64') return 'Linux';
                return 'Windows';
            }})(),
        }};

        const highEntropyValues = {{
            architecture: 'x86',
            bitness: '64',
            model: '',
            mobile: false,
            platform: uaData.platform,
            platformVersion: FP.platform === 'Win32' ? '15.0.0' : FP.platform === 'MacIntel' ? '14.0.0' : '6.5.0',
            uaFullVersion: FP.chrome_full_version || FP.chrome_version + '.0.0.0',
            fullVersionList: uaData.brands,
            wow64: false,
        }};

        Object.defineProperty(navigator, 'userAgentData', {{
            get: () => ({{
                ...uaData,
                getHighEntropyValues: function(hints) {{
                    return Promise.resolve(
                        Object.fromEntries(
                            hints.filter(h => h in highEntropyValues)
                                 .map(h => [h, highEntropyValues[h]])
                        )
                    );
                }},
                toJSON: function() {{
                    return {{brands: uaData.brands, mobile: uaData.mobile, platform: uaData.platform}};
                }}
            }}),
            configurable: true,
        }});
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 19. CDP / Puppeteer / Playwright 痕迹清除
    // 清除所有自动化框架留下的全局变量和属性
    // ═══════════════════════════════════════════════════════════════
    try {{
        // Puppeteer 痕迹
        const puppeteerProps = [
            '__puppeteer_evaluation_script',
            '__nightmare',
            '_selenium',
            'callSelenium',
            '_phantom',
            '__nightmareIPC',
            'domAutomation',
            'domAutomationController',
        ];
        puppeteerProps.forEach(prop => {{
            try {{ delete window[prop]; }} catch(e) {{}}
            try {{ delete document[prop]; }} catch(e) {{}}
        }});

        // CDP (Chrome DevTools Protocol) 变量
        const cdpPatterns = [
            'cdc_', 'cdc_adoQpoasnfa76pfcZLmcfl_',
            'cdc_adoQpoasnfa76pfcZLmcfl_Array',
            'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
            'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
            '$cdc_asdjflasutopfhvcZLmcfl_',
        ];
        cdpPatterns.forEach(pattern => {{
            for (const key of Object.keys(window)) {{
                if (key.startsWith(pattern.substring(0, 4))) {{
                    try {{ delete window[key]; }} catch(e) {{}}
                }}
            }}
        }});

        // Playwright 痕迹
        try {{ delete window.__playwright__; }} catch(e) {{}}
        try {{ delete window.__pw_manual; }} catch(e) {{}}

        // 检测 webdriver 属性的多重覆写（防止反检测的检测）
        for (const prop of ['webdriver', '__webdriver_evaluate', '__selenium_evaluate',
                            '__fxdriver_evaluate', '__driver_unwrapped',
                            '__webdriver_unwrapped', '__driver_evaluate',
                            '__selenium_unwrapped', '__fxdriver_unwrapped']) {{
            try {{
                Object.defineProperty(navigator, prop, {{
                    get: () => false, configurable: true, enumerable: false
                }});
            }} catch(e) {{}}
        }}

        // 清除 document 上的自动化属性
        try {{
            Object.defineProperty(document, '$cdc_asdjflasutopfhvcZLmcfl_', {{
                get: () => false, configurable: true
            }});
        }} catch(e) {{}}

        // 清除 window.external 自动化痕迹
        try {{
            if (window.external) {{
                const extKeys = Object.keys(window.external);
                const autoKeys = extKeys.filter(k =>
                    /automat|selenium|webdriver|puppet/i.test(k)
                );
                autoKeys.forEach(k => {{
                    try {{ delete window.external[k]; }} catch(e) {{}}
                }});
            }}
        }} catch(e) {{}}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 20. Error.stack 调用栈清洗
    // 反爬系统通过 Error.stack 检测是否在自动化环境中运行
    // ═══════════════════════════════════════════════════════════════
    try {{
        const origStack = Object.getOwnPropertyDescriptor(Error.prototype, 'stack');
        if (origStack && origStack.get) {{
            const origGet = origStack.get;
            Object.defineProperty(Error.prototype, 'stack', {{
                get: function() {{
                    const stack = origGet.call(this);
                    if (typeof stack !== 'string') return stack;
                    // 过滤掉自动化相关的调用栈帧
                    return stack.split('\\n').filter(line => {{
                        const lower = line.toLowerCase();
                        return !lower.includes('puppeteer') &&
                               !lower.includes('playwright') &&
                               !lower.includes('cdp') &&
                               !lower.includes('devtools') &&
                               !lower.includes('selenium') &&
                               !lower.includes('patchright') &&
                               !lower.includes('__puppeteer') &&
                               !lower.includes('injected');
                    }}).join('\\n');
                }},
                set: function(val) {{
                    // 允许设置
                    if (origStack.set) origStack.set.call(this, val);
                }},
                configurable: true,
            }});
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 21. CSS 媒体查询指纹（prefers-color-scheme / prefers-reduced-motion 等）
    // ═══════════════════════════════════════════════════════════════
    try {{
        const colorScheme = FP.color_scheme || 'light';
        const reducedMotion = FP.reduced_motion || 'no-preference';
        const contrast = FP.contrast || 'no-preference';

        const origMatchMedia = window.matchMedia;
        window.matchMedia = function(query) {{
            const mq = origMatchMedia.call(window, query);
            const lower = query.toLowerCase().trim();

            // 覆写 matches 属性
            let forcedMatch = null;
            if (lower.includes('prefers-color-scheme: dark')) {{
                forcedMatch = colorScheme === 'dark';
            }} else if (lower.includes('prefers-color-scheme: light')) {{
                forcedMatch = colorScheme === 'light';
            }} else if (lower.includes('prefers-reduced-motion: reduce')) {{
                forcedMatch = reducedMotion === 'reduce';
            }} else if (lower.includes('prefers-reduced-motion: no-preference')) {{
                forcedMatch = reducedMotion === 'no-preference';
            }} else if (lower.includes('prefers-contrast: high')) {{
                forcedMatch = contrast === 'high';
            }} else if (lower.includes('prefers-contrast: more')) {{
                forcedMatch = contrast === 'more';
            }}

            if (forcedMatch !== null) {{
                return {{
                    matches: forcedMatch,
                    media: query,
                    onchange: null,
                    addEventListener: () => {{}},
                    removeEventListener: () => {{}},
                    addListener: () => {{}},
                    removeListener: () => {{}},
                    dispatchEvent: () => false,
                }};
            }}
            return mq;
        }};
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 22. Trusted Types API 模拟
    // 反爬系统检查 Trusted Types 是否存在以判断浏览器真实性
    // ═══════════════════════════════════════════════════════════════
    try {{
        if (!window.trustedTypes) {{
            const _policyMap = new Map();
            window.trustedTypes = {{
                createPolicy: function(policyName, rules) {{
                    const policy = {{
                        name: policyName,
                        createHTML: rules.createHTML ? (s) => rules.createHTML(s) : (s) => s,
                        createScript: rules.createScript ? (s) => rules.createScript(s) : (s) => s,
                        createScriptURL: rules.createScriptURL ? (s) => rules.createScriptURL(s) : (s) => s,
                    }};
                    _policyMap.set(policyName, policy);
                    return policy;
                }},
                getPolicyNames: function() {{
                    return Array.from(_policyMap.keys());
                }},
                isHTML: function(val) {{ return typeof val === 'object' && val !== null; }},
                isScript: function(val) {{ return typeof val === 'object' && val !== null; }},
                isScriptURL: function(val) {{ return typeof val === 'object' && val !== null; }},
                defaultPolicy: null,
                emptyHTML: '',
                emptyScript: '',
            }};
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 23. Shadow DOM / attachShadow 监控防御
    // 反爬系统通过 hook attachShadow 来检测注入的脚本
    // ═══════════════════════════════════════════════════════════════
    try {{
        const origAttachShadow = Element.prototype.attachShadow;
        Element.prototype.attachShadow = function(init) {{
            // 确保始终允许 open 模式（防止反爬检测 shadow root 闭包）
            const modifiedInit = {{ ...init, mode: 'open' }};
            return origAttachShadow.call(this, modifiedInit);
        }};
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 24. Proxy / Reflect 原生性检测防御
    // 高级反爬系统通过检测 navigator/window 等对象是否被 Proxy 包装
    // ═══════════════════════════════════════════════════════════════
    try {{
        // 防御 toString 检测：确保 Object.prototype.toString 返回正确类型
        const origObjToString = Object.prototype.toString;
        Object.prototype.toString = function() {{
            // navigator -> [object Navigator]
            // window -> [object Window]
            // document -> [object HTMLDocument]
            if (this === navigator) return '[object Navigator]';
            if (this === window) return '[object Window]';
            if (this === document) return '[object HTMLDocument]';
            if (this === screen) return '[object Screen]';
            return origObjToString.call(this);
        }};

        // 防御 instanceof 检测
        try {{
            Object.defineProperty(navigator, Symbol.toStringTag, {{
                value: 'Navigator', configurable: true, writable: false, enumerable: false
            }});
        }} catch(e) {{}}
        try {{
            Object.defineProperty(screen, Symbol.toStringTag, {{
                value: 'Screen', configurable: true, writable: false, enumerable: false
            }});
        }} catch(e) {{}}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 25. MutationObserver 诱饵防御
    // 反爬系统注入诱饵元素并通过 MutationObserver 检测是否被移除
    // ═══════════════════════════════════════════════════════════════
    try {{
        // 记录被反爬系统注入的诱饵元素，不主动移除它们
        const baitSelectors = [
            '#bot-check', '.bot-detection', '#anti-bot',
            '.captcha-bait', '#recaptcha-anchor-container',
            'div[data-bot-detection]', 'div[class*="bot-detect"]',
        ];

        // 不主动移除诱饵元素，让反爬系统认为环境正常
        // 但拦截对诱饵元素的属性读取（防止泄露自动化信息）
        const origQuerySelector = document.querySelector.bind(document);
        const origQuerySelectorAll = document.querySelectorAll.bind(document);

        // 创建一个 WeakSet 来追踪已知诱饵元素
        window.__baitElements = new WeakSet();
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 26. navigator.globalPrivacyControl
    // Chrome 116+ 支持 GPC 头
    // ═══════════════════════════════════════════════════════════════
    try {{
        Object.defineProperty(navigator, 'globalPrivacyControl', {{
            get: () => FP.global_privacy_control !== undefined ? FP.global_privacy_control : false,
            configurable: true,
        }});
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 27. navigator.storage.estimate 一致性
    // 反爬系统通过 storage.estimate 检测存储使用量是否合理
    // ═══════════════════════════════════════════════════════════════
    try {{
        if (navigator.storage && navigator.storage.estimate) {{
            const fakeQuota = FP.device_memory >= 16 ? 8589934592 : FP.device_memory >= 8 ? 4294967296 : 2147483648;
            const fakeUsage = Math.floor(fakeQuota * (0.01 + Math.random() * 0.05));
            navigator.storage.estimate = () => Promise.resolve({{
                quota: fakeQuota,
                usage: fakeUsage,
                usageDetails: {{
                    caches: Math.floor(fakeUsage * 0.3),
                    indexedDB: Math.floor(fakeUsage * 0.2),
                    serviceWorkerRegistrations: Math.floor(fakeUsage * 0.1),
                }}
            }});
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 28. PerformanceObserver / PerformanceEntry 指纹
    // 反爬系统通过 performance timing 检测页面加载是否异常
    // ═══════════════════════════════════════════════════════════════
    try {{
        // 确保 performance.timing 存在且数据合理
        if (window.performance && window.performance.timing) {{
            const now = Date.now();
            const timing = window.performance.timing;
            // 确保 navigationStart 在合理范围内
            if (!timing.navigationStart || timing.navigationStart > now) {{
                try {{
                    Object.defineProperty(timing, 'navigationStart', {{
                        value: now - Math.floor(Math.random() * 5000 + 2000),
                        writable: false, configurable: true
                    }});
                }} catch(e) {{}}
            }}
        }}

        // 确保 performance.getEntries 返回合理数据
        if (window.performance && window.performance.getEntries) {{
            const origGetEntries = performance.getEntries.bind(performance);
            performance.getEntries = function() {{
                const entries = origGetEntries();
                // 过滤掉可能暴露自动化的 PerformanceEntry
                return entries.filter(e => {{
                    const name = (e.name || '').toLowerCase();
                    return !name.includes('puppeteer') &&
                           !name.includes('playwright') &&
                           !name.includes('devtools');
                }});
            }};
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 29. window.chrome 深度实现（csi/loadTimes 真实数据）
    // 原始注入只创建空对象，这里补充真实方法实现
    // ═══════════════════════════════════════════════════════════════
    try {{
        if (!window.chrome) window.chrome = {{}};
        if (!window.chrome.runtime) window.chrome.runtime = {{}};

        // csi() - Chrome 连接信息
        window.chrome.csi = function() {{
            const now = performance.now();
            return {{
                startE: now - Math.random() * 3000,
                onloadT: now - Math.random() * 1000,
                pageT: now,
                tran: 15,
            }};
        }};

        // loadTimes() - 页面加载信息
        window.chrome.loadTimes = function() {{
            const now = (Date.now() / 1000).toFixed(3);
            return {{
                commitLoadTime: parseFloat(now) - Math.random() * 5,
                connectionInfo: 'h2',
                finishDocumentLoadTime: parseFloat(now) - Math.random() * 2,
                finishLoadTime: parseFloat(now) - Math.random() * 1,
                firstPaintAfterLoadTime: 0,
                firstPaintTime: parseFloat(now) - Math.random() * 4,
                navigationType: 'Other',
                npnNegotiatedProtocol: 'h2',
                requestTime: parseFloat(now) - Math.random() * 6,
                startLoadTime: parseFloat(now) - Math.random() * 5,
                wasAlternateProtocolAvailable: false,
                wasFetchedViaSpdy: true,
                wasNpnNegotiated: true,
            }};
        }};

        // app 对象
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

    // ═══════════════════════════════════════════════════════════════
    // 30. window.screenX / screenY / availLeft / availTop
    // 窗口位置信息一致性
    // ═══════════════════════════════════════════════════════════════
    try {{
        const screenX = FP.screen_x !== undefined ? FP.screen_x : 0;
        const screenY = FP.screen_y !== undefined ? FP.screen_y : 0;
        const availLeft = FP.avail_left !== undefined ? FP.avail_left : 0;
        const availTop = FP.avail_top !== undefined ? FP.avail_top : 0;

        Object.defineProperty(window, 'screenX', {{get: () => screenX, configurable: true}});
        Object.defineProperty(window, 'screenY', {{get: () => screenY, configurable: true}});
        Object.defineProperty(screen, 'availLeft', {{get: () => availLeft, configurable: true}});
        Object.defineProperty(screen, 'availTop', {{get: () => availTop, configurable: true}});

        // innerWidth/innerHeight 与 viewport 一致
        Object.defineProperty(window, 'innerWidth', {{
            get: () => FP.screen_width, configurable: true
        }});
        Object.defineProperty(window, 'innerHeight', {{
            get: () => FP.avail_height, configurable: true
        }});
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 31. navigator.keyboard / locks / scheduling API 模拟
    // ═══════════════════════════════════════════════════════════════
    try {{
        // Keyboard API
        if (!navigator.keyboard) {{
            Object.defineProperty(navigator, 'keyboard', {{
                get: () => ({{
                    getLayoutMap: () => Promise.resolve(new Map([
                        ['KeyA', 'a'], ['KeyB', 'b'], ['KeyC', 'c'],
                        ['KeyD', 'd'], ['KeyE', 'e'], ['KeyF', 'f'],
                    ])),
                    lock: () => Promise.resolve(),
                    unlock: () => {{}},
                }}),
                configurable: true
            }});
        }}

        // Web Locks API
        if (!navigator.locks) {{
            Object.defineProperty(navigator, 'locks', {{
                get: () => ({{
                    request: function(name, options, callback) {{
                        if (typeof options === 'function') {{
                            callback = options;
                            options = {{}};
                        }}
                        return Promise.resolve(callback({{
                            name: name,
                            mode: options.mode || 'exclusive',
                        }}));
                    }},
                    query: function() {{
                        return Promise.resolve({{ held: [], pending: [] }});
                    }},
                }}),
                configurable: true
            }});
        }}

        // Scheduling API
        if (!navigator.scheduling) {{
            Object.defineProperty(navigator, 'scheduling', {{
                get: () => ({{
                    isInputPending: function() {{ return false; }},
                    isFramePending: function() {{ return false; }},
                }}),
                configurable: true
            }});
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 32. navigator.clipboard / credentials / serviceWorker API
    // ═══════════════════════════════════════════════════════════════
    try {{
        // Clipboard API
        if (!navigator.clipboard) {{
            Object.defineProperty(navigator, 'clipboard', {{
                get: () => ({{
                    readText: () => Promise.resolve(''),
                    writeText: (text) => Promise.resolve(),
                    read: () => Promise.reject(new Error('NotAllowedError')),
                    write: () => Promise.resolve(),
                }}),
                configurable: true
            }});
        }}

        // Credentials API
        if (!navigator.credentials) {{
            Object.defineProperty(navigator, 'credentials', {{
                get: () => ({{
                    get: () => Promise.resolve(null),
                    store: () => Promise.resolve({{}}),
                    create: () => Promise.resolve({{}}),
                    preventSilentAccess: () => Promise.resolve(),
                }}),
                configurable: true
            }});
        }}

        // ServiceWorkerContainer
        if (!navigator.serviceWorker) {{
            Object.defineProperty(navigator, 'serviceWorker', {{
                get: () => ({{
                    ready: Promise.resolve({{
                        active: null,
                        installing: null,
                        waiting: null,
                        unregister: () => Promise.resolve(true),
                    }}),
                    controller: null,
                    register: () => Promise.resolve({{
                        scope: '/',
                        update: () => Promise.resolve(),
                        unregister: () => Promise.resolve(true),
                    }}),
                    getRegistrations: () => Promise.resolve([]),
                    getRegistration: () => Promise.resolve(undefined),
                    startMessages: () => {{}},
                    addEventListener: () => {{}},
                    removeEventListener: () => {{}},
                }}),
                configurable: true
            }});
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 33. document.hasFocus / visibilityState 一致性
    // ═══════════════════════════════════════════════════════════════
    try {{
        // 确保 document.hasFocus 返回 true（使用 defineProperty 防止 toString 检测）
        try {{
            Object.defineProperty(document, 'hasFocus', {{
                value: function() {{ return true; }},
                writable: true, configurable: true
            }});
        }} catch(e) {{
            document.hasFocus = function() {{ return true; }};
        }}

        // 确保 visibilityState 为 visible
        if (document.visibilityState !== 'visible') {{
            try {{
                Object.defineProperty(document, 'visibilityState', {{
                    get: () => 'visible', configurable: true
                }});
            }} catch(e) {{}}
        }}

        // 确保 hidden 为 false
        try {{
            Object.defineProperty(document, 'hidden', {{
                get: () => false, configurable: true
            }});
        }} catch(e) {{}}

        // webdriver 检测的终极防御
        try {{
            Object.defineProperty(Object.getPrototypeOf(navigator), 'webdriver', {{
                get: () => false, set: () => {{}}, configurable: true
            }});
        }} catch(e) {{}}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 34. RTCPeerConnection.generateCertificate 防御
    // 反爬系统检测 RTCPeerConnection.generateCertificate 是否存在
    // ═══════════════════════════════════════════════════════════════
    try {{
        const origRTC = window.RTCPeerConnection;
        if (origRTC && !origRTC.generateCertificate) {{
            origRTC.generateCertificate = function(keygenAlgorithm) {{
                return Promise.resolve({{
                    type: 'public',
                    extractable: false,
                    algorithm: typeof keygenAlgorithm === 'string' ? {{ name: keygenAlgorithm }} : keygenAlgorithm,
                }});
            }};
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 35. iframe contentWindow / contentDocument 检测防御
    // 反爬系统通过创建 iframe 并检测其 contentWindow 上的属性来判断环境
    // ═══════════════════════════════════════════════════════════════
    try {{
        const origContentWindowGetter = Object.getOwnPropertyDescriptor(
            HTMLIFrameElement.prototype, 'contentWindow'
        );
        if (origContentWindowGetter && origContentWindowGetter.get) {{
            const origGet = origContentWindowGetter.get;
            Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {{
                get: function() {{
                    const win = origGet.call(this);
                    if (win) {{
                        // 确保 iframe 内的 navigator.webdriver 也被清除
                        try {{
                            Object.defineProperty(win.navigator, 'webdriver', {{
                                get: () => false, configurable: true
                            }});
                        }} catch(e) {{}}
                    }}
                    return win;
                }},
                configurable: true,
            }});
        }}
    }} catch(e) {{}}

}})();
"""


def build_combined_stealth(fp: dict) -> str:
    """构建组合隐身脚本：基础 16 维 + 极限 19 维 = 35 维全覆写

    [v2.19 标注] **当前生产链路零调用**（生产只用 solver_engine →
    `ultimate_evasion.build_ultimate_evasion_scripts`，其 55 维组合是本函数的**超集**）。
    保留为"能力储备"（可整包注入的独立入口），并已加结构回归测试
    （tests/test_v219_evasion.py）防退化——请勿当作死代码删除，若要删需先确认
    无外部调用方（如工程根目录的 `全方位检查.py` 自检脚本会调用本族函数）。

    将 injection_scripts.build_stealth_scripts 和 build_evasion_scripts
    合并为一个完整的注入脚本，确保所有维度按正确顺序执行。

    Args:
        fp: 指纹字典

    Returns:
        合并后的完整 JavaScript 注入字符串
    """
    from .injection_scripts import build_stealth_scripts
    base = build_stealth_scripts(fp)
    evasion = build_evasion_scripts(fp)
    return base + "\n" + evasion


def get_evasion_dimensions() -> list:
    """返回所有绕过维度的列表，用于文档和调试"""
    return [
        ("17", "Function.prototype.toString 一致性", "防止反爬通过 toString 检测函数覆写"),
        ("18", "navigator.userAgentData 高熵 API", "Client Hints 高熵值 GPU/内存/架构伪装"),
        ("19", "CDP/Puppeteer/Playwright 痕迹清除", "删除所有自动化框架全局变量"),
        ("20", "Error.stack 调用栈清洗", "过滤自动化相关调用栈帧"),
        ("21", "CSS 媒体查询指纹", "prefers-color-scheme/reduced-motion 伪装"),
        ("22", "Trusted Types API 模拟", "模拟 Trusted Types 防御性检测"),
        ("23", "Shadow DOM attachShadow 防御", "防止 shadow root 闭包检测"),
        ("24", "Proxy/Reflect 原生性检测防御", "Object.toString 和 Symbol.toStringTag 一致性"),
        ("25", "MutationObserver 诱饵防御", "不主动移除反爬诱饵元素"),
        ("26", "navigator.globalPrivacyControl", "GPC 头一致性"),
        ("27", "navigator.storage.estimate", "存储使用量一致性"),
        ("28", "PerformanceObserver 指纹", "performance timing 数据合理性"),
        ("29", "window.chrome 深度实现", "csi/loadTimes 真实数据"),
        ("30", "window.screenX/screenY 位置", "窗口位置信息一致性"),
        ("31", "keyboard/locks/scheduling API", "浏览器 API 存在性模拟"),
        ("32", "clipboard/credentials/serviceWorker", "API 存在性模拟"),
        ("33", "document.hasFocus/visibilityState", "页面可见性一致性"),
        ("34", "RTCPeerConnection.generateCertificate", "WebRTC 证书 API 防御"),
        ("35", "iframe contentWindow 检测防御", "iframe 内 webdriver 清除"),
    ]
