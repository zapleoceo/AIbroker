"""Public "ask many models" page: GET /ask2many + JSON under /ask2many/api/*. No login.

Everything dynamic is rendered client-side with textContent (never innerHTML), so model
answers and prompts typed by strangers cannot inject markup; the server sends only a static
shell. Logic lives in services/compare.py.
"""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from aibroker.auth import client_ip
from aibroker.providers import model_scores
from aibroker.routes.dashboard_assets import ASSETS_VERSION
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
    resp = HTMLResponse(_HTML.format(
        favicon=FAVICON_LINKS, v=ASSETS_VERSION, max_chars=svc.MAX_PROMPT_CHARS,
        max_models=svc.MAX_MODELS, busy=escape(svc.BUSY_MESSAGE),
        attribution=escape(model_scores.ATTRIBUTION),
        captured=escape(model_scores.CAPTURED_ON)))
    svc.set_browser_cookie(resp, svc.browser_id_from(request.cookies.get(svc.COOKIE_NAME)),
                           secure=_secure(request))
    return resp


@router.get("/ask2many/api/models")
async def models() -> dict[str, object]:
    return {"data": [{k: m[k] for k in ("id", "name", "provider", "intelligence_index")}
                     for m in svc.chat_models()],
            "attribution": model_scores.ATTRIBUTION, "source": model_scores.SOURCE,
            "captured_on": model_scores.CAPTURED_ON,
            "limits": {"max_prompt_chars": svc.MAX_PROMPT_CHARS, "max_models": svc.MAX_MODELS}}


@router.post("/ask2many/api/requests", status_code=202)
async def submit(body: SubmitIn, request: Request, response: Response) -> dict[str, object]:
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(422, "пустой вопрос")
    catalog = {m["id"]: m for m in svc.chat_models()}
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


_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ask2Many — один вопрос, много моделей</title>
{favicon}
<link rel="stylesheet" href="/dashboard/static/css/tokens.css?v={v}">
<link rel="stylesheet" href="/dashboard/static/css/public.css?v={v}">
<style>
.wrap{{max-width:1080px;margin:0 auto;padding:16px}}
h1{{font-size:22px;margin:8px 0}} h2{{font-size:17px;margin:24px 0 8px}}
textarea,input[type=search]{{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--line);
 border-radius:6px;background:var(--surface);color:var(--text);font:inherit}}
textarea{{min-height:110px;resize:vertical}}
.models{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:6px;margin:8px 0}}
.models label{{display:flex;gap:8px;align-items:center;padding:6px 8px;border:1px solid var(--line);
 border-radius:6px;background:var(--surface);cursor:pointer}}
.score{{margin-left:auto;font-family:var(--mono);color:var(--muted)}}
.row{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}} .muted{{color:var(--muted);font-size:13px}}
button{{padding:10px 18px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);
 color:var(--accent-fg);font:inherit;cursor:pointer}} button:disabled{{opacity:.5;cursor:not-allowed}}
button.ghost{{background:transparent;color:var(--accent)}}
.msg{{margin:8px 0;padding:10px;border-radius:6px;background:var(--warn-soft);display:none}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;margin-top:10px}}
.card{{border:1px solid var(--line);border-radius:8px;background:var(--surface);padding:10px;min-width:0}}
.card h3{{margin:0 0 4px;font-size:15px;overflow-wrap:anywhere}}
.ans{{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:8px;font-size:14px}}
.st-done{{color:var(--good)}} .st-error,.st-refused{{color:var(--bad)}}
details.h{{border:1px solid var(--line);border-radius:8px;background:var(--surface);margin:8px 0;padding:8px 10px}}
details.h summary{{cursor:pointer;overflow-wrap:anywhere}}
footer{{margin:28px 0;font-size:13px;color:var(--muted)}}
</style></head><body><div class="wrap">
<h1>Ask2Many</h1>
<p class="muted">Один вопрос уходит сразу во все отмеченные модели, ответы видно рядом. Платные модели могут
отказать: у проекта задан лимит расходов, и отказ показывается как есть.</p>
<textarea id="prompt" maxlength="{max_chars}" placeholder="Ваш вопрос"></textarea>
<p class="muted" id="public-note">Вопросы и ответы видны всем в общей истории.</p>
<div class="row"><span class="muted"><span id="cnt">0</span>/{max_chars}</span>
<span class="muted">Моделей: <span id="sel">0</span> (максимум {max_models})</span>
<button class="ghost" id="clear" type="button">Снять все</button></div>
<div class="models" id="models"></div>
<div class="row"><button id="go" type="button" disabled>Спросить</button><span class="muted" id="state"></span></div>
<div class="msg" id="msg" role="alert"></div>
<div class="grid" id="out"></div>

<h2>История запросов</h2>
<input type="search" id="q" placeholder="Поиск по тексту вопроса" autocomplete="off">
<div id="hist"></div>
<div class="row"><button class="ghost" id="more" type="button" hidden>Показать ещё</button></div>
<footer>{attribution}. Снимок оценок: {captured} (Intelligence Index). Оценка «—» значит, что для
точно такой версии модели данных нет.</footer>
</div>
<script>
(function(){{
var MAX={max_models}, BUSY="{busy}";
var $=function(i){{return document.getElementById(i);}};
var models=[], timer=null, active=false, page=1, hq="", debounce=null;
function el(tag,cls,text){{var e=document.createElement(tag); if(cls)e.className=cls; if(text!=null)e.textContent=text; return e;}}
function score(v){{return v==null?"\\u2014":String(v);}}
function showMsg(t){{var m=$("msg"); m.textContent=t||""; m.style.display=t?"block":"none";}}
function checked(){{return Array.prototype.filter.call(document.querySelectorAll("#models input"),function(i){{return i.checked;}}).map(function(i){{return i.value;}});}}
function refresh(){{var n=checked().length; $("sel").textContent=n; $("cnt").textContent=$("prompt").value.length;
 $("go").disabled=active||!n||n>MAX||!$("prompt").value.trim();}}
function resultCard(r,name){{var c=el("div","card"); c.appendChild(el("h3",null,name));
 var meta=el("div","muted","Оценка: "+score(r.intelligence_index)+" \\u00b7 модель: "+r.model_served+
  (r.latency_ms!=null?" \\u00b7 "+r.latency_ms+" мс":""));
 c.appendChild(meta);
 var labels={{pending:"ждём ответ\\u2026",done:"готово",error:"ошибка",refused:"отказ"}};
 c.appendChild(el("div","st-"+r.status,labels[r.status]||r.status));
 var body=r.status==="done"?r.answer:(r.error||""); if(body)c.appendChild(el("div","ans",body)); return c;}}
function render(target,data){{target.textContent=""; data.results.forEach(function(r){{target.appendChild(resultCard(r,r.model_id));}});}}
function poll(id){{fetch("/ask2many/api/requests/"+id).then(function(r){{return r.json();}}).then(function(d){{
 render($("out"),d); if(d.done){{active=false; $("state").textContent=""; refresh(); loadHistory(true);}}
 else timer=setTimeout(function(){{poll(id);}},2000);}}).catch(function(){{timer=setTimeout(function(){{poll(id);}},4000);}});}}
$("go").onclick=function(){{showMsg(""); active=true; refresh(); $("state").textContent="Идёт запрос\\u2026";
 fetch("/ask2many/api/requests",{{method:"POST",headers:{{"Content-Type":"application/json"}},
  body:JSON.stringify({{prompt:$("prompt").value,models:checked()}})}}).then(function(r){{return r.json().then(function(j){{return [r.status,j];}});}})
 .then(function(x){{if(x[0]===202){{$("out").textContent=""; poll(x[1].id);}}
  else{{active=false; $("state").textContent=""; var d=x[1].detail; showMsg(x[0]===429?BUSY:(typeof d==="string"?d:"Не удалось отправить вопрос")); refresh();}}}})
 .catch(function(){{active=false; $("state").textContent=""; showMsg("Нет связи с сервером"); refresh();}});}};
$("prompt").oninput=refresh;
$("clear").onclick=function(){{document.querySelectorAll("#models input").forEach(function(i){{i.checked=false;}}); refresh();}};
function loadHistory(reset){{if(reset)page=1; fetch("/ask2many/api/history?page="+page+"&q="+encodeURIComponent(hq)).then(function(r){{return r.json();}}).then(function(d){{
 var box=$("hist"); if(reset)box.textContent="";
 if(reset&&!d.items.length)box.appendChild(el("p","muted","Ничего не найдено"));
 d.items.forEach(function(it){{var det=el("details","h"); var s=el("summary",null,it.created_at.replace("T"," ").slice(0,16)+" \\u2014 "+it.prompt.slice(0,200));
  det.appendChild(s); var g=el("div","grid"); render(g,it); det.appendChild(g); box.appendChild(det);}});
 $("more").hidden=!d.has_more;}});}}
$("more").onclick=function(){{page++; loadHistory(false);}};
$("q").oninput=function(){{clearTimeout(debounce); debounce=setTimeout(function(){{hq=$("q").value.trim(); loadHistory(true);}},300);}};
fetch("/ask2many/api/models").then(function(r){{return r.json();}}).then(function(d){{
 var names={{}}; d.data.forEach(function(m){{names[m.name]=(names[m.name]||0)+1;}});
 var box=$("models"); d.data.forEach(function(m){{var l=el("label"); var c=el("input"); c.type="checkbox"; c.value=m.id;
  c.onchange=function(){{if(checked().length>MAX){{c.checked=false;}} refresh();}};
  var nm=el("span",null,m.name+(names[m.name]>1?" ("+m.provider+")":""));  l.appendChild(c); l.appendChild(nm); l.appendChild(el("span","score",score(m.intelligence_index)));
  l.title=m.id; box.appendChild(l);}}); refresh();}});
loadHistory(true); refresh();
}})();
</script></body></html>"""
