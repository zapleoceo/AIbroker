"""Public "ask many models" page: GET /ask2many + JSON under /ask2many/api/*. No login.

Everything dynamic is rendered client-side with textContent (never innerHTML), so model
answers and prompts typed by strangers cannot inject markup; the server sends only a static
shell (inline CSS/JS, no external assets). Logic lives in services/compare.py.
"""
from __future__ import annotations

import json
from html import escape

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from aibroker.auth import client_ip
from aibroker.providers import model_scores
from aibroker.routes.landing import FAVICON_LINKS
from aibroker.services import compare as svc

router = APIRouter(tags=["ask2many"])


class SubmitIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=svc.MAX_PROMPT_CHARS)
    models: list[str] = Field(min_length=1, max_length=svc.MAX_MODELS)


def _secure(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


@router.api_route("/ask2many", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    html = (_HTML.replace("__FAVICON__", FAVICON_LINKS)
            .replace("__MAX_CHARS__", str(svc.MAX_PROMPT_CHARS))
            .replace("__MAX_MODELS__", str(svc.MAX_MODELS))
            .replace("__BUSY__", json.dumps(svc.BUSY_MESSAGE, ensure_ascii=False))
            .replace("__ATTRIBUTION__", escape(model_scores.ATTRIBUTION))
            .replace("__CAPTURED__", escape(model_scores.CAPTURED_ON)))
    resp = HTMLResponse(html)
    svc.set_browser_cookie(resp, svc.browser_id_from(request.cookies.get(svc.COOKIE_NAME)),
                           secure=_secure(request))
    return resp


@router.get("/ask2many/api/models")
async def models() -> dict[str, object]:
    keys = ("id", "name", "provider", "intelligence_index", "subscription")
    return {"data": [{k: m[k] for k in keys} for m in svc.page_models()],
            "attribution": model_scores.ATTRIBUTION, "source": model_scores.SOURCE,
            "captured_on": model_scores.CAPTURED_ON,
            "limits": {"max_prompt_chars": svc.MAX_PROMPT_CHARS, "max_models": svc.MAX_MODELS}}


@router.post("/ask2many/api/requests", status_code=202)
async def submit(body: SubmitIn, request: Request, response: Response) -> dict[str, object]:
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(422, "пустой вопрос")
    catalog = {m["id"]: m for m in svc.page_models()}
    wanted = list(dict.fromkeys(body.models))
    unknown = [m for m in wanted if m not in catalog]
    if unknown:
        raise HTTPException(422, f"неизвестные модели: {', '.join(unknown)[:200]}")
    project = await svc.get_project()
    if project is None:
        raise HTTPException(503, "проект сравнения не создан на сервере")
    bid = svc.browser_id_from(request.cookies.get(svc.COOKIE_NAME))
    svc.set_browser_cookie(response, bid, secure=_secure(request))
    request_id = await svc.create_request(prompt, bid, client_ip(request),
                                          [catalog[m] for m in wanted])
    svc.spawn_tracker(request_id, project, prompt)
    return {"id": request_id}


@router.get("/ask2many/api/requests/{request_id}")
async def poll(request_id: int) -> dict[str, object]:
    await svc.sync_request(request_id)
    out = await svc.get_request(request_id)
    if out is None:
        raise HTTPException(404, "запрос не найден")
    return out


@router.get("/ask2many/api/history")
async def history(q: str = Query("", max_length=200), page: int = Query(1, ge=1, le=10_000)
                  ) -> dict[str, object]:
    return await svc.search_history(q.strip(), page)


# Static shell. Placeholders (__X__) instead of str.format so CSS/JS braces stay readable.
# Layout (>=1024px): question + selected models left (60%), scrolling model table right (40%);
# answers and history below. No backslashes in this string on purpose (plain UTF-8 text).
_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ask2many — один вопрос, много моделей</title>
__FAVICON__
<style>
:root{color-scheme:light dark;--bg:#f7f7f8;--panel:#fff;--text:#1a1c20;--muted:#6b7280;--line:#e3e5e9;
 --accent:#4f46e5;--accent-fg:#fff;--accent-soft:rgba(79,70,229,.1);--good:#1a9b5b;--warn:#c58a14;--bad:#b4474b;--bad-soft:rgba(180,71,75,.08);
 --s1:4px;--s2:8px;--s3:12px;--s4:16px;--s5:24px;--s6:32px;--r:12px;--r-s:8px}
@media (prefers-color-scheme:dark){:root{--bg:#121316;--panel:#1a1c21;--text:#e8e9ec;--muted:#8d93a0;--line:#2a2d34;
 --accent:#8b85ff;--accent-fg:#12131a;--accent-soft:rgba(139,133,255,.14);--good:#3ec47f;--warn:#e0aa3a;--bad:#e07a7e;--bad-soft:rgba(224,122,126,.1)}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0;background:var(--bg);color:var(--text);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:var(--s5) var(--s4) var(--s6)}
@media (min-width:768px){.wrap{padding:var(--s6) var(--s5)}}
h1{font-size:22px;line-height:1.25;margin:0 0 var(--s1);font-weight:650}
@media (min-width:768px){h1{font-size:24px}}
h2{font-size:17px;margin:var(--s6) 0 var(--s3);font-weight:600}
h3{font-size:14px;margin:0;font-weight:600}
.muted{color:var(--muted)} .small{font-size:13px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
textarea,input[type=search]{width:100%;border:1px solid var(--line);border-radius:var(--r-s);background:var(--panel);
 color:var(--text);font:inherit;padding:var(--s3);min-height:44px;outline:none}
textarea{min-height:168px;resize:vertical;margin-top:var(--s4);border-radius:var(--r)}
textarea:focus,input[type=search]:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
button{font:inherit;color:inherit}
.top{display:grid;grid-template-columns:1fr;gap:var(--s5);margin-top:var(--s2)}
@media (min-width:1024px){.top{grid-template-columns:3fr 2fr;gap:var(--s6)}}
.row{display:flex;align-items:center;justify-content:space-between;gap:var(--s3);flex-wrap:wrap}
.sel-h{margin-top:var(--s5)}
.links button{background:none;border:0;color:var(--accent);font-size:13px;cursor:pointer;padding:var(--s2);min-height:44px}
.chips{display:flex;flex-wrap:wrap;gap:var(--s2);margin-top:var(--s2);min-height:44px;align-content:flex-start}
.chip{display:inline-flex;align-items:center;gap:var(--s1);padding:var(--s1) var(--s1) var(--s1) var(--s3);min-height:36px;
 border:1px solid var(--accent);background:var(--accent-soft);border-radius:var(--r-s);font-size:14px}
.chip button{background:none;border:0;cursor:pointer;color:var(--muted);font-size:18px;line-height:1;min-width:32px;min-height:32px;border-radius:var(--r-s)}
.chip button:hover{color:var(--text)}
@media (max-width:767px){.chip{min-height:44px}.chip button{min-width:44px;min-height:44px}}
.chips .empty{margin:0;align-self:center}
.act{display:flex;justify-content:flex-end;margin-top:var(--s4)}
.go{padding:var(--s3) var(--s6);min-height:48px;border:0;border-radius:var(--r-s);background:var(--accent);color:var(--accent-fg);
 font-size:16px;font-weight:600;cursor:pointer}
@media (max-width:767px){.go{width:100%}}
.go:disabled{opacity:.45;cursor:not-allowed}
.msg{display:none;margin-top:var(--s3);padding:var(--s3) var(--s4);border-radius:var(--r-s);background:var(--bad-soft);color:var(--bad);font-size:14px}
.mp{border:1px solid var(--line);border-radius:var(--r);background:var(--panel);display:flex;flex-direction:column;min-height:0}
.mp-in{display:flex;flex-direction:column;min-height:0;max-height:360px}
@media (min-width:1024px){.mp{position:relative;min-height:420px}.mp-in{position:absolute;inset:0;max-height:none}}
.mp-h{padding:var(--s3);border-bottom:1px solid var(--line)}
.list{overflow-y:auto;flex:1;min-height:0;overscroll-behavior:contain}
.lh,.mr{display:grid;grid-template-columns:minmax(0,1fr) 92px 84px;gap:var(--s2);align-items:center;padding:0 var(--s3)}
.lh{position:sticky;top:0;z-index:1;background:var(--panel);border-bottom:1px solid var(--line);min-height:36px;font-size:13px;color:var(--muted)}
.mr{min-height:44px;cursor:pointer;border-bottom:1px solid var(--line);font-size:14px;user-select:none}
.mr:last-child{border-bottom:0}
.mr:hover{background:var(--accent-soft)}
.mr[aria-pressed=true]{background:var(--accent-soft);box-shadow:inset 3px 0 0 var(--accent)}
.mr .n{overflow-wrap:anywhere}
.mr .s{font-size:13px;color:var(--muted);text-align:right}
.mr .t{font-size:11px;color:var(--muted)}
.mr .t span{border:1px solid var(--line);border-radius:var(--s2);padding:0 var(--s1)}
.lh span:nth-child(2){text-align:right}
.none{padding:var(--s4) var(--s3);font-size:13px;color:var(--muted)}
.grid{display:grid;grid-template-columns:1fr;gap:var(--s3)}
@media (min-width:720px){.grid{grid-template-columns:1fr 1fr}}
@media (min-width:1100px){#out.grid{grid-template-columns:1fr 1fr 1fr}}
.empty-out{grid-column:1/-1;margin:0;padding:var(--s5) var(--s4);border:1px dashed var(--line);border-radius:var(--r);text-align:center;font-size:14px}
.card{border:1px solid var(--line);border-radius:var(--r);background:var(--panel);padding:var(--s4);min-width:0;min-height:120px}
.card-h{display:flex;align-items:center;gap:var(--s2);justify-content:space-between;margin-bottom:var(--s2)}
.card-h h3{overflow-wrap:anywhere}
.st{display:inline-flex;align-items:center;gap:var(--s2);color:var(--muted);font-size:13px;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--warn)}
.done .dot{background:var(--good)} .error .dot,.refused .dot{background:var(--bad)}
.pending .dot{animation:p 1.2s ease-in-out infinite}
@keyframes p{50%{opacity:.35}}
.ans{white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px}
.error .ans,.refused .ans{color:var(--bad)}
.card-meta{margin-top:var(--s2);font-size:13px;color:var(--muted);overflow-wrap:anywhere}
details.h{border:1px solid var(--line);border-radius:var(--r);background:var(--panel);margin:var(--s2) 0;padding:0 var(--s4)}
details.h summary{cursor:pointer;min-height:44px;padding:var(--s2) 0;list-style-position:inside}
details.h .date{color:var(--muted);font-size:13px;margin-right:var(--s2)}
details.h .hm{display:block;color:var(--muted);font-size:13px;overflow-wrap:anywhere}
details.h .body{padding-bottom:var(--s4)}
details.h .full{margin:0 0 var(--s3);white-space:pre-wrap;overflow-wrap:anywhere}
.more{margin:var(--s2) 0;background:none;border:1px solid var(--line);border-radius:var(--r-s);color:var(--accent);
 padding:var(--s2) var(--s4);min-height:44px;cursor:pointer}
footer{margin-top:var(--s6);font-size:13px;color:var(--muted)}
</style></head><body><div class="wrap">
<div class="top">
 <section>
  <h1>Ask2many — один вопрос, много моделей</h1>
  <p class="muted small" id="public-note" style="margin:0">Вопросы и ответы видны всем в общей истории.</p>
  <textarea id="prompt" maxlength="__MAX_CHARS__" placeholder="Задайте вопрос" aria-label="Ваш вопрос"></textarea>
  <div class="row small muted"><span><span id="cnt">0</span> / __MAX_CHARS__</span><span>Ctrl+Enter — отправить</span></div>

  <div class="row sel-h"><h3>Выбранные модели <span class="muted" style="font-weight:400"><span id="sel">0</span> из __MAX_MODELS__</span></h3>
   <span class="links"><button id="all" type="button">выбрать все</button><button id="clear" type="button">снять все</button></span></div>
  <div class="chips" id="selected" aria-live="polite"></div>
  <div class="act"><button class="go" id="go" type="button" disabled>Спросить</button></div>
  <div class="msg" id="msg" role="alert"></div>
 </section>
 <aside class="mp" aria-label="Список моделей"><div class="mp-in">
  <div class="mp-h"><input type="search" id="mf" placeholder="Найти модель по имени" autocomplete="off" aria-label="Фильтр моделей"></div>
  <div class="list" id="models" role="group" aria-label="Модели, самые сильные сверху">
   <div class="lh" aria-hidden="true"><span>Модель</span><span>Балл</span><span></span></div>
   <div class="none" id="mload">Загружаем модели…</div>
  </div>
 </div></aside>
</div>

<h2>Ответы</h2>
<div class="grid" id="out" aria-live="polite"><p class="empty-out muted">Ответы появятся здесь, когда вы зададите вопрос.</p></div>

<h2>История</h2>
<input type="search" id="q" placeholder="Поиск по тексту вопроса" autocomplete="off" aria-label="Поиск по истории">
<div id="hist"></div>
<button class="more" id="more" type="button" hidden>Показать ещё</button>
<footer>__ATTRIBUTION__. Снимок оценок: __CAPTURED__ (Intelligence Index). «нет оценки» значит, что для точно такой
версии модели данных нет.</footer>
</div>
<script>
(function(){
var MAX=__MAX_MODELS__;
var BUSY=__BUSY__;
var $=function(i){return document.getElementById(i);};
var models=[], byId={}, sel=[], active=false, page=1, hq="", debounce=null;
function el(tag,cls,text){var e=document.createElement(tag); if(cls)e.className=cls; if(text!=null)e.textContent=text; return e;}
function score(v){return v==null?"нет оценки":String(v);}
function nameOf(id){return byId[id]?byId[id].label:id;}
function showMsg(t){var m=$("msg"); m.textContent=t||""; m.style.display=t?"block":"none";}
function refresh(){var n=sel.length; $("sel").textContent=n; $("cnt").textContent=$("prompt").value.length;
 $("go").disabled=active||!n||n>MAX||!$("prompt").value.trim(); $("go").textContent=active?"Ждём ответы…":"Спросить";}
function syncSel(){
 Array.prototype.forEach.call(document.querySelectorAll("#models .mr"),function(r){r.setAttribute("aria-pressed",sel.indexOf(r.getAttribute("data-id"))>=0?"true":"false");});
 var box=$("selected"); box.textContent="";
 if(!sel.length)box.appendChild(el("p","empty muted small","Выберите модели в таблице: клик по строке добавляет модель."));
 sel.forEach(function(id){var m=byId[id]; var c=el("div","chip"); c.appendChild(el("span",null,nameOf(id)+" — "+score(m?m.intelligence_index:null)));
  var x=el("button",null,"×"); x.type="button"; x.setAttribute("aria-label","Убрать "+nameOf(id)); x.onclick=function(){toggle(id);}; c.appendChild(x); box.appendChild(c);});
 refresh();}
function toggle(id){var i=sel.indexOf(id); showMsg("");
 if(i>=0)sel.splice(i,1);
 else if(sel.length>=MAX){showMsg("Можно выбрать не больше "+MAX+" моделей. Уберите одну из выбранных.");return;}
 else sel.push(id);
 syncSel();}
var LABEL={pending:"ждём",done:"готово",error:"ошибка",refused:"отказ"};
function card(r){var c=el("div","card "+r.status); var h=el("div","card-h");
 h.appendChild(el("h3",null,nameOf(r.model_id)));
 var st=el("span","st"); st.appendChild(el("span","dot"));
 st.appendChild(el("span",null,r.latency_ms!=null?(r.latency_ms/1000).toFixed(1)+" с":(LABEL[r.status]||r.status)));
 h.appendChild(st); c.appendChild(h);
 var body=r.status==="done"?r.answer:(r.status==="pending"?"":(r.error||"")); if(body)c.appendChild(el("div","ans",body));
 c.appendChild(el("div","card-meta","Балл: "+score(r.intelligence_index)+" · отвечала: "+r.model_served)); return c;}
function render(target,data){target.textContent=""; data.results.forEach(function(r){target.appendChild(card(r));});}
function poll(id){fetch("/ask2many/api/requests/"+id).then(function(r){return r.json();}).then(function(d){
 render($("out"),d); if(d.done){active=false; refresh(); loadHistory(true);}
 else setTimeout(function(){poll(id);},2000);}).catch(function(){setTimeout(function(){poll(id);},4000);});}
function send(){if($("go").disabled)return; showMsg(""); active=true; refresh();
 var chosen=sel.slice();
 fetch("/ask2many/api/requests",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({prompt:$("prompt").value,models:chosen})}).then(function(r){return r.json().then(function(j){return [r.status,j];});})
 .then(function(x){if(x[0]===202){
   render($("out"),{results:chosen.map(function(id){return {model_id:id,status:"pending",intelligence_index:byId[id]?byId[id].intelligence_index:null,model_served:"unknown"};})});
   poll(x[1].id);}
  else{active=false; var d=x[1].detail; showMsg(x[0]===429?BUSY:(typeof d==="string"?d:"Не удалось отправить вопрос. Попробуйте ещё раз.")); refresh();}})
 .catch(function(){active=false; showMsg("Нет связи с сервером. Проверьте соединение и попробуйте ещё раз."); refresh();});}
$("go").onclick=send;
$("prompt").oninput=refresh;
$("prompt").onkeydown=function(e){if((e.ctrlKey||e.metaKey)&&e.key==="Enter"){e.preventDefault(); send();}};
$("all").onclick=function(){sel=models.slice(0,MAX).map(function(m){return m.id;}); showMsg(""); syncSel();};
$("clear").onclick=function(){sel=[]; showMsg(""); syncSel();};
$("mf").oninput=function(){var q=$("mf").value.trim().toLowerCase(),shown=0;
 Array.prototype.forEach.call(document.querySelectorAll("#models .mr"),function(r){var ok=!q||r.getAttribute("data-q").indexOf(q)>=0; r.hidden=!ok; if(ok)shown++;});
 $("mload").hidden=shown>0||!models.length; if(!shown&&models.length)$("mload").textContent="Ничего не найдено";};
function loadHistory(reset){if(reset)page=1; fetch("/ask2many/api/history?page="+page+"&q="+encodeURIComponent(hq)).then(function(r){return r.json();}).then(function(d){
 var box=$("hist"); if(reset)box.textContent="";
 if(reset&&!d.items.length)box.appendChild(el("p","muted small",hq?"Ничего не найдено":"История пока пуста"));
 d.items.forEach(function(it){var det=el("details","h"); var s=el("summary");
  s.appendChild(el("span","date",it.created_at.replace("T"," ").slice(0,16))); s.appendChild(el("span",null,it.prompt.slice(0,140)));
  s.appendChild(el("span","hm","Моделей: "+it.results.length+" — "+it.results.map(function(r){return nameOf(r.model_id);}).join(", ")));
  det.appendChild(s); var b=el("div","body"); b.appendChild(el("p","full",it.prompt)); var g=el("div","grid"); render(g,it); b.appendChild(g); det.appendChild(b); box.appendChild(det);});
 $("more").hidden=!d.has_more;}).catch(function(){var box=$("hist"); if(reset){box.textContent=""; box.appendChild(el("p","muted small","Не удалось загрузить историю"));}});}
$("more").onclick=function(){page++; loadHistory(false);};
$("q").oninput=function(){clearTimeout(debounce); debounce=setTimeout(function(){hq=$("q").value.trim(); loadHistory(true);},300);};
fetch("/ask2many/api/models").then(function(r){return r.json();}).then(function(d){
 models=d.data; var dup={}; models.forEach(function(m){dup[m.name]=(dup[m.name]||0)+1;});
 var list=$("models"); $("mload").hidden=true;
 models.forEach(function(m){m.label=m.name+(dup[m.name]>1?" ("+m.provider+")":""); byId[m.id]=m;
  var r=el("div","mr"); r.setAttribute("role","button"); r.tabIndex=0; r.setAttribute("aria-pressed","false"); r.setAttribute("data-id",m.id);
  r.setAttribute("data-q",(m.label+" "+m.provider+" "+m.id).toLowerCase()); r.title=m.id;
  r.appendChild(el("span","n",m.label)); r.appendChild(el("span","s",score(m.intelligence_index)));
  var t=el("span","t"); if(m.subscription)t.appendChild(el("span",null,"подписка")); r.appendChild(t);
  r.onclick=function(){toggle(m.id);};
  r.onkeydown=function(e){if(e.key==="Enter"||e.key===" "||e.key==="Spacebar"){e.preventDefault(); toggle(m.id);}};
  list.appendChild(r);});
 if(!models.length){$("mload").hidden=false; $("mload").textContent="Список моделей пуст";}
 syncSel(); loadHistory(true);}).catch(function(){$("mload").textContent="Не удалось загрузить модели. Обновите страницу."; loadHistory(true);});
syncSel();
})();
</script></body></html>"""
