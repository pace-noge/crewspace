// Board live-updates: subscribe to the board room and apply minimal deltas
// in place, so other viewers see card create/move/update/comment changes
// without a page reload. The acting client keeps its canonical whole-board
// feedback (HTMX swaps #board-wrap); this file only patches non-acting DOM.
(function () {
  "use strict";

  var BOARD_ID =
    (document.querySelector("meta[name=board-id]") || {}).content || null;

  // Apply one board_delta to the current DOM. Exported for the DOM-shim test.
  function applyBoardDelta(delta, root) {
    if (!delta || typeof delta !== "object") return;
    root = root || document;
    var kind = delta.kind;
    if (kind === "card_created") {
      var col = root.querySelector("#col-" + delta.to_column_id);
      if (!col || !delta.card_html) return;
      if (root.querySelector("#card-" + delta.card_id)) return; // self-echo/replay guard
      col.insertAdjacentHTML("beforeend", delta.card_html);
      return;
    }
    if (kind === "card_moved") {
      var card = root.querySelector("#card-" + delta.card_id);
      if (!card || !delta.to_column_id) return;
      var target = root.querySelector("#col-" + delta.to_column_id);
      if (!target) return;
      // RELOCATE the card to the target column: remove it from its current
      // column, then insert the canonical server-rendered fragment (or the
      // existing node) at the end of the target column. Replacing in place
      // would leave the card in its OLD column — a phantom "move".
      if (delta.card_html) {
        var fresh = document.createElement("div");
        fresh.innerHTML = delta.card_html;
        var freshCard = fresh.querySelector("#card-" + delta.card_id) || fresh.firstElementChild;
        card.remove();
        target.appendChild(freshCard || card);
      } else {
        target.appendChild(card);
      }
      return;
    }
    if (kind === "card_updated") {
      var targetCard = root.querySelector("#card-" + delta.card_id);
      if (!targetCard || !delta.card_html) return;
      var fresh2 = document.createElement("div");
      fresh2.innerHTML = delta.card_html;
      var freshCard2 = fresh2.querySelector("#card-" + delta.card_id) || fresh2.firstElementChild;
      if (freshCard2) targetCard.replaceWith(freshCard2);
      return;
    }
    if (kind === "comment_added") {
      var comments = root.querySelector("#comments-" + delta.card_id);
      if (!comments || !delta.comment_html || !comments.insertAdjacentHTML) return;
      // Guard against double-apply (self-echo/reconnect replay): skip when the
      // comment's canonical node is already present.
      if (delta.comment_id && comments.querySelector("#comment-" + delta.comment_id)) return;
      comments.insertAdjacentHTML("beforeend", delta.comment_html);
      return;
    }
    // Unknown kind: no-op (fail-open client-side, server never sends it).
  }

  // Expose for the real DOM-shim test and potential future reuse. Do this
  // before the board-page guard so loading the module without a page is safe.
  window.applyBoardDelta = applyBoardDelta;

  // Wire the socket only on the board page.
  if (!BOARD_ID || typeof WebSocket === "undefined") return;

  function connectLive() {
    var protocol = location.protocol === "https:" ? "wss" : "ws";
    var ws;
    try {
      ws = new WebSocket(protocol + "://" + location.host + "/boards/" + BOARD_ID + "/ws");
    } catch (e) {
      setTimeout(connectLive, 2000);
      return;
    }
    ws.onmessage = function (event) {
      var data;
      try { data = JSON.parse(event.data); } catch (e) { return; }
      if (data.type === "board_delta" && data.board_id === BOARD_ID) {
        applyBoardDelta(data.delta);
      }
    };
    ws.onclose = function () { setTimeout(connectLive, 1000); };
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", connectLive);
  } else {
    connectLive();
  }
})();