(function () {
  const LOW_MINUTES = 90;
  const state = {
    rows: [],
    lastUpdated: null,
    sortKey: "fp90",
    sortDir: "desc",
    isLocalAdmin: false,
  };

  const el = {
    updated: document.getElementById("updated"),
    status: document.getElementById("status"),
    search: document.getElementById("search"),
    position: document.getElementById("position"),
    roster: document.getElementById("roster"),
    minutes: document.getElementById("minutes"),
    gp: document.getElementById("gp"),
    source: document.getElementById("source"),
    refresh: document.getElementById("refresh"),
    csv: document.getElementById("csv"),
    tbody: document.getElementById("tbody"),
    count: document.getElementById("count"),
  };

  function normalizeName(value) {
    return String(value || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/ø/g, "o")
      .replace(/Ø/g, "o")
      .toLowerCase();
  }

  function fmtNum(value, digits) {
    if (value === null || value === undefined || value === "") return "—";
    return Number(value).toFixed(digits);
  }

  function fmtUpdated(iso) {
    if (!iso) return "알 수 없음";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleString();
  }

  function isLowMinutes(row) {
    return row.minutes === null || row.minutes < LOW_MINUTES;
  }

  function currentFilters() {
    return {
      q: normalizeName(el.search.value.trim()),
      position: el.position.value,
      roster: el.roster.value,
      minutes: Number(el.minutes.value || 0),
      gp: Number(el.gp.value || 0),
      source: el.source.value,
    };
  }

  function filteredRows() {
    const f = currentFilters();
    return state.rows.filter((row) => {
      if (f.q && !normalizeName(row.player).includes(f.q)) return false;
      if (f.position && row.position !== f.position) return false;
      if (f.roster && row.rosterStatus !== f.roster) return false;
      if (f.minutes && !(row.minutes >= f.minutes)) return false;
      if (f.gp && !(row.gp >= f.gp)) return false;
      if (f.source && row.minutesSource !== f.source) return false;
      return true;
    });
  }

  function sortRows(rows) {
    const key = state.sortKey;
    const dir = state.sortDir === "asc" ? 1 : -1;
    const copy = rows.slice();
    copy.sort((a, b) => {
      const av = a[key];
      const bv = b[key];
      const aNull = av === null || av === undefined || av === "";
      const bNull = bv === null || bv === undefined || bv === "";
      if (aNull && bNull) return String(a.player).localeCompare(String(b.player));
      if (aNull) return 1;
      if (bNull) return -1;
      if (typeof av === "number" && typeof bv === "number") {
        if (av === bv) return String(a.player).localeCompare(String(b.player));
        return av > bv ? dir : -dir;
      }
      return String(av).localeCompare(String(bv)) * dir;
    });
    return copy;
  }

  function render() {
    const rows = sortRows(filteredRows());
    const highlightPool = rows.filter((row) => row.fp90 !== null && !isLowMinutes(row));
    const topCutoff = highlightPool.slice(0, 10).map((r) => r.fp90);
    const topSet = new Set(topCutoff);
    el.count.textContent = rows.length + "명";
    el.updated.textContent = "마지막 업데이트: " + fmtUpdated(state.lastUpdated);
    el.tbody.innerHTML = rows
      .map((row) => {
        const low = isLowMinutes(row);
        const fp90 = row.fp90 === null ? "—" : fmtNum(row.fp90, 2);
        const top = !low && row.fp90 !== null && topSet.has(row.fp90);
        const fp90Class = row.fp90 === null ? "muted" : low ? "warn" : "good";
        return (
          "<tr class='" +
          (top ? "top" : "") +
          "'>" +
          "<td class='player'>" +
          escapeHtml(row.player) +
          "</td>" +
          "<td>" +
          escapeHtml(row.position) +
          "</td>" +
          "<td>" +
          escapeHtml(row.eplTeam) +
          "</td>" +
          "<td class='num'>" +
          fmtNum(row.fpts, 2) +
          "</td>" +
          "<td class='num'>" +
          fmtNum(row.fpG, 2) +
          "</td>" +
          "<td class='num'>" +
          (row.minutes === null ? "—" : fmtNum(row.minutes, 1)) +
          "</td>" +
          "<td class='num'>" +
          (row.gp === null ? "—" : fmtNum(row.gp, 1)) +
          "</td>" +
          "<td class='num " +
          fp90Class +
          "'>" +
          fp90 +
          (low && row.fp90 !== null ? "<span class='low' title='Under 90 minutes'>low</span>" : "") +
          "</td>" +
          "<td class='muted'>" +
          escapeHtml(row.minutesSource || "—") +
          "</td>" +
          "<td class='" +
          (row.rosterStatus === "Available" ? "avail" : "rostered") +
          "'>" +
          escapeHtml(row.rosterStatus) +
          "</td>" +
          "<td>" +
          escapeHtml(row.ownerTeamName || "—") +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
    document.querySelectorAll("th[data-key]").forEach((th) => {
      th.classList.toggle("sorted", th.getAttribute("data-key") === state.sortKey);
    });
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function csvEscape(value) {
    const text = value === null || value === undefined ? "" : String(value);
    if (/[",\n]/.test(text)) return '"' + text.replace(/"/g, '""') + '"';
    return text;
  }

  function exportCsv() {
    const rows = sortRows(filteredRows());
    const headers = [
      "Player",
      "Pos",
      "EPL",
      "FPts",
      "FP/G",
      "Min",
      "GP",
      "FP/90",
      "Min source",
      "Roster",
      "Owner",
    ];
    const lines = [headers.join(",")];
    rows.forEach((row) => {
      lines.push(
        [
          row.player,
          row.position,
          row.eplTeam,
          row.fpts,
          row.fpG,
          row.minutes,
          row.gp,
          row.fp90,
          row.minutesSource || "",
          row.rosterStatus,
          row.ownerTeamName || "",
        ]
          .map(csvEscape)
          .join(",")
      );
    });
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "fantrax-fp90.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  function applyPayload(data) {
    state.rows = data.rows || [];
    state.lastUpdated = data.lastUpdated;
    el.status.textContent = data.missingMinutes
      ? data.count + "명 불러옴 (출전분 없음 " + data.missingMinutes + ")"
      : data.count + "명 불러옴";
    render();
  }

  function setAdminMode(enabled) {
    state.isLocalAdmin = !!enabled;
    document.body.classList.toggle("admin", state.isLocalAdmin);
    if (el.refresh) el.refresh.hidden = !state.isLocalAdmin;
  }

  async function readJson(url) {
    const res = await fetch(url, { cache: "no-store" });
    let data;
    try {
      data = await res.json();
    } catch (err) {
      throw new Error("데이터 없음");
    }
    if (!res.ok || data.ok === false) throw new Error((data && data.error) || "데이터 없음");
    return data;
  }

  async function loadData() {
    const localHost =
      location.hostname === "127.0.0.1" || location.hostname === "localhost";
    if (localHost) {
      el.status.textContent = "캐시 불러오는 중…";
      try {
        const data = await readJson("/api/fp90");
        setAdminMode(true);
        applyPayload(data);
        return;
      } catch (err) {
        setAdminMode(false);
      }
    } else {
      setAdminMode(false);
    }
    el.status.textContent = "스냅샷 불러오는 중…";
    applyPayload(await readJson("fp90.json"));
  }

  async function refresh() {
    if (!state.isLocalAdmin) return;
    el.refresh.disabled = true;
    el.status.textContent = "Fantrax에서 새로고침 중…";
    try {
      const res = await fetch("/api/refresh", { method: "POST", cache: "no-store" });
      let data;
      try {
        data = await res.json();
      } catch (err) {
        throw new Error("새로고침 실패");
      }
      if (!res.ok || data.ok === false) throw new Error(data.error || "새로고침 실패");
      applyPayload(data);
    } catch (err) {
      el.status.textContent = "새로고침 실패: " + err.message;
    } finally {
      el.refresh.disabled = false;
    }
  }

  el.search.addEventListener("input", render);
  el.position.addEventListener("change", render);
  el.roster.addEventListener("change", render);
  el.minutes.addEventListener("change", render);
  el.gp.addEventListener("change", render);
  el.source.addEventListener("change", render);
  if (el.refresh) el.refresh.addEventListener("click", refresh);
  el.csv.addEventListener("click", exportCsv);
  document.querySelectorAll("th[data-key]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.getAttribute("data-key");
      if (state.sortKey === key) {
        state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
      } else {
        state.sortKey = key;
        state.sortDir =
          key === "player" ||
          key === "position" ||
          key === "eplTeam" ||
          key === "rosterStatus" ||
          key === "ownerTeamName" ||
          key === "minutesSource"
            ? "asc"
            : "desc";
      }
      render();
    });
  });

  loadData().catch((err) => {
    el.status.textContent = "데이터 없음";
    if (err && err.message && err.message !== "데이터 없음") {
      el.status.textContent = "데이터 없음 (" + err.message + ")";
    }
  });
})();
