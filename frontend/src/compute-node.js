/// Compute Node telemetry panel (Route B).
///
/// Renders the `linux_health` primitive's raw telemetry (CPU temperature,
/// input voltage, load current) as three KPI cards and three streaming line
/// charts, driven by a dedicated `/ws/health` WebSocket. The panel only
/// appears while the deployment actually streams the primitive; when it is
/// absent the `/ws/health` source reports `unavailable` and the panel stays
/// hidden, restoring the original UI.
import {
  Cpu,
  Zap,
  Activity,
  Thermometer,
  createElement as createIconElement,
} from "lucide";

const byId = (id) => document.getElementById(id);

const t = (key, params) => (window.RobonixI18N
  ? window.RobonixI18N.t(key, params)
  : String(key ?? "").replace(/\{([a-zA-Z0-9_]+)\}/g, (whole, name) => (
    params && params[name] !== undefined && params[name] !== null ? String(params[name]) : whole
  )));

function icon(node, size = 15) {
  return createIconElement(node, {
    width: String(size),
    height: String(size),
    "stroke-width": "1.8",
    "aria-hidden": "true",
  });
}

function cssVar(name) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || null;
}

const HISTORY = 120;
const WINDOW_MS = 90_000;

const METRICS = [
  {
    key: "cpuTemp",
    label: () => t("CPU temp"),
    unit: "°C",
    decimals: 1,
    min: 30,
    max: 90,
    kpiId: "compKpiCpu",
    kpiUnit: "compKpiCpuUnit",
    spanId: "compKpiCpuSpan",
    canvasId: "compChartCpu",
    colorVar: "--amber",
    icon: Thermometer,
  },
  {
    key: "voltage",
    label: () => t("Voltage"),
    unit: "V",
    decimals: 2,
    min: 22,
    max: 26,
    kpiId: "compKpiVolt",
    kpiUnit: "compKpiVoltUnit",
    spanId: "compKpiVoltSpan",
    canvasId: "compChartVolt",
    colorVar: "--cyan",
    icon: Zap,
  },
  {
    key: "current",
    label: () => t("Current"),
    unit: "A",
    decimals: 2,
    min: 0,
    max: 2,
    kpiId: "compKpiCurr",
    kpiUnit: "compKpiCurrUnit",
    spanId: "compKpiCurrSpan",
    canvasId: "compChartCurr",
    colorVar: "--green",
    icon: Activity,
  },
];

// Toggled lazily; lives in module scope so the badge survives reconnect.
let lastSource = "connecting";

class ComputeNodePanel {
  constructor(root) {
    this.root = root;
    this.socket = null;
    this.reconnectTimer = 0;
    this.stubTimer = 0;
    this.samples = [];
    this.badgeValue = byId("computeNodeSource") || root.querySelector("[data-compute-source]");
    this.updatedAt = byId("computeNodeUpdated") || root.querySelector("[data-compute-updated]");
    this.statusDot = byId("computeNodeStatusDot") || root.querySelector("[data-compute-dot]");

    const reconnect = byId("computeNodeReconnect");
    if (reconnect) reconnect.addEventListener("click", () => this.connect(true));

    window.addEventListener("robonix:settings", () => {
      if (this.root.offsetWidth || this.root.getBoundingClientRect().width) this.connect(true);
    });
    window.addEventListener("robonix:i18n", () => this.renderLabels());
    window.addEventListener("beforeunload", () => this.disconnect(true));

    METRICS.forEach((metric) => {
      const span = byId(metric.spanId);
      if (span) span.appendChild(icon(metric.icon));
    });
    const headerIcon = byId("computeNodeHeaderIcon");
    if (headerIcon) headerIcon.appendChild(icon(Cpu, 18));

    this.renderLabels();
    this.setSource(lastSource, "");
    this.showPanel(false);
    this.connect();
  }

  settings() {
    if (typeof window.collectSettings === "function") return window.collectSettings();
    const host = byId("robotHost")?.value?.trim() || "";
    const atlasPort = Number.parseInt(byId("atlasPort")?.value || "50051", 10) || 50051;
    return { robotHost: host, atlasPort, atlasEndpoint: host ? `${host}:${atlasPort}` : "" };
  }

  renderLabels() {
    METRICS.forEach((metric) => {
      const card = byId(metric.kpiId)?.closest(".compute-metric");
      const label = card?.querySelector("[data-compute-label]");
      if (label) label.textContent = metric.label();
    });
  }

  setSource(source, error) {
    lastSource = source;
    if (this.badgeValue) {
      this.badgeValue.className =
        `health-label ${source === "primitive" ? "ok" : source === "dev-stub" ? "idle" : "stale"}`;
      this.badgeValue.innerHTML = "";
      const key =
        source === "primitive" ? t("Live primitive") :
        source === "dev-stub" ? t("Dev stub") :
        source === "unavailable" ? t("No model") : t("Connecting");
      this.badgeValue.append(document.createTextNode(key));
      this.badgeValue.title = error || "";
    }
    if (this.statusDot) {
      this.statusDot.className = `vitals-status-dot ${source === "primitive" ? "ok" : source === "dev-stub" ? "idle" : "stale"}`;
    }
    // Without the linux_health primitive the panel has no live source: keep it
    // out of the UI (restoring the original layout) instead of showing a stub.
    if (source === "unavailable") this.showPanel(false);
  }

  showPanel(visible) {
    if (this.root) {
      this.root.style.display = visible ? "" : "none";
      this.panelVisible = visible;
    }
  }

  updateKpis() {
    const latest = this.samples[this.samples.length - 1];
    METRICS.forEach((metric) => {
      const card = byId(metric.kpiId);
      const current = byId(metric.kpiUnit);
      if (!card) return;
      let valueText = "--";
      let health = "ok";
      if (latest && latest[metric.key] != null) {
        valueText = latest[metric.key].toFixed(metric.decimals);
        if (metric.key === "cpuTemp" && latest[metric.key] > 85) health = "warn";
        else if (metric.key === "cpuTemp" && latest[metric.key] > 70) health = "idle";
      } else {
        health = "stale";
      }
      card.className = `compute-metric ${health}`;
      const value = card.querySelector("[data-compute-value]");
      const unit = current || card.querySelector("[data-compute-unit]");
      if (value) value.textContent = valueText;
      if (unit) unit.textContent = metric.unit;
    });
    if (this.updatedAt && latest) {
      this.updatedAt.textContent = new Date(latest.ts).toLocaleTimeString(undefined, {
        hour12: false,
      });
    }
  }

  connect() {
    if (this.reconnectTimer) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = 0;
    }
    this.stopStub();
    const oldSocket = this.socket;
    this.socket = null;
    oldSocket?.close(1000, "Compute-node reconnect");

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${location.host}/ws/health`);
    this.socket = socket;
    socket.onopen = () => socket.send(JSON.stringify({ settings: this.settings() }));
    socket.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data);
        if (event.type === "source") {
          if (event.source === "unavailable") {
            // This deployment has no linux_health primitive: keep the panel
            // hidden and stop retrying (matches the original UI).
            this.showPanel(false);
            this.setSource("unavailable", event.error || "");
            if (this.socket === socket) this.socket = null;
            socket.close(1000, "No health primitive");
            return;
          }
          this.setSource(event.source, event.error || "");
        } else if (event.type === "sample") {
          if (event.source === "primitive") this.showPanel(true);
          this.setSource(event.source);
          this.pushSample(event.data);
        } else if (event.type === "error") {
          this.showPanel(false);
          this.setSource("unavailable", event.error || "");
          if (this.socket === socket) this.socket = null;
          socket.close(1000, "Health error");
        }
      } catch (error) {
        this.scheduleRetry();
      }
    };
    socket.onerror = () => {
      if (this.socket !== socket) return;
      this.setSource("stale");
    };
    socket.onclose = (event) => {
      if (this.socket !== socket) return;
      if (!event.wasClean) this.scheduleRetry();
    };
  }

  scheduleRetry() {
    if (this.reconnectTimer) return;
    this.setSource("connecting");
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = 0;
      this.connect();
    }, 3000);
  }

  pushSample(data) {
    const point = {
      ts: data.ts || Date.now(),
      cpuTemp: data.cpuTemp,
      voltage: data.voltage,
      current: data.current,
    };
    this.samples.push(point);
    const cutoff = Date.now() - WINDOW_MS;
    while (this.samples.length > HISTORY || (this.samples.length && this.samples[0].ts < cutoff)) {
      this.samples.shift();
    }
    this.updateKpis();
    this.draw();
  }

  setupCanvas(canvas) {
    if (canvas.__ready) return canvas;
    canvas.__ready = true;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.__dpr = dpr;
    const observer = new ResizeObserver(() => {
      const rect = canvas.parentElement.getBoundingClientRect();
      canvas.width = Math.max(2, Math.floor(rect.width * dpr));
      canvas.height = Math.max(2, Math.floor(rect.height * dpr));
      const context = canvas.getContext("2d");
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.draw();
    });
    observer.observe(canvas.parentElement);
    return canvas;
  }

  draw() {
    METRICS.forEach((metric) => {
      const canvas = byId(metric.canvasId);
      if (!canvas) return;
      this.setupCanvas(canvas);
      const context = canvas.getContext("2d");
      const width = canvas.width / canvas.__dpr;
      const height = canvas.height / canvas.__dpr;
      const color = cssVar(metric.colorVar) || "#5fcdd8";
      const line = cssVar("--line-soft") || "#1b2a30";
      const dim = cssVar("--dim") || "#65767c";

      context.clearRect(0, 0, width, height);
      const padT = 10;
      const padB = 6;
      const padL = 4;
      const padR = 4;
      const plotW = width - padL - padR;
      const plotH = height - padT - padB;

      // Horizontal gridlines with a min/max label each.
      context.fillStyle = dim;
      context.font = "9px Inter, ui-sans-serif, system-ui, sans-serif";
      context.strokeStyle = line;
      context.lineWidth = 1;
      for (let i = 0; i <= 2; i += 1) {
        const y = padT + (plotH * i) / 2;
        context.beginPath();
        context.moveTo(padL, y);
        context.lineTo(padL + plotW, y);
        context.stroke();
        const labelValue = metric.max - ((metric.max - metric.min) * i) / 2;
        context.fillText(`${labelValue.toFixed(metric.decimals > 0 ? metric.decimals - 1 : 0)}`, 2, y + 7);
      }

      const points = this.samples
        .filter((s) => s[metric.key] != null)
        .map((s, index) => ({
          x: padL + (index / Math.max(1, this.samples.length - 1)) * plotW,
          y: padT + (1 - (s[metric.key] - metric.min) / (metric.max - metric.min)) * plotH,
        }));
      if (points.length > 1) {
        context.strokeStyle = color;
        context.lineWidth = 1.6;
        context.lineJoin = "round";
        context.lineCap = "round";
        context.beginPath();
        context.moveTo(points[0].x, points[0].y);
        for (let i = 1; i < points.length; i += 1) context.lineTo(points[i].x, points[i].y);
        context.stroke();

        const last = points[points.length - 1];
        context.fillStyle = color;
        context.beginPath();
        context.arc(last.x, last.y, 2.4, 0, Math.PI * 2);
        context.fill();
      }
    });
  }

  stopStub() {
    if (this.stubTimer) {
      window.clearInterval(this.stubTimer);
      this.stubTimer = 0;
    }
  }

  disconnect(keepStub = false) {
    if (this.reconnectTimer) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = 0;
    }
    if (this.socket) {
      this.socket.close(1000, "Compute-node page closed");
      this.socket = null;
    }
    if (!keepStub) this.stopStub();
  }
}

const root = byId("computeNodeRoot");
if (root) {
  window.__robonixComputeNode = new ComputeNodePanel(root);
}

export { ComputeNodePanel };