"""Video URL resolver — extracts real stream URLs without browser"""
import re
import json
import logging
logger=logging.getLogger(__name__)

# ═══ B站 resolver ═══
def extract_bilibili_info(html: str, url: str) -> dict:
    """从B站页面HTML提取bvid/cid——兼容bangumi页面和带参数的URL"""
    info={'bvid':'','cid':0,'title':'','pages':[]}
    # Extract bvid from URL — handle video/ and bangumi/play/ patterns
    m=re.search(r'(?:/video/|/bangumi/play/ep?)(BV[a-zA-Z0-9]+)',url)
    if m:info['bvid']=m.group(1)
    # Also extract epid from bangumi URLs
    epid_match=re.search(r'(?:/bangumi/play/)?ep(\d+)',url)
    if not info['bvid']:
        # Try extracting BV from the URL query or path
        bv_match=re.search(r'(?:bvid=|/)(BV[a-zA-Z0-9]+)',url)
        if bv_match:info['bvid']=bv_match.group(1)
    # [v2.18 P2-12] 括号平衡扫描替代贪婪正则：原 (\{.*\}); 捕到全页最后一个 `};`
    # （脚本拼接处）→ json.loads 必失败 → B站流解析静默丢。扫描含字符串/转义感知。
    _idx = html.find("window.__INITIAL_STATE__")
    if _idx >= 0:
        _eq = html.find("=", _idx)
        _start = html.find("{", _eq if _eq >= 0 else _idx)
        if _start >= 0:
            _depth = 0
            _in_str = False
            _esc = False
            for _i in range(_start, min(len(html), _start + 4_000_000)):
                _c = html[_i]
                if _in_str:
                    if _esc:
                        _esc = False
                    elif _c == "\\":
                        _esc = True
                    elif _c == '"':
                        _in_str = False
                    continue
                if _c == '"':
                    _in_str = True
                elif _c == "{":
                    _depth += 1
                elif _c == "}":
                    _depth -= 1
                    if _depth == 0:
                        try:
                            state = json.loads(html[_start:_i + 1])
                            vd = state.get('videoData', {})
                            info['title'] = vd.get('title', '')
                            info['cid'] = vd.get('cid', 0) or (vd.get('pages', [{}])[0].get('cid', 0))
                            for p in vd.get('pages', []):
                                info['pages'].append({'part': p.get('part', ''), 'cid': p.get('cid', 0)})
                        except Exception as e:
                            logger.debug(f"B站 INITIAL_STATE parse: {e}")
                        break
    return info

async def resolve_bilibili_video(bvid: str, cid: int, session=None, quality: int = 127) -> list:
    """调用B站API获取真实视频流URL。quality: 127=8K, 126=杜比, 125=4K, 120=1080p60, 116=1080p, 112=720p, 80=360p"""
    if not bvid or not cid:
        return []
    api_url = f'https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn={quality}&fnval=4048&fourk=1'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36', 'Referer': 'https://www.bilibili.com/'}
    try:
        if session is None:
            # [FIXED & MODIFIED] curl_cffi 替换 aiohttp（本机 aiohttp 外网全超时）
            from curl_cffi.requests import AsyncSession
            async with AsyncSession(timeout=15) as sess:
                resp = await sess.get(api_url, headers=headers)
                data = resp.json()
        else:
            resp = await session.get(api_url, headers=headers, timeout=15)
            data = resp.json()
        if data.get('code')==0:
            urls=[]
            for fmt in data.get('data',{}).get('support_formats',[]):
                urls.append({
                    'quality':fmt.get('quality',0),
                    'desc':fmt.get('new_description',''),
                    'format':fmt.get('format',''),
                })
            dash=data.get('data',{}).get('dash',{})
            if dash:
                videos=dash.get('video',[])
                audios=dash.get('audio',[])
                for v in videos:
                    urls.append({'url':v.get('baseUrl') or v.get('base_url',''),
                                 'quality':v.get('id',0),'type':'video',
                                 'codecs':v.get('codecs',''),'bandwidth':v.get('bandwidth',0)})
                for a in audios:
                    urls.append({'url':a.get('baseUrl') or a.get('base_url',''),
                                 'quality':a.get('id',0),'type':'audio',
                                 'codecs':a.get('codecs',''),'bandwidth':a.get('bandwidth',0)})
            # Also check durl (flv segments)
            durl=data.get('data',{}).get('durl',[])
            for d in durl:
                urls.append({'url':d.get('url',''),'type':'flv','size':d.get('size',0)})
            return urls
    except Exception as e:
        logger.debug(f"B站 API error: {e}")
    return []
