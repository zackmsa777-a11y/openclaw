// Fork-only native proof fixture. Run from the repository root after the canonical
// Control UI production build; this file is not part of the product fix.
// node --import ./scripts/tsx.mjs apps/linux/tests/issue162448_fixture.mts OUTDIR UIROOT
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [outDir, uiRoot, ...extra] = process.argv.slice(2);
if (!outDir || !uiRoot || extra.length || !path.isAbsolute(outDir) || !path.isAbsolute(uiRoot)) {
  throw new Error("Usage: issue162448_fixture.mts <absolute-outdir> <absolute-built-ui-root>");
}

// Resolve from cwd so the same proof script can also be prepared outside the
// checkout. The supported invocation above always runs from the repository root.
const {
  createControlUiMockGatewayInitScript,
  createControlUiMockBootstrapConfig,
  controlUiSessionUrl,
} = await import(pathToFileURL(path.resolve("ui/src/test-helpers/control-ui-e2e.ts")).href);
const html = readFileSync(path.join(uiRoot, "index.html"), "utf8");
const identityScript = html.match(
  /<script\b[^>]*\bid="openclaw-control-ui-build-info"[^>]*>([\s\S]*?)<\/script>/u,
);
const identityJson = identityScript?.[1]?.match(
  /^\s*globalThis\.OPENCLAW_CONTROL_UI_BUILD_INFO\s*=\s*(\{[\s\S]*\})\s*;?\s*$/u,
)?.[1];
if (!identityJson) {
  throw new Error("Built index.html has no canonical Control UI build identity");
}
const identity = JSON.parse(identityJson);
if (
  typeof identity.buildId !== "string" ||
  !identity.buildId.trim() ||
  typeof identity.version !== "string" ||
  !identity.version.trim()
) {
  throw new Error("Built Control UI identity must contain a buildId and version");
}

// Match the product regression: ordinary workspace, Home/Ask panel available,
// no transcript/provider calls. The native application owns all window chrome.
const workKey = "agent:main:assistant-titlebar";
const scenario = {
  basePath: "/fixture",
  serverBuildId: identity.buildId,
  serverVersion: identity.version,
  sessionKey: workKey,
  sessions: [workKey, "agent:main:main"].map((key) => ({
    key,
    kind: "direct",
    label: key === workKey ? "Workspace" : "Home",
    updatedAt: 1,
  })),
  featureMethods: [
    "chat.metadata",
    "chat.startup",
    "chat.history",
    "chat.send",
    "openclaw.chat",
    "openclaw.chat.history",
  ],
  methodResponses: { "openclaw.chat.history": { turns: [] } },
};

// DOM-only observation: no native invokes, private auth, CSS changes, synthetic
// input, app state mutation, or exposed driver API. X11 owns all actual actions.
const observer = String.raw`(() => {
  "use strict";
  const errors = [];
  let lastSnapshot = "";
  let posting = false;
  let sequence = 0;
  let lastPostedAt = 0;
  let animationFrames = 0;
  const inputs = [];
  const animationFrame = () => { animationFrames++; requestAnimationFrame(animationFrame); };
  requestAnimationFrame(animationFrame);
  window.addEventListener("click", (event) => {
    if (!event.isTrusted || inputs.length >= 50) return;
    const target = event.target instanceof Element ? event.target.closest("button") : null;
    inputs.push({ type: event.type, label: target ? target.getAttribute("aria-label") || target.textContent?.trim().slice(0,80) : null,
      x: event.clientX, y: event.clientY });
  }, true);
  const compact = (value) => String(value || "").replace(/\s+/g, " ").trim().slice(0, 180);
  const safeMessage = (value) => compact(value)
    .replace(/https?:\/\/[^\s]+/gi, "[url]")
    .replace(/(?:token|password|secret|authorization)[\s=:]+[^\s,;]+/gi, "[redacted]");
  const recordError = (kind, name, message) => {
    if (errors.length < 12) errors.push({ kind, name: compact(name), message: safeMessage(message) });
  };
  window.addEventListener("error", (event) => {
    recordError("error", event.error instanceof Error ? event.error.name : "Error", event.message);
  });
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    recordError("unhandledrejection", reason instanceof Error ? reason.name : "Unknown",
      reason instanceof Error ? reason.message : typeof reason === "string" ? reason : "Non-string rejection");
  });
  const rect = (element) => {
    if (!element) return null;
    const bounds = element.getBoundingClientRect();
    return { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height };
  };
  const visible = (element) => {
    if (!element || element.closest("[hidden]")) return false;
    const bounds = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return bounds.width > 0 && bounds.height > 0 && style.display !== "none" &&
      style.visibility !== "hidden" && style.visibility !== "collapse";
  };
  const label = (element) => element ? compact(element.getAttribute("aria-label") ||
    element.getAttribute("title") || element.textContent) : "";
  const describe = (element) => {
    const bounds = rect(element);
    const isVisible = visible(element);
    const hit = bounds && isVisible ? document.elementFromPoint(
      bounds.x + bounds.width / 2, bounds.y + bounds.height / 2) : null;
    return {
      visible: isVisible, rect: bounds, label: label(element),
      disabled: Boolean(element && (element.disabled || element.getAttribute("aria-disabled") === "true")),
      clickableAtCenter: Boolean(element && hit && (hit === element || element.contains(hit))),
    };
  };
  const button = (root, name) => Array.from(root ? root.querySelectorAll("button") : [])
    .find((element) => label(element) === name) || null;
  const readSnapshot = () => {
    const panel = document.querySelector(".assistant-panel");
    const shell = document.querySelector(".shell");
    const header = panel && panel.querySelector(".assistant-panel-header");
    const caption = document.querySelector(".openclaw-window-controls");
    const selected = header && header.querySelector('.assistant-panel-tab[aria-pressed="true"]');
    const selectedLabel = label(selected);
    return {
      schemaVersion: 1,
      pathname: location.pathname,
      readyState: document.readyState,
      visibilityState: document.visibilityState,
      documentFocused: document.hasFocus(),
      animationFrames,
      trustedInputs: inputs.slice(),
      document: { width: document.documentElement.clientWidth, height: document.documentElement.clientHeight },
      viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
      window: { innerWidth, innerHeight, outerWidth, outerHeight, screenX, screenY },
      rootClasses: document.documentElement.className,
      nativePlatform: document.documentElement.dataset.nativePlatform || null,
      // Production window-chrome.js invokes __TAURI_INTERNALS__.invoke directly.
      // Presence is diagnostic only; real window-state changes are the proof.
      tauriInternalsPresent: "__TAURI_INTERNALS__" in window,
      shellClasses: shell ? shell.className : "",
      panel: { visible: visible(panel), className: panel ? panel.className : "", rect: rect(panel) },
      caption: { visible: visible(caption), rect: rect(caption),
        buttons: Array.from(caption ? caption.querySelectorAll("button") : [], describe) },
      activePanelMode: selectedLabel === "Home" ? "home" : selectedLabel === "Ask OpenClaw" ? "custodian" : selectedLabel || null,
      header: { rect: rect(header), paddingRight: header ? getComputedStyle(header).paddingRight : null,
        actionsRect: rect(header && header.querySelector(".assistant-panel-actions")),
        tabs: Array.from(header ? header.querySelectorAll(".assistant-panel-tab") : [], describe) },
      headerButtons: Array.from(header ? header.querySelectorAll("button") : [], describe),
      controls: {
        homeFooter: describe(document.querySelector(".sidebar-footer-bar__home")),
        expandSidebar: describe(button(document.querySelector(".shell-chrome-controls"), "Expand sidebar")),
        maximize: describe(caption && caption.querySelector('[data-action="toggle-maximize"]')),
        minimize: describe(caption && caption.querySelector('[data-action="minimize"]')),
        windowClose: describe(caption && caption.querySelector('[data-action="close"]')),
        panelClose: describe(button(panel, "Close assistant sidebar")),
        ask: describe(button(header, "Ask OpenClaw")),
        homeTab: describe(button(header, "Home")),
        dockBottom: describe(button(panel, "Dock assistant sidebar at bottom")),
        dockRight: describe(button(panel, "Dock assistant sidebar at right")),
        divider: describe(panel && panel.querySelector('[role="separator"][aria-label="Resize assistant sidebar"]')),
      },
      errors: errors.slice(),
    };
  };
  const report = async () => {
    if (posting || !document.documentElement) return;
    let snapshot;
    try { snapshot = readSnapshot(); }
    catch (error) {
      recordError("observer", error instanceof Error ? error.name : "Error", error instanceof Error ? error.message : "Observer failed");
      return;
    }
    const serialized = JSON.stringify(snapshot);
    if (serialized === lastSnapshot && Date.now() - lastPostedAt < 1000) return;
    posting = true;
    try {
      const response = await fetch("/fixture/proof", {
        method: "POST", headers: { "Content-Type": "application/json" },
        credentials: "omit", cache: "no-store",
        body: JSON.stringify({ ...snapshot, sequence: ++sequence, observedAt: Date.now() }),
      });
      if (response.ok) { lastSnapshot = serialized; lastPostedAt = Date.now(); }
    } catch { /* A failed local receipt is retried at the next observation. */ }
    finally { posting = false; }
  };
  const timer = setInterval(report, 100);
  window.addEventListener("pagehide", () => clearInterval(timer), { once: true });
  void report();
})();
`;

mkdirSync(outDir, { recursive: true });
const workUrl = new URL(controlUiSessionUrl("http://127.0.0.1/fixture", workKey));
for (const [name, contents] of Object.entries({
  "mock-gateway.js": createControlUiMockGatewayInitScript(scenario),
  "bootstrap.json": `${JSON.stringify(createControlUiMockBootstrapConfig(scenario), null, 2)}\n`,
  "observer.js": observer,
  "workpath.txt": `${workUrl.pathname}${workUrl.search}\n`,
})) {
  writeFileSync(path.join(outDir, name), contents, { flag: "wx" });
}
console.log(
  JSON.stringify({
    outDir,
    uiRoot,
    buildId: identity.buildId,
    version: identity.version,
    workPath: workUrl.pathname,
  }),
);
