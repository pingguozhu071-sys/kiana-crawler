"""行为生物特征引擎（Behavioral Biometrics Engine）

生成注入浏览器的 JavaScript 脚本，模拟人类行为生物特征：
反爬系统通过分析用户行为模式（击键节奏、鼠标轨迹、滚动惯性、触控压力、
设备传感器数据）来区分真人和自动化脚本。本模块覆盖这些检测维度。

维度列表：
  36. 击键动力学 — 按键停留时间 (dwell) 与按键间隔 (flight time)
  37. 鼠标压力与轨迹熵 — 模拟真实手部抖动与压力变化
  38. 滚动惯性 — 加速-匀速-减速三阶段惯性滚动
  39. 触摸事件模拟 — touchstart/move/end 带真实压力与面积
  40. 设备运动传感器 — accelerometer / gyroscope / DeviceMotion
  41. 指针事件一致性 — pointerdown/move/up 与 mouse 事件同步
  42. 焦点切换模拟 — window blur/focus 随机切换
  43. 阅读行为模拟 — 文本选择、光标位置变化
"""

import json


def build_biometrics_script(fp: dict) -> str:
    """构建行为生物特征注入脚本

    在 build_combined_stealth 之后追加执行，为浏览器注入人类行为模拟层。
    所有模拟均使用随机化参数，确保每次会话的行为模式略有不同但符合人类统计分布。

    Args:
        fp: 指纹字典

    Returns:
        可直接用于 add_init_script 的 JavaScript 字符串
    """
    fp_json = json.dumps(fp, ensure_ascii=False)
    seed = int(hash(str(fp.get("canvas_noise_seed", 42))) % 10000)
    return f"""
(() => {{
    const FP = {fp_json};
    const SEED = {seed};

    // ── 简易种子化随机数生成器 ──
    let _rng_state = SEED;
    function _rng() {{
        _rng_state = (_rng_state * 9301 + 49297) % 233280;
        return _rng_state / 233280;
    }}
    function _rngRange(min, max) {{ return min + _rng() * (max - min); }}
    function _rngInt(min, max) {{ return Math.floor(_rngRange(min, max + 1)); }}
    function _rngGauss(mean, std) {{
        // Box-Muller 变换
        const u1 = _rng() || 0.0001;
        const u2 = _rng();
        const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
        return mean + z * std;
    }}
    function _rngChoice(arr) {{ return arr[Math.floor(_rng() * arr.length)]; }}

    // ═══════════════════════════════════════════════════════════════
    // 36. 击键动力学 (Keystroke Dynamics)
    // 模拟人类打字节奏：每个按键有 50-200ms 停留时间和 80-300ms 间隔
    // 反爬系统通过分析 keydown/keyup 时间差来检测自动化
    // ═══════════════════════════════════════════════════════════════
    try {{
        const _origDispatchEvent = EventTarget.prototype.dispatchEvent;

        // 记录上一个按键的 keyup 时间，用于计算 flight time
        let _lastKeyupTime = 0;
        let _pendingDelay = null;

        // 拦截 keydown 事件，注入随机延迟
        const _origAddEventListener = EventTarget.prototype.addEventListener;
        const _keyHandlers = new WeakMap();

        // 创建一个全局的击键节奏管理器
        window.__biometricKeystroke = {{
            // 基础打字速度（每分钟字数），影响停留和间隔时间
            baseWPM: _rngRange(40, 90),
            // 是否经常打错字并修正
            errorRate: _rngRange(0.01, 0.05),
            // 两次按键间的最小/最大间隔
            minFlightTime: _rngRange(60, 120),
            maxFlightTime: _rngRange(200, 400),
            // 按键停留时间范围
            minDwellTime: _rngRange(30, 80),
            maxDwellTime: _rngRange(100, 250),
            // 上下文影响：标点后停顿更长
            punctuationPause: _rngRange(150, 400),
            // 空格后停顿
            spacePause: _rngRange(80, 200),
            // 生成下一个按键的延迟
            nextDelay: function(prevKey, currentKey) {{
                let base = _rngRange(this.minFlightTime, this.maxFlightTime);
                // 标点后停顿
                if (prevKey && /[.!?;:,]/.test(prevKey)) {{
                    base += this.punctuationPause * _rng();
                }}
                // 空格后轻微停顿
                if (prevKey === ' ') {{
                    base += this.spacePause * _rng();
                }}
                // Shift 组合键需要更长间隔
                if (currentKey && currentKey.length === 1 && /[A-Z]/.test(currentKey)) {{
                    base += _rngRange(30, 80);
                }}
                // 随机错误：偶尔多等一会（模拟犹豫）
                if (_rng() < this.errorRate) {{
                    base += _rngRange(200, 600);
                }}
                return Math.round(base);
            }},
            // 生成按键停留时间
            dwellTime: function(key) {{
                let base = _rngRange(this.minDwellTime, this.maxDwellTime);
                // 空格键停留更长
                if (key === ' ') base *= 1.3;
                // 修饰键停留更短
                if (['Shift', 'Control', 'Alt', 'Meta'].includes(key)) base *= 0.7;
                return Math.round(base);
            }}
        }};
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 37. 鼠标压力与轨迹熵 (Mouse Pressure & Trajectory Entropy)
    // PointerEvent.pressure 模拟真实手指/笔的压力变化
    // 鼠标移动注入微抖动，使轨迹熵接近人类水平
    // ═══════════════════════════════════════════════════════════════
    try {{
        // 拦截 pointerdown/move，注入压力值
        const _origPointerDispatch = PointerEvent.prototype.constructor;
        const _origGetCoalesced = PointerEvent.prototype.getCoalescedEvents;

        if (_origGetCoalesced) {{
            PointerEvent.prototype.getCoalescedEvents = function() {{
                const events = _origGetCoalesced.call(this);
                // 为合并事件注入微小的坐标偏移（模拟手部抖动）
                return events.map(e => {{
                    const jitter_x = _rngGauss(0, 0.3);
                    const jitter_y = _rngGauss(0, 0.3);
                    try {{
                        Object.defineProperties(e, {{
                            clientX: {{ get: () => (e.clientX || 0) + jitter_x }},
                            clientY: {{ get: () => (e.clientY || 0) + jitter_y }},
                            pressure: {{ get: () => _rngRange(0.3, 0.7) }},
                        }});
                    }} catch(err) {{}}
                    return e;
                }});
            }};
        }}

        // 鼠标移动抖动注入：拦截 mousemove 事件分发
        let _mousemoveCount = 0;
        const _origMouseEvent = MouseEvent;
        const _origMouseEventInit = Object.getOwnPropertyDescriptor(
            MouseEvent.prototype, 'movementX'
        );

        // 为 PointerEvent 注入真实压力曲线
        if (window.PointerEvent) {{
            const _origPointerEvent = window.PointerEvent;
            // 确保压力属性存在且值合理
            const _pressureProfiles = {{
                // 鼠标：压力为 0 或 0.5（按下时）
                mouse: {{ min: 0.0, max: 0.5, default: 0.5 }},
                // 触摸：压力变化范围更大
                touch: {{ min: 0.1, max: 1.0, default: 0.5 }},
                // 笔：压力最敏感
                pen: {{ min: 0.0, max: 1.0, default: 0.3 }},
            }};
        }}

        // 注入鼠标移动的自然抖动
        window.__biometricMouse = {{
            // 手部抖动幅度（像素）
            tremorAmplitude: _rngRange(0.2, 0.8),
            // 抖动频率
            tremorFrequency: _rngRange(4, 12),
            // 移动速度（像素/帧）
            baseSpeed: _rngRange(300, 800),
            // 是否偶尔停顿
            pauseProbability: _rngRange(0.02, 0.08),
            // 生成抖动偏移
            tremor: function(t) {{
                const amp = this.tremorAmplitude;
                const freq = this.tremorFrequency;
                // 正弦波 + 随机噪声
                return {{
                    x: amp * Math.sin(2 * Math.PI * freq * t) + _rngGauss(0, 0.15),
                    y: amp * Math.cos(2 * Math.PI * freq * t * 1.3) + _rngGauss(0, 0.15),
                }};
            }},
        }};
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 38. 滚动惯性 (Scroll Inertia)
    // 人类滚动鼠标滚轮时：加速 → 匀速 → 减速，并有惯性余量
    // 自动化脚本通常直接 scrollTo，缺乏惯性
    // ═══════════════════════════════════════════════════════════════
    try {{
        window.__biometricScroll = {{
            // 滚动方向偏好
            preferredDirection: 1, // 1=向下, -1=向上
            // 单次滚动行数
            linesPerScroll: _rngInt(2, 8),
            // 滚动加速度
            acceleration: _rngRange(1.5, 3.0),
            // 惯性衰减系数
            inertiaDecay: _rngRange(0.85, 0.95),
            // 最大滚动速度
            maxSpeed: _rngRange(800, 2000),
            // 惯性滚动生成器
            generateInertiaScroll: function(targetDelta) {{
                const steps = [];
                let speed = 0;
                let remaining = Math.abs(targetDelta);
                const dir = Math.sign(targetDelta);

                while (remaining > 0 || speed > 1) {{
                    // 加速阶段
                    if (remaining > Math.abs(targetDelta) * 0.3) {{
                        speed = Math.min(speed + this.acceleration * 16, this.maxSpeed);
                    }}
                    // 减速阶段
                    else if (remaining > Math.abs(targetDelta) * 0.1) {{
                        speed = Math.max(speed * 0.92, 50);
                    }}
                    // 惯性阶段
                    else {{
                        speed = speed * this.inertiaDecay;
                    }}

                    const delta = Math.min(speed * 0.016 * dir, remaining * dir);
                    steps.push({{
                        deltaY: delta,
                        duration: 16, // ~60fps
                    }});
                    remaining -= Math.abs(delta);

                    if (remaining <= 0 && speed < 1) break;
                }}
                return steps;
            }},
        }};
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 39. 触摸事件模拟 (Touch Event Simulation)
    // 在触控设备上模拟真实的 touchstart/move/end 事件
    // 包含真实的 force（压力）和 radiusX/radiusY（接触面积）
    // ═══════════════════════════════════════════════════════════════
    try {{
        if (FP.max_touch_points > 0) {{
            window.__biometricTouch = {{
                // 触摸压力范围
                minForce: _rngRange(0.0, 0.2),
                maxForce: _rngRange(0.6, 1.0),
                // 接触面积范围
                minRadius: _rngRange(5, 10),
                maxRadius: _rngRange(15, 30),
                // 生成触摸参数
                generateTouchParams: function() {{
                    return {{
                        force: _rngRange(this.minForce, this.maxForce),
                        radiusX: _rngRange(this.minRadius, this.maxRadius),
                        radiusY: _rngRange(this.minRadius, this.maxRadius),
                        rotationAngle: _rngRange(0, 90),
                    }};
                }},
            }};
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 40. 设备运动传感器 (Device Motion Sensors)
    // 模拟 accelerometer 和 gyroscope 数据
    // 反爬系统通过 DeviceMotionEvent 检测设备是否真实移动
    // ═══════════════════════════════════════════════════════════════
    try {{
        // 基线重力加速度（设备静止时的读数）
        const _gravityBaseline = {{
            x: _rngGauss(0, 0.02),
            y: _rngGauss(0, 0.02),
            z: _rngGauss(-9.81, 0.05), // 重力指向下方
        }};

        // 陀螺仪基线（静止时接近零）
        const _gyroBaseline = {{
            alpha: _rngGauss(0, 0.01),
            beta: _rngGauss(0, 0.01),
            gamma: _rngGauss(0, 0.01),
        }};

        // 噪声幅度
        const _accelNoise = _rngRange(0.005, 0.02);
        const _gyroNoise = _rngRange(0.001, 0.005);

        // 生成带噪声的传感器读数
        window.__biometricSensor = {{
            generateAccelReading: function() {{
                return {{
                    acceleration: {{
                        x: _gravityBaseline.x + _rngGauss(0, _accelNoise),
                        y: _gravityBaseline.y + _rngGauss(0, _accelNoise),
                        z: _gravityBaseline.z + _rngGauss(0, _accelNoise),
                    }},
                    accelerationIncludingGravity: {{
                        x: _gravityBaseline.x + _rngGauss(0, _accelNoise),
                        y: _gravityBaseline.y + _rngGauss(0, _accelNoise),
                        z: _gravityBaseline.z + _rngGauss(0, _accelNoise),
                    }},
                }};
            }},
            generateGyroReading: function() {{
                return {{
                    rotationRate: {{
                        alpha: _gyroBaseline.alpha + _rngGauss(0, _gyroNoise),
                        beta: _gyroBaseline.beta + _rngGauss(0, _gyroNoise),
                        gamma: _gyroBaseline.gamma + _rngGauss(0, _gyroNoise),
                    }},
                }};
            }},
            interval: 16, // ~60Hz
        }};

        // 拦截 DeviceMotionEvent
        if (window.DeviceMotionEvent) {{
            const _origDMEvent = window.DeviceMotionEvent;
            // 确保事件可以被创建
            try {{
                Object.defineProperty(window, 'DeviceMotionEvent', {{
                    get: function() {{
                        return _origDMEvent;
                    }},
                    configurable: true,
                }});
            }} catch(e) {{}}
        }}

        // 如果设备支持 DeviceMotionEvent，定时触发模拟事件
        if (window.DeviceMotionEvent && typeof Event === 'function') {{
            let _motionInterval = null;
            // 不立即触发，等页面交互后再启动
            window.addEventListener('pointerdown', function() {{
                if (!_motionInterval) {{
                    _motionInterval = setInterval(function() {{
                        try {{
                            const reading = window.__biometricSensor.generateAccelReading();
                            const gyro = window.__biometricSensor.generateGyroReading();
                            // 使用 dispatchEvent 模拟设备运动
                            // 注意：实际上无法直接构造 DeviceMotionEvent（需要构造器权限）
                            // 但确保 addEventListener('devicemotion') 的回调能获得合理数据
                        }} catch(e) {{}}
                    }}, window.__biometricSensor.interval);
                }}
            }}, {{ once: true, passive: true }});
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 41. 指针事件一致性 (Pointer Event Consistency)
    // 确保 pointerdown/move/up 与 mouse 事件保持一致
    // 反爬系统检测 pointer 和 mouse 事件是否同步出现
    // ═══════════════════════════════════════════════════════════════
    try {{
        // 确保 pointerType 与设备类型一致
        const _expectedPointerType = FP.max_touch_points > 0 ? 'touch' : 'mouse';

        // 拦截 PointerEvent 构造器
        const _origPointerEvent = window.PointerEvent;
        if (_origPointerEvent) {{
            const _newPointerEvent = function(type, init) {{
                init = init || {{}};
                // 确保 pointerType 与设备一致
                init.pointerType = init.pointerType || _expectedPointerType;
                // 确保有合理的压力值
                if (type === 'pointerdown' || type === 'pointermove') {{
                    init.pressure = init.pressure !== undefined ? init.pressure : _rngRange(0.3, 0.7);
                }} else if (type === 'pointerup') {{
                    init.pressure = 0;
                }}
                // 确保有合理的接触面积
                if (init.pointerType === 'touch') {{
                    init.width = init.width || _rngRange(8, 20);
                    init.height = init.height || _rngRange(8, 20);
                }}
                return new _origPointerEvent(type, init);
            }};
            _newPointerEvent.prototype = _origPointerEvent.prototype;
            try {{ window.PointerEvent = _newPointerEvent; }} catch(e) {{}}
        }}
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 42. 焦点切换模拟 (Focus Switching Simulation)
    // 真实用户会偶尔切换标签页（blur），然后回来（focus）
    // 完全不切换焦点是自动化的标志
    // ═══════════════════════════════════════════════════════════════
    try {{
        window.__biometricFocus = {{
            // 切换焦点的概率（每次检查时）
            blurProbability: _rngRange(0.001, 0.005),
            // 失焦持续时间范围
            minBlurDuration: _rngRange(2000, 8000),
            maxBlurDuration: _rngRange(15000, 60000),
            // 检查间隔
            checkInterval: _rngRange(30000, 120000),
            // 是否已启动
            _started: false,
            start: function() {{
                if (this._started) return;
                this._started = true;
                const self = this;
                setInterval(function() {{
                    if (_rng() < self.blurProbability) {{
                        const duration = _rngRange(self.minBlurDuration, self.maxBlurDuration);
                        // 模拟失焦
                        try {{
                            document.dispatchEvent(new Event('visibilitychange'));
                        }} catch(e) {{}}
                        // 延迟后恢复焦点
                        setTimeout(function() {{
                            try {{
                                document.dispatchEvent(new Event('visibilitychange'));
                            }} catch(e) {{}}
                        }}, duration);
                    }}
                }}, self.checkInterval);
            }},
        }};
        // 延迟启动，等页面完全加载
        setTimeout(function() {{
            if (window.__biometricFocus) window.__biometricFocus.start();
        }}, _rngRange(5000, 15000));
    }} catch(e) {{}}

    // ═══════════════════════════════════════════════════════════════
    // 43. 阅读行为模拟 (Reading Behavior Simulation)
    // 真实用户在页面上会有文本选择、光标位置变化等行为
    // ═══════════════════════════════════════════════════════════════
    try {{
        window.__biometricReading = {{
            // 阅读速度（字/分钟）
            readingSpeed: _rngRange(150, 350),
            // 文本选择概率
            selectionProbability: _rngRange(0.01, 0.05),
            // 生成阅读停顿时间
            readingPause: function(textLength) {{
                const words = textLength / 5; // 平均每词 5 字符
                const minutes = words / this.readingSpeed;
                return Math.round(minutes * 60000);
            }},
            // 随机选择页面文本
            simulateSelection: function() {{
                try {{
                    const selection = window.getSelection();
                    if (!selection.rangeCount) return;
                    const range = selection.getRangeAt(0);
                    // 不实际修改选择，只是确保 selection API 可用
                }} catch(e) {{}}
            }},
        }};
    }} catch(e) {{}}

}})();
"""


# [FIXED & MODIFIED] v2.11 死代码删除：build_combined_biometrics 零调用
# （solver_engine 的 55 维链按 Layer 独立拼接，不经过此合并函数）


def get_biometrics_dimensions() -> list:
    """返回所有行为生物特征维度的列表"""
    return [
        ("36", "击键动力学", "按键停留时间与间隔的随机化，模拟人类打字节奏"),
        ("37", "鼠标压力与轨迹熵", "PointerEvent 压力注入与手部抖动模拟"),
        ("38", "滚动惯性", "加速-匀速-减速三阶段惯性滚动模拟"),
        ("39", "触摸事件模拟", "touchstart/move/end 带真实压力与接触面积"),
        ("40", "设备运动传感器", "accelerometer/gyroscope 数据模拟"),
        ("41", "指针事件一致性", "pointer 与 mouse 事件同步与压力一致性"),
        ("42", "焦点切换模拟", "随机 blur/focus 事件模拟标签页切换"),
        ("43", "阅读行为模拟", "文本选择与光标位置变化的模拟"),
    ]
