"""Production web dashboard for multi-channel Hype Clipper output."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix


CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
MEDIA_NAME_RE = re.compile(
    r"(?:preview_[A-Za-z0-9_-]+|highlight(?:_chat)?(?:_[0-9]+)?)\.mp4"
)
PUBLIC_STATUS_FIELDS = {
    "state",
    "updated_at",
    "channel",
    "exit_code",
    "message",
    "stream_started_at_epoch",
}
JST = timezone(timedelta(hours=9), "JST")


def normalize_channel(value: str) -> str:
    channel = value.strip().lower().lstrip("#")
    if not CHANNEL_RE.fullmatch(channel):
        raise ValueError("配信者IDは英数字とアンダースコアで入力してください")
    return channel


def now_iso_jst() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


DASHBOARD_HTML = r'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hype Clipper</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0e0e10;color:#efeff1;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}main{width:min(1380px,96vw);margin:28px auto 70px}.add{display:flex;gap:9px;background:#18181b;border:1px solid #2f2f35;border-radius:14px;padding:13px;margin-bottom:20px}.add input{min-width:0;flex:1;border:1px solid #53535f;border-radius:8px;background:#0e0e10;color:#fff;padding:12px;font-size:16px}.add button,.actions button{border:0;border-radius:8px;padding:10px 14px;font-weight:700;cursor:pointer}.add button{background:#9147ff;color:#fff}.tabs{display:flex;gap:8px;overflow-x:auto;padding:2px 2px 9px;scrollbar-width:thin}.tab{white-space:nowrap;border:1px solid #3a3a44;background:#18181b;color:#adadb8;border-radius:10px 10px 0 0;padding:11px 16px;font-size:15px;font-weight:700;cursor:pointer}.tab.active{background:#2b1745;border-color:#9147ff;color:#fff}.tab small{margin-left:7px;color:#bf94ff}.panel{background:#18181b;border:1px solid #2f2f35;border-radius:0 12px 12px 12px;overflow:hidden}.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 15px;border-bottom:1px solid #2f2f35}.name{font-size:20px;font-weight:750}.state{display:inline-block;margin-left:8px;padding:4px 9px;border-radius:999px;background:#26262c;color:#adadb8;font-size:13px}.state.running{background:#153d2a;color:#6ee7a2}.state.error{background:#491c24;color:#ff9aa8}.details{color:#adadb8;font-size:13px;margin-top:5px}.actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px}.sorts{display:flex;border:1px solid #53535f;border-radius:8px;overflow:hidden}.sort{border:0!important;border-radius:0!important;background:#26262c;color:#adadb8;padding:10px 12px!important}.sort.active{background:#9147ff;color:#fff}.delete{background:#4b2028;color:#ffb3bd}.highlights{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;align-items:start;padding:14px;background:#0e0e10}.highlight{background:#18181b;border:1px solid #2f2f35;border-radius:12px;padding:12px}.highlight h2{font-size:20px;margin:4px 4px 12px}.highlight video{display:block;width:100%;max-height:78vh;background:#000;border-radius:8px}.highlight-meta{color:#adadb8;font-size:13px;margin:10px 4px 3px}.waiting{color:#adadb8;background:#18181b;border:1px solid #2f2f35;border-radius:12px;padding:24px}.empty{color:#adadb8;background:#18181b;border:1px dashed #53535f;border-radius:14px;padding:26px}.notice{min-height:24px;color:#bf94ff;margin:5px 2px}.limit{color:#adadb8;font-size:13px;margin-top:14px}@media(max-width:600px){.add{flex-direction:column}.add button{width:100%}.panel-head{align-items:flex-start;flex-direction:column}.actions{width:100%}.sorts{flex:1}.sort{flex:1}.delete{width:100%}.highlights{grid-template-columns:1fr;padding:10px}}
</style></head><body><main>
<form class="add" id="addForm"><input id="channelInput" name="channel" autocomplete="off" maxlength="25" pattern="[A-Za-z0-9_]+" placeholder="Twitch配信者ID" required><button type="submit">監視に追加</button></form>
<div class="notice" id="notice"></div><div class="tabs" id="tabs" role="tablist"></div><section class="panel" id="panel" hidden><div class="panel-head"><div><div><span class="name" id="selectedName"></span><span class="state" id="selectedState"></span></div><div class="details" id="selectedDetails"></div></div><div class="actions"><div class="sorts" aria-label="並び順"><button class="sort" data-sort="rank">ランキング順</button><button class="sort" data-sort="newest">新着順</button></div><button class="delete" id="deleteSelected">停止して削除</button></div></div><div class="highlights" id="rankings"><div class="waiting">ランキングを読み込み中です。</div></div></section><div class="empty" id="empty">読み込み中です。</div><div class="limit" id="limit"></div>
</main><script>
const states={running:"収集中",starting:"開始中",stopping:"停止中",stopped:"配信終了",error:"エラー",not_started:"待機中",unknown:"状態不明"};
const tabs=document.querySelector("#tabs"),panel=document.querySelector("#panel"),empty=document.querySelector("#empty"),rankings=document.querySelector("#rankings"),notice=document.querySelector("#notice"),input=document.querySelector("#channelInput");
let channels=[],maxChannels=2,selectedChannel=decodeURIComponent(location.hash.slice(1)||""),rankingChannel="",rankingToken="",rankingLoading=false;
let sortMode="rank";try{sortMode=localStorage.getItem("hypeSortMode")||"rank"}catch(_){}
const esc=value=>String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const jst=value=>{if(!value)return "";try{return new Date(value).toLocaleString("ja-JP",{timeZone:"Asia/Tokyo",hour12:false})+" JST"}catch(_){return value}};
async function api(url,options={}){const response=await fetch(url,{cache:"no-store",...options,headers:{"Content-Type":"application/json",...(options.headers||{})}});const body=await response.json().catch(()=>({}));if(!response.ok)throw new Error(body.error||`HTTP ${response.status}`);return body}
function selectChannel(channel){selectedChannel=channel;history.replaceState(null,"",`#${encodeURIComponent(channel)}`);render()}
function cardRank(card){return Number(card.dataset.rank||parseInt(card.querySelector("h2")?.textContent||"999",10))}function cardStart(card){if(card.dataset.startSeconds)return Number(card.dataset.startSeconds);const match=(card.querySelector(".highlight-meta")?.textContent||"").match(/(\d+):(\d+):(\d+)/);return match?Number(match[1])*3600+Number(match[2])*60+Number(match[3]):0}function applyRankingSort(container=rankings){const cards=[...container.querySelectorAll(":scope > .highlight")];cards.sort((left,right)=>sortMode==="newest"?cardStart(right)-cardStart(left):cardRank(left)-cardRank(right));cards.forEach(card=>container.appendChild(card));document.querySelectorAll(".sort").forEach(button=>button.classList.toggle("active",button.dataset.sort===sortMode))}
function render(){document.querySelector("#limit").textContent=`${channels.length} / ${maxChannels} 配信者を監視設定中`;if(!channels.length){tabs.innerHTML="";panel.hidden=true;empty.hidden=false;empty.textContent="配信者IDを入力して監視を開始してください。";return}if(!channels.some(item=>item.channel===selectedChannel))selectedChannel=channels[0].channel;tabs.innerHTML=channels.map(item=>`<button class="tab ${item.channel===selectedChannel?"active":""}" role="tab" aria-selected="${item.channel===selectedChannel}" data-channel="${esc(item.channel)}">${esc(item.channel)}<small>${item.highlight_count||0}</small></button>`).join("");const item=channels.find(entry=>entry.channel===selectedChannel),state=item.status.state||"not_started";empty.hidden=true;panel.hidden=false;document.querySelector("#selectedName").textContent=item.channel;const stateNode=document.querySelector("#selectedState");stateNode.textContent=states[state]||state;stateNode.className=`state ${state}`;const updated=jst(item.content_updated_at||item.status.updated_at);document.querySelector("#selectedDetails").textContent=`暫定ハイライト ${item.highlight_count||0}件${updated?`・更新 ${updated}`:""}`;if(rankingChannel!==item.channel){rankingChannel=item.channel;rankingToken="";rankings.innerHTML='<div class="waiting">ランキングを読み込み中です。</div>'}refreshRanking()}
async function refreshRanking(){const channel=selectedChannel;if(!channel||rankingLoading||[...rankings.querySelectorAll("video")].some(video=>!video.paused&&!video.ended))return;rankingLoading=true;try{const pageUrl=`/channels/${encodeURIComponent(channel)}/reactions.html`,response=await fetch(`${pageUrl}?_=${Date.now()}`,{cache:"no-store"}),source=await response.text();if(channel!==selectedChannel)return;const doc=new DOMParser().parseFromString(source,"text/html"),sourceList=doc.querySelector(".highlights"),token=doc.body?.dataset.updateToken||source.length;if(!sourceList){rankings.innerHTML='<div class="waiting">ランキングを準備中です。</div>';return}if(rankingToken===token){applyRankingSort();return}sourceList.querySelectorAll("video[src]").forEach(video=>video.src=new URL(video.getAttribute("src"),new URL(pageUrl,location.origin)).href);applyRankingSort(sourceList);rankings.innerHTML=sourceList.innerHTML;rankingToken=token;applyRankingSort()}catch(error){notice.textContent=error.message}finally{rankingLoading=false}}
async function refresh(){try{const body=await api("/api/channels");channels=body.channels;maxChannels=body.max_channels;render()}catch(error){notice.textContent=error.message}}
document.querySelector("#addForm").addEventListener("submit",async event=>{event.preventDefault();notice.textContent="追加しています…";try{const result=await api("/api/channels",{method:"POST",body:JSON.stringify({channel:input.value})});selectedChannel=result.channel;input.value="";notice.textContent="監視を追加しました。";await refresh()}catch(error){notice.textContent=error.message}});
tabs.addEventListener("click",event=>{const tab=event.target.closest("button[data-channel]");if(tab)selectChannel(tab.dataset.channel)});
document.querySelector(".sorts").addEventListener("click",event=>{const button=event.target.closest("button[data-sort]");if(!button)return;sortMode=button.dataset.sort;try{localStorage.setItem("hypeSortMode",sortMode)}catch(_){}applyRankingSort()});
document.querySelector("#deleteSelected").addEventListener("click",async event=>{const channel=selectedChannel;if(!channel||!confirm(`${channel} の監視を停止し、ランキングと動画を削除しますか？`))return;event.currentTarget.disabled=true;try{await api(`/api/channels/${encodeURIComponent(channel)}`,{method:"DELETE"});notice.textContent=`${channel} を停止して削除します。`;selectedChannel="";rankingChannel="";rankingToken="";rankings.innerHTML='<div class="waiting">ランキングを読み込み中です。</div>';await refresh()}catch(error){notice.textContent=error.message}finally{event.currentTarget.disabled=false}});
refresh();setInterval(refresh,5000);
</script></body></html>'''


def create_app(
    data_dir: str | Path | None = None,
    control_dir: str | Path | None = None,
) -> Flask:
    output_root = Path(data_dir or os.environ.get("HYPE_DATA_DIR", "/data")).resolve()
    configured_control = control_dir or os.environ.get("HYPE_CONTROL_DIR")
    control_root = Path(configured_control or output_root / "control").resolve()
    channels_root = output_root / "channels"
    channels_file = control_root / "channels.json"
    max_channels = max(1, int(os.environ.get("MAX_CHANNELS", "2")))
    config_lock = threading.Lock()
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    def channel_dir(channel: str) -> Path:
        normalized = normalize_channel(channel)
        target = (channels_root / normalized).resolve()
        if target.parent != channels_root.resolve():
            raise ValueError("unsafe channel path")
        return target

    def read_entries() -> list[dict]:
        try:
            payload = json.loads(channels_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            return []
        entries = []
        for entry in payload.get("channels", []):
            if not isinstance(entry, dict):
                continue
            try:
                channel = normalize_channel(str(entry.get("channel", "")))
            except ValueError:
                continue
            entries.append({**entry, "channel": channel})
        return entries[:max_channels]

    def write_entries(entries: list[dict]) -> None:
        atomic_write_json(channels_file, {"channels": entries})

    def status_payload(channel: str) -> dict:
        status_file = channel_dir(channel) / "service_status.json"
        try:
            raw = json.loads(status_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"state": "not_started", "channel": channel}
        except (OSError, json.JSONDecodeError):
            return {"state": "unknown", "channel": channel}
        return {key: raw[key] for key in PUBLIC_STATUS_FIELDS if key in raw}

    def channel_payload(entry: dict) -> dict:
        channel = entry["channel"]
        directory = channel_dir(channel)
        page = directory / "reactions.html"
        try:
            content_updated_at = datetime.fromtimestamp(
                page.stat().st_mtime, tz=JST
            ).isoformat(timespec="seconds")
        except OSError:
            content_updated_at = None
        highlight_count = sum(
            1 for path in directory.glob("preview_*.mp4") if path.is_file()
        )
        if not highlight_count:
            highlight_count = sum(
                1 for path in directory.glob("highlight_chat_*.mp4") if path.is_file()
            )
        return {
            "channel": channel,
            "added_at": entry.get("added_at"),
            "status": status_payload(channel),
            "highlight_count": highlight_count,
            "content_updated_at": content_updated_at,
            "url": f"/channels/{channel}/reactions.html",
        }

    @app.after_request
    def cache_policy(response):
        if response.mimetype in {"text/html", "application/json"}:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/")
    def index():
        return Response(DASHBOARD_HTML, mimetype="text/html")

    @app.get("/reactions.html")
    def old_index():
        return redirect("/", code=302)

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    @app.get("/api/status")
    def api_status():
        entries = read_entries()
        return jsonify(
            state="running",
            max_channels=max_channels,
            channels=[channel_payload(entry) for entry in entries],
        )

    @app.get("/api/channels")
    def api_channels():
        entries = read_entries()
        return jsonify(
            max_channels=max_channels,
            channels=[channel_payload(entry) for entry in entries],
        )

    @app.post("/api/channels")
    def add_channel():
        if not request.is_json:
            return jsonify(error="JSONで配信者IDを送信してください"), 415
        try:
            channel = normalize_channel(str((request.get_json() or {}).get("channel", "")))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        with config_lock:
            entries = read_entries()
            if any(entry["channel"] == channel for entry in entries):
                return jsonify(channel=channel, already_exists=True), 200
            if len(entries) >= max_channels:
                return jsonify(error=f"同時監視は最大{max_channels}配信者です"), 409
            entries.append(
                {
                    "channel": channel,
                    "request_id": secrets.token_urlsafe(18),
                    "added_at": now_iso_jst(),
                }
            )
            write_entries(entries)
        return jsonify(channel=channel, accepted=True), 202

    @app.delete("/api/channels/<channel>")
    def delete_channel(channel: str):
        try:
            channel = normalize_channel(channel)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        with config_lock:
            entries = read_entries()
            remaining = [entry for entry in entries if entry["channel"] != channel]
            if len(remaining) == len(entries):
                return jsonify(error="監視対象にありません"), 404
            write_entries(remaining)
        return jsonify(channel=channel, deleting=True), 202

    @app.get("/api/channels/<channel>/highlights")
    def api_highlights(channel: str):
        try:
            channel = normalize_channel(channel)
            directory = channel_dir(channel)
        except ValueError:
            abort(404)
        files = []
        for path in directory.glob("*.mp4"):
            if not MEDIA_NAME_RE.fullmatch(path.name):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append(
                {
                    "name": path.name,
                    "url": f"/channels/{channel}/{path.name}",
                    "bytes": stat.st_size,
                    "updated_at": stat.st_mtime,
                }
            )
        files.sort(key=lambda item: (-item["updated_at"], item["name"]))
        return jsonify(highlights=files, status=status_payload(channel))

    @app.get("/channels/<channel>/")
    def channel_index(channel: str):
        try:
            channel = normalize_channel(channel)
        except ValueError:
            abort(404)
        return redirect(f"/channels/{channel}/reactions.html", code=302)

    @app.get("/channels/<channel>/reactions.html")
    def reactions(channel: str):
        try:
            channel = normalize_channel(channel)
            directory = channel_dir(channel)
        except ValueError:
            abort(404)
        page = directory / "reactions.html"
        if page.is_file():
            return send_from_directory(directory, page.name, conditional=True, max_age=0)
        state = status_payload(channel).get("state", "not_started")
        return Response(
            "<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"refresh\" content=\"5\"><title>Hype Clipper</title>"
            "<style>body{font-family:system-ui;background:#0e0e10;color:#efeff1;"
            "display:grid;place-items:center;min-height:100vh;margin:0}main{padding:28px;"
            "background:#18181b;border-radius:14px}a{color:#bf94ff}</style></head>"
            f"<body><main><a href=\"/\">← 監視一覧</a><h1>{channel}</h1>"
            f"<p>ハイライトを準備中です。</p><small>状態: {state}</small></main></body></html>",
            mimetype="text/html",
        )

    @app.get("/channels/<channel>/<filename>")
    def media(channel: str, filename: str):
        try:
            channel = normalize_channel(channel)
            directory = channel_dir(channel)
        except ValueError:
            abort(404)
        if "/" in filename or not MEDIA_NAME_RE.fullmatch(filename):
            abort(404)
        path = directory / filename
        if not path.is_file():
            abort(404)
        return send_from_directory(directory, filename, conditional=True, max_age=60)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
