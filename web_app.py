"""台灣法律資料庫查詢網頁 — Flask 前端

直接複用 mcp_server 的 tool 函式，不需啟動 MCP protocol。
三個查詢介面：裁判書搜尋、法規查詢、大法官解釋/憲判字。
"""

import asyncio
import logging
import os
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

# ── 設定日誌 ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("legal-web")

# ── 初始化 MCP 工具的共用資源 ──
from mcp_server.cache.db import CacheDB
from mcp_server.tools.regulations import RegulationClient
from mcp_server.tools.judicial_search import JudicialSearchClient
from mcp_server.tools.judicial_doc import JudgmentDocClient
from mcp_server.tools.waf_bypass import JudicialWAFBypass
from mcp_server.tools.constitutional_court import (
    get_interpretation as cc_get_interpretation,
    search_interpretations as cc_search_interpretations,
    get_citations as cc_get_citations,
)
from mcp_server.tools.regulations import _PCODE_ALL, _ABOLISHED_SET

# ── 全域資源（asyncio loop 共用） ──
_cache: CacheDB | None = None
_reg_client: RegulationClient | None = None
_jud_search: JudicialSearchClient | None = None
_jud_doc: JudgmentDocClient | None = None
_waf: JudicialWAFBypass | None = None
_loop: asyncio.AbstractEventLoop | None = None

app = Flask(__name__)


async def _init_resources():
    """初始化所有 async 資源（在 app 啟動前呼叫一次）"""
    global _cache, _reg_client, _jud_search, _jud_doc, _waf
    _cache = CacheDB()
    await _cache.initialize()
    await _cache.cleanup_expired()
    _waf = JudicialWAFBypass()
    _reg_client = RegulationClient(_cache)
    _jud_search = JudicialSearchClient(_cache, _waf)
    _jud_doc = JudgmentDocClient(_cache, _waf)
    logger.info("法律資料庫資源初始化完成")


async def _cleanup_resources():
    global _reg_client, _jud_search, _jud_doc, _cache
    if _reg_client:
        await _reg_client.close()
    if _jud_search:
        await _jud_search.close()
    if _jud_doc:
        await _jud_doc.close()
    if _cache:
        await _cache.close()


def _get_loop() -> asyncio.AbstractEventLoop:
    """取得或建立全域 event loop（Flask 是同步框架，需橋接 async）"""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        # 在 loop 中初始化資源
        _loop.run_until_complete(_init_resources())
    return _loop


def _ensure_initialized():
    """確保 async 資源已初始化（每個 endpoint 進入時呼叫）"""
    if _reg_client is None:
        _get_loop()


def _run_async(coro):
    """在同步 Flask handler 中執行 async coroutine"""
    loop = _get_loop()
    return loop.run_until_complete(coro)


# ============================================================
# 路由：首頁
# ============================================================

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


# ============================================================
# API：裁判書搜尋
# ============================================================

@app.route("/api/search_judgments", methods=["POST"])
def api_search_judgments():
    _ensure_initialized()
    data = request.get_json(force=True)
    keyword = data.get("keyword", "").strip()
    court = data.get("court", "").strip()
    case_type = data.get("case_type", "").strip()
    year_from = int(data.get("year_from") or 0)
    year_to = int(data.get("year_to") or 0)
    case_word = data.get("case_word", "").strip()
    case_number = data.get("case_number", "").strip()
    main_text = data.get("main_text", "").strip()
    max_results = min(int(data.get("max_results") or 20), 200)

    result = _run_async(_jud_search.search(
        keyword=keyword, court=court, case_type=case_type,
        year_from=year_from, year_to=year_to,
        case_word=case_word, case_number=case_number,
        main_text=main_text, max_results=max_results,
    ))
    return jsonify(result)


# ============================================================
# API：取得裁判書全文
# ============================================================

@app.route("/api/get_judgment", methods=["POST"])
def api_get_judgment():
    _ensure_initialized()
    data = request.get_json(force=True)
    jid = data.get("jid", "").strip()
    url = data.get("url", "").strip()
    if jid:
        result = _run_async(_jud_doc.get_by_jid(jid))
    elif url:
        result = _run_async(_jud_doc.get_by_url(url))
    else:
        result = {"success": False, "error": "需要 jid 或 url"}
    return jsonify(result)


# ============================================================
# API：查詢法規條文
# ============================================================

@app.route("/api/query_regulation", methods=["POST"])
def api_query_regulation():
    _ensure_initialized()
    data = request.get_json(force=True)
    law_name = data.get("law_name", "").strip()
    pcode = data.get("pcode", "").strip()
    article_no = data.get("article_no", "").strip()
    from_no = data.get("from_no", "").strip()
    to_no = data.get("to_no", "").strip()
    include_history = data.get("include_history", False)

    if not pcode and law_name:
        pcode = _reg_client.resolve_pcode(law_name)
        if not pcode:
            return jsonify({
                "success": False,
                "error": f"找不到法規「{law_name}」的代碼（pcode）",
            })

    if not pcode:
        return jsonify({"success": False, "error": "須提供 law_name 或 pcode"})

    if article_no:
        result = _run_async(_reg_client.get_article(pcode, article_no))
    elif from_no and to_no:
        result = _run_async(_reg_client.get_article_range(pcode, from_no, to_no))
    else:
        result = _run_async(_reg_client.get_all_articles(pcode))

    if include_history and result.get("success"):
        from mcp_server.tools.regulations import get_law_history
        history = get_law_history(pcode)
        if history:
            result["history"] = history

    return jsonify(result)


# ============================================================
# API：搜尋法規名稱
# ============================================================

@app.route("/api/search_regulations", methods=["POST"])
def api_search_regulations():
    data = request.get_json(force=True)
    keyword = data.get("keyword", "").strip()
    offset = int(data.get("offset") or 0)
    exclude_abolished = data.get("exclude_abolished", False)

    if not keyword:
        return jsonify({"success": False, "error": "請提供搜尋關鍵字"})

    matches = []
    for name, code in _PCODE_ALL.items():
        if keyword in name:
            if exclude_abolished and code in _ABOLISHED_SET:
                continue
            matches.append({
                "law_name": name,
                "pcode": code,
                "status": "已廢止" if code in _ABOLISHED_SET else "現行法規",
            })

    matches.sort(key=lambda m: (m["status"] != "現行法規", m["law_name"]))
    page_size = 50
    page = matches[offset:offset + page_size]

    return jsonify({
        "success": True,
        "keyword": keyword,
        "total_count": len(matches),
        "offset": offset,
        "has_more": offset + page_size < len(matches),
        "results": page,
    })


# ============================================================
# API：大法官解釋 / 憲判字
# ============================================================

@app.route("/api/get_interpretation", methods=["POST"])
def api_get_interpretation():
    data = request.get_json(force=True)
    case_id = data.get("case_id", "").strip()
    include_reasoning = data.get("include_reasoning", False)
    reasoning_keyword = data.get("reasoning_keywords", "").strip()
    include_opinions = data.get("include_opinions", False)
    opinions_keyword = data.get("opinions_keyword", "").strip()

    if not case_id:
        return jsonify({"success": False, "error": "請提供 case_id"})

    result = cc_get_interpretation(
        case_id, include_reasoning, reasoning_keyword,
        include_opinions, opinions_keyword,
    )
    return jsonify(result)


@app.route("/api/search_interpretations", methods=["POST"])
def api_search_interpretations():
    data = request.get_json(force=True)
    keyword = data.get("keyword", "").strip()
    year = int(data.get("year") or 0)
    number_from = int(data.get("number_from") or 0)
    number_to = int(data.get("number_to") or 0)
    max_results = int(data.get("max_results") or 30)

    result = cc_search_interpretations(
        keyword=keyword, year=year,
        number_from=number_from, number_to=number_to,
        max_results=max_results,
    )
    return jsonify(result)


@app.route("/api/get_citations", methods=["POST"])
def api_get_citations():
    data = request.get_json(force=True)
    case_id = data.get("case_id", "").strip()
    include_context = data.get("include_context", False)

    if not case_id:
        return jsonify({"success": False, "error": "請提供 case_id"})

    result = cc_get_citations(case_id, include_context)
    return jsonify(result)


# ============================================================
# HTML 模板（深色主題、手機優先、三 Tab）
# ============================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>台灣法律資料庫查詢</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif;background:#1a1a2e;color:#e0e0e0;font-size:15px;line-height:1.6}
.container{max-width:900px;margin:0 auto;padding:12px}
h1{text-align:center;padding:16px 0;font-size:22px;color:#8be9fd}
.footer{text-align:center;padding:20px 0 10px;color:#666;font-size:13px}
.tabs{display:flex;gap:2px;background:#16213e;border-radius:8px 8px 0 0;overflow:hidden}
.tab{flex:1;padding:10px 8px;text-align:center;cursor:pointer;background:#16213e;color:#888;border:none;font-size:15px;transition:.2s}
.tab.active{background:#1a1a2e;color:#8be9fd;border-bottom:2px solid #8be9fd}
.tab:hover:not(.active){background:#1a2a4e;color:#bbb}
.panel{display:none;background:#1a1a2e;border:1px solid #16213e;border-top:none;border-radius:0 0 8px 8px;padding:16px}
.panel.active{display:block}
.field{margin-bottom:10px}
.field label{display:block;margin-bottom:3px;color:#aaa;font-size:13px}
.field input,.field select{width:100%;padding:8px 10px;background:#16213e;border:1px solid #333;border-radius:4px;color:#e0e0e0;font-size:15px}
.field input:focus,.field select:focus{outline:none;border-color:#8be9fd}
.row{display:flex;gap:10px;flex-wrap:wrap}
.row .field{flex:1;min-width:120px}
.btn{padding:10px 24px;background:#3a8a5a;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:16px;transition:.2s}
.btn:hover{background:#4a9a6a}
.btn:active{background:#2a7a4a}
.btn:disabled{background:#555;cursor:not-allowed}
.btn-sm{padding:5px 12px;font-size:13px}
.result-area{margin-top:16px}
.result-card{background:#16213e;border:1px solid #2a2a4e;border-radius:6px;padding:14px;margin-bottom:10px}
.result-card .case-id{color:#8be9fd;font-weight:600;font-size:16px}
.result-card .meta{color:#999;font-size:13px;margin-top:4px}
.result-card .summary{margin-top:8px;color:#ccc;font-size:14px}
.result-card .full-text{margin-top:12px;white-space:pre-wrap;font-size:14px;max-height:600px;overflow-y:auto;background:#0f0f23;padding:12px;border-radius:4px;border:1px solid #333}
.result-card .label{color:#ffb86c;font-weight:600;margin-top:10px;display:block}
.result-card .section{margin-top:8px;white-space:pre-wrap;font-size:14px}
.stat-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;margin-left:6px}
.stat-badge.green{background:#2a5a3a;color:#6fcf97}
.stat-badge.red{background:#5a2a2a;color:#ff6b6b}
.stat-badge.gray{background:#3a3a4a;color:#999}
.loading{text-align:center;padding:30px;color:#888}
.error-msg{background:#3a1a1a;border:1px solid #5a2a2a;border-radius:6px;padding:12px;color:#ff6b6b;margin:10px 0}
.judgment-link{color:#8be9fd;cursor:pointer;text-decoration:underline}
.judgment-link:hover{color:#a0d4fd}
.citation-item{padding:6px 10px;background:#0f0f23;border-radius:4px;margin:4px 0;font-size:14px}
.empty-hint{text-align:center;color:#666;padding:20px;font-size:14px}
@media(max-width:600px){
  .container{padding:8px}
  h1{font-size:18px}
  .tab{font-size:13px;padding:8px 4px}
  .row .field{min-width:100%}
  .field input,.field select{font-size:14px}
}
</style>
</head>
<body>
<div class="container">
<h1>⚖️ 台灣法律資料庫查詢</h1>

<div class="tabs">
  <button class="tab active" onclick="switchTab('judgments')">裁判書搜尋</button>
  <button class="tab" onclick="switchTab('regulations')">法規查詢</button>
  <button class="tab" onclick="switchTab('constitutional')">大法官解釋</button>
</div>

<!-- Tab 1: 裁判書搜尋 -->
<div id="panel-judgments" class="panel active">
  <div class="field">
    <label>關鍵字全文搜尋</label>
    <input id="jud-keyword" placeholder="如：預售屋 遲延交屋" onkeydown="if(event.key==='Enter')doSearchJudgments()">
  </div>
  <div class="row">
    <div class="field"><label>法院</label>
      <select id="jud-court">
        <option value="">全部法院</option>
        <option value="最高法院">最高法院</option>
        <option value="最高行政法院">最高行政法院</option>
        <option value="臺灣高等法院">臺灣高等法院</option>
        <option value="臺灣臺北地方法院">臺灣臺北地方法院</option>
        <option value="臺灣新北地方法院">臺灣新北地方法院</option>
        <option value="臺灣臺中地方法院">臺灣臺中地方法院</option>
        <option value="臺灣高雄地方法院">臺灣高雄地方法院</option>
        <option value="智慧財產及商業法院">智慧財產及商業法院</option>
      </select>
    </div>
    <div class="field"><label>案件類型</label>
      <select id="jud-case-type">
        <option value="">全部</option>
        <option value="民事">民事</option>
        <option value="刑事">刑事</option>
        <option value="行政">行政</option>
        <option value="懲戒">懲戒</option>
      </select>
    </div>
  </div>
  <div class="row">
    <div class="field"><label>起始年度（民國）</label><input id="jud-year-from" type="number" placeholder="如 110"></div>
    <div class="field"><label>截止年度（民國）</label><input id="jud-year-to" type="number" placeholder="如 115"></div>
    <div class="field"><label>最大筆數</label><input id="jud-max" type="number" value="20"></div>
  </div>
  <div style="margin-top:8px"><button class="btn" onclick="doSearchJudgments()">搜尋裁判書</button></div>
  <div id="jud-results" class="result-area"></div>
</div>

<!-- Tab 2: 法規查詢 -->
<div id="panel-regulations" class="panel">
  <div class="field">
    <label>法規名稱</label>
    <input id="reg-law-name" placeholder="如：民法、勞動基準法" list="reg-suggestions" onkeydown="if(event.key==='Enter')doQueryRegulation()">
    <datalist id="reg-suggestions"></datalist>
  </div>
  <div class="row">
    <div class="field"><label>單一條號（如 184）</label><input id="reg-article" placeholder="如 184 或 247-1"></div>
    <div class="field"><label>起始條號</label><input id="reg-from" placeholder="如 184"></div>
    <div class="field"><label>截止條號</label><input id="reg-to" placeholder="如 198"></div>
  </div>
  <div style="margin-top:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <button class="btn" onclick="doQueryRegulation()">查詢法規</button>
    <button class="btn btn-sm" onclick="doSearchRegulations()">搜尋法規名稱</button>
    <label style="font-size:13px;color:#aaa"><input type="checkbox" id="reg-history"> 含修法沿革</label>
  </div>
  <div id="reg-search-results" class="result-area"></div>
  <div id="reg-results" class="result-area"></div>
</div>

<!-- Tab 3: 大法官解釋 -->
<div id="panel-constitutional" class="panel">
  <div class="field">
    <label>查詢字號（如 釋字748、111年憲判字第1號）</label>
    <input id="cc-case-id" placeholder="如：釋字748 或 111年憲判字第1號" onkeydown="if(event.key==='Enter')doGetInterpretation()">
  </div>
  <div style="margin-top:8px;display:flex;gap:10px;flex-wrap:wrap">
    <button class="btn" onclick="doGetInterpretation()">查詢解釋</button>
    <button class="btn btn-sm" onclick="doGetInterpretationFull()">含理由書全文</button>
    <button class="btn btn-sm" onclick="doGetCitations()">引用關係</button>
  </div>
  <hr style="border-color:#2a2a4e;margin:16px 0">
  <div class="field">
    <label>搜尋關鍵字（搜爭點 + 理由書全文）</label>
    <input id="cc-keyword" placeholder="如：集會自由、言論自由" onkeydown="if(event.key==='Enter')doSearchInterpretations()">
  </div>
  <div style="margin-top:8px"><button class="btn btn-sm" onclick="doSearchInterpretations()">搜尋解釋</button></div>
  <div id="cc-results" class="result-area"></div>
</div>

<div class="footer">
  資料來源：司法院裁判書系統 · 全國法規資料庫 · 憲法法庭<br>
  Fork from <a href="https://github.com/lawchat-oss/mcp-taiwan-legal-db" style="color:#666">lawchat-oss/mcp-taiwan-legal-db</a> · Sam Tsai 製作
</div>
</div>

<script>
function switchTab(name){
  document.querySelectorAll('.tab').forEach((t,i)=>{
    const panels=['judgments','regulations','constitutional'];
    const isActive=panels[i]===name;
    t.classList.toggle('active',isActive);
  });
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
}

async function postJSON(url,body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return r.json();
}

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function showLoading(id){document.getElementById(id).innerHTML='<div class="loading">查詢中…</div>';}
function showError(id,msg){document.getElementById(id).innerHTML='<div class="error-msg">'+esc(msg)+'</div>';}
function showEmpty(id,hint){document.getElementById(id).innerHTML='<div class="empty-hint">'+esc(hint)+'</div>';}

// ── 裁判書搜尋 ──
async function doSearchJudgments(){
  showLoading('jud-results');
  const body={
    keyword:document.getElementById('jud-keyword').value,
    court:document.getElementById('jud-court').value,
    case_type:document.getElementById('jud-case-type').value,
    year_from:document.getElementById('jud-year-from').value||0,
    year_to:document.getElementById('jud-year-to').value||0,
    max_results:document.getElementById('jud-max').value||20,
  };
  try{
    const r=await postJSON('/api/search_judgments',body);
    if(!r.success){showError('jud-results',r.error||'搜尋失敗');return;}
    if(!r.results||r.results.length===0){showEmpty('jud-results','查無結果');return;}
    let h='<div style="color:#aaa;margin-bottom:10px">共 '+r.total_count+' 筆'+(r.cached?'（快取）':'')+'</div>';
    r.results.forEach(j=>{
      h+='<div class="result-card">'+
        '<div class="case-id">'+esc(j.case_id)+'</div>'+
        '<div class="meta">'+esc(j.court)+' · '+esc(j.case_type||'')+' · '+esc(j.date)+' · '+esc(j.cause||'')+'</div>'+
        (j.summary?'<div class="summary">'+esc(j.summary.substring(0,200))+(j.summary.length>200?'…':'')+'</div>':'')+
        '<div style="margin-top:8px"><span class="judgment-link" onclick="getJudgment(\''+esc(j.jid)+'\')">查看全文 →</span></div>'+
        '<div id="jud-detail-'+esc(j.jid)+'"></div>'+
        '</div>';
    });
    document.getElementById('jud-results').innerHTML=h;
  }catch(e){showError('jud-results',e.message);}
}

async function getJudgment(jid){
  const el=document.getElementById('jud-detail-'+jid);
  if(el.innerHTML){el.innerHTML='';return;}
  el.innerHTML='<div class="loading">取得全文中…</div>';
  try{
    const r=await postJSON('/api/get_judgment',{jid});
    if(!r.success){el.innerHTML='<div class="error-msg">'+esc(r.error)+'</div>';return;}
    let h='<div class="full-text">';
    if(r.main_text)h+='<span class="label">主文</span>\n'+esc(r.main_text)+'\n\n';
    if(r.facts)h+='<span class="label">事實</span>\n'+esc(r.facts.substring(0,2000))+(r.facts.length>2000?'…':'')+'\n\n';
    if(r.reasoning)h+='<span class="label">理由</span>\n'+esc(r.reasoning.substring(0,3000))+(r.reasoning.length>3000?'…':'')+'\n\n';
    if(r.cited_statutes&&r.cited_statutes.length)h+='<span class="label">引用法條</span>\n'+r.cited_statutes.map(esc).join('、')+'\n\n';
    if(r.full_text)h+='<span class="label">完整全文</span>\n'+esc(r.full_text.substring(0,8000))+(r.full_text.length>8000?'…（截斷）':'');
    if(!r.full_text&&!r.main_text)h+='（無全文內容）';
    h+='</div>';
    if(r.source_url)h+='<div style="margin-top:6px;font-size:13px"><a href="'+esc(r.source_url)+'" target="_blank" style="color:#666">原始來源 →</a></div>';
    el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="error-msg">'+esc(e.message)+'</div>';}
}

// ── 法規查詢 ──
async function doQueryRegulation(){
  const lawName=document.getElementById('reg-law-name').value.trim();
  if(!lawName){showError('reg-results','請輸入法規名稱');return;}
  showLoading('reg-results');
  document.getElementById('reg-search-results').innerHTML='';
  const body={
    law_name:lawName,
    article_no:document.getElementById('reg-article').value.trim(),
    from_no:document.getElementById('reg-from').value.trim(),
    to_no:document.getElementById('reg-to').value.trim(),
    include_history:document.getElementById('reg-history').checked,
  };
  try{
    const r=await postJSON('/api/query_regulation',body);
    if(!r.success){showError('reg-results',r.error||'查詢失敗');return;}
    let h='<div class="result-card">';
    if(r.law){
      h+='<div class="case-id">'+esc(r.law.name||r.law.pcode);
      const st=r.law.status||'';
      h+=' <span class="stat-badge '+(st==='現行法規'?'green':'red')+'">'+esc(st)+'</span></div>';
    }
    if(r.note)h+='<div class="meta">'+esc(r.note)+'</div>';
    if(r.articles&&r.articles.length){
      h+='<div style="margin-top:10px;max-height:600px;overflow-y:auto">';
      r.articles.forEach(a=>{
        h+='<div style="padding:8px;border-bottom:1px solid #2a2a4e">'+
          '<span style="color:#ffb86c;font-weight:600">第 '+esc(a.number)+' 條</span>'+
          '<div style="white-space:pre-wrap;margin-top:4px;font-size:14px">'+esc(a.content)+'</div></div>';
      });
      h+='</div>';
    }
    if(r.history){
      h+='<span class="label">修法沿革</span><div class="section">'+esc(r.history)+'</div>';
    }
    if(r.source_url)h+='<div style="margin-top:6px;font-size:13px"><a href="'+esc(r.source_url)+'" target="_blank" style="color:#666">原始來源 →</a></div>';
    h+='</div>';
    document.getElementById('reg-results').innerHTML=h;
  }catch(e){showError('reg-results',e.message);}
}

async function doSearchRegulations(){
  const lawName=document.getElementById('reg-law-name').value.trim();
  if(!lawName){showError('reg-search-results','請輸入搜尋關鍵字');return;}
  showLoading('reg-search-results');
  try{
    const r=await postJSON('/api/search_regulations',{keyword:lawName});
    if(!r.success){showError('reg-search-results',r.error);return;}
    if(!r.results||r.results.length===0){showEmpty('reg-search-results','查無相符法規');return;}
    let h='<div style="color:#aaa;margin-bottom:8px">共 '+r.total_count+' 部法規</div>';
    r.results.forEach(reg=>{
      h+='<div class="result-card" style="padding:8px 12px">'+
        '<span class="judgment-link" onclick="document.getElementById(\'reg-law-name\').value=\''+esc(reg.law_name).replace(/'/g,"\\'")+'\';doQueryRegulation()">'+esc(reg.law_name)+'</span>'+
        ' <span class="stat-badge '+(reg.status==='現行法規'?'green':'red')+'">'+esc(reg.status)+'</span>'+
        '</div>';
    });
    document.getElementById('reg-search-results').innerHTML=h;
    document.getElementById('reg-results').innerHTML='';
  }catch(e){showError('reg-search-results',e.message);}
}

// ── 大法官解釋 ──
async function doGetInterpretation(full){
  const caseId=document.getElementById('cc-case-id').value.trim();
  if(!caseId){showError('cc-results','請輸入字號');return;}
  showLoading('cc-results');
  const body={case_id:caseId};
  if(full)body.include_reasoning=true;
  try{
    const r=await postJSON('/api/get_interpretation',body);
    if(!r.success){showError('cc-results',r.error||'查詢失敗');return;}
    let h='<div class="result-card">';
    h+='<div class="case-id">'+esc(r.case_id)+'</div>';
    h+='<div class="meta">'+esc(r.type||'')+' · '+esc(r.date||'')+(r.main_text_truncated?' （主文已截斷）':'')+'</div>';
    if(r.issues)h+='<span class="label">解釋爭點</span><div class="section">'+esc(r.issues)+'</div>';
    if(r.issue_summary)h+='<span class="label">案由</span><div class="section">'+esc(r.issue_summary)+'</div>';
    if(r.main_text)h+='<span class="label">'+(r.type==='憲判字'?'主文':'解釋文')+'</span><div class="section">'+esc(r.main_text)+'</div>';
    if(r.summary)h+='<span class="label">判決摘要</span><div class="section">'+esc(r.summary)+'</div>';
    if(r.reasoning)h+='<span class="label">理由書'+(r.reasoning_truncated?'（已截斷）':'')+'</span><div class="section" style="max-height:500px;overflow-y:auto">'+esc(r.reasoning)+'</div>';
    if(r.related_statutes)h+='<span class="label">相關法令</span><div class="section">'+esc(r.related_statutes)+'</div>';
    if(r.source_url)h+='<div style="margin-top:6px;font-size:13px"><a href="'+esc(r.source_url)+'" target="_blank" style="color:#666">原始來源 →</a></div>';
    h+='</div>';
    document.getElementById('cc-results').innerHTML=h;
  }catch(e){showError('cc-results',e.message);}
}

async function doGetInterpretationFull(){doGetInterpretation(true);}

async function doSearchInterpretations(){
  const kw=document.getElementById('cc-keyword').value.trim();
  if(!kw){showError('cc-results','請輸入搜尋關鍵字');return;}
  showLoading('cc-results');
  try{
    const r=await postJSON('/api/search_interpretations',{keyword:kw,max_results:30});
    if(!r.success){showError('cc-results',r.error);return;}
    if(!r.results||r.results.length===0){showEmpty('cc-results','查無結果');return;}
    let h='<div style="color:#aaa;margin-bottom:10px">共 '+r.count+' 筆</div>';
    r.results.forEach(item=>{
      h+='<div class="result-card" style="padding:10px 14px">'+
        '<span class="judgment-link" onclick="document.getElementById(\'cc-case-id\').value=\''+esc(item.case_id).replace(/'/g,"\\'")+'\';doGetInterpretation()">'+esc(item.case_id)+'</span>'+
        ' <span class="stat-badge gray">'+esc(item.type)+'</span>'+
        (item.issues?'<div class="meta" style="margin-top:4px">'+esc(item.issues.substring(0,150))+(item.issues.length>150?'…':'')+'</div>':'')+
        '</div>';
    });
    document.getElementById('cc-results').innerHTML=h;
  }catch(e){showError('cc-results',e.message);}
}

async function doGetCitations(){
  const caseId=document.getElementById('cc-case-id').value.trim();
  if(!caseId){showError('cc-results','請輸入字號');return;}
  showLoading('cc-results');
  try{
    const r=await postJSON('/api/get_citations',{case_id:caseId,include_context:false});
    if(!r.success){showError('cc-results',r.error);return;}
    let h='<div class="result-card">';
    h+='<div class="case-id">'+esc(r.source_case_id)+' — 引用關係</div>';
    h+='<div class="meta">引用 '+r.citation_count+' 件</div>';
    if(r.reasoning_truncated)h+='<div class="meta" style="color:#ff6b6b">⚠ 理由書已截斷，引用清單可能不完整</div>';
    if(r.citations&&r.citations.length){
      r.citations.forEach(c=>{
        h+='<div class="citation-item"><span class="judgment-link" onclick="document.getElementById(\'cc-case-id\').value=\''+esc(c.case_id).replace(/'/g,"\\'")+'\';doGetInterpretation()">'+esc(c.case_id)+'</span> <span class="stat-badge gray">'+esc(c.type)+'</span></div>';
      });
    }else{h+='<div class="empty-hint">無引用其他解釋</div>';}
    h+='</div>';
    document.getElementById('cc-results').innerHTML=h;
  }catch(e){showError('cc-results',e.message);}
}

// ── 法規名稱自動完成 ──
document.getElementById('reg-law-name').addEventListener('input',async function(){
  const v=this.value.trim();
  if(v.length<1)return;
  const r=await postJSON('/api/search_regulations',{keyword:v});
  if(r.success&&r.results){
    const dl=document.getElementById('reg-suggestions');
    dl.innerHTML=r.results.slice(0,15).map(x=>'<option value="'+esc(x.law_name)+'">').join('');
  }
});
</script>
</body>
</html>
"""

# ============================================================
# 啟動
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5070))
    logger.info("啟動台灣法律資料庫查詢網頁 (port %d)", port)
    app.run(host="0.0.0.0", port=port, debug=False)