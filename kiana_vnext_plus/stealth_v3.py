"""Stealth v3 — GoogleBot / BingBot tier browser camouflage"""
import random
import logging
logger=logging.getLogger(__name__)

# ═══ Browser-level spoofing config (Google Chrome 130 on Windows) ═══
BROWSER_SPOOF = {
    "args": [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-site-isolation-trials",
        "--disable-web-security",
        "--disable-features=VizDisplayCompositor",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-infobars",
        "--disable-dev-shm-usage",
        "--disable-accelerated-2d-canvas",
        "--no-first-run",
        "--no-zygote",
        "--disable-gpu",
        "--hide-scrollbars",
        "--mute-audio",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-breakpad",
        "--disable-component-extensions-with-background-pages",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-hang-monitor",
        "--disable-ipc-flooding-protection",
        "--disable-popup-blocking",
        "--disable-prompt-on-repost",
        "--disable-renderer-backgrounding",
        "--disable-sync",
        "--enable-features=NetworkService,NetworkServiceInProcess",
        "--force-color-profile=srgb",
        "--metrics-recording-only",
        "--password-store=basic",
        f"--window-size={random.choice([1920,1680,1600,1536,1440,1366])},{random.choice([1080,1050,900,864,768])}",
    ],
    "viewport": {"width": 1920, "height": 1080},
    "locale": random.choice(["zh-CN","zh-CN,zh;q=0.9,en;q=0.8"]),
    "timezone_id": "Asia/Shanghai",
    "geolocation": {"latitude": 31.2304+random.uniform(-0.5,0.5), "longitude": 121.4737+random.uniform(-0.5,0.5)},
    "permissions": random.sample(["geolocation","notifications","camera","microphone","midi","midi-sysex"],3),
    "color_scheme": random.choice(["light","dark","no-preference"]),
    "device_scale_factor": 1,
    "is_mobile": False,
    "has_touch": False,
    "user_agent": random.choice([
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    ]),
}

# ═══ GoogleBot-level CDP evasions ═══
GOOGLEBOT_CDP_EVASION = """
// === GoogleBot-tier browser fingerprint spoofing ===
(function(){
    const rng=()=>Math.random();
    // 1. Navigator properties — match real Chrome on Windows
    const navProps={
        appCodeName:'Mozilla',appName:'Netscape',appVersion:'5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        platform:'Win32',productSub:'20030107',vendor:'Google Inc.',vendorSub:'',
        hardwareConcurrency:20,deviceMemory:16,maxTouchPoints:0,
        language:'zh-CN',languages:['zh-CN','zh','en'],
        doNotTrack:null,cookieEnabled:true,
    };
    for(const [k,v] of Object.entries(navProps)){
        try{Object.defineProperty(navigator,k,{get:()=>v,configurable:true});}catch(e){}
    }
    // 2. Screen properties
    const screens={width:1920,height:1080,availWidth:1920,availHeight:1040,colorDepth:24,pixelDepth:24,availLeft:0,availTop:0};
    for(const [k,v] of Object.entries(screens)){
        try{Object.defineProperty(screen,k,{get:()=>v,configurable:true});}catch(e){}
    }
    // 3. Frame busting — prevent detection via iframe
    if(window.top!==window.self)window.top=window.self;
    // 4. Chrome object shape
    if(!window.chrome)window.chrome={};
    window.chrome.loadTimes=function(){};
    window.chrome.csi=function(){};
    window.chrome.app={isInstalled:false,InstallState:{DISABLED:'disabled',INSTALLED:'installed',NOT_INSTALLED:'not_installed'},RunningState:{CANNOT_RUN:'cannot_run',READY_TO_RUN:'ready_to_run',RUNNING:'running'}};
    // 5. Battery API spoof
    if(navigator.getBattery){
        const origGetBattery=navigator.getBattery.bind(navigator);
        navigator.getBattery=function(){return origGetBattery().then(b=>({
            charging:true,chargingTime:0,dischargingTime:Infinity,
            level:0.95+rng()*0.05,
            onchargingchange:null,onchargingtimechange:null,ondischargingtimechange:null,onlevelchange:null,
            addEventListener:()=>{},removeEventListener:()=>{},dispatchEvent:()=>true,
        }));};
    }
    // 6. RequestAnimationFrame noise
    const origRAF=window.requestAnimationFrame;
    let rafIdx=0;
    window.requestAnimationFrame=function(cb){return origRAF.call(window,t=>{if(++rafIdx%97===0)t+=rng()*0.5;cb(t);});};
    // 7. Date.getTimezoneOffset stability
    const origDateGet=Date.prototype.getTimezoneOffset;
    Date.prototype.getTimezoneOffset=function(){return -480;}; // UTC+8
    // 8. History length spoof
    Object.defineProperty(window.history,'length',{get:()=>Math.floor(2+rng()*5),configurable:true});
})();
"""

# ═══ Human-like behavior simulation (Playwright/Puppeteer tier) ═══
HUMAN_BEHAVIOR_SCRIPT = """
// Human-like scroll, mouse movement, click timing
(function(){
    let scrollPos=0,scrollSpeed=0;
    const humanScroll=()=>{
        const maxScroll=document.body.scrollHeight-window.innerHeight;
        if(maxScroll<=0)return;
        const target=Math.min(maxScroll,scrollPos+(Math.random()*300-150));
        scrollPos=Math.max(0,target);
        window.scrollTo({top:scrollPos,behavior:'smooth'});
        scrollSpeed=Math.abs(scrollPos-target);
        setTimeout(humanScroll,3000+Math.random()*5000);
    };
    setTimeout(humanScroll,1000+Math.random()*3000);
    // Simulate mouse trail
    let mx=0,my=0;
    document.addEventListener('mousemove',e=>{mx=e.clientX;my=e.clientY;});
    setInterval(()=>{
        if(Math.random()<0.3)return;
        const ev=new MouseEvent('mousemove',{clientX:mx+Math.random()*20-10,clientY:my+Math.random()*20-10,bubbles:true});
        document.dispatchEvent(ev);
    },2000+Math.random()*3000);
})();
"""

# ═══ Network-level fingerprint: HTTP/2 header order matching real Chrome ═══
CHROME_HTTP2_HEADER_ORDER = [
    ":method",":authority",":scheme",":path",
    "sec-ch-ua","sec-ch-ua-mobile","sec-ch-ua-platform",
    "upgrade-insecure-requests","user-agent","accept",
    "sec-fetch-site","sec-fetch-mode","sec-fetch-dest",
    "accept-encoding","accept-language","cookie",
]
