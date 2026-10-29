(function () {
  const el = {
    updated: document.getElementById("updated"),
    status: document.getElementById("status"),
    automatic: document.getElementById("automatic"),
    voted: document.getElementById("voted"),
    automaticEmpty: document.getElementById("automatic-empty"),
    votedEmpty: document.getElementById("voted-empty"),
  };

  function fmtNum(value) {
    if (value === null || value === undefined || value === "") return "—";
    const num = Number(value);
    if (Number.isNaN(num)) return String(value);
    if (Number.isInteger(num)) return String(num);
    return num.toFixed(Math.abs(num) < 1 ? 3 : 2);
  }

  function fmtUpdated(iso) {
    if (!iso) return "알 수 없음";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleString();
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function kindLabel(kind) {
    if (kind === "voted") return "투표";
    if (kind === "automatic") return "자동";
    return kind || "";
  }

  function isFilled(slot) {
    return slot.status === "ready" && (slot.entries || []).length;
  }

  function primaryValue(slot, entry) {
    if (slot.id === 28) return entry.wins;
    if (slot.id === 29) return entry.winPct;
    if (slot.id === 31) return entry.record;
    return entry.score;
  }

  function entryMeta(slot, entry) {
    const bits = [];
    if (entry.record && slot.id !== 31) bits.push(entry.record);
    if (entry.games != null) bits.push(entry.games + " GW");
    if (entry.score != null && (slot.id === 28 || slot.id === 29 || slot.id === 31)) {
      bits.push(fmtNum(entry.score) + " FP");
    }
    return bits.join(" · ");
  }

  function formatValue(slot, entry) {
    const value = fmtNum(primaryValue(slot, entry));
    if (slot.unit) return value + " " + slot.unit;
    return value;
  }

  function card(slot) {
    const entries = slot.entries || [];
    let body;
    if (isFilled(slot)) {
      const leader = entries[0];
      const rest = entries.slice(1);
      body =
        "<div class='leader'>" +
        "<div class='leader-rank'>#1</div>" +
        "<div class='leader-name'>" +
        escapeHtml(leader.teamName || leader.playerName || "?") +
        "</div>" +
        "<div class='leader-score'>" +
        escapeHtml(formatValue(slot, leader)) +
        "</div>" +
        "<div class='leader-meta'>" +
        escapeHtml(entryMeta(slot, leader)) +
        "</div>" +
        "</div>";
      if (rest.length) {
        body +=
          "<ol class='rest'>" +
          rest
            .map(function (entry, index) {
              const extra = entryMeta(slot, entry);
              return (
                "<li" +
                (index === 0 ? " class='rest-second'" : "") +
                ">" +
                "<span class='rest-rank'>#" +
                (index + 2) +
                "</span>" +
                "<div class='rest-main'>" +
                "<div class='rest-name'>" +
                escapeHtml(entry.teamName || entry.playerName || "?") +
                "</div>" +
                (extra ? "<div class='rest-meta'>" + escapeHtml(extra) + "</div>" : "") +
                "</div>" +
                "<div class='rest-score'>" +
                escapeHtml(formatValue(slot, entry)) +
                "</div>" +
                "</li>"
              );
            })
            .join("") +
          "</ol>";
      }
    } else {
      body = "<div class='empty'>" + escapeHtml(slot.reason || "—") + "</div>";
    }
    return (
      "<article class='record-card" +
      (isFilled(slot) ? " filled" : " vacant") +
      "'>" +
      "<div class='hof-kind'>" +
      escapeHtml(kindLabel(slot.kind)) +
      (slot.provisional ? " · 잠정" : "") +
      "</div>" +
      "<div class='meta'>#" +
      slot.id +
      "</div>" +
      "<h2>" +
      escapeHtml(slot.title) +
      "</h2>" +
      (slot.blurb ? "<div class='blurb'>" + escapeHtml(slot.blurb) + "</div>" : "") +
      body +
      "</article>"
    );
  }

  function renderGroup(grid, emptyWrap, slots) {
    const filled = slots.filter(isFilled);
    const empty = slots.filter(function (slot) {
      return !isFilled(slot);
    });
    grid.innerHTML = filled.map(card).join("");
    if (!empty.length) {
      emptyWrap.innerHTML = "";
      return;
    }
    emptyWrap.innerHTML =
      "<details class='empty-block'><summary>빈 칸 (" +
      empty.length +
      ")</summary><div class='record-grid'>" +
      empty.map(card).join("") +
      "</div></details>";
  }

  fetch("hof.json", { cache: "no-store" })
    .then(function (res) {
      return res.json();
    })
    .then(function (data) {
      if (!data || data.ok === false) throw new Error("데이터 없음");
      const automatic = [];
      const voted = [];
      (data.hof || []).forEach(function (slot) {
        if (slot.kind === "voted") voted.push(slot);
        else automatic.push(slot);
      });
      renderGroup(el.automatic, el.automaticEmpty, automatic);
      renderGroup(el.voted, el.votedEmpty, voted);
      const note = document.getElementById("note");
      if (note && data.completedThrough) {
        note.textContent =
          "자동 칸의 승·승률·FP는 2026/27 잠정입니다. GW" +
          data.completedThrough +
          "까지 완료, 진행 중인 GW는 제외합니다. 투표 명패와 시즌 종료 타이틀은 접혀 있습니다.";
      }
      el.updated.textContent =
        "마지막 업데이트: " +
        fmtUpdated(data.generatedAt) +
        (data.completedThrough ? " · GW " + data.completedThrough + "까지 완료" : "");
      const ready = (data.hof || []).filter(isFilled).length;
      el.status.textContent = ready + " / " + (data.hof || []).length + " 채움";
    })
    .catch(function () {
      el.updated.textContent = "데이터 없음";
    });
})();
