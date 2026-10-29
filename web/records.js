(function () {
  const el = {
    updated: document.getElementById("updated"),
    status: document.getElementById("status"),
    team: document.getElementById("team"),
    player: document.getElementById("player"),
    transaction: document.getElementById("transaction"),
    teamEmpty: document.getElementById("team-empty"),
    playerEmpty: document.getElementById("player-empty"),
    transactionEmpty: document.getElementById("transaction-empty"),
  };

  function fmtNum(value) {
    if (value === null || value === undefined || value === "") return "—";
    const num = Number(value);
    if (Number.isNaN(num)) return String(value);
    return num.toFixed(Number.isInteger(num) ? 0 : 2);
  }

  function fmtUpdated(iso) {
    if (!iso) return "알 수 없음";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleString();
  }

  function primaryValue(record, entry) {
    if (record.id === 3 || record.id === 4) return entry.length;
    if (record.id === 5 || record.id === 6) return entry.margin;
    if (record.id === 9 || record.id === 10) return entry.combined;
    if (record.id === 20) return entry.bid != null ? entry.bid : entry.score;
    return entry.score;
  }

  function entryMeta(record, entry) {
    const bits = [];
    if (record.id === 3 || record.id === 4) {
      bits.push("GW" + entry.periodFrom + "–GW" + entry.periodTo);
      return bits.join(" · ");
    }
    if (entry.position) bits.push(entry.position);
    if (entry.eplTeam) bits.push(entry.eplTeam);
    if (entry.period) bits.push("GW" + entry.period);
    if (entry.opponentName && record.category === "team") bits.push("vs " + entry.opponentName);
    if (
      entry.opponentScore != null &&
      (record.id === 5 || record.id === 6 || record.id === 9 || record.id === 10)
    ) {
      bits.push(fmtNum(entry.score) + "–" + fmtNum(entry.opponentScore));
    }
    if (entry.games != null && record.id === 19) {
      bits.push(entry.games + " GW");
    }
    if (entry.gp != null && record.id === 12) bits.push(entry.gp + " GP");
    if (entry.minutes != null && record.id === 13) bits.push(fmtNum(entry.minutes) + "분");
    if (record.category === "player" && entry.teamName) bits.push(entry.teamName);
    if (record.category === "transaction" && entry.teamName) bits.push(entry.teamName);
    if (record.id === 19 && entry.bid != null) bits.push("입찰 " + fmtNum(entry.bid));
    if (record.id === 20 && entry.fpts != null) bits.push("이후 " + fmtNum(entry.fpts) + " FP");
    if (record.id === 21) {
      if (entry.claims != null) bits.push("영입 " + entry.claims);
      if (entry.drops != null) bits.push("방출 " + entry.drops);
    }
    return bits.join(" · ");
  }

  function categoryLabel(category) {
    if (category === "team") return "팀";
    if (category === "player") return "선수";
    if (category === "transaction") return "거래";
    return category || "";
  }

  function displayName(record, entry) {
    if (record.category === "player" || record.category === "transaction") {
      return entry.playerName || "?";
    }
    return entry.teamName || entry.playerName || "?";
  }

  function formatValue(record, entry) {
    const value = fmtNum(primaryValue(record, entry));
    if (record.unit === "$") return "$" + value;
    if (record.unit) return value + " " + record.unit;
    return value;
  }

  function isFilled(record) {
    return record.status === "ready" && (record.entries || []).length;
  }

  function card(record) {
    const entries = record.entries || [];
    let body;
    if (isFilled(record)) {
      const leader = entries[0];
      const rest = entries.slice(1);
      body =
        "<div class='leader'>" +
        "<div class='leader-rank'>#1</div>" +
        "<div class='leader-name'>" +
        escapeHtml(displayName(record, leader)) +
        "</div>" +
        "<div class='leader-score'>" +
        escapeHtml(formatValue(record, leader)) +
        "</div>" +
        "<div class='leader-meta'>" +
        escapeHtml(entryMeta(record, leader)) +
        "</div>" +
        "</div>";
      if (rest.length) {
        body +=
          "<ol class='rest'>" +
          rest
            .map(function (entry, index) {
              const extra = entryMeta(record, entry);
              return (
                "<li" +
                (index === 0 ? " class='rest-second'" : "") +
                ">" +
                "<span class='rest-rank'>#" +
                (index + 2) +
                "</span>" +
                "<div class='rest-main'>" +
                "<div class='rest-name'>" +
                escapeHtml(displayName(record, entry)) +
                "</div>" +
                (extra ? "<div class='rest-meta'>" + escapeHtml(extra) + "</div>" : "") +
                "</div>" +
                "<div class='rest-score'>" +
                escapeHtml(formatValue(record, entry)) +
                "</div>" +
                "</li>"
              );
            })
            .join("") +
          "</ol>";
      }
    } else {
      body = "<div class='empty'>" + escapeHtml(record.reason || "—") + "</div>";
    }
    return (
      "<article class='record-card" +
      (isFilled(record) ? " filled" : " vacant") +
      "'>" +
      "<div class='meta'>#" +
      record.id +
      " · " +
      categoryLabel(record.category) +
      "</div>" +
      "<h2>" +
      escapeHtml(record.title) +
      "</h2>" +
      (record.blurb ? "<div class='blurb'>" + escapeHtml(record.blurb) + "</div>" : "") +
      body +
      "</article>"
    );
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function renderGroup(grid, emptyWrap, records) {
    const filled = records.filter(isFilled);
    const empty = records.filter(function (record) {
      return !isFilled(record);
    });
    grid.innerHTML = filled.map(card).join("");
    if (!emptyWrap) return;
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

  function render(data) {
    const groups = { team: [], player: [], transaction: [] };
    (data.records || []).forEach(function (record) {
      if (groups[record.category]) groups[record.category].push(record);
    });
    renderGroup(el.team, el.teamEmpty, groups.team);
    renderGroup(el.player, el.playerEmpty, groups.player);
    renderGroup(el.transaction, el.transactionEmpty, groups.transaction);
    const note = document.getElementById("note");
    if (note && data.completedThrough) {
      note.textContent =
        "GW" +
        data.completedThrough +
        "까지 완료. 진행 중인 GW는 제외합니다. 구단명은 프랜차이즈명입니다.";
    }
    el.updated.textContent =
      "마지막 업데이트: " +
      fmtUpdated(data.generatedAt) +
      " · GW " +
      (data.completedThrough || "—") +
      "까지 완료";
    const ready = (data.records || []).filter(isFilled).length;
    el.status.textContent = ready + " / " + (data.records || []).length + " 채움";
  }

  fetch("records.json", { cache: "no-store" })
    .then(function (res) {
      return res.json();
    })
    .then(function (data) {
      if (!data || data.ok === false) throw new Error("데이터 없음");
      render(data);
    })
    .catch(function () {
      el.status.textContent = "데이터 없음";
    });
})();
