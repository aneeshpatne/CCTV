const state = {
  events: [],
  visibleEvents: 8,
  liveUrl: "",
  hours: 24,
  selectedEventId: null,
};

const elements = {
  systemState: document.querySelector("#systemState"),
  systemStateText: document.querySelector("#systemStateText"),
  refreshButton: document.querySelector("#refreshButton"),
  clock: document.querySelector("#clock"),
  today: document.querySelector("#today"),
  dayPeriod: document.querySelector("#dayPeriod"),
  liveFrame: document.querySelector("#liveFrame"),
  clipPlayer: document.querySelector("#clipPlayer"),
  playerStage: document.querySelector("#playerStage"),
  playerLoading: document.querySelector("#playerLoading"),
  playerEyebrow: document.querySelector("#playerEyebrow"),
  playerTitle: document.querySelector("#playerTitle"),
  liveButton: document.querySelector("#liveButton"),
  popoutButton: document.querySelector("#popoutButton"),
  eventList: document.querySelector("#eventList"),
  eventCount: document.querySelector("#eventCount"),
  hoursFilter: document.querySelector("#hoursFilter"),
  dateFilter: document.querySelector("#dateFilter"),
  showMoreButton: document.querySelector("#showMoreButton"),
  latestRecording: document.querySelector("#latestRecording"),
  motionStat: document.querySelector("#motionStat"),
  motionPeriod: document.querySelector("#motionPeriod"),
  recordingStat: document.querySelector("#recordingStat"),
  activitySummary: document.querySelector("#activitySummary"),
  sparkBars: document.querySelector("#sparkBars"),
  toast: document.querySelector("#toast"),
};

function updateClock() {
  const now = new Date();
  const hour = now.getHours();
  elements.clock.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  elements.today.textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  elements.dayPeriod.textContent = hour < 12 ? "morning" : hour < 17 ? "afternoon" : "evening";
}

function formatEventTime(value) {
  const date = new Date(value);
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  const isSameDay = (a, b) => a.toDateString() === b.toDateString();
  const prefix = isSameDay(date, today) ? "Today" : isSameDay(date, yesterday) ? "Yesterday" : date.toLocaleDateString([], { month: "short", day: "numeric" });
  return `${prefix}, ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
}

function relativeTime(value) {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function durationLabel(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 60) return `${Math.round(value)}s`;
  return `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
}

function eventIcon() {
  return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h3l2-5 4 10 2-5h5" /></svg>`;
}

function eventDescription(event) {
  const labels = Array.isArray(event.labels) ? event.labels : [];
  const names = labels
    .map(label => {
      if (typeof label === "string") return label;
      if (label && typeof label === "object") return label.name || label.label || label.type || "";
      return "";
    })
    .filter(Boolean)
    .map(name => String(name).replaceAll("_", " "));
  if (names.length) {
    return names
      .map(name => `${name.charAt(0).toUpperCase()}${name.slice(1)}`)
      .join(" · ");
  }
  return "Motion detected";
}

function renderEvents() {
  elements.eventCount.textContent = state.events.length;
  elements.motionStat.textContent = state.events.length.toLocaleString();

  if (!state.events.length) {
    elements.eventList.innerHTML = `<div class="empty-state"><strong>No activity found</strong><span>Try a wider time range or another day.</span></div>`;
    elements.showMoreButton.hidden = true;
    renderActivityBars([]);
    return;
  }

  elements.eventList.innerHTML = "";
  state.events.slice(0, state.visibleEvents).forEach(event => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `event-item${state.selectedEventId === event.id ? " active" : ""}`;
    item.dataset.eventId = String(event.id);
    item.innerHTML = `
      <span class="event-thumb">${eventIcon()}</span>
      <span class="event-copy">
        <strong>${eventDescription(event)}</strong>
        <small>${formatEventTime(event.start_time)}</small>
      </span>
      <span class="event-duration">${durationLabel(event.duration)}</span>
    `;
    item.addEventListener("click", () => playEvent(event));
    elements.eventList.appendChild(item);
  });
  elements.showMoreButton.hidden = state.visibleEvents >= state.events.length;
  renderActivityBars(state.events);
}

function updateEventSelection() {
  elements.eventList.querySelectorAll(".event-item").forEach(item => {
    item.classList.toggle("active", item.dataset.eventId === String(state.selectedEventId));
  });
}

function renderActivityBars(events) {
  const buckets = Array.from({ length: 24 }, () => 0);
  events.forEach(event => { buckets[new Date(event.start_time).getHours()] += 1; });
  const max = Math.max(...buckets, 1);
  elements.sparkBars.innerHTML = buckets.map((count, hour) => (
    `<span class="spark-bar" style="--height:${Math.max(5, (count / max) * 100)}%" title="${hour.toString().padStart(2, "0")}:00 · ${count} event${count === 1 ? "" : "s"}"></span>`
  )).join("");
  const peak = buckets.indexOf(Math.max(...buckets));
  elements.activitySummary.textContent = events.length
    ? `Peak activity around ${peak.toString().padStart(2, "0")}:00`
    : "No motion in this period";
}

function clipUrl(event) {
  const params = new URLSearchParams({
    start: event.start_time,
    end: event.end_time,
    pre_seconds: "8",
    post_seconds: "8",
  });
  return `/video/v2/by-event?${params}`;
}

function playEvent(event) {
  const url = clipUrl(event);
  const viewport = { left: window.scrollX, top: window.scrollY };
  const restoreViewport = () => window.scrollTo(viewport);
  const startPlayback = () => {
    elements.clipPlayer.play().catch(() => {});
    restoreViewport();
    requestAnimationFrame(restoreViewport);
  };
  state.selectedEventId = event.id;
  elements.playerStage.classList.add("clip-mode");
  elements.liveButton.classList.remove("active");
  elements.playerEyebrow.textContent = "Recorded event";
  elements.playerTitle.textContent = formatEventTime(event.start_time);
  elements.popoutButton.href = url;
  elements.playerLoading.hidden = false;
  elements.clipPlayer.muted = true;
  elements.clipPlayer.src = url;
  elements.clipPlayer.addEventListener("loadedmetadata", restoreViewport, { once: true });
  elements.clipPlayer.addEventListener("canplay", restoreViewport, { once: true });
  elements.clipPlayer.addEventListener("canplay", startPlayback, { once: true });
  elements.clipPlayer.load();
  updateEventSelection();
  requestAnimationFrame(restoreViewport);
}

function showLive() {
  state.selectedEventId = null;
  elements.clipPlayer.pause();
  elements.clipPlayer.removeAttribute("src");
  elements.clipPlayer.load();
  elements.playerLoading.hidden = true;
  elements.playerStage.classList.remove("clip-mode");
  elements.liveButton.classList.add("active");
  elements.playerEyebrow.textContent = "Live view";
  elements.playerTitle.textContent = "Front camera";
  elements.popoutButton.href = state.liveUrl;
  updateEventSelection();
}

function setSystemState(online, label = online ? "All systems operational" : "Server unavailable") {
  elements.systemState.classList.toggle("offline", !online);
  elements.systemStateText.textContent = label;
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  clearTimeout(showToast.timeout);
  showToast.timeout = setTimeout(() => elements.toast.classList.remove("visible"), 2600);
}

async function fetchJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

function applyDashboardData(data) {
  state.events = [...(data.events || [])].sort((a, b) => new Date(b.start_time) - new Date(a.start_time));
  state.liveUrl = data.live_stream_url;
  if (!elements.liveFrame.src) elements.liveFrame.src = state.liveUrl;
  if (!elements.playerStage.classList.contains("clip-mode")) elements.popoutButton.href = state.liveUrl;
  elements.recordingStat.textContent = Number(data.recordings_count || 0).toLocaleString();
  elements.latestRecording.textContent = data.latest_recording
    ? `Latest segment ${relativeTime(data.latest_recording.timestamp)}`
    : "No recordings indexed";
  elements.motionPeriod.textContent = `Past ${data.hours === 1 ? "hour" : `${data.hours} hours`}`;
  renderEvents();
}

async function loadDashboard({ announce = false } = {}) {
  elements.refreshButton.style.transform = "rotate(90deg)";
  try {
    const data = await fetchJson(`/api/dashboard?hours=${state.hours}`);
    applyDashboardData(data);
    setSystemState(true);
    if (announce) showToast("Dashboard refreshed");
  } catch (error) {
    setSystemState(false);
    elements.eventList.innerHTML = `<div class="empty-state"><strong>Could not load activity</strong><span>${error.message}</span></div>`;
    if (announce) showToast(error.message);
  } finally {
    elements.refreshButton.style.transform = "";
  }
}

async function loadDate(date) {
  elements.eventList.innerHTML = `<div class="event-skeleton"></div><div class="event-skeleton"></div>`;
  try {
    const data = await fetchJson(`/motion/day?date=${encodeURIComponent(date)}`);
    state.events = [...(data.events || [])].sort((a, b) => new Date(b.start_time) - new Date(a.start_time));
    state.visibleEvents = 8;
    elements.motionPeriod.textContent = new Date(`${date}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
    renderEvents();
  } catch (error) {
    showToast(error.message);
  }
}

elements.clipPlayer.addEventListener("canplay", () => { elements.playerLoading.hidden = true; });
elements.clipPlayer.addEventListener("playing", () => { elements.playerLoading.hidden = true; });
elements.clipPlayer.addEventListener("error", () => {
  elements.playerLoading.hidden = true;
  showToast("This event clip could not be prepared");
});
elements.liveButton.addEventListener("click", showLive);
elements.refreshButton.addEventListener("click", () => loadDashboard({ announce: true }));
elements.hoursFilter.addEventListener("change", () => {
  state.hours = Number(elements.hoursFilter.value);
  state.visibleEvents = 8;
  elements.dateFilter.value = "";
  loadDashboard();
});
elements.dateFilter.addEventListener("change", () => {
  if (elements.dateFilter.value) loadDate(elements.dateFilter.value);
});
elements.showMoreButton.addEventListener("click", () => {
  state.visibleEvents += 8;
  renderEvents();
});

updateClock();
setInterval(updateClock, 1000);
loadDashboard();
setInterval(() => loadDashboard(), 60_000);
