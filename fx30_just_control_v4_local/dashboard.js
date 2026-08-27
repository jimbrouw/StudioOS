const $ = (selector) => document.querySelector(selector);
const labels = {
  idle: "Idle",
  queued: "Queued",
  waiting_for_files: "Waiting for files to close",
  backing_up: "Copying SSD backup",
  copying_masters: "Finalising local masters",
  creating_proxies: "Creating proxy files",
  copying_proxies: "Verifying local proxies",
  cancelling: "Cancelling safely…",
  cancelled: "Cancelled",
  complete: "Complete",
  failed: "Failed",
};
const order = ["copying_masters", "creating_proxies", "copying_proxies", "complete"];
let busy = false;

function bytes(value) {
  if (!Number.isFinite(value)) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) { value /= 1000; unit += 1; }
  return `${value.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`;
}

function clock(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 3600)}h ${String(Math.floor((whole % 3600) / 60)).padStart(2, "0")}m`;
}

function elapsedSince(isoTime) {
  const started = Date.parse(isoTime || "");
  return Number.isFinite(started) ? Math.max(0, (Date.now() - started) / 1000) : null;
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.style.border = `1px solid ${isError ? "#ff5d66" : "#46545d"}`;
  el.classList.add("show");
  window.setTimeout(() => el.classList.remove("show"), 3500);
}

async function action(path, body = {}) {
  if (busy) return;
  busy = true;
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    toast("Command accepted");
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    busy = false;
  }
}

function render(data) {
  $("#connectionDot").classList.add("ok");
  $("#connection").textContent = "Bridge live";

  $("#recordingValue").textContent = data.recording ? "RECORDING" : "Standby";
  $("#recordingValue").style.color = data.recording ? "var(--red)" : "var(--text)";
  $("#recordingSub").textContent = data.apiReachable ? "Just In Mac Lite NDI connected" : "Just In Mac Lite NDI API unavailable";

  const summary = data.postRecordSummary || { state: "pending", checks: [] };
  const hero = $("#postRecordHero");
  hero.className = `postHero ${summary.state || "pending"}`;
  $("#postRecordHeadline").textContent = summary.headline || "NO COMPLETED POST-RECORD CHECK YET";
  $("#postRecordDetail").textContent = summary.detail || "Run STOP + DELIVER to produce a verified completion result.";
  $("#postRecordCompletedAt").textContent = summary.completedAt ? `Last verified ${summary.completedAt}` : "No completed check loaded";
  $("#postRecordChecks").replaceChildren(...(summary.checks || []).map((check) => {
    const item = document.createElement("div");
    item.className = `heroCheck ${check.passed ? "pass" : (summary.state === "pending" || summary.state === "running" || summary.state === "recording" ? "pending" : "fail")}`;
    const label = document.createElement("span");
    label.textContent = `${check.label}${Number.isFinite(check.count) ? ` (${check.count})` : ""}`;
    const result = document.createElement("strong");
    result.textContent = check.passed ? "PASS" : (summary.state === "pending" || summary.state === "running" || summary.state === "recording" ? "PENDING" : "ATTENTION");
    item.append(label, result);
    return item;
  }));

  const post = data.postRecord || { state: "idle" };
  const active = ["queued", "waiting_for_files", "backing_up", "copying_masters", "creating_proxies", "copying_proxies", "cancelling"].includes(post.state);
  $("#startButton").disabled = busy || data.recording || active || !data.ok;
  $("#stopButton").disabled = busy || !data.recording;
  $("#cancelButton").disabled = busy || !post.cancellable;

  $("#postValue").textContent = labels[post.state] || post.state;
  const fileText = post.currentFile ? ` · ${post.currentFile}` : "";
  const countText = Number.isFinite(post.totalFiles) ? `${post.completedFiles || 0} of ${post.totalFiles} files` : (post.jobId ? `Job ${post.jobId}` : "No post-record work running");
  const byteText = Number.isFinite(post.bytesTotal) ? ` · ${bytes(post.bytesCopied || 0)} of ${bytes(post.bytesTotal)}` : "";
  $("#postSub").textContent = `${countText}${byteText}${fileText}`;
  const timing = post.proxyTiming || summary.proxyTiming;
  let timingText = "Proxy timing will appear while proxies are being made.";
  if (post.state === "creating_proxies") {
    const elapsed = elapsedSince(post.proxyStartedAt);
    const completed = post.completedFiles || 0;
    const total = post.totalFiles || 0;
    if (elapsed !== null && completed > 0 && total > completed) {
      const estimatedTotal = elapsed / completed * total;
      timingText = `Proxies: ${clock(elapsed)} elapsed · about ${clock(estimatedTotal - elapsed)} remaining at this take's measured pace.`;
    } else if (elapsed !== null) {
      timingText = `Proxies: ${clock(elapsed)} elapsed · measuring the first file.`;
    }
  } else if (timing && Number.isFinite(timing.elapsedSeconds)) {
    const count = timing.sourceCount || summary.proxyCount || 0;
    const perCamera = count ? timing.elapsedSeconds / count : null;
    timingText = `Last proxy job: ${clock(timing.elapsedSeconds)} total${count ? ` · ${clock(perCamera)} per source file` : ""}.`;
  }
  $("#proxyTiming").textContent = timingText;
  $("#postError").textContent = post.error || "";

  let percent = 0;
  if (post.bytesTotal) percent = (post.bytesCopied || 0) / post.bytesTotal * 100;
  else if (post.totalFiles) percent = (post.completedFiles || 0) / post.totalFiles * 100;
  else if (post.state === "complete") percent = 100;
  $("#progressBar").style.width = `${Math.max(0, Math.min(100, percent))}%`;

  const effectiveState = post.state === "cancelling" ? post.previousState : post.state;
  const currentIndex = order.indexOf(effectiveState);
  document.querySelectorAll(".stage").forEach((stage, index) => {
    stage.classList.toggle("active", index === currentIndex && post.state !== "complete");
    stage.classList.toggle("done", post.state === "complete" || (currentIndex > index));
  });

  const channels = data.channels || [];
  const readyChannels = channels.filter((channel) => channel.active && channel.canRecord && !channel.error).length;
  $("#channelValue").textContent = `${readyChannels} / ${data.requestedChannels.length}`;
  const channelProblems = channels.filter((channel) => !channel.active || channel.error || !channel.canRecord).map((channel) => {
    if (channel.error) return `${channel.name}: ${channel.error}`;
    if (!channel.active) return `${channel.name}: inactive`;
    return `${channel.name}: cannot record`;
  });
  $("#channelSub").textContent = data.apiReachable
    ? (channelProblems.length ? `Not ready: ${channelProblems.join("; ")}` : "All requested channels active and record-ready")
    : (data.error || "Connection unavailable");

  const readiness = data.postRecordReadiness || { checks: {} };
  $("#storageValue").textContent = readiness.ok ? "Ready" : "Attention";
  $("#storageValue").style.color = readiness.ok ? "var(--green)" : "var(--amber)";
  const friendly = {
    sourceRoot: "Live recording folder",
    backupVolume: "Extreme SSD",
    deliveryVolume: "Delivery volume",
    proxyWorkingParent: "Local proxy folder",
    proxyDeliveryVolume: "Proxy delivery volume",
    ffmpeg: "FFmpeg",
    ffprobe: "FFprobe",
  };
  $("#checks").innerHTML = Object.entries(readiness.checks || {}).map(([name, ok]) =>
    `<div class="check"><span>${friendly[name] || name}</span><b class="${ok ? "" : "bad"}">${ok ? "READY" : "OFFLINE"}</b></div>`
  ).join("");

  const storage = data.storageReport || { status: "unavailable", surfaces: [] };
  const rodeCapacity = (storage.surfaces || []).find((surface) => surface.key === "rodecaster-capacity") || {};
  const capacityState = rodeCapacity.state || "unavailable";
  $("#capacityValue").textContent = capacityState === "confirmed" ? "RØDE CONFIRMED" : capacityState === "override" ? "AMBER OVERRIDE" : capacityState.replaceAll("-", " ").toUpperCase();
  $("#capacityValue").style.color = capacityState === "confirmed" ? "var(--green)" : ["override", "below-threshold", "attention"].includes(capacityState) ? "var(--amber)" : "var(--red)";
  const projectedMinutes = rodeCapacity.remainingMinutes == null ? "No projected capacity" : `${Math.floor(rodeCapacity.remainingMinutes)} minutes projected remaining`;
  const confirmationScope = rodeCapacity.confirmationScope || "current bridge session and production day";
  $("#capacitySub").textContent = `${projectedMinutes} · ${rodeCapacity.requiredMinutes || 207} minutes required · fresh device/app check after ${confirmationScope}, or after any incomplete take`;
  $("#capacitySurfaces").innerHTML = (storage.surfaces || []).map((surface) => {
    const good = ["ready", "bypassed"].includes(surface.status);
    const detail = surface.estimatedHours != null ? `${surface.message} · ${surface.estimatedHours.toFixed(1)} h estimated` : surface.message;
    return `<div class="check"><span><b>${surface.label}</b><br><small>${detail || "No detail"}</small></span><b class="${good ? "" : "bad"}">${surface.status.toUpperCase()}</b></div>`;
  }).join("");
  const cleanup = data.rodecasterCleanup || { state: "idle" };
  $("#cleanupStatus").textContent = cleanup.error ? `${cleanup.state}: ${cleanup.error}` : `RØDECaster SSD archive: ${cleanup.state}${cleanup.take ? ` · ${cleanup.take}` : ""}`;
  $("#cleanStorageButton").disabled = busy || data.recording || active || ["checking", "copying", "verifying", "cleaning"].includes(cleanup.state);
  $("#confirmCapacityButton").disabled = busy || data.recording || active;
  $("#overrideCapacityButton").disabled = busy || data.recording || active;
  $("#recoverCapacityButton").disabled = busy || data.recording || active;
  $("#openDeliveredFilesButton").disabled = busy || !(summary.checks || []).some((check) => check.passed);
  $("#acknowledgePostRecordButton").disabled = busy || data.recording || active || summary.state !== "attention";

  const last = data.lastResult;
  if (last) {
    $("#activityValue").textContent = last.action ? last.action.replaceAll("-", " ") : "Updated";
    $("#activitySub").textContent = last.completedAt || last.stoppedAt || last.startedAt || last.cancelledAt || "Most recent bridge action";
  }
}

async function refresh() {
  try {
    const response = await fetch("/v4/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`Status ${response.status}`);
    render(await response.json());
  } catch (error) {
    $("#connectionDot").classList.remove("ok");
    $("#connection").textContent = "Bridge disconnected";
  }
}

$("#startButton").addEventListener("click", () => action("/v4/record/start"));
$("#stopButton").addEventListener("click", () => action("/v4/record/stop"));
$("#cancelButton").addEventListener("click", () => {
  if (window.confirm("Cancel the active post-record task? Finished verified files will remain in place.")) {
    action("/v4/post-record/cancel");
  }
});
$("#storageRefreshButton").addEventListener("click", () => action("/v4/storage/refresh"));
$("#confirmCapacityButton").addEventListener("click", () => {
  const remainingMinutes = window.prompt("Read the remaining recording time from the supported RØDECaster screen or app. Enter minutes:");
  if (remainingMinutes === null) return;
  const source = window.prompt("Source (for example: RØDECaster device screen):", "RØDECaster device screen");
  if (source !== null) action("/v4/capacity/confirm", { remainingMinutes, source });
});
$("#overrideCapacityButton").addEventListener("click", () => {
  const phrase = window.prompt("Type the explicit override phrase shown in the capacity status:");
  if (phrase === null) return;
  const reason = window.prompt("Reason for this five-minute, single-use override:");
  if (reason !== null) action("/v4/capacity/override", { phrase, reason });
});
$("#recoverCapacityButton").addEventListener("click", () => {
  if (window.confirm("Clear the capacity evidence? Recording stays blocked until a new confirmation.")) action("/v4/capacity/recover");
});
$("#openDeliveredFilesButton").addEventListener("click", () => action("/v4/files/open", { target: "all" }));
$("#acknowledgePostRecordButton").addEventListener("click", () => {
  if (window.confirm("Acknowledge this historical post-record warning? This clears only the dashboard receipt. No recording or media file will be changed.")) {
    action("/v4/post-record/acknowledge");
  }
});
$("#cleanStorageButton").addEventListener("click", () => {
  if (window.confirm("This will enter RØDECaster Transfer Mode and archive stable takes to the Extreme SSD only. The card copies will be retained. Continue?")) {
    action("/v4/storage/rodecaster-archive");
  }
});

refresh();
window.setInterval(refresh, 1000);
