(function () {
  "use strict";
  const api = window.PluginApi;
  if (!api) return;
  const React = api.React;
  const ReactDOM = api.ReactDOM;
  const { NavLink } = api.libraries.ReactRouterDOM;
  const { Button, Modal, Form } = api.libraries.Bootstrap;
  const PLUGIN_ID = "librarymanager";
  const PRODUCT_NAME = "Watchtower";
  const PATH = "/library-manager";

  async function gql(query, variables) {
    const base = document.querySelector("base")?.getAttribute("href") || "/";
    const response = await fetch(`${base}graphql`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, variables: variables || {} })
    });
    const result = await response.json();
    if (result.errors?.length) throw new Error(result.errors[0].message);
    return result.data;
  }

  async function operation(mode, extra) {
    const data = await gql(`mutation Run($id: ID!, $args: Map) {
      runPluginOperation(plugin_id: $id, args: $args)
    }`, { id: PLUGIN_ID, args: { mode, ...(extra || {}) } });
    const parsed = typeof data.runPluginOperation === "string"
      ? JSON.parse(data.runPluginOperation) : data.runPluginOperation;
    if (parsed?.error) throw new Error(parsed.error);
    // Raw Stash plugins may return either their payload directly or an {output: ...} envelope.
    return parsed && Object.prototype.hasOwnProperty.call(parsed, "output") ? parsed.output : parsed;
  }

  async function getConfig() {
    const data = await gql(`query Config { configuration { plugins } }`);
    return data.configuration.plugins?.[PLUGIN_ID] || {};
  }

  async function saveConfig(config) {
    const data = await gql(`mutation Save($id: ID!, $input: Map!) {
      configurePlugin(plugin_id: $id, input: $input)
    }`, { id: PLUGIN_ID, input: config });
    return data.configurePlugin;
  }

  async function runTask(name) {
    const allowed = new Set([
      ...Object.values(readOnlyTasks), "Preview Configured Test Rename", "Apply Configured Test Rename",
      "Start Read-Only Filesystem Monitor", "Stop Filesystem Monitor", "Filesystem Monitor Status",
      "Generate Missing Contact Sheets for Incoming Folder"
    ]);
    if (!allowed.has(name)) throw new Error("Unknown Library Manager task");
    const data = await gql(`mutation { runPluginTask(plugin_id: "${PLUGIN_ID}", task_name: ${JSON.stringify(name)}) }`);
    return data.runPluginTask;
  }

  async function waitForJob(id, timeoutSeconds = 180) {
    const deadline = Date.now() + timeoutSeconds * 1000;
    while (Date.now() < deadline) {
      const data = await gql(`query Job($id: ID!) { findJob(input: {id: $id}) { status description } }`, { id: String(id) });
      const status = data.findJob?.status;
      if (status === "FINISHED") return;
      if (status === "CANCELLED") throw new Error("The Stash job was cancelled.");
      await new Promise(resolve => window.setTimeout(resolve, 1000));
    }
    throw new Error("The task is still running. Refresh later to see its results.");
  }

  const sections = [
    ["overview", "Overview"], ["manage", "File Management"],
    ["activity", "Activity"], ["advanced", "Advanced Settings"]
  ];

  const readOnlyTasks = {
    inventory: "Build Read-Only Inventory",
    find: "Find Renamed Files (Read Only)",
    plan: "Build Resolution Plan (Read Only)",
    merge: "Preview Metadata Merge (Read Only)",
    filenames: "Preview Safe Filenames (Read Only)",
    events: "Reconcile Filesystem Events (Read Only)",
    activity: "View / Export Recent Activity"
  };

  const filenameOrders = [
    ["title,studio,performers", "Title, then studio, then performers"],
    ["title,performers,studio", "Title, then performers, then studio"],
    ["studio,title,performers", "Studio, then title, then performers"],
    ["studio,performers,title", "Studio, then performers, then title"],
    ["performers,title,studio", "Performers, then title, then studio"],
    ["performers,studio,title", "Performers, then studio, then title"]
  ];
  const sectionSeparators = [["dash", "Dash"], ["comma", "Comma"], ["space", "Space"], ["underscore", "Underscore"]];
  const performerSeparators = [["comma", "Comma"], ["space", "Space"], ["dash", "Dash"], ["ampersand", "And sign (&)"]];

  function ChoiceField({ label, help, value, choices, disabled, onChange }) {
    const [open, setOpen] = React.useState(false);
    const root = React.useRef(null);
    React.useEffect(() => {
      if (!open) return undefined;
      const close = event => {
        if (event.type === "keydown" && event.key !== "Escape") return;
        if (event.type === "pointerdown" && root.current?.contains(event.target)) return;
        setOpen(false);
      };
      document.addEventListener("pointerdown", close);
      document.addEventListener("keydown", close);
      return () => {
        document.removeEventListener("pointerdown", close);
        document.removeEventListener("keydown", close);
      };
    }, [open]);
    const current = choices.find(([choice]) => String(choice) === String(value)) || choices[0];
    return React.createElement("div", { className: "lm-field lm-choice", ref: root },
      label && React.createElement("strong", null, label),
      help && React.createElement("small", null, help),
      React.createElement("button", { type: "button", className: "lm-choice-button", disabled,
        "aria-haspopup": "listbox", "aria-expanded": open,
        onClick: () => setOpen(previous => !previous) },
        React.createElement("span", null, current?.[1] || "Choose"),
        React.createElement("span", { className: "lm-choice-arrow", "aria-hidden": "true" }, open ? "▴" : "▾")),
      open && React.createElement("div", { className: "lm-choice-menu", role: "listbox" }, choices.map(([choice, text]) =>
        React.createElement("button", { type: "button", role: "option", key: choice,
          className: String(choice) === String(value) ? "selected" : "",
          "aria-selected": String(choice) === String(value), onClick: () => { setOpen(false); onChange(choice); } },
          React.createElement("span", null, text), String(choice) === String(value) && React.createElement("b", { "aria-hidden": "true" }, "✓")))));
  }

  function RealScenePreviewer({ config, data }) {
    const [sceneIdInput, setSceneIdInput] = React.useState("");
    const [previewResult, setPreviewResult] = React.useState(null);
    const [loading, setLoading] = React.useState(false);
    const [errorMsg, setErrorMsg] = React.useState("");

    const runPreview = async (targetId) => {
      const id = String(targetId || sceneIdInput).trim();
      if (!id) return;
      setLoading(true); setErrorMsg("");
      try {
        const gqlRes = await gql(`query FetchPreviewScene($id: ID!) {
          findScene(id: $id) { id title studio { name } performers { name } files { path basename } }
        }`, { id });
        const scene = gqlRes?.findScene;
        if (!scene) throw new Error(`Scene ${id} not found in Stash`);
        const currentBasename = scene.files?.[0]?.basename || scene.files?.[0]?.path?.split(/[\\/]/).pop() || "unknown.mp4";
        const ext = currentBasename.includes(".") ? "." + currentBasename.split(".").pop() : ".mp4";

        const parts = {
          title: scene.title || "Untitled",
          studio: scene.studio?.name || "",
          performers: (scene.performers || []).map(p => p.name).join(filenamePerformerCharacters[config.filenamePerformerSeparator] || ", ")
        };
        const order = (config.filenameOrder || "title,studio,performers").split(",");
        const proposedStem = order.map(p => parts[p]).filter(Boolean)
          .join(filenameSectionCharacters[config.filenameSectionSeparator] || " - ");
        const proposed = proposedStem ? proposedStem + ext : currentBasename;

        setPreviewResult({
          sceneId: id,
          title: scene.title || "Untitled",
          studio: scene.studio?.name || "None",
          performers: (scene.performers || []).map(p => p.name).join(", ") || "None",
          current: currentBasename,
          proposed: proposed,
          matches: currentBasename === proposed
        });
      } catch (e) {
        setErrorMsg(e.message || String(e));
        setPreviewResult(null);
      } finally {
        setLoading(false);
      }
    };

    const pickRecent = () => {
      const recentWithScene = (data?.activity || []).find(r => r.scene_id);
      if (recentWithScene) {
        setSceneIdInput(String(recentWithScene.scene_id));
        runPreview(recentWithScene.scene_id);
      } else {
        runPreview("1");
      }
    };

    return React.createElement("div", { className: "lm-real-scene-tester" },
      React.createElement("div", { className: "lm-real-scene-header" },
        React.createElement("strong", null, "Test Rules with a Real Scene"),
        React.createElement("small", null, "Type a Scene ID or test with a recent scene from your library (Read-Only).")),
      React.createElement("div", { className: "lm-real-scene-inputs" },
        React.createElement("input", {
          type: "text",
          placeholder: "Enter Scene ID (e.g. 6318)",
          value: sceneIdInput,
          onChange: e => setSceneIdInput(e.target.value),
          onKeyDown: e => { if (e.key === "Enter") runPreview(); }
        }),
        React.createElement(Button, { variant: "secondary", disabled: loading, onClick: () => runPreview() },
          loading ? "Checking…" : "Test Scene"),
        React.createElement(Button, { variant: "secondary", disabled: loading, onClick: pickRecent },
          "Pick Recent Scene")),
      errorMsg && React.createElement("p", { className: "lm-real-scene-error" }, `! ${errorMsg}`),
      previewResult && React.createElement("div", { className: "lm-real-scene-result" },
        React.createElement("div", { className: "lm-real-scene-meta" },
          React.createElement("span", null, React.createElement("b", null, "Title: "), previewResult.title),
          React.createElement("span", null, React.createElement("b", null, "Studio: "), previewResult.studio),
          React.createElement("span", null, React.createElement("b", null, "Performers: "), previewResult.performers)),
        React.createElement("div", { className: "lm-real-scene-diff" },
          React.createElement("div", null, React.createElement("b", null, "Current on disk: "), React.createElement("code", null, previewResult.current)),
          React.createElement("div", null, React.createElement("b", null, "Proposed filename: "), React.createElement("code", { className: previewResult.matches ? "matches" : "proposed" }, previewResult.proposed)),
          React.createElement("span", { className: `lm-badge ${previewResult.matches ? "ok" : "warn"}` },
            previewResult.matches ? "Already matches current format" : "Would rename if automatic renaming enabled"))));
  }

  function Dashboard() {
    const [tab, setTab] = React.useState("overview");
    const [data, setData] = React.useState(null);
    const [config, setConfig] = React.useState({});
    const [busy, setBusy] = React.useState("");
    const [notice, setNotice] = React.useState("");
    const [error, setError] = React.useState("");
    const [search, setSearch] = React.useState("");
    const [showFilenamePreview, setShowFilenamePreview] = React.useState(false);
    const [reports, setReports] = React.useState(null);
    const [correction, setCorrection] = React.useState({ sceneId: "", filename: "", preview: null });
    const [clock, setClock] = React.useState(Date.now());
    const [expandedOverviewEvents, setExpandedOverviewEvents] = React.useState(() => new Set());
    const [terminalFilter, setTerminalFilter] = React.useState("all");
    const [sceneHover, setSceneHover] = React.useState(null);
    const sceneHoverCache = React.useRef(new Map());
    const sceneHoverTimer = React.useRef(null);
    const [useOriginalHeader, setUseOriginalHeader] = React.useState(() => {
      try {
        return window.localStorage.getItem("lm_header_original") === "true";
      } catch (_) {
        return false;
      }
    });

    const toggleHeaderArt = React.useCallback(() => {
      setUseOriginalHeader(prev => {
        const next = !prev;
        try {
          window.localStorage.setItem("lm_header_original", String(next));
        } catch (_) {}
        return next;
      });
    }, []);

    const refresh = React.useCallback(async () => {
      setBusy("refresh"); setError("");
      try {
        const [raw, settings] = await Promise.all([operation("dashboard", { limit: 250 }), getConfig()]);
        const payload = typeof raw === "string" ? JSON.parse(raw) : raw;
        setData({ ...payload, _liveReceivedAt: Date.now() }); setConfig(settings); setNotice("");
      } catch (e) { setError(e.message); }
      finally { setBusy(""); }
    }, []);

    const loadReports = React.useCallback(async () => {
      const raw = await operation("reports");
      const payload = typeof raw === "string" ? JSON.parse(raw) : raw;
      setReports(payload.reports || []);
      return payload.reports || [];
    }, []);

    const toggleExpandedActivity = React.useCallback((id) => {
      setExpandedOverviewEvents(prev => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
    }, []);

    React.useEffect(() => { document.title = "Watchtower | Stash"; refresh(); }, [refresh]);
    React.useEffect(() => {
      if (tab === "reports") loadReports().catch(error => setError(error.message));
    }, [tab, loadReports]);
    React.useEffect(() => {
      if (!["overview", "manage"].includes(tab)) return undefined;
      let stopped = false;
      const poll = async () => {
        try {
          const raw = await operation("live_status");
          const live = typeof raw === "string" ? JSON.parse(raw) : raw;
          if (!stopped) setData(previous => ({ ...previous, ...live, _liveReceivedAt: Date.now() }));
        } catch (pollError) {
          if (!stopped) setError(`Live status could not be refreshed: ${pollError.message}`);
        }
      };
      poll();
      const pollTimer = window.setInterval(poll, 2000);
      const clockTimer = window.setInterval(() => setClock(Date.now()), 1000);
      return () => { stopped = true; window.clearInterval(pollTimer); window.clearInterval(clockTimer); };
    }, [tab]);

    async function task(name, dangerous, afterTab, showResults) {
      if (dangerous && !window.confirm(`Run “${name}”? Review the preview first. This can rename files.`)) return;
      setBusy(name); setError("");
      try {
        const id = await runTask(name);
        setNotice(`Started “${name}” as Stash job ${id}. Progress is available in the task queue.`);
        if (showResults === "filenames") {
          await waitForJob(id);
          await refresh();
          setShowFilenamePreview(true);
          setTab("overview");
          setNotice("Filename preview is ready on this page. No files were renamed.");
          window.setTimeout(() => document.querySelector(".lm-preview-list")?.scrollIntoView({ behavior: "smooth", block: "start" }), 150);
          return;
        }
        if (showResults === "reports") {
          await waitForJob(id);
          await loadReports();
          setTab("reports");
          setNotice("The diagnostic report is ready below. No files were changed.");
          return;
        }
        if (afterTab) {
          await waitForJob(id);
          await refresh();
          setTab(afterTab);
          setNotice("Review complete. The detected changes are shown below.");
          return;
        }
        window.setTimeout(refresh, 1200);
      } catch (e) { setError(e.message); }
      finally { setBusy(""); }
    }

    async function updateSettings(changes, stopWhenDisabled) {
      const next = { ...config, ...changes };
      setConfig(next); setBusy("settings"); setError("");
      try {
        await saveConfig(next);
        if (Object.prototype.hasOwnProperty.call(changes, "startAtLogin")) {
          await operation("configure_startup", { enabled: next.startAtLogin === true });
        }
        const restart = ["automaticMoveReconciliation", "macNotifications", "automaticIncomingScan",
          "incomingFolder", "incomingSettleMinutes", "generateContactSheets", "contactSheetGrid",
          "contactSheetBanner", "contactSheetAdjustVertical", "contactSheetScript"].some(key => key in changes);
        if ((restart || stopWhenDisabled) && data?.monitor?.state === "running") {
          await operation("stop_monitor");
        }
        if (next.autoStartMonitor === true && (restart || changes.autoStartMonitor === true)) await operation("ensure_monitor");
        setNotice("Settings saved.");
        window.setTimeout(refresh, 500);
      }
      catch (e) { setError(e.message); await refresh(); }
      finally { setBusy(""); }
    }

    async function correctFilename(apply) {
      const sceneId = correction.sceneId.trim();
      const filename = correction.filename.trim();
      if (!sceneId || !filename) { setError("Enter both the Scene ID and corrected filename."); return; }
      if (apply && correction.preview?.status !== "ready") { setError("Preview this correction before applying it."); return; }
      setBusy("correction"); setError("");
      try {
        const raw = await operation(apply ? "apply_manual_filename" : "preview_manual_filename",
          { scene_id: sceneId, filename });
        const result = typeof raw === "string" ? JSON.parse(raw) : raw;
        setCorrection({ sceneId, filename, preview: result });
        setNotice(apply && result.status === "renamed" ? "Filename corrected successfully." : `Correction preview: ${result.status}.`);
        if (apply) await refresh();
      } catch (e) { setError(e.message); }
      finally { setBusy(""); }
    }

    async function resolveAllPendingEvents(resolution = "dismiss") {
      const count = monitor.pending_events || data?.pending_events?.length || 0;
      if (!count) return;
      if (!window.confirm(`Dismiss all ${count} pending filesystem change${count === 1 ? "" : "s"}?\n\nThis marks them as reviewed with no further action taken.`)) return;
      setBusy("review:all"); setError("");
      try {
        await operation("resolve_all_filesystem_events", { resolution });
        setData(prev => prev ? { ...prev, pending_events: [], monitor: { ...prev.monitor, pending_events: 0 } } : prev);
        await refresh();
      } catch (reviewError) {
        setError(`Could not dismiss all changes: ${reviewError.message}`);
      } finally { setBusy(""); }
    }

    async function resolvePendingEvent(event, resolution) {
      const info = pendingEventInfo(event);
      if (resolution === "remove_stash_scene" && !window.confirm(
        `Remove the stale Stash scene for “${basename(event.source_path)}”?\n\nThe video file is already absent. Watchtower will remove only its Stash database record.`)) return;
      setBusy(`review:${event.event_key}`); setError("");
      try {
        await operation("resolve_filesystem_event", { event_key: event.event_key, resolution });
        setData(prev => {
          if (!prev) return prev;
          const remaining = (prev.pending_events || []).filter(e => e.event_key !== event.event_key);
          return { ...prev, pending_events: remaining, monitor: { ...prev.monitor, pending_events: Math.max(0, (prev.monitor?.pending_events || 1) - 1) } };
        });
        await refresh();
      } catch (reviewError) {
        setError(`Could not resolve “${basename(event.source_path)}”: ${reviewError.message}`);
      } finally { setBusy(""); }
    }

    const updateSetting = (key, value) => updateSettings({ [key]: value }, false);

    async function setAutomaticManagement(enabled) {
      if (enabled && config.testSceneId && !window.confirm(
        `Finish testing and manage future edits for all scenes? This removes the current Scene ${config.testSceneId} testing limit.`)) return;
      await updateSettings({ automaticRenaming: enabled, autoStartMonitor: enabled,
        automaticMoveReconciliation: enabled, ...(!enabled ? { startAtLogin: false } : {}),
        ...(enabled ? { testSceneId: "" } : {}) }, !enabled);
    }

    function TaskButton({ name, label, dangerous, variant, help, afterTab, showResults }) {
      return React.createElement(Button, { variant: variant || (dangerous ? "danger" : "secondary"),
        disabled: !!busy, onClick: () => task(name, dangerous, afterTab, showResults), title: help || label || name }, label || name);
    }

    function Switch({ setting, label, help, defaultValue = false }) {
      const isChecked = defaultValue ? config[setting] !== false : config[setting] === true;
      return React.createElement("label", { className: "lm-switch-row", title: help },
        React.createElement("input", { type: "checkbox", checked: isChecked,
          disabled: busy === "settings", onChange: e => updateSetting(setting, e.target.checked) }),
        React.createElement("span", null, React.createElement("strong", null, label),
          React.createElement("small", null, help)));
    }

    const inventory = data?.inventory;
    const monitor = data?.monitor || {};
    const filenamePreview = data?.filename_preview;
    const incoming = data?.incoming || {};
    const incomingFolder = data?.incoming_folder || {};
    const automaticManagement = config.automaticRenaming === true && config.autoStartMonitor === true &&
      config.automaticMoveReconciliation === true && !config.testSceneId;
    const filenameSectionCharacters = { dash: " - ", comma: ", ", space: " ", underscore: "_" };
    const filenamePerformerCharacters = { comma: ", ", space: " ", dash: " - ", ampersand: " & " };
    const exampleParts = {
      title: "Example Scene",
      studio: "Example Studio",
      performers: ["Alex Smith", "Jamie Jones"].join(filenamePerformerCharacters[config.filenamePerformerSeparator] || ", ")
    };
    const exampleFilename = (config.filenameOrder || "title,studio,performers").split(",")
      .map(part => exampleParts[part]).filter(Boolean)
      .join(filenameSectionCharacters[config.filenameSectionSeparator] || " - ") + ".mp4";
    const activity = (data?.activity || []).filter(row => {
      const term = search.trim().toLowerCase();
      return !term || [row.recorded_at, row.category, row.action, row.status, row.scene_id, row.file_id,
        row.old_path, row.new_path, row.detail].some(value => String(value || "").toLowerCase().includes(term));
    });

        function StatusCard({ title, value, detail, tone }) {
      return React.createElement("div", { className: `lm-status ${tone || ""}` },
        React.createElement("small", null, title), React.createElement("strong", null, value),
        React.createElement("span", null, detail || ""));
    }

    function SwitchIndicators() {
      const isIncomingScope = config.contactSheetScope === "incoming";
      const switches = [
        { label: "Watcher", key: "autoStartMonitor", active: config.autoStartMonitor === true, state: config.autoStartMonitor === true ? "ON" : "OFF", help: "Automatically starts filesystem monitor", tab: "manage" },
        { label: "Reconcile", key: "automaticMoveReconciliation", active: config.automaticMoveReconciliation === true, state: config.automaticMoveReconciliation === true ? "ON" : "OFF", help: "Automatically updates Stash when files are moved in Finder", tab: "manage" },
        { label: "Auto-Rename", key: "automaticRenaming", active: config.automaticRenaming === true, state: config.testSceneId ? `TEST ${config.testSceneId}` : (config.automaticRenaming === true ? "ON" : "OFF"), help: config.testSceneId ? `Active (Limited to Test Scene ${config.testSceneId})` : "Automatically renames files when metadata is edited", tab: "manage" },
        { label: "Incoming", key: "automaticIncomingScan", active: config.automaticIncomingScan === true, state: config.automaticIncomingScan === true ? "ON" : "OFF", help: "Watches incoming folder and adds completed downloads", tab: "manage" },
        { label: "Clean Titles", key: "stripMetadataFromTitle", active: config.stripMetadataFromTitle !== false, state: config.stripMetadataFromTitle !== false ? "ON" : "OFF", help: "Removes duplicate studio/performers from generated filenames", tab: "manage" },
        { label: "Login Startup", key: "startAtLogin", active: config.startAtLogin === true, state: config.startAtLogin === true ? "ON" : "OFF", help: "Runs watcher in background on macOS login", tab: "manage" },
        { label: "Sheets", key: "generateContactSheets", active: config.generateContactSheets === true, state: config.generateContactSheets === true ? (config.contactSheetGrid || "ON") : "OFF", help: "Generates multi-frame contact sheets with CSM", tab: "settings" },
        { label: "Scope", key: "contactSheetScope", active: true, state: isIncomingScope ? "INCOMING" : "ALL", help: isIncomingScope ? "Contact sheets restricted to incoming folder" : "Contact sheets generated for entire library", tab: "settings" },
        { label: "Alerts", key: "macNotifications", active: config.macNotifications === true, state: config.macNotifications === true ? "ON" : "OFF", help: "Sends native macOS notification center alerts", tab: "notifications" }
      ];

      return React.createElement("div", { className: "lm-switch-indicators", title: "File Management feature switches (click to configure)" },
        switches.map(sw => React.createElement("button", {
          key: sw.key,
          type: "button",
          className: `lm-indicator-pill ${sw.active ? "active" : "inactive"}`,
          onClick: () => setTab(sw.tab || "manage"),
          title: `${sw.label}: ${sw.state} — ${sw.help}. Click to configure.`
        },
          React.createElement("span", { className: `lm-indicator-dot ${sw.active ? "on" : "off"}` }, sw.active ? "●" : "○"),
          React.createElement("strong", null, sw.label),
          React.createElement("span", { className: "lm-indicator-state" }, sw.state))));
    }

    function download(kind) {
      const fields = ["recorded_at", "category", "severity", "action", "status", "scene_id", "file_id", "old_path", "new_path", "detail"];
      const content = kind === "json" ? JSON.stringify(activity, null, 2) : [fields.join(","), ...activity.map(row =>
        fields.map(key => `"${String(row[key] || "").replaceAll('"', '""')}"`).join(","))].join("\n");
      const link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob([content], { type: kind === "json" ? "application/json" : "text/csv" }));
      link.download = `librarymanager-activity.${kind}`; link.click(); URL.revokeObjectURL(link.href);
    }

    function panel(title, description, children, className) {
      return React.createElement("section", { className: `lm-panel ${className || ""}` },
        React.createElement("h2", null, title), React.createElement("p", { className: "lm-help" }, description), children);
    }

    function readableKey(key) {
      return String(key).replaceAll("_", " ").replace(/\b\w/g, letter => letter.toUpperCase());
    }

    function basename(path) {
      return String(path || "").split(/[\\/]/).pop() || "Unknown file";
    }
    function formatBytes(bytes) {
      if (bytes === 0) return "0 B";
      if (!bytes) return "";
      const k = 1024;
      const sizes = ["B", "KB", "MB", "GB", "TB"];
      const i = Math.floor(Math.log(bytes) / Math.log(k));
      return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
    }


    function pendingEventInfo(event) {
      const sourceName = basename(event.source_path);
      const destinationName = basename(event.destination_path);
      const deletionMarker = String(event.destination_path || "").toLowerCase().endsWith(".delete");
      const isVideo = /\.(mp4|m4v|avi|mkv|mov|wmv|flv|webm)$/i.test(sourceName);
      if (event.event_type === "deleted" || deletionMarker) return {
        kind: isVideo ? "Video deletion detected" : "Companion file deletion detected",
        summary: `DELETED  ${sourceName}`,
        explanation: isVideo
          ? "The video disappeared or was placed in a temporary .delete path. Confirm that its Stash scene was also removed."
          : "A companion file (cover art, subtitle, or funscript) was deleted. Confirm this companion file is no longer needed."
      };
      if (event.event_type === "moved") return {
        kind: isVideo ? "Move or rename detected" : "Companion move or rename detected",
        summary: `MOVED  ${sourceName}  →  ${destinationName}`,
        explanation: isVideo
          ? "Watchtower could not safely prove that Stash already knows this new location."
          : "A companion file was moved or renamed."
      };
      if (event.event_type === "created") return {
        kind: "New file detected",
        summary: `NEW FILE  ${sourceName}`,
        explanation: "This file could not be matched safely to an existing missing Stash file."
      };
      return { kind: readableKey(event.event_type), summary: `${readableKey(event.event_type).toUpperCase()}  ${sourceName}`,
        explanation: "Watchtower recorded this change but could not classify it safely." };
    }

    function pendingSummary(events) {
      if (!events?.length) return "0 changes waiting for review";
      const deletions = events.filter(event => event.event_type === "deleted" || String(event.destination_path || "").toLowerCase().endsWith(".delete")).length;
      const moves = events.filter(event => event.event_type === "moved" && !String(event.destination_path || "").toLowerCase().endsWith(".delete")).length;
      const parts = [];
      if (deletions) parts.push(`${deletions} deletion${deletions === 1 ? "" : "s"}`);
      if (moves) parts.push(`${moves} move${moves === 1 ? "" : "s"}`);
      const other = events.length - deletions - moves;
      if (other) parts.push(`${other} other change${other === 1 ? "" : "s"}`);
      return `${parts.join(", ")} waiting for review`;
    }

    async function showSceneHover(event, sceneId) {
      if (!sceneId) return;
      window.clearTimeout(sceneHoverTimer.current);
      const rect = event.currentTarget.getBoundingClientRect();
      const position = { left: Math.min(rect.left, window.innerWidth - 340), top: Math.min(rect.bottom + 8, window.innerHeight - 300) };
      const cached = sceneHoverCache.current.get(String(sceneId));
      if (cached) { setSceneHover({ scene: cached, ...position }); return; }
      setSceneHover({ scene: { id: String(sceneId), title: "Loading scene…", performers: [], studio: null, paths: {} }, ...position });
      try {
        const result = await gql(`query LibraryManagerHoverScene($id: ID!) {
          findScene(id: $id) { id title studio { name } performers { name } paths { screenshot preview } }
        }`, { id: String(sceneId) });
        if (!result.findScene) return;
        sceneHoverCache.current.set(String(sceneId), result.findScene);
        setSceneHover(current => current?.scene?.id === String(sceneId) ? { ...current, scene: result.findScene } : current);
      } catch (_error) {
        setSceneHover(current => current?.scene?.id === String(sceneId)
          ? { ...current, scene: { ...current.scene, title: `Scene ${sceneId}` } } : current);
      }
    }

    const sceneHoverOpenTimer = React.useRef(null);

    function scheduleSceneHoverClose() {
      window.clearTimeout(sceneHoverOpenTimer.current);
      window.clearTimeout(sceneHoverTimer.current);
      sceneHoverTimer.current = window.setTimeout(() => setSceneHover(null), 250);
    }

    function scheduleSceneHoverOpen(event, sceneId) {
      window.clearTimeout(sceneHoverTimer.current);
      window.clearTimeout(sceneHoverOpenTimer.current);
      const rect = event.currentTarget.getBoundingClientRect();
      const capturedEvent = { currentTarget: { getBoundingClientRect: () => rect } };
      sceneHoverOpenTimer.current = window.setTimeout(() => {
        showSceneHover(capturedEvent, sceneId);
      }, 400);
    }

    function SceneLink(sceneId, label, extraClass = "") {
      return React.createElement("a", {
        href: `/scenes/${sceneId}`,
        className: `lm-scene-link ${extraClass}`.trim(),
        onClick: event => event.stopPropagation(),
        onMouseEnter: event => scheduleSceneHoverOpen(event, sceneId),
        onMouseLeave: scheduleSceneHoverClose
      }, label);
    }

    function countdown(item) {
      const elapsed = Math.floor((clock - (data?._liveReceivedAt || clock)) / 1000);
      const remaining = Math.max(0, Number(item.remaining_seconds || 0) - elapsed);
      const minutes = Math.floor(remaining / 60);
      const seconds = String(remaining % 60).padStart(2, "0");
      return `${minutes}:${seconds}`;
    }

    function friendlyActivity(row) {
      if (row.category === "incoming" && row.status === "imported") return `ADDED  ${basename(row.new_path)}`;
      if (row.category === "rename" && row.status === "renamed") return `RENAMED  ${basename(row.new_path)}`;
      if (row.category === "reconciliation" && row.status === "updated") return `RECONNECTED  ${basename(row.new_path)}`;
      if ((row.category === "incoming" || row.category === "filesystem" || row.category === "companion") &&
          (row.action === "deleted" || row.action === "external deletion" || row.action === "incoming file removed" || String(row.new_path || "").toLowerCase().endsWith(".delete")))
        return `${row.status === "review" ? "DELETED — REVIEW" : "DELETED"}  ${basename(row.old_path)}`;
      if (row.category === "filesystem" && ["external move", "moved"].includes(row.action))
        return `MOVED  ${basename(row.old_path)}  →  ${basename(row.new_path)}`;
      if (row.category === "rename" && row.status === "skipped") return `NO RENAME NEEDED  ${row.scene_id ? `Scene ${row.scene_id}` : "metadata unchanged"}`;
      return `${readableKey(row.status).toUpperCase()}  ${basename(row.new_path || row.old_path)}`;
    }

    


    async function handleRetryIncoming(path) {
      setBusy("retry_incoming"); setError("");
      try {
        await operation("retry_incoming_file", { path });
        await refresh();
      } catch (err) {
        setError(err.message || String(err));
      } finally {
        setBusy("");
      }
    }

    async function handleDismissIncoming(path) {
      setBusy("dismiss_incoming"); setError("");
      try {
        await operation("dismiss_incoming_file", { path });
        await refresh();
      } catch (err) {
        setError(err.message || String(err));
      } finally {
        setBusy("");
      }
    }

    async function handleRetryAllIncoming() {
      setBusy("retry_all_incoming"); setError("");
      try {
        const raw = await operation("retry_all_incoming_files");
        const result = typeof raw === "string" ? JSON.parse(raw) : raw;
        await refresh();
      } catch (err) {
        setError(err.message || String(err));
      } finally {
        setBusy("");
      }
    }

    async function handleDismissAllIncoming(count) {
      if (!window.confirm(`Dismiss all ${count} failed video alerts?`)) return;
      setBusy("dismiss_all_incoming"); setError("");
      try {
        const raw = await operation("dismiss_all_incoming_files");
        const result = typeof raw === "string" ? JSON.parse(raw) : raw;
        await refresh();
      } catch (err) {
        setError(err.message || String(err));
      } finally {
        setBusy("");
      }
    }

        function RetroStatus() {
      const allActive = incoming.active || [];
      const waitingAndScanning = allActive.filter(item => item.status !== "failed");
      const hasActiveRename = waitingAndScanning.some(i => i.status === "pending_rename" || i.status === "renaming");
      const activeJobs = (data?.active_jobs || []).filter(job => {
        if (job.status !== "RUNNING" && job.status !== "QUEUED") return false;
        if (hasActiveRename && /rename/i.test(job.description)) return false;
        return true;
      });
      const failedIncoming = allActive.filter(item => item.status === "failed");
      const unavailableRoots = monitor.unavailable_roots || [];
      const unresolved = data?.pending_events || [];
      const stream = (data?.activity || []).slice(0, 250);
      const isMonitorStale = monitor.is_stale === true || monitor.state === "stale";
      const watcherWorking = monitor.state === "running" && !isMonitorStale;

      const totalProblems = failedIncoming.length + unavailableRoots.length + unresolved.length + (isMonitorStale ? 1 : 0);

      const problemsCount = stream.filter(r => r.severity === "error" || r.severity === "warning" || r.status === "failed" || r.status === "review").length;
      const addedCount = stream.filter(r => r.category === "incoming" && r.status === "imported").length;
      const renamedCount = stream.filter(r => r.category === "rename" && r.status === "renamed").length;

      let filteredStream = stream;
      if (terminalFilter === "problems") {
        filteredStream = stream.filter(r => r.severity === "error" || r.severity === "warning" || r.status === "failed" || r.status === "review");
      } else if (terminalFilter === "added") {
        filteredStream = stream.filter(r => r.category === "incoming" && r.status === "imported");
      } else if (terminalFilter === "renamed") {
        filteredStream = stream.filter(r => r.category === "rename" && r.status === "renamed");
      }

      return React.createElement("section", { className: "lm-terminal", "aria-label": `${PRODUCT_NAME} live activity` },
        React.createElement("header", null,
          React.createElement("strong", { className: "lm-terminal-brand" },
            React.createElement("img", { src: "/plugin/librarymanager/assets/watchtower-icon.png", alt: "" }),
            `${PRODUCT_NAME.toUpperCase()} // LIVE`),
          React.createElement("div", { style: { display: "flex", alignItems: "center", gap: "0.75rem" } },
            React.createElement("button", {
              type: "button",
              className: `lm-terminal-refresh-btn ${busy === "refresh" ? "refreshing" : ""}`,
              disabled: !!busy,
              title: "Refresh Watchtower telemetry, queue and activity feed",
              onClick: refresh
            },
              React.createElement("span", { className: `lm-refresh-icon ${busy === "refresh" ? "spinning" : ""}` }, "⟳"),
              busy === "refresh" ? " REFRESHING…" : " REFRESH"
            ),
            totalProblems > 0 && React.createElement("span", { className: "lm-terminal-header-alert" }, `⚠️ ${totalProblems} PROBLEM${totalProblems === 1 ? "" : "S"}`),
            React.createElement("span", { className: watcherWorking ? "online" : "offline" }, watcherWorking ? "● LISTENING" : (isMonitorStale ? "● STALE" : "● STOPPED")))),

        totalProblems > 0 && React.createElement("div", { className: "lm-terminal-attention-card" },
          React.createElement("div", { className: "lm-terminal-attention-header" },
            React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: ".65rem" } },
              React.createElement("span", { className: "lm-terminal-attention-tag" }, "⚠️ NEEDS ATTENTION"),
              React.createElement("span", { className: "lm-terminal-attention-count" }, `${totalProblems} item${totalProblems === 1 ? " requires" : "s require"} your action`)),
            React.createElement("div", { className: "lm-terminal-batch-btns" },
              isMonitorStale && React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn retry",
                disabled: !!busy,
                onClick: async () => {
                  setBusy("restart_monitor");
                  setError("");
                  try {
                    await operation("stop_monitor");
                    await operation("ensure_monitor");
                    await refresh();
                  } catch (e) {
                    setError(`Restart failed: ${e.message}`);
                  } finally {
                    setBusy("");
                  }
                }
              }, "⟳ RESTART WATCHER"),
              failedIncoming.length > 1 && React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn retry",
                disabled: !!busy,
                onClick: handleRetryAllIncoming
              }, `⟳ RETRY ALL (${failedIncoming.length})`),
              failedIncoming.length > 1 && React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn dismiss",
                disabled: !!busy,
                onClick: () => handleDismissAllIncoming(failedIncoming.length)
              }, `✕ DISMISS ALL (${failedIncoming.length})`),
              unresolved.length > 1 && React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn dismiss",
                disabled: !!busy,
                onClick: () => resolveAllPendingEvents("dismiss")
              }, `✕ DISMISS ALL CHANGES (${unresolved.length})`))),

          failedIncoming.map(item => React.createElement("div", { className: "lm-terminal-attention-item failed", key: item.path },
            React.createElement("div", { className: "lm-terminal-attention-title" },
              React.createElement("strong", null, `! VIDEO NOT ADDED: ${basename(item.path)}`),
              React.createElement("span", { className: "lm-terminal-badge error" }, "SCAN FAILED")),
            React.createElement("p", { className: "lm-terminal-attention-detail" },
              React.createElement("b", null, "Reason: "),
              item.detail || "Stash scan job finished without adding the video to your library."),
            React.createElement("p", { className: "lm-terminal-attention-sub" },
              `Location: ${item.path} • Size: ${item.size != null ? (item.size === 0 ? "0 bytes (empty or interrupted download)" : formatBytes(item.size)) : "unknown"} • Attempts: ${item.attempts || 1}`),
            React.createElement("p", { className: "lm-terminal-attention-fix" },
              "→ Fix: Verify that the video finished downloading or replace the file. Once ready, click RETRY SCAN. If you removed it or want to ignore it, click DISMISS."),
            React.createElement("div", { className: "lm-terminal-actions" },
              React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn retry",
                disabled: !!busy,
                onClick: () => handleRetryIncoming(item.path)
              }, "⟳ RETRY SCAN"),
              React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn dismiss",
                disabled: !!busy,
                onClick: () => handleDismissIncoming(item.path)
              }, "✕ DISMISS ALERT"),
              React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn details",
                onClick: () => { setSearch(basename(item.path)); setTab("activity"); }
              }, "👁 VIEW LOG ENTRY")))),

          unavailableRoots.map(path => React.createElement("div", { className: "lm-terminal-attention-item warn", key: path },
            React.createElement("div", { className: "lm-terminal-attention-title" },
              React.createElement("strong", null, `! LIBRARY FOLDER UNAVAILABLE: ${path}`),
              React.createElement("span", { className: "lm-terminal-badge warn" }, "DRIVE OFFLINE")),
            React.createElement("p", { className: "lm-terminal-attention-detail" },
              "Storage volume or network mount is disconnected. Check that the drive is plugged in or mounted."))),

          unresolved.map((event, idx) => {
            const info = pendingEventInfo(event);
            const deletion = event.event_type === "deleted" || String(event.destination_path || "").toLowerCase().endsWith(".delete");
            const isVideo = /\.(mp4|m4v|avi|mkv|mov|wmv|flv|webm)$/i.test(event.source_path || event.destination_path || "");
            const sourceName = basename(event.source_path);

            return React.createElement("div", { className: `lm-terminal-attention-item ${deletion ? "warn" : "info"}`, key: `pending-${event.last_seen_at}-${idx}` },
              React.createElement("div", { className: "lm-terminal-attention-title" },
                React.createElement("strong", null, `! ${info.kind.toUpperCase()}: ${sourceName}`),
                React.createElement("span", { className: `lm-terminal-badge ${deletion ? "warn" : "imported"}` }, event.event_type.toUpperCase())),
              React.createElement("p", { className: "lm-terminal-attention-detail" }, info.explanation),
              React.createElement("p", { className: "lm-terminal-attention-sub" },
                React.createElement("b", null, "From: "), event.source_path),
              event.destination_path && React.createElement("p", { className: "lm-terminal-attention-sub" },
                React.createElement("b", null, "To: "), event.destination_path),
              React.createElement("div", { className: "lm-terminal-actions" },
                deletion && isVideo && event.scene_id && React.createElement("button", {
                  type: "button", className: "lm-terminal-btn danger", disabled: !!busy,
                  onClick: () => resolvePendingEvent(event, "remove_stash_scene")
                }, "🗑 REMOVE STASH SCENE"),
                !deletion && isVideo && React.createElement("button", {
                  type: "button", className: "lm-terminal-btn retry", disabled: !!busy,
                  onClick: () => resolvePendingEvent(event, "scan_destination")
                }, event.event_type === "created" ? "⟳ ADD TO STASH" : "⟳ CHECK DESTINATION"),
                React.createElement("button", {
                  type: "button",
                  className: !isVideo && deletion ? "lm-terminal-btn details" : "lm-terminal-btn dismiss",
                  disabled: !!busy,
                  onClick: () => resolvePendingEvent(event, "dismiss")
                }, !isVideo && deletion ? "✓ ACKNOWLEDGE DELETION" : "✕ DISMISS")));
          })),

        React.createElement("div", { className: "lm-terminal-section" },
          React.createElement("h3", null, "HAPPENING NOW"),
          (waitingAndScanning.length || activeJobs.length) ? React.createElement(React.Fragment, null,
            activeJobs.map(job => {
              const isScan = /scan/i.test(job.description);
              const isRename = /rename/i.test(job.description);
              const isInventory = /inventory/i.test(job.description);
              const isContactSheet = /contact\s*sheet|csm/i.test(job.description);
              const badgeText = isScan ? "SCANNING" : isRename ? "RENAMING TASK" : isContactSheet ? "CSM TASK" : isInventory ? "INVENTORY" : "STASH TASK";
              const pct = (job.progress !== null && job.progress !== undefined && job.progress > 0)
                ? ` (${Math.round(job.progress * 100)}%)`
                : "";
              const statusDetail = job.status === "QUEUED" ? "QUEUED IN STASH" : `RUNNING IN STASH${pct}`;
              return React.createElement("div", { className: "lm-terminal-line running_job", key: `job-${job.id}`, style: { display: "flex", alignItems: "center", gap: "8px" } },
                React.createElement("span", null, badgeText),
                React.createElement("strong", null, job.description),
                React.createElement("em", null, statusDetail),
                React.createElement("button", {
                  type: "button",
                  className: "lm-btn lm-btn-sm lm-btn-danger",
                  style: { marginLeft: "auto", padding: "1px 6px", fontSize: "10px", height: "auto", border: "1px solid rgba(255, 68, 68, 0.4)", borderRadius: "3px", background: "rgba(255, 68, 68, 0.15)", color: "#ff6b6b", cursor: "pointer" },
                  title: "Stop this running task in Stash",
                  onClick: async (e) => {
                    e.stopPropagation();
                    try {
                      await gql(`mutation StopJob($id: ID!) { stopJob(job_id: $id) }`, { id: String(job.id) });
                      await refresh();
                    } catch (err) {
                      console.error("Failed to stop job:", err);
                    }
                  }
                }, "✕ STOP")
              );
            }),
            waitingAndScanning.map(item => {
              const isDownloading = item.status === "downloading";
              const isScanning = item.status === "scanning";
              const isPendingRename = item.status === "pending_rename";
              const isRenaming = item.status === "renaming";
              const isGeneratingSheet = item.status === "generating_sheet";
              let displayName = basename(item.path);
              if (isDownloading) {
                displayName = displayName.replace(/\.(crdownload|part|partial|download|tmp|temp|!qb)$/i, "");
              } else if (isPendingRename || isRenaming) {
                displayName = item.path.replace(/^scene:\/\/\d+\//, "");
              }

              if (isPendingRename) {
                const currentName = item.current_name || displayName;
                const proposedName = item.proposed_name || displayName;
                return React.createElement("div", {
                  className: "lm-terminal-pending-rename-card",
                  key: item.path,
                  style: {
                    margin: ".5rem 0",
                    padding: ".65rem .85rem",
                    border: "1px dashed rgba(32, 230, 74, .45)",
                    borderRadius: "4px",
                    background: "rgba(20, 210, 55, .05)"
                  }
                },
                  React.createElement("div", {
                    style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: ".4rem" }
                  },
                    React.createElement("span", { style: { fontWeight: "700", color: "#39ff64", fontSize: ".82rem" } },
                      `⏳ PENDING RENAME (SCENE #${item.scene_id})`
                    ),
                    React.createElement("em", { style: { fontStyle: "normal", color: "#ffb52e", fontSize: ".8rem", fontWeight: "700" } },
                      `SETTLES IN ${countdown(item)}`
                    )
                  ),
                  React.createElement("div", { style: { fontSize: ".82rem", lineHeight: "1.45", overflowWrap: "anywhere" } },
                    React.createElement("div", { style: { color: "rgba(32, 230, 74, .7)" } },
                      React.createElement("span", { style: { opacity: .65 } }, "Current:  "),
                      currentName
                    ),
                    React.createElement("div", { style: { color: "#39ff64", fontWeight: "600", marginTop: "2px" } },
                      React.createElement("span", { style: { opacity: .65 } }, "Proposed: "),
                      proposedName
                    )
                  ),
                  React.createElement("div", { className: "lm-terminal-actions", style: { marginTop: ".55rem", gap: ".5rem" } },
                    React.createElement("button", {
                      type: "button",
                      className: "lm-terminal-btn retry",
                      disabled: !!busy,
                      onClick: async () => {
                        setBusy(`rename:${item.scene_id}`);
                        setError("");
                        try {
                          await operation("execute_pending_rename_now", { scene_id: item.scene_id });
                          await refresh();
                        } catch (e) {
                          setError(`Rename failed: ${e.message}`);
                        } finally {
                          setBusy("");
                        }
                      }
                    }, "▶ RENAME NOW"),
                    React.createElement("button", {
                      type: "button",
                      className: "lm-terminal-btn dismiss",
                      disabled: !!busy,
                      onClick: async () => {
                        setBusy(`cancel:${item.scene_id}`);
                        setError("");
                        try {
                          await operation("cancel_pending_rename", { scene_id: item.scene_id });
                          await refresh();
                        } catch (e) {
                          setError(`Cancel failed: ${e.message}`);
                        } finally {
                          setBusy("");
                        }
                      }
                    }, "✕ CANCEL RENAME")
                  )
                );
              }

              const badgeText = isRenaming
                ? "RENAMING"
                : isDownloading
                ? "DOWNLOADING"
                : isScanning
                ? "ADDING"
                : isGeneratingSheet
                ? "GENERATING"
                : "FINISHING";
              const statusDetail = isRenaming
                ? "APPLYING FILENAME IN STASH"
                : isDownloading
                ? "INCOMING DOWNLOAD (IN PROGRESS)"
                : isScanning
                ? "STASH IS CHECKING IT"
                : isGeneratingSheet
                ? "CREATING CONTACT SHEET (CSM)"
                : `READY IN ${countdown(item)}`;

              return React.createElement("div", { className: `lm-terminal-line ${item.status}`, key: item.path },
                React.createElement("span", null, badgeText),
                React.createElement("strong", null, displayName),
                React.createElement("em", null, statusDetail)
              );
            })
          ) :
            React.createElement("p", { className: "lm-terminal-empty" }, watcherWorking ? "No videos are downloading or waiting. The watcher is listening." : "The watcher is not running.")),

        React.createElement("div", { className: "lm-terminal-section lm-terminal-stream-section" },
          React.createElement("div", { className: "lm-terminal-stream-header" },
            React.createElement("div", { className: "lm-terminal-stream-title-group" },
              React.createElement("h3", null, "EVENT STREAM // LIVE FEED"),
              React.createElement("small", null, "Click item to inspect • Scroll for full history")),
            React.createElement("div", { className: "lm-terminal-filter-bar" },
              React.createElement("button", {
                type: "button",
                className: `lm-terminal-filter-pill ${terminalFilter === "all" ? "active" : ""}`,
                onClick: () => setTerminalFilter("all")
              }, `ALL (${stream.length})`),
              React.createElement("button", {
                type: "button",
                className: `lm-terminal-filter-pill ${terminalFilter === "problems" ? "active" : ""}`,
                onClick: () => setTerminalFilter("problems")
              }, `⚠️ PROBLEMS (${problemsCount})`),
              React.createElement("button", {
                type: "button",
                className: `lm-terminal-filter-pill ${terminalFilter === "added" ? "active" : ""}`,
                onClick: () => setTerminalFilter("added")
              }, `➕ ADDED (${addedCount})`),
              React.createElement("button", {
                type: "button",
                className: `lm-terminal-filter-pill ${terminalFilter === "renamed" ? "active" : ""}`,
                onClick: () => setTerminalFilter("renamed")
              }, `🏷️ RENAMED (${renamedCount})`))),
          React.createElement("div", { className: "lm-terminal-stream-container" },
            filteredStream.length ? filteredStream.map(row => {
              const isExpanded = expandedOverviewEvents.has(row.id);
              let badgeClass = "ok";
              let badgeText = "RECORDED";
              if (row.severity === "error" || row.status === "failed") {
                badgeClass = "error";
                badgeText = "FAILED";
              } else if (row.severity === "warning") {
                badgeClass = "warn";
                badgeText = "WARN";
              } else if (row.category === "incoming" && row.status === "imported") {
                badgeText = "ADDED";
              } else if (row.category === "rename" && row.status === "renamed") {
                badgeText = "RENAMED";
              } else if (row.category === "reconciliation" && row.status === "updated") {
                badgeText = "RECONNECTED";
              } else if (row.status === "deleted") {
                badgeText = "DELETED";
              }

              const targetName = basename(row.new_path || row.old_path || row.action || "Event");

              return React.createElement("div", {
                key: row.id,
                className: `lm-terminal-stream-row ${isExpanded ? "expanded" : ""} ${row.severity || ""}`,
                onClick: () => toggleExpandedActivity(row.id)
              },
                React.createElement("div", { className: "lm-terminal-stream-summary" },
                  React.createElement("span", { className: "lm-terminal-stream-caret" }, isExpanded ? "▼" : "▶"),
                  React.createElement("time", null, new Date(row.recorded_at).toLocaleString()),
                  React.createElement("span", { className: `lm-terminal-badge ${badgeClass}` }, badgeText),
                  React.createElement("span", { className: "lm-terminal-stream-name" }, targetName),
                  row.scene_id && React.createElement("span", { className: "scene-card lm-scene-pill-card", onClick: e => e.stopPropagation() },
                    SceneLink(row.scene_id, `Scene ${row.scene_id}`, "lm-terminal-stream-pill"))),
                isExpanded && React.createElement("div", { className: "lm-terminal-stream-drawer", onClick: e => e.stopPropagation() },
                  React.createElement("div", { className: "lm-terminal-drawer-grid" },
                    React.createElement("div", null, React.createElement("b", null, "ACTION: "), `${row.category} / ${row.action} (${row.status})`),
                    row.scene_id && React.createElement("div", null, React.createElement("b", null, "SCENE: "), SceneLink(row.scene_id, `Open Scene ${row.scene_id} in Stash ↗`)),
                    row.old_path && React.createElement("div", { className: "lm-drawer-path" }, React.createElement("b", null, "BEFORE: "), React.createElement("code", null, row.old_path)),
                    row.new_path && React.createElement("div", { className: "lm-drawer-path" }, React.createElement("b", null, "AFTER: "), React.createElement("code", null, row.new_path)),
                    row.detail && React.createElement("div", { className: "lm-drawer-detail" }, React.createElement("b", null, "DETAIL: "), row.detail))));
            }) : React.createElement("p", { className: "lm-terminal-empty" }, `No events match filter “${terminalFilter}”.`))),

        React.createElement("footer", null,
          React.createElement("span", null, "Watchtower Live Terminal • Deep History Active • Full exports in Activity tab"),
          React.createElement("span", { className: "lm-terminal-event-count" }, `${filteredStream.length} event${filteredStream.length === 1 ? "" : "s"} shown`)));
    }


    function RecentActivity() {
      const rows = (data?.activity || []).filter(row => row.category !== "monitor").slice(0, 8);
      return panel("Recent activity", "The latest useful actions, with their date and time. Open an item for its full path and explanation.",
        React.createElement("div", { className: "lm-overview-activity" }, rows.length ? rows.map(row =>
          React.createElement("details", { key: row.id, className: `lm-overview-event ${row.severity}`,
            open: expandedOverviewEvents.has(row.id),
            onToggle: event => {
              const isOpen = event.currentTarget.open;
              setExpandedOverviewEvents(previous => {
                if (previous.has(row.id) === isOpen) return previous;
                const next = new Set(previous);
                if (isOpen) next.add(row.id); else next.delete(row.id);
                return next;
              });
            } },
            React.createElement("summary", null,
              React.createElement("time", null, new Date(row.recorded_at).toLocaleString()),
              React.createElement("strong", null, friendlyActivity(row)),
              row.scene_id && SceneLink(row.scene_id, "Open scene")),
            React.createElement("div", { className: "lm-log-detail" },
              row.old_path && React.createElement("p", null, React.createElement("b", null, "Before: "), row.old_path),
              row.new_path && React.createElement("p", null, React.createElement("b", null, "After: "), row.new_path),
              React.createElement("p", null, row.detail || "Completed successfully.")))) :
              React.createElement("p", { className: "lm-empty" }, "No file activity has been recorded yet.")));
    }

    let content;
            if (tab === "overview") content = React.createElement(React.Fragment, null,
      React.createElement("div", { className: "lm-status-bar" },
        React.createElement("div", { className: "lm-status-grid" },
          React.createElement(StatusCard, {
            title: "Filesystem monitoring",
            value: (monitor.is_stale || monitor.state === "stale")
              ? "Stale"
              : (monitor.state === "running" ? "Running" : (config.autoStartMonitor ? "Stopped" : "Off")),
            detail: monitor.unavailable_roots?.length
              ? `${monitor.unavailable_roots.length} library folder${monitor.unavailable_roots.length === 1 ? "" : "s"} unavailable`
              : ((monitor.is_stale || monitor.state === "stale")
                  ? (monitor.stale_reason || `Heartbeat lost (${monitor.heartbeat_age_seconds ? Math.round(monitor.heartbeat_age_seconds) + "s ago" : "no heartbeat"}) — watcher may have stopped`)
                  : (monitor.state === "running"
                      ? (config.automaticMoveReconciliation ? "Watching moves & move reconciliation active" : "Watcher active (reconciliation paused)")
                      : (config.autoStartMonitor ? "Watcher stopped unexpectedly" : "Filesystem watching disabled"))),
            tone: (monitor.unavailable_roots?.length || monitor.is_stale || monitor.state === "stale")
              ? "warn"
              : (monitor.state === "running" ? "ok" : (config.autoStartMonitor ? "warn" : ""))
          }),
          React.createElement(StatusCard, {
            title: "Automatic renaming",
            value: config.automaticRenaming ? (config.testSceneId ? "Test Scene" : "On") : "Off",
            detail: config.testSceneId
              ? `Limited to scene ${config.testSceneId}`
              : (config.automaticRenaming ? "Renames on title, studio & performer edits" : "Metadata-based renaming is off"),
            tone: "ok"
          }),
          React.createElement(StatusCard, {
            title: "Stash library",
            value: `${data?.current_scene_count ?? inventory?.stash_scene_count ?? 0} scenes`,
            detail: inventory ? `Last full file check ${new Date(inventory.completed_at).toLocaleString()}: ${inventory.present_count} available, ${inventory.missing_count} missing` : "No full file check recorded yet",
            tone: !inventory ? "" : inventory.missing_count ? "warn" : "ok"
          }),
          React.createElement(StatusCard, {
            title: "Problems",
            value: `${data?.rename_queue?.failed || 0}`,
            detail: `${data?.rename_queue?.pending || 0} filename changes waiting`,
            tone: data?.rename_queue?.failed ? "warn" : "ok"
          })),
        SwitchIndicators()),
      RetroStatus(),
      showFilenamePreview && filenamePreview && panel("Filename preview",
        `${filenamePreview.run.examined_count} files checked: ${filenamePreview.run.proposed_count} would change, ${filenamePreview.run.unchanged_count} already match and ${filenamePreview.run.conflict_count} are blocked. Showing up to 200 changes or conflicts.`,
        React.createElement(React.Fragment, null,
          React.createElement("div", { className: "lm-actions" },
            React.createElement(Button, { variant: "secondary", onClick: () => setShowFilenamePreview(false), title: "Hide these preview results." }, "Close Preview")),
          React.createElement("div", { className: "lm-preview-list" }, filenamePreview.rows.length ? filenamePreview.rows.map(row =>
            React.createElement("details", { className: `lm-preview-row ${row.status}`, key: `${row.file_id}-${row.proposed_path}` },
              React.createElement("summary", null, React.createElement("span", { className: "lm-badge" }, row.status),
                React.createElement("strong", null, row.proposed_path.split("/").pop()),
                SceneLink(row.scene_id, `Scene ${row.scene_id}`)),
              React.createElement("div", { className: "lm-log-detail" },
                React.createElement("p", null, React.createElement("b", null, "Current: "), row.current_path),
                React.createElement("p", null, React.createElement("b", null, "Proposed: "), row.proposed_path),
                React.createElement("p", null, row.reason)))) :
            React.createElement("p", { className: "lm-empty" }, "Every filename already matches the current safe format.")))));
    else if (tab === "manage") content = React.createElement(React.Fragment, null,
      panel("Filesystem monitoring", "The background watcher monitors your Stash library folders, detects moved or renamed files in Finder, and safely reconnects them to Stash.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "autoStartMonitor", label: "Automatically start filesystem monitor",
            help: "Recommended. Ensures the watcher is running whenever Stash is open in a browser." }),
          React.createElement(Switch, { setting: "automaticMoveReconciliation", label: "Reconcile verified external moves",
            help: "When a file is moved in Finder and verified by size/hash, ask Stash to scan the new path and reconnect it." }),
          data?.startup?.supported && React.createElement(Switch, { setting: "startAtLogin", label: "Keep monitoring without opening Stash in a browser",
            help: data.startup.enabled ? "The watcher starts with macOS and waits for Stash if necessary." : "Recommended: start the watcher with macOS instead of waiting for a Stash page to open." }))),
      panel("Automatic renaming", "Optional. Automatically renames a scene's file when you edit its title, studio or performers in Stash.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "automaticRenaming", label: "Automatic renaming",
            help: "Rename edited scenes using the stable filename base. Turning this off leaves filenames untouched while filesystem monitoring continues running." }),
          config.testSceneId && React.createElement("p", { className: "lm-help", style: { marginTop: "8px", color: "var(--lm-accent-gold, #f59e0b)" } },
            `Testing limit active: automatic renaming applies only to Scene ${config.testSceneId}.`))),
      panel("How filenames look", "Choose a clear style for future filename changes. Saving these choices does not rename your existing library.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "stripMetadataFromTitle", defaultValue: true,
            label: "Clean embedded performers and studio from titles",
            help: "Automatically removes performer and studio names from the Stash title when building filenames to prevent duplicate names." }),
          React.createElement("div", { className: "lm-filename-style-grid" },
            React.createElement(ChoiceField, { label: "Information order", help: "Choose what appears first, second and third.", value: config.filenameOrder || "title,studio,performers", choices: filenameOrders, disabled: busy === "settings", onChange: value => updateSetting("filenameOrder", value) }),
            React.createElement(ChoiceField, { label: "Between the main parts", help: "Choose what appears between the title, studio and performer list.", value: config.filenameSectionSeparator || "dash", choices: sectionSeparators, disabled: busy === "settings", onChange: value => updateSetting("filenameSectionSeparator", value) }),
            React.createElement(ChoiceField, { label: "Between performer names", help: "Choose what appears between two or more performer names.", value: config.filenamePerformerSeparator || "comma", choices: performerSeparators, disabled: busy === "settings", onChange: value => updateSetting("filenamePerformerSeparator", value) })),
          React.createElement("div", { className: "lm-filename-example" },
            React.createElement("small", null, "Example filename"),
            React.createElement("strong", null, exampleFilename),
            React.createElement("span", null, "Only future edits are affected. Very long or duplicate filenames are safely blocked.")),
          React.createElement(RealScenePreviewer, { config, data }),
          React.createElement("div", { className: "lm-actions" },
            React.createElement(TaskButton, { name: readOnlyTasks.filenames, label: "Preview existing filenames", showResults: "filenames", help: "Shows what would change using these choices. It does not rename anything." })))),
      panel("Add completed downloads", "Library Manager can watch one incoming folder and ask Stash to add each new video after the download has completely finished.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "automaticIncomingScan", label: "Automatically add completed videos",
            help: "Existing files are left alone. Only new or still-changing videos are considered." }),
          React.createElement("div", { className: "lm-incoming-fields" },
            React.createElement("label", { className: "lm-field" },
              React.createElement("strong", null, "Incoming folder"),
              React.createElement("small", null, "Choose the folder where downloads arrive. It must be inside one of your Stash library folders."),
              React.createElement("input", { value: config.incomingFolder || "", disabled: busy === "settings",
                onChange: event => setConfig({ ...config, incomingFolder: event.target.value }),
                onBlur: event => updateSetting("incomingFolder", event.target.value.trim()),
                placeholder: "/Volumes/Library/Incoming" })),
            React.createElement(ChoiceField, { label: "Wait before adding",
              help: "The video must stay completely unchanged for this long.",
              value: Number(config.incomingSettleMinutes || 5), disabled: busy === "settings",
              choices: [[1, "1 minute"], [5, "5 minutes (recommended)"], [10, "10 minutes"], [15, "15 minutes"], [30, "30 minutes"]],
              onChange: value => updateSetting("incomingSettleMinutes", Number(value)) })),
          React.createElement("div", { className: `lm-incoming-state ${incomingFolder.valid ? "ready" : "warning"}` },
            React.createElement("strong", null, incomingFolder.valid ? "Folder is ready" : "Folder needs attention"),
            React.createElement("span", null, incomingFolder.reason || "Choose and save an incoming folder."),
            React.createElement("small", null, `${incoming.downloading ? `${incoming.downloading} downloading, ` : ""}${incoming.waiting || 0} waiting, ${incoming.scanning || 0} being added, ${incoming.imported || 0} added, ${incoming.failed || 0} failed.`)),
          React.createElement("p", { className: "lm-help" }, "Part-download files are ignored. When they become a finished MP4, MKV, AVI, MOV, MPG, MPEG, M4V, WMV, WEBM, FLV or another supported video, the unchanged-file wait begins."))),
      config.testSceneId && panel("Testing limit is active", `Automatic filename changes currently apply only to scene ${config.testSceneId}. Finder move tracking still covers the whole library.`,
        React.createElement("p", null, "This is safe while testing. Remove the Test Scene ID under Advanced Tools when you are ready for all edited scenes."), "lm-warning-panel"))
    else if (tab === "rename") content = React.createElement(React.Fragment, null,
      panel("Automatic renaming", "Only title, studio or performer changes trigger renaming. Tag-only edits are ignored.", React.createElement(React.Fragment, null,
        React.createElement(Switch, { setting: "automaticRenaming", label: "Automatic Renaming", help: "Rename edited scenes using the stable filename base." }),
        React.createElement("label", { className: "lm-field" }, React.createElement("strong", null, "Test Scene ID"),
          React.createElement("small", null, "While populated, automatic renaming is restricted to this scene."),
          React.createElement("input", { value: config.testSceneId || "", onChange: e => setConfig({ ...config, testSceneId: e.target.value }),
            onBlur: e => updateSetting("testSceneId", e.target.value.trim()), placeholder: "Leave blank only after testing" })))),
      panel("Preview and controlled test", "Preview is read-only. Applying the configured test can rename one scene.",
        React.createElement(React.Fragment, null,
          React.createElement("div", { className: "lm-actions" },
            React.createElement(TaskButton, { name: readOnlyTasks.filenames, label: "Preview All Filenames" }),
            React.createElement(TaskButton, { name: "Preview Configured Test Rename" }),
            React.createElement(TaskButton, { name: "Apply Configured Test Rename", dangerous: true })),
          React.createElement(Switch, { setting: "allowTestRename", label: "Allow One Test Rename",
            help: "Safety lock required by the manual Apply Configured Test Rename task." }))))
    else if (tab === "companions") content = panel("Companion files", "Matching companions move with a renamed video and roll back if Stash rejects the video rename.",
      React.createElement("div", { className: "lm-info-list" },
        React.createElement("p", null, React.createElement("strong", null, "Subtitles and data: "), ".srt, .vtt, .scc, .ttml, .dfxp, .lrc, .txt and .funscript"),
        React.createElement("p", null, React.createElement("strong", null, "Artwork: "), ".jpg, .jpeg, .png and .webp"),
        React.createElement("p", null, "Both video.jpg and video.ext.jpg styles are recognised. Only exact same-folder matches are touched.")));
    else if (tab === "monitor") content = React.createElement(React.Fragment, null,
      panel("Monitor status", "The watcher records filesystem events but never changes Stash or library files.", React.createElement("div", { className: "lm-status-grid compact" },
        React.createElement(StatusCard, {
          title: "State",
          value: (monitor.is_stale || monitor.state === "stale") ? "Stale" : (monitor.state === "running" ? "Running" : (monitor.state || "Unknown")),
          detail: (monitor.is_stale || monitor.state === "stale")
            ? (monitor.stale_reason || `Heartbeat lost (${monitor.heartbeat_at || "no timestamp"})`)
            : (monitor.heartbeat_at ? `Heartbeat: ${monitor.heartbeat_at}` : "No heartbeat"),
          tone: (monitor.is_stale || monitor.state === "stale") ? "warn" : (monitor.state === "running" ? "ok" : "")
        }),
        React.createElement(StatusCard, { title: "Events", value: monitor.pending_events || 0, detail: `${(monitor.roots || []).length} configured roots` }))),
      panel("Controls", "Start and stop monitoring or convert recorded events into read-only proposals.", React.createElement("div", { className: "lm-actions" },
        React.createElement(TaskButton, { name: "Start Read-Only Filesystem Monitor", label: "Start Monitor", variant: "primary" }),
        React.createElement(TaskButton, { name: "Stop Filesystem Monitor", label: "Stop Monitor" }),
        React.createElement(TaskButton, { name: readOnlyTasks.events, label: "Reconcile Events" }))),
      panel("Automatic monitoring", "Monitoring can restart when the Stash web interface loads. Only verified exact moves may request a targeted Stash scan.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "autoStartMonitor", label: "Automatically start monitor",
            help: "Recommended. Ensures the watcher is running whenever Stash is opened in a browser." }),
          React.createElement(Switch, { setting: "automaticMoveReconciliation", label: "Update Stash after verified external moves",
            help: "Requires an exact inventoried source plus matching size/hash. Ambiguous moves remain read-only." }))));
    else if (tab === "reconcile") content = panel("Find and reconcile", "These operations inspect the inventory and produce reports. They do not move files or update Stash.",
      React.createElement("div", { className: "lm-task-list" },
        [[readOnlyTasks.inventory, "Refresh the SQLite inventory."], [readOnlyTasks.find, "Find safe candidates for missing paths."],
         [readOnlyTasks.plan, "Build a conflict-aware resolution plan."], [readOnlyTasks.merge, "Preview metadata that could be recovered."],
         [readOnlyTasks.events, "Review events recorded by the filesystem monitor."]].map(([name, help]) => React.createElement("div", { className: "lm-task", key: name },
           React.createElement("span", null, React.createElement("strong", null, name), React.createElement("small", null, help)),
           React.createElement(TaskButton, { name, label: "Run" })))))
    else if (tab === "reports") content = panel("Diagnostic results", "Technical results are kept here for troubleshooting. Normal file management does not require this screen.",
      React.createElement(React.Fragment, null,
        React.createElement("div", { className: "lm-actions" },
          React.createElement(Button, { variant: "secondary", onClick: () => setTab("advanced") }, "Back to Advanced")),
        React.createElement("div", { className: "lm-report-list" }, reports ? reports.map(report =>
        React.createElement("details", { className: "lm-report", key: report.id, open: report.id === "filesystem" && report.available },
          React.createElement("summary", null,
            React.createElement("strong", null, report.title),
            React.createElement("span", { className: `lm-badge ${report.available ? "" : "muted"}` }, report.available ? `${report.row_count} findings` : "Not run yet"),
            report.updated_at && React.createElement("time", null, new Date(report.updated_at).toLocaleString())),
          report.available ? React.createElement("div", { className: "lm-report-body" },
            React.createElement("div", { className: "lm-report-summary" }, Object.entries(report.summary || {}).map(([key, value]) =>
              React.createElement("span", { key }, React.createElement("small", null, readableKey(key)), React.createElement("strong", null, String(value ?? "—"))))),
            report.rows.length ? React.createElement("div", { className: "lm-report-rows" }, report.rows.map((row, index) =>
              React.createElement("details", { key: index },
                React.createElement("summary", null, row.status || row.confidence || row.action || row.recommendation || `Finding ${index + 1}`,
                  row.scene_id && React.createElement("span", { className: "scene-card lm-scene-pill-card", onClick: e => e.stopPropagation() }, SceneLink(row.scene_id, `Scene ${row.scene_id}`, "lm-activity-scene-pill"))),
                React.createElement("pre", null, JSON.stringify(row, null, 2))))) :
              React.createElement("p", { className: "lm-empty" }, "This report contains no individual findings."),
            React.createElement("small", { className: "lm-report-file" }, `Export file: ${report.filename}`)) :
            React.createElement("p", { className: "lm-empty" }, report.error || "Run the corresponding tool under Advanced Tools to create this report."))) :
        React.createElement("p", { className: "lm-empty" }, "Loading diagnostic results…"))))
        else if (tab === "activity") content = panel("Activity", "Search the history of detected moves, filename changes, warnings and failures.",
      React.createElement(React.Fragment, null,
        React.createElement("div", { className: "lm-log-tools" },
          React.createElement("input", { type: "search", value: search, onChange: e => setSearch(e.target.value), placeholder: "Search scene, filename, action or outcome…" }),
          React.createElement(Button, { variant: "secondary", onClick: () => download("csv") }, "Download CSV"),
          React.createElement(Button, { variant: "secondary", onClick: () => download("json") }, "Download JSON")),
        React.createElement("div", { className: "lm-log" }, activity.length ? activity.map(row => {
          const targetName = basename(row.new_path || row.old_path || row.action);
          return React.createElement("details", { key: row.id, className: `lm-log-row ${row.severity || ""}` },
            React.createElement("summary", null,
              React.createElement("time", null, new Date(row.recorded_at).toLocaleString()),
              React.createElement("span", { className: `lm-badge ${row.status || ""}` }, row.status),
              React.createElement("span", { className: "lm-log-filename", title: targetName }, targetName),
              React.createElement("small", { className: "lm-log-action-tag" }, row.action),
              row.scene_id && React.createElement("span", { className: "scene-card lm-scene-pill-card", onClick: e => e.stopPropagation() }, SceneLink(row.scene_id, `Scene ${row.scene_id}`, "lm-activity-scene-pill"))),
            React.createElement("div", { className: "lm-log-detail" },
              React.createElement("p", null, React.createElement("b", null, "Category: "), `${row.category} • Action: ${row.action} (${row.status})`),
              row.old_path && React.createElement("p", null, React.createElement("b", null, "Before: "), React.createElement("code", null, row.old_path)),
              row.new_path && React.createElement("p", null, React.createElement("b", null, "After: "), React.createElement("code", null, row.new_path)),
              row.detail && React.createElement("p", null, React.createElement("b", null, "Detail: "), row.detail),
              row.scene_id && React.createElement("p", null, SceneLink(row.scene_id, `Open Scene ${row.scene_id} in Stash ↗`))));
        }) : React.createElement("p", { className: "lm-empty" }, "No matching activity has been recorded yet."))));
    else if (tab === "notifications") content = panel("macOS notifications", "Notifications are optional. Routine file modifications never produce alerts.", React.createElement(React.Fragment, null,
      React.createElement(Switch, { setting: "macNotifications", label: "Important warnings and failures", help: "Notify for failed renames, unavailable roots and events needing review." }),
      React.createElement(Switch, { setting: "notifySuccessfulRenames", label: "Successful renames", help: "Also notify after a completed automatic rename." })));
    else content = React.createElement(React.Fragment, null,
      panel("Plugin Settings", "Safely configure all Watchtower monitor, renaming, and file management settings without leaving the plugin.",
        React.createElement(React.Fragment, null,
          React.createElement("div", { className: "lm-field", style: { padding: "10px 14px", background: "rgba(255,255,255,0.03)", borderRadius: "6px", marginBottom: "14px", border: "1px solid rgba(255,255,255,0.08)" } },
            React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "4px" } },
              React.createElement("strong", null, "Hooks: Rename Edited Scene"),
              React.createElement("span", { className: "lm-badge ok", style: { fontSize: "11px" } }, "Triggers on: Scene.Update.Post")),
            React.createElement("small", null, "Handles relevant scene metadata changes when Automatic Renaming is explicitly enabled.")),
          React.createElement(Switch, { setting: "autoStartMonitor", label: "Automatically Start Filesystem Monitor",
            help: "Ensure monitoring is running when the Stash web interface loads. Recommended." }),
          data?.startup?.supported && React.createElement(Switch, { setting: "startAtLogin", label: "Start Monitoring with macOS",
            help: data.startup.enabled ? "The watcher starts with macOS and waits for Stash if necessary." : "Start the background watcher at login and retry once a minute until Stash is available." }),
          React.createElement(Switch, { setting: "automaticMoveReconciliation", label: "Reconcile Verified External Moves",
            help: "After an exact watched move passes size/hash checks, ask Stash to scan the destination and verify its path update." }),
          React.createElement(Switch, { setting: "automaticIncomingScan", label: "Automatically Add Completed Videos",
            help: "Watch one chosen incoming folder and ask Stash to add a new video after it has completely finished downloading." }),
          React.createElement(Switch, { setting: "automaticRenaming", label: "Automatic Renaming",
            help: "Rename an edited scene from its stable base, studio and performers. Test Scene ID restricts this to one scene while testing." }))),
      panel("Testing & Safety Limits", "Safe controls for testing renames before applying them across your entire library.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "allowTestRename", label: "Allow One Test Rename",
            help: "Must be enabled before Apply Configured Test Rename can change the configured scene’s filename." }),
          React.createElement("label", { className: "lm-field" },
            React.createElement("strong", null, "Test Scene ID"),
            React.createElement("small", null, "Scene used by Step 6 tests. While set, Automatic Renaming is restricted to this scene only."),
            React.createElement("input", {
              value: config.testSceneId || "",
              disabled: busy === "settings",
              onChange: e => setConfig({ ...config, testSceneId: e.target.value }),
              onBlur: e => updateSetting("testSceneId", e.target.value.trim()),
              placeholder: "Leave blank for all scenes"
            })),
          React.createElement("div", { className: "lm-actions", style: { marginTop: "12px" } },
            React.createElement(TaskButton, { name: "Preview Configured Test Rename", label: "Preview Test Rename", help: "Preview the filename for Test Scene ID without changing any file." }),
            React.createElement(TaskButton, { name: "Apply Configured Test Rename", label: "Apply Configured Test Rename", dangerous: true, help: "Rename Test Scene ID. Requires Allow One Test Rename to be enabled." })))),
      panel("Filename Information & Formatting", "Choose whether the title, studio or performers appear first and how they are separated.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "stripMetadataFromTitle", defaultValue: true,
            label: "Clean Embedded Metadata from Titles",
            help: "Automatically remove tagged performers and studio names from the Stash title when building filenames to prevent duplication." }),
          React.createElement("div", { className: "lm-filename-style-grid" },
            React.createElement(ChoiceField, { label: "Filename Information Order", help: "Choose whether the title, studio or performers appear first. This affects future renames only.", value: config.filenameOrder || "title,studio,performers", choices: filenameOrders, disabled: busy === "settings", onChange: value => updateSetting("filenameOrder", value) }),
            React.createElement(ChoiceField, { label: "Between Title, Studio and Performers", help: "Friendly choice used between the main parts of a filename, such as Dash or Space.", value: config.filenameSectionSeparator || "dash", choices: sectionSeparators, disabled: busy === "settings", onChange: value => updateSetting("filenameSectionSeparator", value) }),
            React.createElement(ChoiceField, { label: "Between Performer Names", help: "Friendly choice used when a scene has more than one performer, such as Comma or Space.", value: config.filenamePerformerSeparator || "comma", choices: performerSeparators, disabled: busy === "settings", onChange: value => updateSetting("filenamePerformerSeparator", value) })),
          React.createElement("div", { className: "lm-filename-example" },
            React.createElement("small", null, "Example filename"),
            React.createElement("strong", null, exampleFilename),
            React.createElement("span", null, "Only future edits are affected. Very long or duplicate filenames are safely blocked.")),
          React.createElement(RealScenePreviewer, { config, data }),
          React.createElement("div", { className: "lm-actions", style: { marginTop: "12px" } },
            React.createElement(TaskButton, { name: readOnlyTasks.filenames, label: "Preview All Filenames (Read Only)", showResults: "filenames", help: "Shows what would change using these choices. It does not rename anything." })))),
      panel("Finder Contact Sheets (CSM)", "Generate multi-frame visual contact sheet companion images (.mp4.jpg) for browsing in macOS Finder or network shares without modifying Stash's native previews.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "generateContactSheets", label: "Automatically Generate Contact Sheets for New Downloads",
            help: "Runs quietly on Apple Silicon efficiency cores after a new video settles in your incoming folder." }),
          React.createElement("div", { className: "lm-filename-style-grid", style: { marginTop: "10px" } },
            React.createElement(ChoiceField, {
              label: "Contact Sheet Location Scope",
              help: "Choose whether contact sheets are generated across all library folders or restricted to your incoming folder.",
              value: config.contactSheetScope || "all",
              choices: [
                ["all", "Entire Library (All Folders) — Generates and refreshes sheets for all scenes"],
                ["incoming", "Incoming Folder Only — Restricts contact sheets strictly to your incoming downloads folder"]
              ],
              disabled: busy === "settings",
              onChange: value => updateSetting("contactSheetScope", value)
            }),
            React.createElement(ChoiceField, {
              label: "Rename & Contact Sheet Settle Delay",
              help: "Wait this many seconds after metadata edits before renaming files and refreshing contact sheets. Additional edits reset the timer.",
              value: Number(config.renameSettleSeconds !== undefined ? config.renameSettleSeconds : 30),
              choices: [
                [0, "Immediate (No delay)"],
                [15, "15 seconds"],
                [30, "30 seconds (Recommended)"],
                [60, "60 seconds"]
              ],
              disabled: busy === "settings",
              onChange: value => updateSetting("renameSettleSeconds", Number(value))
            }),
            React.createElement(ChoiceField, {
              label: "Grid Layout",
              help: "Number of timestamped scene snapshots per contact sheet.",
              value: config.contactSheetGrid || "4x4",
              choices: [["4x4", "4×4 (16 frames) — Standard"], ["4x5", "4×5 (20 frames) — Detailed"], ["5x5", "5×5 (25 frames) — Dense Overview"], ["3x4", "3×4 (12 frames) — Compact"], ["4x6", "4×6 (24 frames) — Extended"]],
              disabled: busy === "settings",
              onChange: value => updateSetting("contactSheetGrid", value)
            })),
          React.createElement(Switch, { setting: "contactSheetBanner", defaultValue: true,
            label: "Include Metadata Header Banner",
            help: "Adds a top header banner displaying filename, resolution, size, and duration." }),
          React.createElement(Switch, { setting: "contactSheetAdjustVertical", defaultValue: true,
            label: "Auto-adjust Grid for Vertical (9:16) Videos",
            help: "Arranges portrait clips into wider grids so contact sheets fit standard widescreen monitors." }),
          React.createElement("label", { className: "lm-field", style: { marginTop: "12px" } },
            React.createElement("strong", null, "Custom Script Override (Optional)"),
            React.createElement("small", null, "Leave blank to use Watchtower’s high-speed built-in generator, or specify an external script path."),
            React.createElement("input", {
              value: config.contactSheetScript || "",
              disabled: busy === "settings",
              onChange: e => setConfig({ ...config, contactSheetScript: e.target.value }),
              onBlur: e => updateSetting("contactSheetScript", e.target.value.trim()),
              placeholder: "Leave blank for built-in generator (or e.g. /path/to/custom_script.sh)"
            })),
          React.createElement("div", { className: "lm-actions", style: { marginTop: "14px" } },
            React.createElement(TaskButton, {
              name: "Generate Missing Contact Sheets for Incoming Folder",
              label: "🎞️ Generate Missing Contact Sheets for Incoming Folder",
              help: "Safely process existing videos in your incoming folder that are currently missing a contact sheet."
            })))),
      panel("Incoming Downloads", "One folder inside your Stash library where new downloads arrive before you organise them.",
        React.createElement(React.Fragment, null,
          React.createElement("div", { className: "lm-incoming-fields" },
            React.createElement("label", { className: "lm-field" },
              React.createElement("strong", null, "Incoming Folder"),
              React.createElement("small", null, "One folder inside your Stash library where new downloads arrive before you organise them."),
              React.createElement("input", {
                value: config.incomingFolder || "",
                disabled: busy === "settings",
                onChange: event => setConfig({ ...config, incomingFolder: event.target.value }),
                onBlur: event => updateSetting("incomingFolder", event.target.value.trim()),
                placeholder: "/Volumes/Library/Incoming"
              })),
            React.createElement(ChoiceField, {
              label: "Wait Before Adding a Video",
              help: "Minutes that a video must remain completely unchanged before Library Manager asks Stash to add it. Default is 5.",
              value: Number(config.incomingSettleMinutes || 5),
              disabled: busy === "settings",
              choices: [[1, "1 minute"], [5, "5 minutes (recommended)"], [10, "10 minutes"], [15, "15 minutes"], [30, "30 minutes"]],
              onChange: value => updateSetting("incomingSettleMinutes", Number(value))
            })),
          React.createElement("div", { className: `lm-incoming-state ${incomingFolder.valid ? "ready" : "warning"}` },
            React.createElement("strong", null, incomingFolder.valid ? "Folder is ready" : "Folder needs attention"),
            React.createElement("span", null, incomingFolder.reason || "Choose and save an incoming folder."),
            React.createElement("small", null, `${incoming.downloading ? `${incoming.downloading} downloading, ` : ""}${incoming.waiting || 0} waiting, ${incoming.scanning || 0} being added, ${incoming.imported || 0} added, ${incoming.failed || 0} failed.`)))),
      panel("macOS Notifications", "Show important Library Manager warnings and failures through macOS. Disabled by default.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "macNotifications", label: "macOS Notifications",
            help: "Show important Library Manager warnings and failures through macOS. Disabled by default." }),
          React.createElement(Switch, { setting: "notifySuccessfulRenames", label: "Notify Successful Renames",
            help: "Also show a macOS notification after a successful automatic rename. Requires macOS Notifications." }))),
      panel("Maintenance tools", "These are safe diagnostic and report-building operations. Hover over Run for an explanation.",
        React.createElement(React.Fragment, null,
          React.createElement("div", { className: "lm-actions" },
            React.createElement(Button, { variant: "secondary", onClick: () => { loadReports().catch(error => setError(error.message)); setTab("reports"); } }, "View diagnostic results")),
          React.createElement("div", { className: "lm-task-list" },
          [[readOnlyTasks.inventory, "Compare every recorded Stash path with the filesystem and refresh the private inventory."],
           [readOnlyTasks.find, "Look in a missing file's original folder for a renamed file with the same identity."],
           [readOnlyTasks.plan, "Explain how stale and live Stash records could be resolved without changing either one."],
           [readOnlyTasks.merge, "Show metadata that could be recovered without overwriting different existing values."],
           [readOnlyTasks.filenames, "Show proposed filenames and collisions without renaming anything."],
           [readOnlyTasks.events, "Convert recorded filesystem events into verified, review or informational findings."],
           [readOnlyTasks.activity, "Refresh the permanent JSON and CSV activity exports."]].map(([name, help]) =>
            React.createElement("div", { className: "lm-task", key: name, title: help },
              React.createElement("span", null, React.createElement("strong", null, name.replace(" (Read Only)", "")), React.createElement("small", null, help)),
              React.createElement(TaskButton, { name, label: "Run", help, showResults: "reports" })))))))

    return React.createElement("main", { className: "lm-dashboard" },
      React.createElement("header", { className: "lm-header" }, React.createElement("div", { className: "lm-page-brand" },
        React.createElement("div", { className: "lm-brand-wrapper" },
          React.createElement("img", {
            src: useOriginalHeader
              ? "/plugin/librarymanager/assets/watchtower-header-original.png"
              : "/plugin/librarymanager/assets/watchtower-header-v2.png",
            alt: "Watchtower — Stash Library Manager",
            className: "lm-brand-lockup"
          }),
          !useOriginalHeader && React.createElement("div", { className: "lm-lighthouse-container" },
            React.createElement("div", { className: "lm-lighthouse-beam-left" }),
            React.createElement("div", { className: "lm-lighthouse-beam-right" }),
            React.createElement("div", { className: "lm-lighthouse-flare-streak" }),
            React.createElement("div", { className: "lm-lighthouse-core" })
          ),
          React.createElement("div", {
            className: "lm-easter-egg-t",
            onClick: toggleHeaderArt
          })
        ),
        React.createElement("p", null, "Inventory, safe renaming, monitoring and recovery in one place."))),
      error && React.createElement("div", { className: "lm-message error" }, error),
      notice && React.createElement("div", { className: "lm-message" }, notice),
      React.createElement("div", { className: "lm-layout" },
        React.createElement("nav", { className: "lm-tabs" },
          sections.map(([id, label]) => React.createElement("button", {
            key: id, className: tab === id ? "active" : "", onClick: () => setTab(id)
          }, label)),
          React.createElement("div", { className: "lm-tabs-divider" }),
          React.createElement("a", {
            href: "https://buymeacoffee.com/kamarsh",
            target: "_blank",
            rel: "noopener noreferrer",
            className: "lm-kitkat-btn",
            title: "Support continued Watchtower & FastTag development"
          }, "Buy me a KitKat 🍫")
        ), React.createElement("div", { className: "lm-content" }, data ? content : React.createElement("p", null, "Loading Library Manager…"))),
      sceneHover && React.createElement("article", { className: "scene-card lm-scene-hover-card",
        style: { left: `${Math.max(8, sceneHover.left)}px`, top: `${Math.max(8, sceneHover.top)}px` },
        onMouseEnter: () => window.clearTimeout(sceneHoverTimer.current), onMouseLeave: scheduleSceneHoverClose },
        React.createElement("a", { href: `/scenes/${sceneHover.scene.id}`, className: "lm-scene-hover-image" },
          sceneHover.scene.paths?.screenshot ? React.createElement("img", { src: sceneHover.scene.paths.screenshot, alt: "" }) : React.createElement("span", null, "Scene preview")),
        React.createElement("div", { className: "lm-scene-hover-info" },
          React.createElement("strong", null, sceneHover.scene.title || `Scene ${sceneHover.scene.id}`),
          sceneHover.scene.studio?.name && React.createElement("span", null, sceneHover.scene.studio.name),
          sceneHover.scene.performers?.length ? React.createElement("span", null, sceneHover.scene.performers.map(item => item.name).join(", ")) : null,
          React.createElement("small", null, window.FastTag ? "Right-click here for FastTag" : "Open scene"))));
  }

    function NavStatus() {
    const [health, setHealth] = React.useState({ tone: "checking", title: "Library Manager: checking watcher…", status: null });
    const [showHud, setShowHud] = React.useState(false);
    const hudTimer = React.useRef(null);

    const check = React.useCallback(async () => {
      try {
        const raw = await operation("monitor_health");
        const status = typeof raw === "string" ? JSON.parse(raw) : raw;
        const heartbeatAge = status.heartbeat_at ? Date.now() - Date.parse(status.heartbeat_at) : Infinity;
        const unavailable = status.unavailable_roots?.length || 0;
        const pending = status.pending_events || 0;
        const incomingFailed = status.incoming?.failed || 0;
        const incomingWaiting = status.incoming?.waiting || 0;
        let tone = "healthy";
        let title = `Library Manager: watching ${status.roots?.length || 0} library folders`;
        if (status.state !== "running" || heartbeatAge > 15000) {
          tone = "error";
          title = status.state !== "running" ? "Library Manager warning: file watcher is stopped" : "Library Manager warning: watcher heartbeat is stale";
        } else if (unavailable || incomingFailed) {
          tone = "error";
          title = unavailable
            ? `Library Manager warning: ${unavailable} library folder${unavailable === 1 ? " is" : "s are"} unavailable`
            : `Library Manager warning: ${incomingFailed} completed video${incomingFailed === 1 ? " could" : "s could"} not be added`;
        } else if (pending) {
          tone = "warning";
          title = `Library Manager: listening; ${pending} detected change${pending === 1 ? " needs" : "s need"} review`;
        } else if (incomingWaiting) {
          tone = "healthy";
          title = `Library Manager: watching; ${incomingWaiting} video${incomingWaiting === 1 ? " is" : "s are"} finishing`;
        }
        setHealth({ tone, title, status });
      } catch (error) {
        setHealth({ tone: "error", title: `Library Manager status unavailable: ${error.message}`, status: null });
      }
    }, []);

    React.useEffect(() => {
      check();
      const timer = window.setInterval(check, 30000);
      const visible = () => { if (!document.hidden) check(); };
      document.addEventListener("visibilitychange", visible);
      return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", visible); };
    }, [check]);

    return React.createElement("div", {
      className: "nav-utility lm-nav-wrapper",
      style: { position: "relative", display: "inline-flex", alignItems: "center" },
      onMouseEnter: () => { window.clearTimeout(hudTimer.current); setShowHud(true); },
      onMouseLeave: () => { hudTimer.current = window.setTimeout(() => setShowHud(false), 250); }
    },
      React.createElement(NavLink, { className: "lm-nav-link", exact: true, to: PATH, title: health.title,
        "aria-label": health.title },
        React.createElement(Button, {
          className: `minimal d-flex align-items-center h-100 lm-nav-button lm-health-${health.tone}`
        }, React.createElement("img", { className: "lm-watchtower-icon",
          src: "/plugin/librarymanager/assets/watchtower-icon.png", alt: "" }))),
      showHud && React.createElement("div", {
        className: "lm-navbar-hud",
        onMouseEnter: () => window.clearTimeout(hudTimer.current),
        onMouseLeave: () => setShowHud(false)
      },
        React.createElement("div", { className: "lm-navbar-hud-header" },
          React.createElement("strong", null, "Watchtower Monitor"),
          React.createElement("span", { className: `lm-hud-pill ${health.tone}` },
            health.tone === "healthy" ? "● Running" : health.tone === "warning" ? "● Action Needed" : "● Attention")),
        React.createElement("div", { className: "lm-navbar-hud-grid" },
          React.createElement("div", null, React.createElement("span", null, "Watched Folders:"), React.createElement("strong", null, `${health.status?.roots?.length || 0}`)),
          React.createElement("div", null, React.createElement("span", null, "Unreviewed Changes:"), React.createElement("strong", null, `${health.status?.pending_events || 0}`)),
          React.createElement("div", null, React.createElement("span", null, "Failed Downloads:"), React.createElement("strong", null, `${health.status?.incoming?.failed || 0}`)),
          React.createElement("div", null, React.createElement("span", null, "Incoming Finishing:"), React.createElement("strong", null, `${health.status?.incoming?.waiting || 0}`))),
        React.createElement(NavLink, { to: PATH, className: "lm-navbar-hud-link", onClick: () => setShowHud(false) },
          "Open Watchtower Dashboard ↗"))
    );
  }

  function SceneCorrectionModal() {
    const [state, setState] = React.useState({ show: false, sceneId: "", filename: "", current: "", preview: null, busy: false, error: "" });

    React.useEffect(() => {
      const open = async event => {
        const sceneId = String(event.detail?.sceneId || "");
        setState({ show: true, sceneId, filename: "", current: "", preview: null, busy: true, error: "" });
        try {
          const data = await gql(`query CorrectionScene($id: ID!) { findScene(id: $id) { files { id path basename } } }`, { id: sceneId });
          const file = data.findScene?.files?.[0];
          if (!file) throw new Error("This scene has no video file.");
          const basename = file.basename || file.path?.split("/").pop() || "";
          setState({ show: true, sceneId, filename: basename, current: file.path || basename, preview: null, busy: false, error: "" });
        } catch (error) {
          setState(previous => ({ ...previous, busy: false, error: error.message }));
        }
      };
      window.addEventListener("librarymanager:correct-filename", open);
      return () => window.removeEventListener("librarymanager:correct-filename", open);
    }, []);

    async function run(apply) {
      if (apply && state.preview?.status !== "ready") return;
      setState(previous => ({ ...previous, busy: true, error: "" }));
      try {
        const raw = await operation(apply ? "apply_manual_filename" : "preview_manual_filename",
          { scene_id: state.sceneId, filename: state.filename });
        const result = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (apply && result.status === "renamed") {
          setState(previous => ({ ...previous, show: false, busy: false, preview: result, current: result.proposed_path }));
          const notification = document.createElement("div");
          notification.className = "lm-correction-toast";
          notification.textContent = `✓ Filename corrected to ${result.proposed_path.split("/").pop()}`;
          document.body.appendChild(notification);
          window.setTimeout(() => notification.remove(), 5000);
        } else {
          setState(previous => ({ ...previous, busy: false, preview: result }));
        }
      } catch (error) {
        setState(previous => ({ ...previous, busy: false, error: error.message }));
      }
    }

    return React.createElement(Modal, { show: state.show, onHide: () => setState(previous => ({ ...previous, show: false })), centered: true, dialogClassName: "lm-themed-modal" },
      React.createElement(Modal.Header, { closeButton: true }, React.createElement(Modal.Title, null, "Correct Filename")),
      React.createElement(Modal.Body, null,
        React.createElement("p", { className: "lm-help" }, `Scene ${state.sceneId}. This changes only this file and its exact matching companion files.`),
        state.error && React.createElement("div", { className: "lm-message error" }, state.error),
        state.current && React.createElement("p", { className: "lm-modal-path" }, React.createElement("b", null, "Current: "), state.current),
        React.createElement(Form.Group, null,
          React.createElement(Form.Label, null, "Correct filename"),
          React.createElement(Form.Control, { value: state.filename, disabled: state.busy,
            onChange: event => setState(previous => ({ ...previous, filename: event.target.value, preview: null })) })),
        state.preview && React.createElement("div", { className: `lm-correction-preview ${state.preview.status}` },
          React.createElement("strong", null, String(state.preview.status || "").replaceAll("_", " ").replace(/\b\w/g, letter => letter.toUpperCase())),
          state.preview.proposed_path && React.createElement("p", null, React.createElement("b", null, "Proposed: "), state.preview.proposed_path),
          React.createElement("p", null, state.preview.reason),
          state.preview.associated_files?.length ? React.createElement("small", null, `${state.preview.associated_files.length} companion file(s) will follow it.`) : null)),
      React.createElement(Modal.Footer, null,
        React.createElement(Button, { variant: "secondary", onClick: () => setState(previous => ({ ...previous, show: false })) }, "Close"),
        React.createElement(Button, { variant: "secondary", disabled: state.busy || !state.filename.trim(), onClick: () => run(false) }, state.busy ? "Checking…" : "Preview"),
        React.createElement(Button, { variant: "danger", disabled: state.busy || state.preview?.status !== "ready", onClick: () => run(true) }, "Apply Correction")));
  }

  function installSceneMenuCorrection() {
    if (document.getElementById("librarymanager-correction-root")) return;
    const root = document.createElement("div");
    root.id = "librarymanager-correction-root";
    document.body.appendChild(root);
    function addCorrectionItem(sceneId) {
      const isSceneMenu = candidate => {
        const rect = candidate.getBoundingClientRect();
        const style = window.getComputedStyle(candidate);
        const text = candidate.textContent || "";
        return rect.width > 80 && rect.height > 50 && style.display !== "none" && style.visibility !== "hidden" &&
          text.includes("Rescan") && text.includes("Generate") && text.includes("Delete");
      };
      let candidates = [...document.querySelectorAll(".dropdown-menu, [role='menu']")];
      let menu = candidates.reverse().find(isSceneMenu);
      if (!menu) {
        candidates = [...document.querySelectorAll("div, ul")].filter(candidate => candidate.children.length < 20);
        menu = candidates.reverse().find(isSceneMenu);
      }
      if (!menu || menu.querySelector(".lm-correct-filename-item")) return false;
      const divider = document.createElement("div");
      divider.className = "dropdown-divider lm-correct-filename-divider";
      const item = document.createElement("button");
      item.type = "button";
      item.className = "dropdown-item lm-correct-filename-item";
      item.textContent = "✏️ Correct Filename";
      item.addEventListener("click", event => {
        event.preventDefault();
        event.stopPropagation();
        window.dispatchEvent(new CustomEvent("librarymanager:correct-filename", { detail: { sceneId } }));
      }, true);
      menu.append(divider, item);
      return true;
    }

    document.addEventListener("click", event => {
      const sceneMatch = window.location.pathname.match(/^\/scenes\/(\d+)(?:\/|$)/);
      if (!sceneMatch) return;
      const button = event.target.closest("button");
      if (!button) return;
      [0, 40, 120].forEach(delay => window.setTimeout(() => addCorrectionItem(sceneMatch[1]), delay));
    }, true);
    const menuObserver = new MutationObserver(() => {
      const sceneMatch = window.location.pathname.match(/^\/scenes\/(\d+)(?:\/|$)/);
      if (!sceneMatch) return;
      window.setTimeout(() => addCorrectionItem(sceneMatch[1]), 0);
    });
    menuObserver.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
    try {
      ReactDOM.render(React.createElement(SceneCorrectionModal), root);
    } catch (error) {
      console.error("[LibraryManager] Could not mount filename correction modal:", error);
    }
  }

  window.StashLibraryManager = Object.freeze({
    version: "0.7.0",
    openFilenameCorrection(sceneId) {
      const normalized = String(sceneId || "").trim();
      if (!/^\d+$/.test(normalized)) throw new Error("Library Manager requires a valid Scene ID");
      window.dispatchEvent(new CustomEvent("librarymanager:correct-filename", { detail: { sceneId: normalized } }));
    }
  });

  api.register.route(PATH, Dashboard);
  api.patch.before("MainNavBar.UtilityItems", function (props) {
    return [{ children: React.createElement(React.Fragment, null, props.children,
      React.createElement(NavStatus)) }];
  });
  installSceneMenuCorrection();
  // This is idempotent: the backend returns immediately when the monitor is already alive.
  getConfig().then(config => {
    if (config.autoStartMonitor === true) operation("ensure_monitor").catch(error =>
      console.error("[LibraryManager] Could not auto-start filesystem monitor:", error));
  }).catch(error => console.error("[LibraryManager] Could not read auto-start setting:", error));
})();
