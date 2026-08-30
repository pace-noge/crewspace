"""M7.3-3 — Client-side board_delta application (DOM, no full re-render).

The real `static/board_live.js` runs under a tiny Node DOM shim (no browser),
mirroring the chat-render verification pattern (chat-render-dom-shim.md).
`applyBoardDelta(delta)` must, given a DOM with columns and cards:
  - card_created: append the card fragment into the target column.
  - card_moved:    move the card element from its current column to the target,
                   updating its in-place data (position) — no full re-render.
  - card_updated:  replace the card element's content in place.
  - comment_added: append the comment fragment to the card's comment list.

It must be resilient: unknown kind / missing node → no throw.
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JS_FILE = REPO / "src" / "crewspace" / "static" / "board_live.js"

SHIM = textwrap.dedent(
    r"""
    function parseOne(html){
      const m=html.match(/^\s*<([a-z0-9]+)([^>]*)>([\s\S]*)<\/\1>\s*$/i); if(!m) return null;
      const el=new El(m[1]); const id=m[2].match(/id="([^"]+)"/); const cls=m[2].match(/class="([^"]+)"/);
      if(id) el.id=id[1]; if(cls) el.className=cls[1]; el.textContent=m[3].replace(/<[^>]+>/g,""); return el;
    }
    class El {
      constructor(tag){ this.tagName=tag; this.children=[]; this.parentNode=null; this._text=""; this.dataset={}; this._attrs={};
        this.classList={_s:new Set(),add(...c){c.forEach(x=>this._s.add(x))},remove(...c){c.forEach(x=>this._s.delete(x))},contains(c){return this._s.has(c)}}; this.style={}; }
      set className(v){ this._class=v; } get className(){ return this._class||""; }
      set textContent(v){ this._text=v; this.children=[]; } get textContent(){ return this._text; }
      set innerHTML(v){ this._html=v; this.children=[]; const el=parseOne(v); if(el) this.append(el); } get innerHTML(){ return this._html||""; }
      set id(v){ this._attrs.id=v; } get id(){ return this._attrs.id||""; }
      get firstElementChild(){ return this.children[0]||null; }
      append(...nodes){ nodes.forEach(n=>{ if(n.parentNode){ n.parentNode.children=n.parentNode.children.filter(c=>c!==n); } n.parentNode=this; this.children.push(n); }); }
      appendChild(n){ this.append(n); return n; }
      addEventListener(){}
      querySelectorAll(sel){ const out=[]; const walk=n=>{ n.children.forEach(c=>{ if(sel[0]==="#" && c.id===sel.slice(1)) out.push(c); walk(c); }); }; walk(this); return out; }
      querySelector(sel){ return this.querySelectorAll(sel)[0]||null; }
      replaceChildren(...nodes){ this.children=[]; this.append(...nodes); }
      insertAdjacentHTML(where, html){ const el=parseOne(html); if(el) this.append(el); }
      replaceWith(next){ if(!this.parentNode) return; const p=this.parentNode; const i=p.children.indexOf(this); if(i>=0){ next.parentNode=p; p.children[i]=next; this.parentNode=null; } }
      remove(){ if(this.parentNode) this.parentNode.children=this.parentNode.children.filter(c=>c!==this); }
    }
    global.document={createElement:t=>new El(t),getElementById:()=>null,querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},readyState:"loading"};
    global.window={innerWidth:1200,innerHeight:800};
    global.getComputedStyle=()=>({display:"block"});
    global.setTimeout=()=>0; global.clearTimeout=()=>{};
    global.Intl=Intl; global.Date=Date; global.CSS={escape:s=>s};
    global.fetch=async()=>({ok:false,json:async()=>[]});
    const js = process.argv[1];
    eval(require("fs").readFileSync(js, "utf8"));
    // The file exposes the applier on window; alias it into the shim scope.
    const applyBoardDelta = (window.applyBoardDelta || function(){});
    const results = [];
    function test(name, fn){ try { fn(); results.push({name, ok:true}); } catch(e){ results.push({name, ok:false, err:String(e && e.message || e)}); } }

    // Build a minimal DOM: #board-wrap -> .board -> two columns with cards.
    function makeDom(){
      const wrap = new El("div"); wrap.id = "board-wrap";
      const board = new El("div"); board.className = "board";
      const colTodo = new El("div"); colTodo.id = "col-col_todo"; colTodo.className = "column";
      const cardC1 = new El("div"); cardC1.id = "card-c1"; cardC1.className = "card";
      const title = new El("div"); title.className = "title"; title.textContent = "Original";
      cardC1.append(title);
      colTodo.append(cardC1);
      const colDoing = new El("div"); colDoing.id = "col-col_doing"; colDoing.className = "column";
      board.append(colTodo, colDoing); wrap.append(board);
      return {wrap, board, colTodo, colDoing, cardC1};
    }

    test("card_created appends card fragment into target column", () => {
      const d = makeDom();
      applyBoardDelta({kind:"card_created", card_id:"c2", to_column_id:"col_todo", card_html:'<div class="card" id="card-c2">New</div>'}, d.wrap);
      if (d.colTodo.children.filter(c=>c.id==="card-c2").length !== 1) throw new Error("card-c2 not appended; children="+d.colTodo.children.map(c=>c.id).join(",")+" matched="+(d.wrap.querySelector("#col-col_todo")===d.colTodo));
    });

    test("card_moved relocates the card element to the target column", () => {
      const d = makeDom();
      applyBoardDelta({kind:"card_moved", card_id:"c1", from_column_id:"col_todo", to_column_id:"col_doing", card_html:'<div class="card" id="card-c1">Moved</div>'}, d.wrap);
      if (d.colTodo.children.some(c=>c.id==="card-c1")) throw new Error("card-c1 still in col-todo");
      if (!d.colDoing.children.some(c=>c.id==="card-c1")) throw new Error("card-c1 not in col-doing");
    });

    test("card_updated replaces the card content in place", () => {
      const d = makeDom();
      applyBoardDelta({kind:"card_updated", card_id:"c1", card_html:'<div class="card" id="card-c1">Updated</div>'}, d.wrap);
      const updated = d.wrap.querySelector("#card-c1");
      if (!updated || updated.textContent !== "Updated") throw new Error("card-c1 not updated in place; updated="+(updated&&updated.textContent)+" todo="+d.colTodo.children.map(c=>c.id+":"+c.textContent).join(","));
    });

    test("comment_added appends comment into the card comment list", () => {
      const d = makeDom();
      const comments = new El("div"); comments.id = "comments-c1";
      d.cardC1.append(comments);
      applyBoardDelta({kind:"comment_added", card_id:"c1", comment_html:'<div class="c">hi</div>'}, d.wrap);
      if (comments.children.length !== 1) throw new Error("comment not appended; commentsNode="+(d.wrap.querySelector("#comments-c1")===comments)+" count="+comments.children.length);
    });

    test("unknown kind is a no-op (no throw)", () => {
      const d = makeDom();
      applyBoardDelta({kind:"nonsense", card_id:"c1"}, d.wrap);
      if (d.colTodo.children.filter(c=>c.id==="card-c1").length !== 1) throw new Error("board mutated");
    });

    test("card_created twice with same card_id does not duplicate (self-echo/replay)", () => {
      const d = makeDom();
      const delta = {kind:"card_created", card_id:"c2", to_column_id:"col_todo", card_html:'<div class="card" id="card-c2">New</div>'};
      applyBoardDelta(delta, d.wrap);
      applyBoardDelta(delta, d.wrap);
      if (d.colTodo.children.filter(c=>c.id==="card-c2").length !== 1) throw new Error("card-c2 duplicated");
    });

    test("comment_added twice with same card_id does not duplicate (self-echo/replay)", () => {
      const d = makeDom();
      const comments = new El("div"); comments.id = "comments-c1";
      d.cardC1.append(comments);
      const delta = {kind:"comment_added", card_id:"c1", comment_id:"cmt1", comment_html:'<div class="c" id="comment-cmt1">hi</div>'};
      applyBoardDelta(delta, d.wrap);
      applyBoardDelta(delta, d.wrap);
      if (comments.children.length !== 1) throw new Error("comment duplicated");
    });

    for (const r of results) console.log((r.ok?"PASS":"FAIL") + " " + r.name + (r.err?" :: "+r.err:""));
    if (results.some(r=>!r.ok)) process.exit(1);
    """
)


@pytest.mark.skipif(not JS_FILE.exists(), reason="board_live.js not implemented yet")
def test_board_live_js_applies_deltas_in_place() -> None:
    """The real client applier must update one card/comment without reloading."""
    result = subprocess.run(
        ["node", "-e", SHIM, str(JS_FILE)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"board_live.js DOM test failed:\n{result.stdout}\n{result.stderr}"


def test_board_live_js_exists_and_registers_listener() -> None:
    """The board page must load board_live.js (the applier wiring)."""
    board_html = (REPO / "src" / "crewspace" / "templates" / "board.html").read_text()
    assert "board_live.js" in board_html
    live_js = JS_FILE.read_text()
    assert "applyBoardDelta" in live_js
    assert "WebSocket" in live_js