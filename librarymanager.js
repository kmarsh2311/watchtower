(function () {
  "use strict";
  const api = window.PluginApi;
  if (!api) return;
  const React = api.React;
  const ReactDOM = api.ReactDOM;
  const { useState, useEffect, useRef, useMemo, useCallback } = React;
  const { NavLink } = api.libraries.ReactRouterDOM;
  const { Button, Modal, Form } = api.libraries.Bootstrap;
  const PLUGIN_ID = "librarymanager";

  function RestartIcon({ size = 15, className = "lm-btn-icon-svg", spinning = false }) {
    return React.createElement("svg", {
      width: size,
      height: size,
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      strokeWidth: "2.75",
      strokeLinecap: "round",
      strokeLinejoin: "round",
      className: `${className} ${spinning ? "spinning" : ""}`.trim()
    },
      React.createElement("path", { d: "M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" }),
      React.createElement("path", { d: "M21 3v5h-5" })
    );
  }

  function StopIcon({ size = 13, className = "lm-btn-icon-svg" }) {
    return React.createElement("svg", {
      width: size,
      height: size,
      viewBox: "0 0 24 24",
      fill: "currentColor",
      className: className
    },
      React.createElement("rect", { x: "4", y: "4", width: "16", height: "16", rx: "2" })
    );
  }

  
  function Toast({ notice, error, onClose }) {
    const message = error || notice;
    const isError = !!error;
    if (!message) return null;

    return React.createElement("div", {
      className: `lm-toast-notification ${isError ? "error" : "success"}`,
      role: "alert"
    },
      React.createElement("span", { className: "lm-toast-icon" }, isError ? "✕" : "✓"),
      React.createElement("span", { className: "lm-toast-msg" }, message),
      React.createElement("button", {
        type: "button",
        className: "lm-toast-close",
        onClick: onClose,
        title: "Dismiss"
      }, "✕")
    );
  }

  function StartIcon({ size = 14, className = "lm-btn-icon-svg" }) {
    return React.createElement("svg", {
      width: size,
      height: size,
      viewBox: "0 0 24 24",
      fill: "currentColor",
      className: className
    },
      React.createElement("polygon", { points: "6 3 20 12 6 21 6 3" })
    );
  }

  const PRODUCT_NAME = "Watchtower";
  const PATH = "/library-manager";

  function onboardingControlState(step, indexing, inventoryComplete) {
    return {
      canClose: !indexing,
      canGoBack: step > 0 && !indexing,
      canUseCompletedSteps: !indexing,
      canGoNext: !indexing && (step !== 4 || inventoryComplete)
    };
  }

  function formatInventoryProgress(progress) {
    const processed = Math.max(0, Number(progress?.processed || 0));
    const total = Math.max(0, Number(progress?.total || 0));
    if (!total) return `Indexing your library… ${progress?.detail || "Reading scenes from Stash"}. Please keep this setup window open.`;
    const percentage = Math.min(100, Math.round((processed / total) * 100));
    return `Indexing your library… ${processed.toLocaleString()} of ${total.toLocaleString()} files checked (${percentage}%).`;
  }

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
    let parsed = data.runPluginOperation;
    if (typeof parsed === "string") {
      try {
        parsed = JSON.parse(parsed);
      } catch {
        // Plain text message from backend, keep as string
      }
    }
    if (parsed?.error) throw new Error(parsed.error);
    // Raw Stash plugins may return either their payload directly or an {output: ...} envelope.
    let result = parsed && Object.prototype.hasOwnProperty.call(parsed, "output") ? parsed.output : parsed;
    if (typeof result === "string") {
      try {
        result = JSON.parse(result);
      } catch {
        // keep as string
      }
    }
    return result;
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

  function startMonitorAndRemember(operationFn, updateSettingFn) {
    return operationFn("ensure_monitor").then(result =>
      Promise.resolve(updateSettingFn("autoStartMonitor", true)).then(() => result));
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
    ["overview", "Overview"],
    ["monitor", "Filesystem Monitor"],
    ["incoming", "Incoming Downloads"],
    ["csm", "Contact Sheets (CSM)"],
    ["manage", "Filename Management"],
    ["activity", "Activity"],
    ["advanced", "Advanced Diagnostics"],
    ["help", "Help & Guide"]
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
  const datePositions = [["beginning", "Beginning (Recommended)"], ["end", "End"]];
  const performerCountLimits = [
    [0, "All tagged performers"],
    [1, "First 1 performer only"],
    [2, "First 2 performers only"],
    [3, "First 3 performers only"],
    [4, "First 4 performers only"],
    [5, "First 5 performers only"]
  ];
  const filenameSectionCharacters = { dash: " - ", comma: ", ", space: " ", underscore: "_" };
  const filenamePerformerCharacters = { comma: ", ", space: " ", dash: " - ", ampersand: " & " };
  const masterTitleSources = [
    ["stash_title", "Stash Title (Fallback to Filename)"],
    ["filename", "Original Filename Stem on Disk"],
    ["strict_title", "Stash Title Only (Skip Blank Titles)"]
  ];

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
    const [searchResults, setSearchResults] = React.useState([]);
    const [loading, setLoading] = React.useState(false);
    const [errorMsg, setErrorMsg] = React.useState("");
    const activeSceneIdRef = React.useRef(null);

    const runPreviewForSceneId = async (id, isLiveUpdate = false) => {
      if (!id) return;
      activeSceneIdRef.current = String(id);
      if (!isLiveUpdate) {
        setLoading(true);
        setErrorMsg("");
        setSearchResults([]);
      }
      try {
        const [gqlRes, backendRaw] = await Promise.all([
          (!previewResult || previewResult.sceneId !== String(id))
            ? gql(`query FetchPreviewScene($id: ID!) {
                findScene(id: $id) { id title studio { name } performers { name } files { path basename } paths { screenshot preview } }
              }`, { id })
            : Promise.resolve({ findScene: previewResult }),
          operation("preview_test_rename", { scene_id: id, config: config }).catch(err => ({ error: err.message }))
        ]);
        const scene = gqlRes?.findScene;
        if (!scene) throw new Error(`Scene ${id} not found in Stash`);
        
        let backendResult = typeof backendRaw === "string" ? JSON.parse(backendRaw) : backendRaw;
        const currentBasename = backendResult?.current_path?.split(/[\\/]/).pop() || scene.current || scene.files?.[0]?.basename || scene.files?.[0]?.path?.split(/[\\/]/).pop() || "unknown.mp4";
        const proposedBasename = backendResult?.proposed_path?.split(/[\\/]/).pop() || currentBasename;
        const status = backendResult?.status || (currentBasename === proposedBasename ? "unchanged" : "ready");
        const reason = backendResult?.reason || "";

        setPreviewResult(prev => ({
          sceneId: String(id),
          title: scene.title || prev?.title || "Untitled",
          studio: (typeof scene.studio === "string" ? scene.studio : scene.studio?.name) || prev?.studio || "None",
          performers: Array.isArray(scene.performers) ? (typeof scene.performers[0] === "string" ? scene.performers.join(", ") : scene.performers.map(p => p.name).join(", ")) : (prev?.performers || "None"),
          screenshot: scene.paths?.screenshot || scene.screenshot || prev?.screenshot || null,
          current: currentBasename,
          proposed: proposedBasename,
          status: status,
          reason: reason,
          matches: status === "unchanged" || currentBasename === proposedBasename,
          sidecars: backendResult?.associated_files || []
        }));
      } catch (e) {
        if (!isLiveUpdate) {
          setErrorMsg(e.message || String(e));
          setPreviewResult(null);
        }
      } finally {
        if (!isLiveUpdate) setLoading(false);
      }
    };

    // Real-time live update when naming or cleaning rules change
    React.useEffect(() => {
      if (activeSceneIdRef.current) {
        runPreviewForSceneId(activeSceneIdRef.current, true);
      }
    }, [
      config.filenameOrder,
      config.filenameSectionSeparator,
      config.filenamePerformerSeparator,
      config.maxPerformersInFilename,
      config.masterTitleSource,
      config.includeStudio,
      config.includePerformers,
      config.includeSceneDate,
      config.filenameDatePosition,
      config.cleanPerformerOnlyTitles,
      config.stripStudioFromTitle,
      config.stripPerformersFromTitle,
      config.stripConnectiveWords,
      config.collapseMultipleDashes
    ]);

    const handleSearchOrTest = async (queryText) => {
      const q = String(queryText !== undefined ? queryText : sceneIdInput).trim();
      if (!q) return;
      
      // If pure digits, directly test that scene ID
      if (/^\d+$/.test(q)) {
        setSceneIdInput(q);
        await runPreviewForSceneId(q);
        return;
      }

      // Otherwise perform a text search across scenes
      setLoading(true); setErrorMsg(""); setSearchResults([]);
      try {
        const res = await gql(`query SearchPreviewScenes($filter: FindFilterType) {
          findScenes(filter: $filter) {
            count
            scenes {
              id
              title
              studio { name }
              performers { name }
              paths { screenshot }
            }
          }
        }`, { filter: { q: q, per_page: 8 } });

        const scenes = res?.findScenes?.scenes || [];
        if (!scenes.length) {
          setErrorMsg(`No scenes found matching "${q}"`);
          setPreviewResult(null);
        } else if (scenes.length === 1) {
          setSceneIdInput(String(scenes[0].id));
          await runPreviewForSceneId(scenes[0].id);
        } else {
          setSearchResults(scenes);
        }
      } catch (err) {
        setErrorMsg(err.message || String(err));
      } finally {
        setLoading(false);
      }
    };

    const pickRecent = async () => {
      setLoading(true); setErrorMsg(""); setSearchResults([]);
      try {
        const res = await gql(`query RecentScenes {
          findScenes(filter: { per_page: 6, sort: "created_at", direction: DESC }) {
            scenes {
              id
              title
              studio { name }
              performers { name }
              paths { screenshot }
            }
          }
        }`);
        const scenes = res?.findScenes?.scenes || [];
        if (scenes.length) {
          setSearchResults(scenes);
        } else {
          setErrorMsg("No scenes found in library to pick from.");
        }
      } catch (err) {
        setErrorMsg(err.message || String(err));
      } finally {
        setLoading(false);
      }
    };

    return React.createElement("div", { className: "lm-real-scene-tester" },
      React.createElement("div", { className: "lm-real-scene-header" },
        React.createElement("div", { className: "lm-real-scene-title-row" },
          React.createElement("strong", null, "Test Rules with a Real Scene"),
          React.createElement("span", { className: "lm-real-scene-badge" }, "Safe Dry-Run — No files are modified")),
        React.createElement("small", null, "Enter a Scene ID, search by title/performer, or pick a recent scene to simulate renaming. No files are modified.")),
      React.createElement("div", { className: "lm-real-scene-inputs" },
        React.createElement("input", {
          type: "text",
          placeholder: "Scene ID or search by name / performer / studio…",
          value: sceneIdInput,
          onChange: e => { setSceneIdInput(e.target.value); setSearchResults([]); },
          onKeyDown: e => { if (e.key === "Enter") handleSearchOrTest(); }
        }),
        React.createElement(Button, { variant: "secondary", disabled: loading, onClick: () => handleSearchOrTest() },
          loading ? "Searching…" : "Test / Search"),
        React.createElement(Button, { variant: "secondary", disabled: loading, onClick: pickRecent },
          "Pick Recent Scene")),
      searchResults.length > 0 && React.createElement("div", { className: "lm-real-scene-search-results" },
        searchResults.map(s => React.createElement("button", {
          key: s.id,
          type: "button",
          className: "lm-real-scene-search-item",
          onClick: () => {
            setSceneIdInput(String(s.id));
            runPreviewForSceneId(s.id);
          }
        },
          s.paths?.screenshot
            ? React.createElement("img", { src: s.paths.screenshot, alt: "", className: "lm-real-scene-search-thumb" })
            : React.createElement("div", { className: "lm-real-scene-search-thumb", style: { display: "flex", alignItems: "center", justifyContent: "center", fontSize: "0.7rem", color: "#666" } }, `#${s.id}`),
          React.createElement("div", { className: "lm-real-scene-search-info" },
            React.createElement("span", { className: "lm-real-scene-search-title" }, s.title || `Scene ${s.id}`),
            React.createElement("span", { className: "lm-real-scene-search-sub" },
              `Scene #${s.id}${s.studio?.name ? ` • ${s.studio.name}` : ""}${s.performers?.length ? ` • ${s.performers.map(p => p.name).join(", ")}` : ""}`))
        ))),
      errorMsg && React.createElement("p", { className: "lm-real-scene-error" }, `! ${errorMsg}`),
      previewResult && React.createElement("div", { className: "lm-real-scene-result" },
        React.createElement("div", { className: "lm-real-scene-card" },
          previewResult.screenshot && React.createElement("a", {
            href: `/scenes/${previewResult.sceneId}`,
            target: "_blank",
            rel: "noopener noreferrer",
            className: "lm-real-scene-thumb-wrap",
            title: `Open Scene ${previewResult.sceneId} in Stash`
          },
            React.createElement("img", { src: previewResult.screenshot, alt: "", className: "lm-real-scene-thumb" }),
            React.createElement("span", { className: "lm-real-scene-id-badge" }, `Scene ${previewResult.sceneId} ↗`)),
          React.createElement("div", { className: "lm-real-scene-body" },
            React.createElement("div", { className: "lm-real-scene-meta" },
              React.createElement("span", null, React.createElement("b", null, "Title: "), previewResult.title),
              React.createElement("span", null, React.createElement("b", null, "Studio: "), previewResult.studio),
              React.createElement("span", null, React.createElement("b", null, "Performers: "), previewResult.performers)),
            React.createElement("div", { className: "lm-real-scene-diff" },
              React.createElement("div", { className: "lm-real-scene-diff-row" },
                React.createElement("span", { className: "lm-diff-label" }, "Current on disk:"),
                React.createElement("code", { className: "lm-diff-code" }, previewResult.current)),
              React.createElement("div", { className: "lm-real-scene-diff-row" },
                React.createElement("span", { className: "lm-diff-label" }, "Proposed filename:"),
                React.createElement("code", { className: `lm-diff-code ${previewResult.matches ? "matches" : "proposed"}` }, previewResult.proposed)),
              React.createElement("div", { style: { marginTop: "6px", display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" } },
                React.createElement("span", { className: `lm-badge ${previewResult.matches ? "ok" : (previewResult.status === "blocked" ? "error" : "warn")}` },
                  previewResult.matches ? "✓ Already matches current format (No rename needed)" : (previewResult.status === "blocked" ? `Blocked: ${previewResult.reason}` : "Would rename if automatic renaming enabled")),
                previewResult.sidecars && previewResult.sidecars.length > 0 && React.createElement("span", { className: "lm-badge muted", title: `${previewResult.sidecars.length} companion file(s) safely paired` },
                  `📁 ${previewResult.sidecars.length} companion(s) paired`
                )
              )
            )
          )
        )
      )
    );
  }


  function OnboardingBanner({ onStart }) {
    return React.createElement("div", { className: "lm-onboarding-banner" },
      React.createElement("div", { className: "lm-onboarding-banner-icon-wrap" },
        React.createElement("img", {
          src: "/plugin/librarymanager/assets/watchtower-icon.png",
          alt: "Watchtower",
          className: "lm-onboarding-banner-icon"
        })
      ),
      React.createElement("div", { className: "lm-onboarding-banner-text" },
        React.createElement("strong", null, "Welcome to Watchtower! Complete the initial library setup."),
        React.createElement("p", null, "Walk through 4 quick steps to verify storage drives, index your scene database, and preflight renaming safety rules.")
      ),
      React.createElement("div", { className: "lm-onboarding-banner-actions" },
        React.createElement("button", {
          type: "button",
          className: "btn btn-primary lm-onboarding-start-btn",
          onClick: onStart
        }, "🚀 Start Guided Setup (3 mins)"),
        React.createElement("span", { className: "lm-onboarding-required" }, "Required before Watchtower can operate")
      )
    );
  }

  function playWelcomeChime() {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      if (ctx.state === "suspended") {
        ctx.resume().catch(() => {});
      }
      const now = ctx.currentTime;
      // Soft, high-end harmonic chime: D5 (587.33Hz), A5 (880Hz), D6 (1174.66Hz)
      const freqs = [587.33, 880.0, 1174.66];
      freqs.forEach((freq, idx) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, now + idx * 0.07);
        gain.gain.setValueAtTime(0, now + idx * 0.07);
        gain.gain.linearRampToValueAtTime(0.035, now + idx * 0.07 + 0.035);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + idx * 0.07 + 1.2);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(now + idx * 0.07);
        osc.stop(now + idx * 0.07 + 1.25);
      });
    } catch (_) {}
  }

  function OnboardingWizardModal({ show, onHide, data, config, updateSetting, requestAutomaticRenaming, updateSettings, operation, refresh, onNavigateTab }) {
    React.useEffect(() => {
      if (show) {
        const chimeTimer = window.setTimeout(() => {
          playWelcomeChime();
        }, 150);
        return () => window.clearTimeout(chimeTimer);
      }
    }, [show]);

    // Lock background body/screen scrolling when modal is open
    React.useEffect(() => {
      if (!show) return;
      const prevBodyOverflow = document.body.style.overflow;
      const prevHtmlOverflow = document.documentElement.style.overflow;
      document.body.style.overflow = "hidden";
      document.documentElement.style.overflow = "hidden";
      return () => {
        document.body.style.overflow = prevBodyOverflow;
        document.documentElement.style.overflow = prevHtmlOverflow;
      };
    }, [show]);

    // step 0: Welcome, steps 1..6: Setup, step 7: Complete
    const [step, setStep] = React.useState(0);
    const [stepDirection, setStepDirection] = React.useState("forward");
    const [isExiting, setIsExiting] = React.useState(false);
    const [indexing, setIndexing] = React.useState(false);
    const [indexResult, setIndexResult] = React.useState(null);
    const [indexError, setIndexError] = React.useState("");
    const [indexProgress, setIndexProgress] = React.useState(null);

    if (!show) return null;

    const libraryRoots = data?.library_roots || [];
    const inventory = data?.inventory || {};
    const totalScenes = inventory?.stash_scene_count ?? inventory?.total_scenes ?? 0;
    const totalFiles = inventory?.stash_file_count ?? inventory?.total_files ?? 0;
    const availableRoots = libraryRoots.filter(root => {
      const path = typeof root === "string" ? root : root?.path;
      return Boolean(path) && (typeof root === "string" || root.exists === true);
    });
    const inventoryComplete = inventory?.status === "complete" && Boolean(inventory?.completed_at);
    const setupReady = availableRoots.length > 0 && inventoryComplete;
    const onboardingControls = onboardingControlState(step, indexing, inventoryComplete);

    // Incoming multi-folder handling in wizard
    const rawFolders = Array.isArray(config.incomingFolders)
      ? config.incomingFolders
      : (config.incomingFolder ? [config.incomingFolder] : [""]);
    const incomingFoldersList = rawFolders.length > 0 ? rawFolders : [""];
    const multiStatus = data?.incoming_folders || { folders: [], valid_count: 0, total_count: 0, all_valid: false };
    const statusFolders = multiStatus.folders || [];

    const goToStep = (nextStep) => {
      setStepDirection(nextStep >= step ? "forward" : "backward");
      setStep(nextStep);
    };

    const handleCloseGracefully = (targetTab = null) => {
      if (isExiting || !onboardingControls.canClose) return;
      setIsExiting(true);
      window.setTimeout(() => {
        onHide();
        setIsExiting(false);
        if (targetTab) onNavigateTab(targetTab);
      }, 220);
    };

    const handleUpdateFolder = (index, val) => {
      const next = [...incomingFoldersList];
      next[index] = val;
      updateSettings({ incomingFolders: next, incomingFolder: next[0] || "" });
    };

    const handleAddFolder = () => {
      if (incomingFoldersList.length >= 5) return;
      const next = [...incomingFoldersList, ""];
      updateSettings({ incomingFolders: next });
    };

    const handleRemoveFolder = (index) => {
      if (incomingFoldersList.length <= 1) {
        updateSettings({ incomingFolders: [""], incomingFolder: "" });
      } else {
        const next = incomingFoldersList.filter((_, idx) => idx !== index);
        const cleaned = next.map(f => (f || "").trim()).filter(Boolean);
        updateSettings({
          incomingFolders: next,
          incomingFolder: cleaned[0] || ""
        });
      }
    };

    const handleRunIndex = async () => {
      setIndexing(true);
      setIndexError("");
      setIndexResult(null);
      setIndexProgress({ status: "preparing", processed: 0, total: 0, detail: "Reading scenes from Stash" });
      let progressRequestActive = false;
      let progressPolling = true;
      const progressTimer = window.setInterval(async () => {
        if (!progressPolling || progressRequestActive) return;
        progressRequestActive = true;
        try {
          const progress = await operation("inventory_progress");
          if (progressPolling && progress && typeof progress === "object") setIndexProgress(progress);
        } catch {
          // The primary indexing operation remains authoritative if polling is unavailable.
        } finally {
          progressRequestActive = false;
        }
      }, 750);
      try {
        const res = await operation("build_inventory");
        setIndexResult(res);
        setIndexProgress({ status: "complete", processed: res.files || 0, total: res.files || 0, detail: "Baseline inventory complete" });
        await refresh();
      } catch (err) {
        setIndexError(err.message || "Failed to build inventory");
        setIndexProgress(previous => ({ ...(previous || {}), status: "failed", detail: err.message || "Failed to build inventory" }));
      } finally {
        progressPolling = false;
        window.clearInterval(progressTimer);
        setIndexing(false);
      }
    };

    const handleFinish = (targetTab = "overview") => {
      if (!setupReady) {
        setIndexError("Complete the initial inventory and make sure at least one Stash library folder is online before finishing setup.");
        goToStep(4);
        return;
      }
      handleCloseGracefully(targetTab);
      updateSetting("onboardingCompleted", true).catch(() => {});
      window.dispatchEvent(new CustomEvent("librarymanager:health-check"));
    };

    const stepsList = [
      { num: 1, label: "Overview" },
      { num: 2, label: "Storage Roots" },
      { num: 3, label: "Watched Folders" },
      { num: 4, label: "Index Database" },
      { num: 5, label: "Renaming" },
      { num: 6, label: "Contact Sheets" }
    ];

    const capabilities = [
      {
        icon: "🔍",
        title: "Live Library Monitor",
        short: "Continuously tracks files across all your Stash library folders.",
        details: "Watches your storage drives in real-time. When new files are added, modified, or removed, Watchtower catches them immediately without slow manual rescan sweeps.",
        side: "left"
      },
      {
        icon: "⚡",
        title: "Automatic Move Tracking",
        short: "Updates Stash instantly when files are organized outside Stash.",
        details: "Move or rename folders in Finder or Explorer without breaking Stash. Watchtower automatically updates scene paths, hashes, and histories in the Stash database.",
        side: "right"
      },
      {
        icon: "🛡️",
        title: "Artwork & Subtitle Sync",
        short: "Keeps covers, posters, and subtitles linked when files move.",
        details: "Protects companion files (.jpg, .png, .vtt, .srt). When a scene is renamed or relocated, all associated artwork and subtitles follow in lockstep.",
        side: "left"
      },
      {
        icon: "📥",
        title: "Watched Download Folders",
        short: "Safely imports finished videos from your download folders.",
        details: "Monitors temporary or completed download staging areas. Once a download finishes writing and settles, Watchtower asks Stash to scan and add it from its existing folder.",
        side: "right"
      },
      {
        icon: "🖼️",
        title: "Contact Sheet Previews",
        short: "Saves video storyboard sheets in video folders (not in Stash).",
        details: "Generates multi-frame storyboard sheets (.jpg) saved directly to disk in each video's folder. Because they are saved as companion files on your drive, they never clutter your Stash image database.",
        side: "left"
      },
      {
        icon: "🏷️",
        title: "Organized Filenames",
        badge: "BETA",
        short: "Standardizes filenames from Studio, Performers, and Title.",
        details: "Automatically formats filenames using clean, customizable templates whenever metadata changes in Stash — 100% non-destructive with collision protection.",
        side: "right"
      }
    ];

    const modalElement = React.createElement("div", {
      className: `lm-wizard-backdrop ${isExiting ? "lm-exiting" : ""}`,
      onWheel: (e) => { if (e.target === e.currentTarget) e.preventDefault(); }
    },
      React.createElement("div", {
        className: `lm-wizard-dialog ${isExiting ? "lm-exiting" : ""}`,
        role: "dialog",
        "aria-modal": "true",
        "aria-labelledby": "lm-wizard-title"
      },
        React.createElement("header", { className: "lm-wizard-header" },
          React.createElement("div", { style: { display: "flex", alignItems: "center", gap: "10px" } },
            React.createElement("img", {
              src: "/plugin/librarymanager/assets/watchtower-icon.png",
              alt: "",
              style: { width: "32px", height: "32px", borderRadius: "6px" }
            }),
            React.createElement("div", null,
              React.createElement("span", { className: "lm-wizard-badge" },
                step === 0 ? "WELCOME" : step === 7 ? "COMPLETE" : `STEP ${step} OF 6`),
              React.createElement("h2", { id: "lm-wizard-title" }, "Watchtower Guided Setup")
            )
          ),
          React.createElement("button", {
            type: "button",
            className: "lm-wizard-close-btn",
            disabled: !onboardingControls.canClose,
            "aria-disabled": indexing ? "true" : undefined,
            onClick: (e) => { e.preventDefault(); e.stopPropagation(); handleCloseGracefully(); },
            title: indexing ? "Please wait while Watchtower indexes your library" : "Close Setup Wizard"
          }, "✕")
        ),
        step > 0 && step < 7 && React.createElement("div", { className: "lm-wizard-stepper" },
          stepsList.map(s => {
            const isDone = step > s.num;
            const isCurrent = step === s.num;
            return React.createElement("div", {
              key: s.num,
              className: `lm-wizard-step-item ${isCurrent ? "current" : ""} ${isDone ? "done" : ""}`,
              onClick: () => { if (onboardingControls.canUseCompletedSteps && s.num < step) goToStep(s.num); },
              style: { cursor: onboardingControls.canUseCompletedSteps && s.num < step ? "pointer" : "default" }
            },
              React.createElement("div", { className: "lm-wizard-step-circle" }, isDone ? "✓" : s.num),
              React.createElement("span", { className: "lm-wizard-step-label" }, s.label)
            );
          })
        ),
        React.createElement("div", { className: "lm-wizard-body" },
          step === 0 && React.createElement("div", { className: `lm-wizard-pane lm-pane-${stepDirection}`, style: { textAlign: "center", padding: "1rem 0.5rem" } },
            React.createElement("img", {
              src: "/plugin/librarymanager/assets/watchtower-icon.png",
              alt: "Watchtower",
              style: { width: "72px", height: "72px", borderRadius: "14px", marginBottom: "1rem", boxShadow: "0 8px 24px rgba(0,0,0,0.4)" }
            }),
            React.createElement("h2", { style: { fontSize: "1.5rem", color: "#ffffff", marginBottom: "0.45rem" } }, "Welcome to Watchtower"),
            React.createElement("p", { className: "lm-wizard-desc", style: { maxWidth: "520px", margin: "0 auto 1.4rem auto" } },
              "Your intelligent library manager, companion file synchronizer, and live monitor for Stash."),
            React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: "0.65rem", textAlign: "left", maxWidth: "520px", margin: "0 auto 1.4rem auto" } },
              React.createElement("div", { className: "lm-wizard-feature-chip" },
                React.createElement("span", { style: { fontSize: "1.25rem" } }, "🛡️"),
                React.createElement("div", null,
                  React.createElement("strong", null, "Zero-Collision Safeguards"),
                  React.createElement("p", null, "Safely coordinates filenames, artwork, and subtitles with collision prevention.")
                )
              ),
              React.createElement("div", { className: "lm-wizard-feature-chip" },
                React.createElement("span", { style: { fontSize: "1.25rem" } }, "⚡"),
                React.createElement("div", null,
                  React.createElement("strong", null, "Automatic Move Tracking"),
                  React.createElement("p", null, "Tracks files moved or renamed in Finder or Explorer and syncs paths without losing tags or metadata.")
                )
              ),
              React.createElement("div", { className: "lm-wizard-feature-chip" },
                React.createElement("span", { style: { fontSize: "1.25rem" } }, "📥"),
                React.createElement("div", null,
                  React.createElement("strong", null, "Watched Download Folders"),
                  React.createElement("p", null, "Monitors download folders and automatically imports completed videos into Stash.")
                )
              )
            ),
            React.createElement("div", { style: { textAlign: "center", color: "var(--text-muted, #aab3c5)", fontSize: "0.85rem" } },
              "⏱️ Guided setup takes about 2 minutes."
            )
          ),
          step === 1 && React.createElement("div", { className: `lm-wizard-pane lm-pane-${stepDirection}` },
            React.createElement("h3", null, "1. What Watchtower Does for Your Stash Library"),
            React.createElement("p", { className: "lm-wizard-desc", style: { marginBottom: "12px" } },
              "Watchtower acts as an intelligent bridge between your physical drive storage and Stash:"),
            React.createElement("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px", marginBottom: "12px" } },
              capabilities.map((cap) =>
                React.createElement("div", {
                  key: cap.title,
                  className: "lm-wizard-feature-chip",
                  style: { padding: "0.65rem 0.8rem", position: "relative" }
                },
                  React.createElement("span", { style: { fontSize: "1.15rem", flexShrink: 0 } }, cap.icon),
                  React.createElement("div", { style: { flex: 1, minWidth: 0 } },
                    React.createElement("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "4px" } },
                      React.createElement("div", { style: { display: "flex", alignItems: "center", gap: "6px" } },
                        React.createElement("strong", { style: { fontSize: "0.86rem" } }, cap.title),
                        cap.badge && React.createElement("span", { className: "lm-badge-beta" }, cap.badge)
                      ),
                      React.createElement("span", {
                        className: "lm-chip-info-cue",
                        title: "Hover for details"
                      }, "ℹ️")
                    ),
                    React.createElement("p", { style: { fontSize: "0.78rem", margin: "2px 0 0 0" } }, cap.short)
                  ),
                  React.createElement("div", {
                    className: `lm-wizard-chip-tooltip ${cap.side === "right" ? "tooltip-right" : "tooltip-left"}`
                  },
                    React.createElement("strong", { style: { display: "block", color: "#39ff64", marginBottom: "4px", fontSize: "0.82rem" } }, `${cap.icon} ${cap.title}`),
                    React.createElement("span", null, cap.details)
                  )
                )
              )
            ),
            React.createElement("div", { className: "lm-wizard-callout" },
              "💡 Let's verify your storage directories and initialize baseline indexing in the next few quick steps.")
          ),
          step === 2 && React.createElement("div", { className: `lm-wizard-pane lm-pane-${stepDirection}` },
            React.createElement("h3", null, "2. Verify Library Storage Roots"),
            React.createElement("p", { className: "lm-wizard-desc" },
              "Watchtower uses your Stash-configured library directories to monitor files and coordinate safe disk operations."),
            React.createElement("div", { className: "lm-wizard-roots-list" },
              libraryRoots.length > 0
                ? libraryRoots.map((r, idx) => {
                    const rootPath = typeof r === "string" ? r : (r?.path || "");
                    const isOnline = typeof r === "object" && typeof r?.exists === "boolean"
                      ? r.exists
                      : !(data?.monitor?.unavailable_roots || []).includes(rootPath);
                    return React.createElement("div", { key: rootPath || idx, className: "lm-wizard-root-row" },
                      React.createElement("span", { style: { color: "#39ff64", fontWeight: "700", display: "inline-flex", alignItems: "center", gap: "4px" } }, "📁 Root:"),
                      React.createElement("span", { className: "lm-wizard-root-path", title: rootPath }, rootPath),
                      React.createElement("span", { className: `lm-badge ${isOnline ? "ok" : "err"}` }, isOnline ? "Online" : "Missing")
                    );
                  })
                : React.createElement("p", { style: { color: "#ffb52e" } }, "No library folders detected from Stash.")
            ),
            React.createElement("div", { className: "lm-wizard-callout" },
              "💡 Storage roots are read directly from Stash. To add or adjust library folders, go to Stash Settings → Library.")
          ),
          step === 3 && React.createElement("div", { className: `lm-wizard-pane lm-pane-${stepDirection}` },
            React.createElement("h3", null, "3. Watched Download Folders"),
            React.createElement("p", { className: "lm-wizard-desc" },
              "Watch up to 5 incoming download folders. When downloaded videos finish saving, Watchtower asks Stash to scan and add them from those folders."),
            React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" } },
              React.createElement("strong", null, `Watched Folders (${incomingFoldersList.length}/5)`),
              incomingFoldersList.length < 5 && React.createElement("button", {
                type: "button",
                className: "btn btn-secondary",
                style: { padding: "0.25rem 0.65rem", fontSize: "0.82rem" },
                onClick: handleAddFolder
              }, "+ Add Folder")
            ),
            React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: "8px", marginBottom: "12px" } },
              incomingFoldersList.map((folder, idx) => {
                const cleanFolder = (folder || "").trim().replace(/\/+$/, "");
                const stat = statusFolders.find(f => {
                  if (!f || !f.path) return false;
                  return f.path.trim().replace(/\/+$/, "") === cleanFolder;
                }) || statusFolders[idx] || {};
                const isConfigured = Boolean(cleanFolder);
                const isValid = Boolean(stat?.valid);
                const isMissing = isConfigured && stat && stat.exists === false;
                const isOutside = isConfigured && stat && stat.exists === true && !stat.valid;
                const statusLabel = !isConfigured ? "" : (isValid ? "✓ Valid" : (isOutside ? "⚠ Outside Root" : "⚠ Missing"));
                const statusReason = stat?.reason || (isValid ? "Folder is accessible and inside library root" : "Directory not found on disk");

                return React.createElement("div", {
                  key: idx,
                  style: { display: "flex", flexDirection: "column", gap: "4px" }
                },
                  React.createElement("div", {
                    style: { display: "flex", gap: "6px", alignItems: "center" }
                  },
                    React.createElement("input", {
                      type: "text",
                      placeholder: "/path/to/downloads",
                      value: folder,
                      style: {
                        flex: 1,
                        padding: "0.45rem 0.65rem",
                        background: "var(--input-bg, #101827)",
                        border: isConfigured ? (isValid ? "1px solid #2fa66d" : "1px solid #e09822") : "1px solid #52617c",
                        borderRadius: "0.35rem",
                        color: "inherit"
                      },
                      onChange: e => handleUpdateFolder(idx, e.target.value)
                    }),
                    isConfigured && React.createElement("span", {
                      className: `lm-incoming-status-pill ${isValid ? "ok" : "warn"}`,
                      style: { padding: "0.35rem 0.6rem", fontSize: "0.78rem", cursor: "help" },
                      title: statusReason
                    }, statusLabel),
                    incomingFoldersList.length > 1 && React.createElement("button", {
                      type: "button",
                      className: "btn btn-danger",
                      style: { width: "32px", height: "32px", padding: 0, display: "inline-flex", alignItems: "center", justifyContent: "center" },
                      onMouseDown: event => event.preventDefault(),
                      onClick: () => handleRemoveFolder(idx),
                      title: "Remove watched folder"
                    }, "✕")
                  ),
                  isConfigured && !isValid && React.createElement("div", {
                    style: { fontSize: "0.76rem", color: "#ffb52e", display: "flex", alignItems: "center", gap: "4px", paddingLeft: "2px" }
                  }, `⚠️ ${statusReason}`)
                );
              })
            ),
            React.createElement("div", { className: "lm-wizard-callout" },
              "💡 Completed videos stay in their incoming folders. Watchtower asks Stash to scan and add them there.")
          ),
          step === 4 && React.createElement("div", { className: `lm-wizard-pane lm-pane-${stepDirection}` },
            React.createElement("h3", null, "4. Index Library Database"),
            React.createElement("p", { className: "lm-wizard-desc" },
              "Watchtower records Stash scenes, file paths and available fingerprints in its local librarymanager.sqlite3 database for collision protection and diagnostics. This scan creates the baseline needed to initialize your library."),
            React.createElement("div", { className: "lm-wizard-index-box" },
              totalScenes > 0
                ? React.createElement("div", { style: { display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "6px", width: "100%" } },
                    React.createElement("div", { style: { fontSize: "2.4rem", color: "#39ff64", lineHeight: 1 } }, "✓"),
                    React.createElement("strong", { style: { fontSize: "1.1rem", color: "#ffffff" } }, `Database Indexed: ${totalScenes.toLocaleString()} Scenes Found`),
                    React.createElement("p", { style: { margin: "2px 0 16px 0", color: "var(--text-muted, #aab3c5)", fontSize: "0.85rem", maxWidth: "440px" } },
                      `Total files mapped: ${Number(totalFiles).toLocaleString()}. Last scanned: ${inventory.completed_at || "recently"}.`)
                  )
                : React.createElement("div", { style: { display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "6px", width: "100%" } },
                    React.createElement("div", { style: { fontSize: "2.4rem", color: "#ffb52e", lineHeight: 1 } }, "⚡"),
                    React.createElement("strong", { style: { fontSize: "1.1rem", color: "#ffffff" } }, "Database Ready to Index"),
                    React.createElement("p", { style: { margin: "2px 0 16px 0", color: "var(--text-muted, #aab3c5)", fontSize: "0.85rem", maxWidth: "440px" } },
                      "Click below to scan your collection and build your baseline index.")
                  ),
              React.createElement("div", { style: { display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "10px", width: "100%" } },
                React.createElement("button", {
                  type: "button",
                  className: "btn btn-primary",
                  style: { padding: "0.6rem 1.6rem", fontSize: "0.95rem", fontWeight: 700 },
                  disabled: indexing,
                  onClick: (e) => { e.preventDefault(); e.stopPropagation(); handleRunIndex(); }
                }, indexing ? "⚡ Indexing Collection…" : (indexError ? "↻ Try Again" : (totalScenes > 0 ? "🔄 Re-Index Database" : "⚡ Build Initial Inventory Now"))),
                indexing && React.createElement("span", {
                  role: "status",
                  "aria-live": "polite",
                  style: { color: "#39ff64", fontSize: "0.88rem", marginTop: "4px" }
                }, formatInventoryProgress(indexProgress)),
                indexing && indexProgress?.total > 0 && React.createElement("progress", {
                  className: "lm-wizard-index-progress",
                  max: Number(indexProgress.total),
                  value: Number(indexProgress.processed || 0),
                  "aria-label": "Library indexing progress"
                })
              ),
              indexResult && React.createElement("div", { className: "lm-wizard-index-result", style: { marginTop: "16px", maxWidth: "480px" } },
                React.createElement("span", { style: { color: "#39ff64", fontWeight: "700" } }, "✓ Indexing Complete: "),
                `Inventoried ${indexResult.scenes || 0} scenes and ${indexResult.files || 0} files into librarymanager.sqlite3.`
              ),
              indexError && React.createElement("div", { className: "lm-message error", style: { marginTop: "14px", maxWidth: "480px" } }, indexError)
            )
          ),
          step === 5 && React.createElement("div", { className: `lm-wizard-pane lm-pane-${stepDirection}` },
            React.createElement("div", { style: { display: "flex", alignItems: "center", gap: "8px", marginBottom: "0.45rem" } },
              React.createElement("h3", { style: { margin: 0 } }, "5. Automatic File Renaming"),
              React.createElement("span", { className: "lm-badge-beta" }, "BETA")
            ),
            React.createElement("p", { className: "lm-wizard-desc", style: { marginBottom: "12px" } },
              "Watchtower can automatically standardize video filenames to match metadata whenever scenes are updated in Stash. Sidecar files (artwork and subtitles) are always renamed in lockstep with collision protection."),
            React.createElement("div", {
              className: "lm-wizard-option-card",
              style: {
                marginBottom: "14px",
                padding: "0.75rem 0.95rem",
                borderColor: config.automaticRenaming === true ? "#2fa66d" : "rgba(140, 155, 185, 0.25)",
                background: config.automaticRenaming === true ? "rgba(47, 166, 109, 0.1)" : "rgba(0, 0, 0, 0.25)"
              }
            },
              React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "10px" } },
                React.createElement("div", null,
                  React.createElement("strong", { style: { fontSize: "0.95rem" } }, "Enable Automatic Renaming?"),
                  React.createElement("p", { style: { margin: "2px 0 0 0", color: "var(--text-muted, #aab3c5)", fontSize: "0.83rem" } },
                    config.automaticRenaming === true
                      ? "Active — Stash metadata edits will automatically standardize filenames on disk."
                      : "OFF by default — Your files remain untouched until you choose to activate it.")
                ),
                React.createElement("button", {
                  type: "button",
                  className: `btn ${config.automaticRenaming === true ? "btn-primary" : "btn-secondary"}`,
                  style: { minWidth: "88px", fontWeight: 700, padding: "0.38rem 0.8rem", fontSize: "0.84rem" },
                  onClick: (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    requestAutomaticRenaming(!(config.automaticRenaming === true));
                  }
                }, config.automaticRenaming === true ? "✓ Active" : "Disabled (Off)")
              )
            ),
            React.createElement("div", {
              className: "lm-wizard-callout",
              style: { background: "rgba(52, 86, 164, 0.15)", borderColor: "rgba(77, 113, 199, 0.4)" }
            },
              React.createElement("strong", { style: { color: "#79a2ff", display: "block", marginBottom: "4px" } }, "🏷️ Customize in Filename Management"),
              React.createElement("p", { style: { margin: 0, fontSize: "0.83rem", color: "#ccd6ea", lineHeight: 1.45 } },
                "All naming rules, separators, section ordering, multi-performer formatting, and the real-time simulation sandbox can be configured and tested safely in the Filename Management tab.")
            )
          ),
          step === 6 && React.createElement("div", { className: `lm-wizard-pane lm-pane-${stepDirection}` },
            React.createElement("h3", null, "6. Contact Sheet Previews (CSM)"),
            React.createElement("p", { className: "lm-wizard-desc", style: { marginBottom: "12px" } },
              "Watchtower can generate high-resolution video storyboard sheets and save them directly on disk in each video's folder (stored as companion files, not added to Stash's image library)."),
            React.createElement("div", {
              className: "lm-wizard-option-card",
              style: {
                marginBottom: "14px",
                padding: "0.75rem 0.95rem",
                borderColor: config.generateContactSheets === true ? "#2fa66d" : "rgba(140, 155, 185, 0.25)",
                background: config.generateContactSheets === true ? "rgba(47, 166, 109, 0.1)" : "rgba(0, 0, 0, 0.25)"
              }
            },
              React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "10px" } },
                React.createElement("div", null,
                  React.createElement("strong", { style: { fontSize: "0.95rem" } }, "Enable Automatic Contact Sheets?"),
                  React.createElement("p", { style: { margin: "2px 0 0 0", color: "var(--text-muted, #aab3c5)", fontSize: "0.83rem" } },
                    config.generateContactSheets === true
                      ? "Active — Contact sheets (.jpg) will be saved in each video's folder on disk for quick desktop previewing."
                      : "OFF by default — Generate contact sheets manually on demand, or enable whenever ready.")
                ),
                React.createElement("button", {
                  type: "button",
                  className: `btn ${config.generateContactSheets === true ? "btn-primary" : "btn-secondary"}`,
                  style: { minWidth: "88px", fontWeight: 700, padding: "0.38rem 0.8rem", fontSize: "0.84rem" },
                  onClick: (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    updateSetting("generateContactSheets", !(config.generateContactSheets === true));
                  }
                }, config.generateContactSheets === true ? "✓ Active" : "Disabled (Off)")
              )
            ),
            React.createElement("div", {
              className: "lm-wizard-callout",
              style: { background: "rgba(52, 86, 164, 0.15)", borderColor: "rgba(77, 113, 199, 0.4)" }
            },
              React.createElement("strong", { style: { color: "#79a2ff", display: "block", marginBottom: "4px" } }, "📁 Saved to Video Folders (Zero Stash Image Clutter)"),
              React.createElement("p", { style: { margin: 0, fontSize: "0.83rem", color: "#ccd6ea", lineHeight: 1.45 } },
                "Contact sheets are saved directly beside the video file on your drive so you can inspect scenes in Finder or Explorer. They are managed as companion files and are not imported into Stash's image database. Grid layouts (e.g. 4x4, 5x4) and banner styling can be customized in the Contact Sheets tab.")
            )
          ),
          step === 7 && React.createElement("div", { className: `lm-wizard-pane lm-pane-${stepDirection}`, style: { textAlign: "center", padding: "1.4rem 0.5rem" } },
            React.createElement("div", { style: { fontSize: "3.5rem", marginBottom: "0.4rem" } }, "🎉"),
            React.createElement("h3", { style: { fontSize: "1.45rem", color: "#39ff64" } }, "You're All Set!"),
            React.createElement("p", { className: "lm-wizard-desc", style: { maxWidth: "520px", margin: "0.4rem auto 1.4rem" } },
              setupReady
                ? `Required setup is complete. Automatic renaming is ${config.automaticRenaming === true ? "on" : "off"}, and automatic contact sheets are ${config.generateContactSheets === true ? "on" : "off"}.`
                : "Setup is not complete yet. An online Stash library folder and a completed initial inventory are required."),
            !setupReady && React.createElement("div", { className: "lm-message error" },
              "Return to Index Database and complete the initial inventory before finishing."),
            React.createElement("div", { style: { display: "flex", justifyContent: "center" } },
              React.createElement("button", {
                type: "button",
                className: "btn btn-secondary",
                onClick: () => handleFinish("help")
              }, "📖 Open Help & Reference Manual")
            )
          )
        ),
        React.createElement("footer", { className: "lm-wizard-footer" },
          step === 0 && React.createElement("button", {
            type: "button",
            className: "btn btn-primary",
            style: { marginLeft: "auto", padding: "0.55rem 1.4rem", fontSize: "0.95rem", fontWeight: 700, background: "#218657", borderColor: "#2da76f" },
            onClick: (e) => { e.preventDefault(); e.stopPropagation(); goToStep(1); }
          }, "🚀 Get Started ➔"),
          step > 0 && step < 7 && React.createElement("button", {
            type: "button",
            className: "btn btn-secondary",
            disabled: !onboardingControls.canGoBack,
            onClick: (e) => { e.preventDefault(); e.stopPropagation(); goToStep(step - 1); }
          }, step === 1 ? "⬅ Back to Welcome" : "⬅ Back"),
          step > 0 && step < 7 && React.createElement("button", {
            type: "button",
            className: "btn btn-primary",
            disabled: !onboardingControls.canGoNext,
            title: step === 4 && !inventoryComplete
              ? (indexing ? "Watchtower is indexing your library" : "Build the initial inventory before continuing")
              : undefined,
            style: { marginLeft: "auto" },
            onClick: (e) => { e.preventDefault(); e.stopPropagation(); goToStep(step + 1); }
          }, step === 6 ? "Review & Complete ➔" : "Next ➔"),
          step === 7 && React.createElement("button", {
            type: "button",
            className: "btn btn-primary",
            disabled: !setupReady,
            title: setupReady ? "Complete setup" : "Complete the initial inventory first",
            style: { marginLeft: "auto", background: "#218657", borderColor: "#2da76f", fontWeight: 700 },
            onClick: (e) => { e.preventDefault(); e.stopPropagation(); handleFinish("overview"); }
          }, "🚀 Finish & Go to Overview")
        )
      )
    );

    return (ReactDOM && typeof ReactDOM.createPortal === "function")
      ? ReactDOM.createPortal(modalElement, document.body)
      : (api?.ReactDOM && typeof api.ReactDOM.createPortal === "function")
        ? api.ReactDOM.createPortal(modalElement, document.body)
        : modalElement;
  }
  function AutomaticRenamingWarning({ show, onCancel, onConfirm }) {
    if (!show) return null;
    const warning = React.createElement("div", {
      className: "lm-confirm-backdrop",
      role: "presentation",
      onMouseDown: event => { if (event.target === event.currentTarget) onCancel(); }
    },
      React.createElement("div", {
        className: "lm-confirm-dialog",
        role: "dialog",
        "aria-modal": "true",
        "aria-labelledby": "lm-automatic-renaming-warning-title"
      },
        React.createElement("div", { className: "lm-confirm-header" },
          React.createElement("h3", { id: "lm-automatic-renaming-warning-title" }, "Beta Feature 🧪")),
        React.createElement("div", { className: "lm-confirm-body" },
          React.createElement("p", null,
            "Automatic Renaming is still in beta and changes filenames on disk. Missing metadata, long names, uncommon symbols or unusual metadata combinations may produce unexpected filenames."),
          React.createElement("p", null,
            "After enabling it, please monitor Watchtower’s activity and confirm that video and companion files are renamed as expected."),
          React.createElement("p", null,
            "Watchtower protects against common duplication, collision and formatting issues, but cannot anticipate every filename and filesystem combination.")),
        React.createElement("div", { className: "lm-confirm-actions" },
          React.createElement(Button, { variant: "secondary", onClick: onCancel }, "Cancel"),
          React.createElement(Button, { variant: "primary", onClick: onConfirm }, "I understand"))));
    return (ReactDOM && typeof ReactDOM.createPortal === "function")
      ? ReactDOM.createPortal(warning, document.body)
      : warning;
  }

  function dashboardIndicatorState(config, monitor, incoming, payload) {
    config = config || {};
    monitor = monitor || {};
    incoming = incoming || {};
    payload = payload || {};
    const stale = monitor.is_stale === true || monitor.state === "stale";
    const watcherRunning = monitor.state === "running" && !stale && monitor.pid_alive !== false;
    const watcherState = stale ? "STALE" : (watcherRunning ? "RUNNING" : "STOPPED");
    const autoStartState = config.autoStartMonitor === true ? "enabled" : "disabled";
    const activeIncoming = incoming.active || [];
    const failedIncoming = activeIncoming.filter(item => item.status === "failed").length;
    const filingAttentionIncoming = activeIncoming.filter(item =>
      item.status === "imported" &&
      isIncomingWorkVisible(item, config.autoFilingEnabled === true) &&
      item.has_pending_proposal !== true && item.needs_recovery !== true &&
      !String(item.filing_diagnostic || "").startsWith("Proposal ready:")).length;
    const unavailableRoots = (monitor.unavailable_roots || []).length;
    const attentionEvents = (payload.pending_events || []).filter(event => !event.processing_state).length;
    const groupedReconciliation = (payload.grouped_reconciliation || []).length;
    const filingRecovery = (payload.filing_proposals || []).filter(item => item.status === "needs_recovery").length;
    const alertCount = failedIncoming + filingAttentionIncoming + unavailableRoots + attentionEvents + groupedReconciliation + filingRecovery + (stale ? 1 : 0);
    return {
      watcher: {
        active: watcherRunning,
        state: watcherState,
        help: watcherRunning
          ? `Filesystem monitor process is running; automatic startup is ${autoStartState}`
          : (stale
              ? (monitor.stale_reason || "Filesystem monitor heartbeat is stale")
              : `Filesystem monitor process is stopped; automatic startup is ${autoStartState}`)
      },
      moveSync: {
        active: config.automaticMoveReconciliation === true,
        state: config.automaticMoveReconciliation === true ? "ON" : "OFF"
      },
      incoming: {
        active: config.automaticIncomingScan === true,
        state: config.automaticIncomingScan === true ? "ON" : "OFF"
      },
      alerts: {
        active: alertCount > 0,
        state: alertCount > 0 ? String(alertCount) : "CLEAR",
        count: alertCount
      }
    };
  }

  function isIncomingWorkVisible(item, automaticFilingEnabled) {
    if (!item) return false;
    const status = String(item.status || "");
    if (["downloading", "waiting", "scanning", "generating_sheet", "renaming", "pending_rename"].includes(status)) {
      return true;
    }
    if (status === "failed" || status === "unmatched") {
      return true;
    }
    if (status !== "imported" || item.filed === true) {
      return false;
    }
    if (item.needs_recovery === true || item.has_pending_proposal === true) {
      return true;
    }
    return automaticFilingEnabled === true && item.is_baseline !== true &&
      item.exists_on_disk !== false && item.is_in_incoming_folder !== false;
  }

  function backlogResultReason(result) {
    if (!result) return "Watchtower did not return a reason.";
    return result.diagnostic || result.error || result.message || "Watchtower did not return a reason.";
  }

  function backlogOutcomeLabel(outcome) {
    return ({
      no_identity_found: "No Identity Found",
      destination_not_found: "No Destination",
      ambiguous_match: "Ambiguous Match",
      duplicate_review: "Duplicate Existing File",
      ineligible: "Ineligible",
      already_filed: "Already Filed",
      errors: "Errors"
    })[outcome] || "Needs Review";
  }

  function navigateToNeedsAttention(setTab, setTerminalFilter) {
    setTerminalFilter("attention");
    setTab("overview");
    window.setTimeout(() => {
      const target = document.getElementById("lm-needs-attention");
      if (target && typeof target.scrollIntoView === "function") {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }, 0);
  }

  function Dashboard() {
    const [tab, setTab] = React.useState("overview");
    const [data, setData] = React.useState(null);
    const [showOnboardingWizard, setShowOnboardingWizard] = React.useState(false);
    const onboardingClosedForSession = React.useRef(false);
    const [config, setConfig] = React.useState({});
    const [busy, setBusy] = React.useState("");
    const [notice, setNotice] = React.useState("");
    const [error, setError] = React.useState("");
    const [search, setSearch] = React.useState("");
    const [showFilenamePreview, setShowFilenamePreview] = React.useState(false);
    const [showAutomaticRenamingWarning, setShowAutomaticRenamingWarning] = React.useState(false);
    const [reports, setReports] = React.useState(null);
    const [correction, setCorrection] = React.useState({ sceneId: "", filename: "", preview: null });
    const [testRenameResult, setTestRenameResult] = React.useState(null);
    const [clock, setClock] = React.useState(Date.now());
    const [expandedOverviewEvents, setExpandedOverviewEvents] = React.useState(() => new Set());
    const [terminalFilter, setTerminalFilter] = React.useState("all");
    const [sceneHover, setSceneHover] = React.useState(null);
    const sceneHoverCache = React.useRef(new Map());
    const sceneHoverTimer = React.useRef(null);
    const [helpSectionId, setHelpSectionId] = React.useState("getting-started");
    const [helpSearch, setHelpSearch] = React.useState("");
    const [filingOptions, setFilingOptions] = React.useState({});
    const [activeFilingMenuId, setActiveFilingMenuId] = React.useState(null);

    React.useEffect(() => {
      const handleGlobalClick = () => setActiveFilingMenuId(null);
      window.addEventListener("click", handleGlobalClick);
      return () => window.removeEventListener("click", handleGlobalClick);
    }, []);
    const [newMappingType, setNewMappingType] = React.useState("performer");
    const [newMappingName, setNewMappingName] = React.useState("");
    const [newMappingFolder, setNewMappingFolder] = React.useState("");
    const [showBacklogModal, setShowBacklogModal] = React.useState(false);
    const [backlogData, setBacklogData] = React.useState(null);
    const [loadingBacklog, setLoadingBacklog] = React.useState(false);
    const [backlogError, setBacklogError] = React.useState("");
    const [selectedBacklogPaths, setSelectedBacklogPaths] = React.useState(new Set());
    const [backlogTab, setBacklogTab] = React.useState("eligible");
    const [backlogSearch, setBacklogSearch] = React.useState("");
    const [showBacklogConfirm, setShowBacklogConfirm] = React.useState(false);
    const [isBacklogEvaluating, setIsBacklogEvaluating] = React.useState(false);
    const [backlogProgress, setBacklogProgress] = React.useState({ current: 0, total: 0, currentFile: "", tally: {} });
    const [backlogCompletedSummary, setBacklogCompletedSummary] = React.useState(null);
    const [duplicateCompanionChoices, setDuplicateCompanionChoices] = React.useState({});
    const backlogCancelRequested = React.useRef(false);
    const [useOriginalHeader, setUseOriginalHeader] = React.useState(() => {
      try {
        return window.localStorage.getItem("lm_header_original") === "true";
      } catch (_) {
        return false;
      }
    });

    React.useEffect(() => {
      if (!notice) return;
      const timer = window.setTimeout(() => setNotice(""), 3200);
      return () => window.clearTimeout(timer);
    }, [notice]);

    React.useEffect(() => {
      if (!error) return;
      const timer = window.setTimeout(() => setError(""), 6000);
      return () => window.clearTimeout(timer);
    }, [error]);

    const toggleHeaderArt = React.useCallback(() => {
      setUseOriginalHeader(prev => {
        const next = !prev;
        try {
          window.localStorage.setItem("lm_header_original", String(next));
        } catch (_) {}
        return next;
      });
    }, []);

    const refresh = React.useCallback(async (isUserClick = false) => {
      if (isUserClick === true) setBusy("refresh");
      setError("");
      try {
        const [raw, settings] = await Promise.all([operation("dashboard", { limit: 250 }), getConfig()]);
        const payload = typeof raw === "string" ? JSON.parse(raw) : raw;
        setData({ ...payload, _liveReceivedAt: Date.now() }); setConfig(settings);
        const hasCompletedInventory = payload?.inventory?.status === "complete" && Boolean(payload?.inventory?.completed_at) && ((payload?.inventory?.present_count || 0) > 0 || (payload?.inventory?.stash_file_count || 0) > 0);
        if (settings.onboardingCompleted !== true) {
          if (hasCompletedInventory) {
            updateSetting("onboardingCompleted", true).catch(() => {});
          } else if (!onboardingClosedForSession.current) {
            setShowOnboardingWizard(true);
          }
        }
        window.dispatchEvent(new CustomEvent("librarymanager:health-check"));
      } catch (e) { setError(e.message); }
      finally { if (isUserClick === true) setBusy(""); }
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

    const loadBacklog = React.useCallback(async () => {
      setLoadingBacklog(true);
      setBacklogError("");
      try {
        const raw = await operation("get_backlog_items");
        const payload = typeof raw === "string" ? JSON.parse(raw) : raw;
        setBacklogData(payload);
      } catch (err) {
        setBacklogError(err.message || "Failed to load backlog files.");
      } finally {
        setLoadingBacklog(false);
      }
    }, []);

    function openBacklogMetadataEditor(event, sceneId) {
      event?.stopPropagation?.();
      if (!sceneId) return;
      window.open(`/scenes/${sceneId}/edit`, "_blank", "noopener");
    }

    function openBacklogFastTag(event, sceneId) {
      event?.stopPropagation?.();
      if (!sceneId) return;
      const cardEl = event?.currentTarget?.closest?.(`[data-scene-id="${sceneId}"]`)
        || document.querySelector(`[data-scene-id="${sceneId}"]`);
      if (cardEl && window.FastTag) {
        cardEl.dispatchEvent(new MouseEvent("contextmenu", {
          bubbles: true,
          cancelable: true,
          clientX: event?.clientX || window.innerWidth / 2,
          clientY: event?.clientY || window.innerHeight / 3
        }));
        return;
      }
      window.open(`/scenes/${sceneId}/edit`, "_blank", "noopener");
    }

    async function handleReevaluateBacklogItem(item) {
      if (!item?.path) return;
      setBusy(`backlog_refresh:${item.path}`);
      setError("");
      try {
        const raw = await operation("evaluate_backlog_batch", {
          paths: [item.path],
          refresh_metadata: true
        });
        const result = typeof raw === "string" ? JSON.parse(raw) : raw;
        setBacklogCompletedSummary({
          total: 1,
          processed: 1,
          cancelled: false,
          tally: result?.tally || {},
          results: result?.results || []
        });
        await loadBacklog();
      } catch (err) {
        setError(err?.message || "Backlog re-evaluation failed.");
      } finally {
        setBusy("");
      }
    }

    async function handleInspectBacklogDuplicate(item, requestVerification) {
      if (!item?.path) return;
      setBusy(`duplicate_verify:${item.path}`);
      setError("");
      try {
        const raw = await operation("inspect_backlog_duplicate", {
          path: item.path,
          request_verification: requestVerification === true
        });
        const duplicateInfo = typeof raw === "string" ? JSON.parse(raw) : raw;
        setBacklogCompletedSummary(previous => previous ? ({
          ...previous,
          results: (previous.results || []).map(result =>
            result.path === item.path ? { ...result, duplicate_info: duplicateInfo } : result
          )
        }) : previous);
        setBacklogData(previous => previous ? ({
          ...previous,
          items: (previous.items || []).map(result =>
            result.path === item.path ? { ...result, duplicate_info: duplicateInfo } : result
          )
        }) : previous);
        setNotice(duplicateInfo?.checksum_status === "verified"
          ? "Exact duplicate verified. Review the paths before deleting the incoming copy."
          : duplicateInfo?.reason || "Checksum verification is pending.");
      } catch (err) {
        setError(err?.message || "Duplicate verification failed.");
      } finally {
        setBusy("");
      }
    }

    async function handleDeleteBacklogDuplicate(item) {
      const info = item?.duplicate_info;
      if (!item?.path || !info || info.checksum_status !== "verified") return;
      const includeCompanions = duplicateCompanionChoices[item.path] === true;
      const companions = includeCompanions ? (info.companions || []) : [];
      const companionText = companions.length
        ? `\n\nAlso permanently delete ${companions.length} selected companion file(s):\n${companions.map(entry => entry.path).join("\n")}`
        : "";
      const confirmed = window.confirm(
        `Permanently delete this verified duplicate from the incoming folder?\n\n${item.path}\n\nThe organised file will remain:\n${info.retained_path}${companionText}\n\nThis cannot be undone.`
      );
      if (!confirmed) return;
      setBusy(`duplicate_delete:${item.path}`);
      setError("");
      try {
        const raw = await operation("delete_backlog_duplicate", {
          path: item.path,
          scene_id: info.scene_id,
          file_id: info.candidate_file_id,
          retained_file_id: info.retained_file_id,
          sha256: info.sha256,
          companions
        });
        const result = typeof raw === "string" ? JSON.parse(raw) : raw;
        setNotice(result?.message || "Verified incoming duplicate deleted.");
        setDuplicateCompanionChoices(previous => ({ ...previous, [item.path]: false }));
        setBacklogCompletedSummary(null);
        await loadBacklog();
        refresh();
      } catch (err) {
        setError(err?.message || "Duplicate deletion failed.");
      } finally {
        setBusy("");
      }
    }

    const handleSelectAllEligible = React.useCallback(() => {
      if (!backlogData?.items) return;
      const eligible = backlogData.items.filter(i => i.eligible).map(i => i.path);
      setSelectedBacklogPaths(new Set(eligible));
    }, [backlogData]);

    const handleClearBacklogSelection = React.useCallback(() => {
      setSelectedBacklogPaths(new Set());
    }, []);

    const toggleBacklogItemSelection = React.useCallback((path) => {
      setSelectedBacklogPaths(prev => {
        const next = new Set(prev);
        if (next.has(path)) next.delete(path);
        else next.add(path);
        return next;
      });
    }, []);

    const startBacklogEvaluation = React.useCallback(async () => {
      setShowBacklogConfirm(false);
      if (!backlogData?.items || selectedBacklogPaths.size === 0) return;
      
      const eligibleList = backlogData.items.filter(i => i.eligible && selectedBacklogPaths.has(i.path));
      if (eligibleList.length === 0) return;

      setIsBacklogEvaluating(true);
      backlogCancelRequested.current = false;
      setBacklogCompletedSummary(null);

      const total = eligibleList.length;
      let evaluated = 0;
      const runningTally = {
        proposal_ready: 0,
        candidate_selection_required: 0,
        no_identity_found: 0,
        destination_not_found: 0,
        ambiguous_match: 0,
        already_filed: 0,
        duplicate_review: 0,
        ineligible: 0,
        errors: 0
      };
      const runningResults = [];

      setBacklogProgress({
        current: 0,
        total,
        currentFile: eligibleList[0].basename,
        tally: { ...runningTally }
      });

      const batchSize = 3;
      for (let i = 0; i < total; i += batchSize) {
        if (backlogCancelRequested.current) break;
        const batch = eligibleList.slice(i, i + batchSize);
        setBacklogProgress({
          current: evaluated,
          total,
          currentFile: batch.map(b => b.basename).join(", "),
          tally: { ...runningTally }
        });

        try {
          const raw = await operation("evaluate_backlog_batch", { paths: batch.map(b => b.path) });
          const res = typeof raw === "string" ? JSON.parse(raw) : raw;
          if (res?.tally) {
            for (const [k, v] of Object.entries(res.tally)) {
              runningTally[k] = (runningTally[k] || 0) + (v || 0);
            }
          }
          if (Array.isArray(res?.results)) runningResults.push(...res.results);
        } catch (err) {
          runningTally.errors = (runningTally.errors || 0) + batch.length;
          for (const item of batch) {
            runningResults.push({
              path: item.path,
              basename: item.basename,
              success: false,
              outcome: "errors",
              error: err?.message || "Backlog evaluation request failed."
            });
          }
        }

        evaluated += batch.length;
        setBacklogProgress({
          current: Math.min(evaluated, total),
          total,
          currentFile: batch.map(b => b.basename).join(", "),
          tally: { ...runningTally }
        });
      }

      setBacklogCompletedSummary({
        total,
        processed: evaluated,
        cancelled: backlogCancelRequested.current,
        tally: { ...runningTally },
        results: runningResults
      });
      setIsBacklogEvaluating(false);
      refresh();
      loadBacklog();
    }, [backlogData, selectedBacklogPaths, refresh, loadBacklog]);

    React.useEffect(() => { document.title = "Watchtower | Stash"; refresh(); }, [refresh]);
    React.useEffect(() => {
      if (tab === "advanced") loadReports().catch(error => setError(error.message));
    }, [tab, loadReports]);
    React.useEffect(() => {
      if (showBacklogModal) {
        loadBacklog();
      } else {
        setShowBacklogConfirm(false);
        setBacklogCompletedSummary(null);
      }
    }, [showBacklogModal, loadBacklog]);
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

    async function handleFactoryReset() {
      if (!window.confirm("Are you sure you want to reset all Watchtower settings to factory defaults? This will restore standard naming rules with Desktop Notifications ON. Your video files on disk and Stash records are completely safe and untouched.")) return;
      setBusy("reset"); setError("");
      try {
        const defaultSettings = {
          onboardingCompleted: false,
          automaticRenaming: false,
          masterTitleSource: "stash_title",
          includeStudio: true,
          includePerformers: true,
          includeSceneDate: false,
          filenameDatePosition: "beginning",
          cleanPerformerOnlyTitles: true,
          stripStudioFromTitle: true,
          stripPerformersFromTitle: true,
          stripConnectiveWords: true,
          collapseMultipleDashes: true,
          maxPerformersInFilename: 0,
          filenameOrder: "title,studio,performers",
          filenameSectionSeparator: "dash",
          filenamePerformerSeparator: "comma",
          renameSettleSeconds: 30,
          testSceneId: "",
          autoStartMonitor: true,
          startAtLogin: false,
          automaticMoveReconciliation: false,
          transcoderReplacementCompatibility: false,
          automaticIncomingScan: false,
          incomingFolder: "",
          incomingSettleMinutes: 5,
          generateContactSheets: false,
          refreshContactSheetsOnRename: true,
          contactSheetGrid: "5x4",
          contactSheetBanner: true,
          contactSheetAdjustVertical: true,
          contactSheetScript: "",
          allowCustomContactSheetScript: false,
          macNotifications: true,
          notifySuccessfulRenames: true
        };
        await saveConfig(defaultSettings);
        setConfig(defaultSettings);
        await refresh();
        setNotice("Watchtower has been reset to factory defaults with Desktop Notifications ON.");
      } catch (err) {
        setError(`Factory reset failed: ${err.message || err}`);
      } finally {
        setBusy("");
      }
    }

    async function handleCleanStash() {
      if (!window.confirm("Run Safe Auto-Clean? Any verified renamed files will be safely linked to their scenes first to preserve all metadata, then dead references will be pruned.")) return;
      setBusy("clean"); setError("");
      try {
        const verifiedPaths = [];
        for (const rep of (reports || [])) {
          for (const row of (rep.rows || [])) {
            if (row.confidence === "verified" && row.candidate_path && !verifiedPaths.includes(row.candidate_path)) {
              verifiedPaths.push(row.candidate_path);
            }
          }
        }
        if (verifiedPaths.length > 0) {
          setNotice(`Reconciling ${verifiedPaths.length} verified renamed file(s) first to preserve metadata...`);
          const scanRes = await gql(`mutation Scan($paths: [String!]) { metadataScan(input: { paths: $paths }) }`, { paths: verifiedPaths });
          if (scanRes?.metadataScan) {
            await waitForJob(scanRes.metadataScan);
          }
        }
        setNotice("Pruning orphaned records in Stash...");
        const data = await gql(`mutation Clean { metadataClean(input: { dryRun: false }) }`);
        if (data?.metadataClean) {
          await waitForJob(data.metadataClean);
        }
        setNotice("Updating Watchtower reports...");
        const invJob = await runTask(readOnlyTasks.inventory);
        if (invJob) await waitForJob(invJob);
        const findJob = await runTask(readOnlyTasks.find);
        if (findJob) await waitForJob(findJob);
        await loadReports();
        await refresh();
        setNotice("Resolution complete! Verified files were re-linked and dead records were cleanly pruned.");
      } catch (err) {
        setError(`Safe clean failed: ${err.message || err}`);
      } finally {
        setBusy("");
      }
    }

    async function handleReconcilePath(candidatePath) {
      if (!candidatePath) return;
      setBusy("reconcile-item"); setError("");
      try {
        setNotice(`Scanning ${candidatePath.split("/").pop()} in Stash...`);
        const data = await gql(`mutation Scan($paths: [String!]) { metadataScan(input: { paths: $paths }) }`, { paths: [candidatePath] });
        if (data?.metadataScan) {
          await waitForJob(data.metadataScan);
        }
        setNotice("Updating Watchtower reports...");
        const invJob = await runTask(readOnlyTasks.inventory);
        if (invJob) await waitForJob(invJob);
        const findJob = await runTask(readOnlyTasks.find);
        if (findJob) await waitForJob(findJob);
        await loadReports();
        await refresh();
        setNotice(`Successfully reconciled ${candidatePath.split("/").pop()} in Stash!`);
      } catch (err) {
        setError(`Reconciliation failed: ${err.message || err}`);
      } finally {
        setBusy("");
      }
    }

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
          setTab("advanced");
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

    async function updateSettings(changes, notifyMsg) {
      const next = { ...config, ...changes };
      setConfig(next); setError("");
      try {
        await saveConfig(next);
        await operation("record_config_change", { changes }).catch(() => {});
        if (Object.prototype.hasOwnProperty.call(changes, "startAtLogin")) {
          await operation("configure_startup", { enabled: next.startAtLogin === true });
        }
        // Dynamic in-memory hot-reload without restarting or stopping the watcher process
        if (data?.monitor?.state === "running") {
          await operation("reload_monitor");
        }
        if (notifyMsg) {
          setNotice(notifyMsg);
        }
        // Silent setting update to avoid distracting toasts while testing
        window.setTimeout(refresh, 500);
      }
      catch (e) { setError(e.message || String(e)); await refresh(); }
    }

    const updateSetting = (key, value, notifyMsg) => updateSettings({ [key]: value }, notifyMsg);

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

    async function resolveAllPendingEvents() {
      const count = monitor.pending_events || data?.pending_events?.length || 0;
      if (!count) return;
      if (!window.confirm(`Dismiss all ${count} pending filesystem change${count === 1 ? "" : "s"}?\n\nThis marks them as reviewed with no further action taken.`)) return;
      setBusy("review:all"); setError("");
      try {
        await operation("resolve_all_filesystem_events", { resolution: "dismiss" });
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

    async function executeGroupedReconciliation(batch) {
      const resuming = batch.state === "scanning" || batch.state === "verifying";
      const rechecking = batch.state === "partially_verified";
      if (!resuming && !rechecking && !window.confirm(
        `Reconcile this verified move through Stash?\n\nWatchtower will ask Stash to scan:\n${batch.destination_prefix || "Unknown destination"}\n\nStash may update its library according to its own scanner rules. Watchtower will resolve only files that retain the original scene ID and file ID at the exact expected path. No media files will be moved or deleted.`
      )) return;
      setBusy(`grouped-execute:${batch.id}`); setError("");
      try {
        const raw = await operation("execute_grouped_reconciliation", { batch_id: batch.id });
        const result = typeof raw === "string" ? JSON.parse(raw) : raw;
        const completedBatch = result?.batch || result || {};
        const state = completedBatch.state || result?.status || "review";
        const verifiedCount = Number(completedBatch.verified_count || 0);
        const trackedCount = Number(completedBatch.tracked_count || 0);
        const remainingCount = Math.max(0, trackedCount - verifiedCount);
        setNotice(state === "resolved"
          ? "Grouped move verified successfully in Stash."
          : `Grouped verification completed: ${verifiedCount} verified, ${remainingCount} still need review.`);
        await refresh();
      } catch (reviewError) {
        setError(`Could not reconcile grouped move: ${reviewError.message}`);
      } finally { setBusy(""); }
    }

    async function dismissGroupedReconciliation(batch) {
      if (!window.confirm(`Dismiss this grouped review?\n\n${batch.source_prefix || "Unknown source"}\n→ ${batch.destination_prefix || "Unknown destination"}\n\nNo files or Stash records will be changed.`)) return;
      setBusy(`grouped:${batch.id}`); setError("");
      try {
        await operation("dismiss_grouped_reconciliation", { batch_id: batch.id });
        setData(prev => prev ? {
          ...prev,
          grouped_reconciliation: (prev.grouped_reconciliation || []).filter(item => item.id !== batch.id)
        } : prev);
        await refresh();
      } catch (reviewError) {
        setError(`Could not dismiss grouped review: ${reviewError.message}`);
      } finally { setBusy(""); }
    }

    async function previewTestRename() {
      const sceneId = (config.testSceneId || "").trim();
      if (!sceneId) { setError("Enter a Test Scene ID in the field above first."); return; }
      setBusy("test_rename_preview"); setError(""); setTestRenameResult(null);
      try {
        const raw = await operation("preview_test_rename", { scene_id: sceneId });
        const result = typeof raw === "string" ? JSON.parse(raw) : raw;
        setTestRenameResult(result);
        if (result.status === "ready") {
          setNotice(`Preview generated for Scene ${sceneId}.`);
        } else if (result.status === "unchanged") {
          setNotice(`Scene ${sceneId} already matches configured filename.`);
        } else {
          setNotice(`Scene ${sceneId}: ${result.status} (${result.reason || ""})`);
        }
      } catch (e) { setError(e.message); }
      finally { setBusy(""); }
    }

    async function applyTestRename() {
      const sceneId = (config.testSceneId || "").trim();
      if (!sceneId) { setError("Enter a Test Scene ID in the field above first."); return; }
      if (!window.confirm(`Rename Scene ${sceneId} on disk now?\n\nThis will rename the video and its companion files.`)) return;
      setBusy("test_rename_apply"); setError("");
      try {
        const raw = await operation("apply_test_rename", { scene_id: sceneId });
        const result = typeof raw === "string" ? JSON.parse(raw) : raw;
        setTestRenameResult(result);
        if (result.status === "renamed" || result.status === "ready") {
          setNotice(`✓ Scene ${sceneId} renamed successfully on disk.`);
          await refresh();
        } else {
          setNotice(`Scene ${sceneId}: ${result.status} (${result.reason || ""})`);
        }
      } catch (e) { setError(e.message); }
      finally { setBusy(""); }
    }

    function requestAutomaticRenaming(enabled) {
      if (enabled !== true) {
        updateSetting("automaticRenaming", false);
        return;
      }
      if (config.automaticRenaming === true) return;
      setShowAutomaticRenamingWarning(true);
    }

    async function confirmAutomaticRenaming() {
      setShowAutomaticRenamingWarning(false);
      await updateSetting("automaticRenaming", true);
    }

    async function setAutomaticManagement(enabled) {
      if (enabled && config.testSceneId && !window.confirm(
        `Finish testing and manage future edits for all scenes? This removes the current Scene ${config.testSceneId} testing limit.`)) return;
      await updateSettings({ automaticRenaming: enabled, autoStartMonitor: enabled,
        automaticMoveReconciliation: enabled, ...(!enabled ? { startAtLogin: false } : {}),
        ...(enabled ? { testSceneId: "" } : {}) }, !enabled);
    }

    function TaskButton({ name, label, dangerous, variant, help, afterTab, showResults }) {
      const isTaskRunning = busy === `task:${name}` || busy === name || busy === "task";
      return React.createElement(Button, { variant: variant || (dangerous ? "danger" : "secondary"),
        disabled: isTaskRunning, onClick: () => task(name, dangerous, afterTab, showResults), title: help || label || name }, label || name);
    }

    function Switch({ setting, label, help, defaultValue = false }) {
      const isChecked = defaultValue ? config[setting] !== false : config[setting] === true;
      return React.createElement("div", { className: "lm-switch-row", title: help },
        React.createElement("input", {
          type: "checkbox",
          checked: isChecked,
          style: { cursor: "pointer" },
          onChange: e => {
            if (setting === "automaticRenaming") {
              requestAutomaticRenaming(e.target.checked);
            } else if (setting === "autoFilingEnabled") {
              updateSetting(setting, e.target.checked, e.target.checked ? "Automatic Filing proposals enabled." : "Automatic Filing proposals disabled.");
            } else if (setting === "autoFilingPreserveFilename") {
              updateSetting(setting, e.target.checked, e.target.checked ? "Filename preservation enabled." : "Filename preservation disabled.");
            } else {
              updateSetting(setting, e.target.checked);
            }
          }
        }),
        React.createElement("div", { className: "lm-switch-text" },
          React.createElement("strong", null, label),
          React.createElement("small", null, help)
        )
      );
    }

    const inventory = data?.inventory;
    const monitor = data?.monitor || {};
    const unavailableRoots = monitor.unavailable_roots || [];
    const isMonitorStale = monitor.is_stale === true || monitor.state === "stale";
    const watcherWorking = monitor.state === "running" && !isMonitorStale;
    const filenamePreview = data?.filename_preview;
    const incoming = data?.incoming || {};
    const allActive = incoming.active || [];
    const incomingFolder = data?.incoming_folder || {};
    const indicatorState = dashboardIndicatorState(config, monitor, incoming, data || {});
    const automaticManagement = config.automaticRenaming === true && config.autoStartMonitor === true &&
      config.automaticMoveReconciliation === true && !config.testSceneId;
    const filenameSectionCharacters = { dash: " - ", comma: ", ", space: " ", underscore: "_" };
    const filenamePerformerCharacters = { comma: ", ", space: " ", dash: " - ", ampersand: " & " };
    const performerLimit = Number(config.maxPerformersInFilename || 0);
    const samplePerformers = ["Performer One", "Performer Two"];
    const limitedPerformers = performerLimit > 0 ? samplePerformers.slice(0, performerLimit) : samplePerformers;
    const exampleParts = {
      title: "Example Scene",
      studio: config.includeStudio !== false ? "Example Studio" : "",
      performers: config.includePerformers !== false ? limitedPerformers.join(filenamePerformerCharacters[config.filenamePerformerSeparator] || ", ") : ""
    };
    const exampleMainParts = (config.filenameOrder || "title,studio,performers").split(",")
      .map(part => exampleParts[part]).filter(Boolean)
    const exampleDate = "2026-09-14";
    if (config.includeSceneDate === true) {
      config.filenameDatePosition === "end" ? exampleMainParts.push(exampleDate) : exampleMainParts.unshift(exampleDate);
    }
    const exampleFilename = exampleMainParts.join(filenameSectionCharacters[config.filenameSectionSeparator] || " - ") + ".mp4";
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
        { label: "Watcher", key: "monitorRuntime", active: indicatorState.watcher.active, state: indicatorState.watcher.state, help: indicatorState.watcher.help, tab: "monitor" },
        { label: "Move Sync", key: "automaticMoveReconciliation", active: indicatorState.moveSync.active, state: indicatorState.moveSync.state, help: "Automatically updates Stash when files are moved or renamed externally", tab: "monitor" },
        { label: "Auto-Rename", key: "automaticRenaming", active: config.automaticRenaming === true, state: config.testSceneId ? `TEST ${config.testSceneId}` : (config.automaticRenaming === true ? "ON" : "OFF"), help: config.testSceneId ? `Active (Limited to Test Scene ${config.testSceneId})` : "Automatically renames files when metadata is edited", tab: "manage" },
        { label: "Incoming", key: "automaticIncomingScan", active: indicatorState.incoming.active, state: indicatorState.incoming.state, help: "Watches incoming folder and adds completed downloads", tab: "incoming" },
        { label: "Clean Titles", key: "stripMetadataFromTitle", active: config.stripMetadataFromTitle !== false, state: config.stripMetadataFromTitle !== false ? "ON" : "OFF", help: "Removes duplicate studio/performers from generated filenames", tab: "manage" },
        { label: "Login Startup", key: "startAtLogin", active: config.startAtLogin === true, state: config.startAtLogin === true ? "ON" : "OFF", help: `Runs watcher in background on ${data?.startup?.platform_label || "OS"} login`, tab: "monitor" },
        { label: "Sheets", key: "generateContactSheets", active: config.generateContactSheets === true, state: config.generateContactSheets === true ? (config.contactSheetGrid || "ON") : "OFF", help: "Generates multi-frame contact sheets with CSM", tab: "csm" },
        { label: "Sheet Scope", key: "contactSheetScope", active: true, state: isIncomingScope ? "INCOMING" : "ALL", help: isIncomingScope ? "Contact sheets restricted to incoming folder" : "Contact sheets generated for entire library", tab: "csm" },
        { label: "Alerts", key: "activeAlerts", active: indicatorState.alerts.active, state: indicatorState.alerts.state, help: indicatorState.alerts.active ? `${indicatorState.alerts.count} item${indicatorState.alerts.count === 1 ? "" : "s"} need review` : "No active alerts", action: "attention" }
      ];

      return React.createElement("div", { className: "lm-switch-indicators", title: "Watchtower runtime and feature status" },
        switches.map(sw => React.createElement("button", {
          key: sw.key,
          type: "button",
          className: `lm-indicator-pill ${sw.active ? "active" : "inactive"}`,
          onClick: () => sw.action === "attention"
            ? navigateToNeedsAttention(setTab, setTerminalFilter)
            : setTab(sw.tab || "manage"),
          title: `${sw.label}: ${sw.state} — ${sw.help}. Click to ${sw.action === "attention" ? "review" : "configure"}.`
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
      if (key === "conflicts" || key === "conflict_count") return "Protected / Skipped";
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
      if (event.event_subtype === "ambiguous_duplicate_detected" || (event.duplicate_info && event.duplicate_info.is_ambiguous)) {
        const d = event.duplicate_info;
        const isVerified = d?.checksum_status === "verified";
        return {
          kind: "Ambiguous duplicate candidates",
          summary: `AMBIGUOUS DUPLICATE  ${sourceName}`,
          explanation: isVerified
            ? `Multiple checksum-matched Stash scenes (${d?.ambiguous_count || "multiple"} candidates) found. Manual review is required.`
            : `Multiple same-size Stash scenes (${d?.ambiguous_count || "multiple"} candidates) found, but checksum verification is incomplete. Manual review is required.`
        };
      }
      if (event.event_subtype === "duplicate_detected" || (event.duplicate_info && !event.duplicate_info.is_external_move)) {
        const d = event.duplicate_info;
        const isPending = d?.checksum_status === "pending";
        const isUnverified = d?.checksum_status === "unverified";
        return {
          kind: isPending ? "Duplicate candidate (verification pending)" : (isUnverified ? "Unverified duplicate candidate" : "Possible duplicate detected"),
          summary: `DUPLICATE  ${sourceName}`,
          explanation: isPending
            ? `Possible match with Scene #${d?.scene_id || ""}${d?.title ? ` (${d.title})` : ""}; calculating checksum in background…`
            : (isUnverified
                ? `Possible match with Scene #${d?.scene_id || ""}${d?.title ? ` (${d.title})` : ""}, but the source checksum is unavailable.`
                : `Identical file already exists in Stash as Scene #${d?.scene_id || ""}${d?.title ? ` (${d.title})` : ""}.`)
        };
      }
      if (event.event_subtype === "external_move_detected" || (event.duplicate_info && event.duplicate_info.is_external_move)) {
        const d = event.duplicate_info;
        const isPending = d?.checksum_status === "pending";
        const isUnverified = d?.checksum_status === "unverified";
        return {
          kind: isPending ? "External move candidate (verification pending)" : (isUnverified ? "Unverified external move candidate" : "External move detected"),
          summary: `EXTERNAL MOVE  ${sourceName}`,
          explanation: isPending
            ? `Possible match for missing Scene #${d?.scene_id || ""}${d?.title ? ` (${d.title})` : ""}; calculating checksum in background…`
            : (isUnverified
                ? `Possible match for missing Scene #${d?.scene_id || ""}${d?.title ? ` (${d.title})` : ""}, but no source checksum was recorded.`
                : `File matches missing Stash Scene #${d?.scene_id || ""}${d?.title ? ` (${d.title})` : ""}.`)
        };
      }
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
      let remaining = 0;
      if (item.settling_deadline != null) {
        remaining = Math.max(0, Math.round(Number(item.settling_deadline) - (Date.now() / 1000)));
      } else {
        const elapsed = Math.floor((clock - (data?._liveReceivedAt || clock)) / 1000);
        remaining = Math.max(0, Number(item.remaining_seconds || 0) - elapsed);
      }
      const minutes = String(Math.floor(remaining / 60)).padStart(2, "0");
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
      if (row.category === "companion") {
        if (row.action.includes("contact sheet updated") || row.action.includes("sheet updated"))
          return `SHEET UPDATED  ${basename(row.new_path || row.old_path)}`;
        if (row.action.includes("contact sheet") || row.action.includes("sheet"))
          return `SHEET GENERATED  ${basename(row.new_path || row.old_path)}`;
        if (row.action.includes("move") || row.action.includes("rename"))
          return `COMPANION MOVED  ${basename(row.new_path || row.old_path)}`;
        return `PAIRED  ${basename(row.new_path || row.old_path)}`;
      }
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

    async function handleRetryFiling(path) {
      setBusy(`retry_filing:${path}`); setError("");
      try {
        const raw = await operation("retry_filing_proposal", { path });
        const res = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (res && res.success) {
          setNotice(res.message || "Filing proposal generated successfully.");
          await refresh(true);
        } else {
          setNotice("");
          setError(res?.error || res?.message || res?.diagnostic || "Filing evaluation did not produce a proposal.");
          await refresh(true);
        }
      } catch (err) {
        setError(err.message || String(err));
        await refresh(true);
      } finally {
        setBusy("");
      }
    }

    async function handleProcessIncomingNow(path, displayName) {
      const name = displayName || basename(path);
      if (!window.confirm(`Process "${name}" now?\n\nOnly proceed if you know this download has completely finished. All standard safety and existence checks will still run.`)) {
        return;
      }
      setBusy(`process_incoming:${path}`); setError("");
      try {
        const raw = await operation("process_incoming_file_now", { path });
        const res = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (res && res.success) {
          setNotice(res.message || `Settling delay bypassed for ${name}. Processing initiated.`);
          await refresh();
        } else {
          setError(res?.error || "Failed to process incoming file now.");
          await refresh();
        }
      } catch (err) {
        setError(err.message || String(err));
        await refresh();
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



    async function handleApproveFiling(proposalId, updateMetadata = false, targetDest = null, targetEntityType = null, targetEntityId = null) {
      setBusy(`filing_${proposalId}`); setError("");
      try {
        const opts = filingOptions[proposalId] || {};
        const shouldUpdateMetadata = updateMetadata || Boolean(opts.updateMetadata);
        const selectedDestination = targetDest || opts.targetDest || undefined;
        const selectedEntityType = targetEntityType || opts.targetEntityType || undefined;
        const selectedEntityId = targetEntityId || opts.targetEntityId || undefined;
        const raw = await operation("approve_filing_proposal", {
          proposal_id: proposalId,
          update_metadata: shouldUpdateMetadata,
          target_destination_folder: selectedDestination,
          target_entity_type: selectedEntityType,
          target_entity_id: selectedEntityId
        });
        const res = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (res && res.status === "completed") {
          let msg = `✓ Video successfully moved to ${res.destination_folder || (res.proposed_path ? res.proposed_path.split("/").slice(-2).join("/") : "destination")}`;
          if (res.companions_moved > 0) msg += ` (${res.companions_moved} companion file${res.companions_moved === 1 ? "" : "s"} moved)`;
          if (res.metadata_updated) msg += " — metadata updated in Stash";
          else if (res.metadata_error) msg += ` (metadata note: ${res.metadata_error})`;
          setNotice(msg);
        } else if (res && res.status === "needs_recovery") {
          setError(`CRITICAL: Move incomplete / uncertain. Recovery required: ${res.reason || "Transaction halted safely"}`);
        } else if (res && (res.status === "blocked" || res.status === "failed" || res.reason)) {
          setError(`Filing failed: ${res.reason || "The operation was rejected by safety verification"}`);
        } else {
          setError("Filing approval returned an unexpected result.");
        }
        await refresh(true);
      } catch (err) {
        setError(err.message || String(err));
      } finally {
        setBusy("");
      }
    }

    async function handleIgnoreFiling(proposalId) {
      setBusy(`ignore_filing_${proposalId}`); setError("");
      try {
        await operation("ignore_filing_proposal", { proposal_id: proposalId });
        await refresh(true);
      } catch (err) {
        setError(err.message || String(err));
      } finally {
        setBusy("");
      }
    }

    async function handleRefreshFilingProposal(filePath, proposalId) {
      setBusy("refresh_prop_" + filePath); setError("");
      try {
        const raw = await operation("retry_filing_proposal", {
          path: filePath,
          proposal_id: proposalId,
          allow_refresh: true,
          allow_baseline: true
        });
        const res = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (res && res.success) {
          setNotice("Filing proposal destination choices refreshed successfully.");
          const newCandidates = res.proposal?.candidate_destinations || [];
          const currentSelected = filingOptions[proposalId]?.targetDest;
          if (currentSelected && !newCandidates.some(c => (c.destination_folder || c) === currentSelected)) {
            setFilingOptions(prev => ({
              ...prev,
              [proposalId]: { ...(prev[proposalId] || {}), targetDest: undefined }
            }));
          }
        } else {
          setError(`Failed to refresh proposal: ${res?.error || res?.diagnostic || "No matching destination found"}`);
        }
        await refresh(true);
      } catch (err) {
        setError(err.message || String(err));
      } finally {
        setBusy("");
      }
    }

    async function handleRecoverFiling(proposalId) {
      setBusy(`recover_filing_${proposalId}`); setError("");
      try {
        const raw = await operation("recover_filing_proposal", { proposal_id: proposalId });
        const res = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (res && res.recovered) {
          setNotice("Filing move recovered: video and companions safely restored to source folder");
        } else {
          setError(`Recovery attempt failed: ${res.reason || "Check file permissions"}`);
        }
        await refresh(true);
      } catch (err) {
        setError(err.message || String(err));
      } finally {
        setBusy("");
      }
    }

        function RetroStatus() {
      const activeIncoming = allActive.filter(item => {
        if (item.status === "downloading" || item.status === "scanning" || item.status === "generating_sheet" || item.status === "renaming" || item.status === "pending_rename") {
          return true;
        }
        if (item.status === "waiting") {
          const isImage = /\.(jpg|jpeg|png|webp)$/i.test(item.path);
          return !isImage;
        }
        return false;
      });
      const waitingAndScanning = activeIncoming;
      const hasActiveRename = activeIncoming.some(i => i.status === "pending_rename" || i.status === "renaming");
      const activeJobs = (data?.active_jobs || []).filter(job => {
        if (job.status !== "RUNNING" && job.status !== "QUEUED") return false;
        if (hasActiveRename && /rename/i.test(job.description)) return false;
        return true;
      });
      const failedIncoming = allActive.filter(item => item.status === "failed");
      const filingAttentionIncoming = allActive.filter(item =>
        item.status === "imported" &&
        isIncomingWorkVisible(item, config.autoFilingEnabled === true) &&
        item.has_pending_proposal !== true && item.needs_recovery !== true &&
        !String(item.filing_diagnostic || "").startsWith("Proposal ready:"));
      const transcoderCandidates = data?.transcoder_candidates || [];
      const unavailableRoots = monitor.unavailable_roots || [];
      const unresolved = data?.pending_events || [];
      const reconnectingMoves = unresolved.filter(e => e.processing_state === "reconnecting" || e.processing_state === "queued");
      const waitingMoves = unresolved.filter(e => e.processing_state === "waiting_video");
      const deferredMoves = unresolved.filter(e => e.processing_state === "deferred");
      const attentionEvents = unresolved.filter(e => !e.processing_state);
      const groupedReconciliation = data?.grouped_reconciliation || [];

      const filingProposals = data?.filing_proposals || [];
      const activeFilingTransfers = data?.active_filing_transfers || [];
      const activeTransferMap = {};
      for (const t of activeFilingTransfers) {
        if (t.proposal_id) activeTransferMap[t.proposal_id] = t;
      }
      const isAnyTransferActive = activeFilingTransfers.length > 0 || String(busy || "").startsWith("filing_");
      const pendingFilingProposals = filingProposals.filter(p => p.status !== "needs_recovery");
      const filingRecoveryProposals = filingProposals.filter(p => p.status === "needs_recovery");
      const filingRecoveryCount = filingRecoveryProposals.length;

      const inFlightCount = reconnectingMoves.length + waitingMoves.length + deferredMoves.length;
      const totalPending = monitor.pending_events != null ? monitor.pending_events : unresolved.length;
      const attentionCount = attentionEvents.length;

      const stream = (data?.activity || []).slice(0, 250);
      const isMonitorStale = monitor.is_stale === true || monitor.state === "stale";
      const watcherWorking = monitor.state === "running" && !isMonitorStale;

      const totalProblems = indicatorState.alerts.count;

      const problemsCount = stream.filter(r => r.severity === "error" || r.severity === "warning" || r.status === "failed" || r.status === "review").length;
      const addedCount = stream.filter(r => r.category === "incoming" && r.status === "imported").length;
      const renamedCount = stream.filter(r => r.category === "rename" && r.status === "renamed").length;

      let filteredStream = stream;
      if (terminalFilter === "problems") {
        filteredStream = stream.filter(r => r.severity === "error" || r.severity === "warning" || r.status === "failed" || r.status === "review");
      } else if (terminalFilter === "attention") {
        filteredStream = totalProblems === 0 ? [] : stream.filter(r => (r.severity === "error" || r.severity === "warning" || r.status === "failed" || r.status === "review"));
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
              onClick: () => refresh(true)
            },
              React.createElement(RestartIcon, { size: 14, className: "lm-refresh-icon", spinning: busy === "refresh" }),
              busy === "refresh" ? " REFRESHING…" : " REFRESH"
            ),
            totalProblems > 0 && React.createElement("span", { className: "lm-terminal-header-alert" }, `⚠️ ${totalProblems} PROBLEM${totalProblems === 1 ? "" : "S"}`),
            React.createElement("span", { className: watcherWorking ? "online" : "offline" }, watcherWorking ? "● LISTENING" : (isMonitorStale ? "● STALE" : "● STOPPED")))),

        (totalProblems > 0 || terminalFilter === "attention") && React.createElement("div", {
          id: "lm-needs-attention",
          className: "lm-terminal-attention-card"
        },
          React.createElement("div", { className: "lm-terminal-attention-header" },
            React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: ".65rem" } },
              React.createElement("span", { className: "lm-terminal-attention-tag" }, "⚠️ NEEDS ATTENTION"),
              React.createElement("span", { className: "lm-terminal-attention-count" }, totalProblems > 0
                ? `${totalProblems} item${totalProblems === 1 ? " requires" : "s require"} your action`
                : "No active alerts")),
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
              },
                React.createElement(RestartIcon, { size: 16 }),
                "RESTART WATCHER"),
              failedIncoming.length > 1 && React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn retry",
                disabled: !!busy,
                onClick: handleRetryAllIncoming
              }, React.createElement(React.Fragment, null, React.createElement(RestartIcon, { size: 13 }), ` RETRY ALL (${failedIncoming.length})`)),
              failedIncoming.length > 1 && React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn dismiss",
                disabled: !!busy,
                onClick: () => handleDismissAllIncoming(failedIncoming.length)
              }, `✕ DISMISS ALL (${failedIncoming.length})`),
              attentionCount > 1 && React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn dismiss",
                disabled: !!busy,
                onClick: () => resolveAllPendingEvents("dismiss")
              }, `✕ DISMISS ALL CHANGES (${attentionCount})`))),

          totalProblems === 0 && React.createElement("p", { className: "lm-terminal-empty" },
            "No items currently need attention. Watchtower is listening."),

          isMonitorStale && React.createElement("div", { className: "lm-terminal-attention-item warn", key: "stale-monitor" },
            React.createElement("div", { className: "lm-terminal-attention-title" },
              React.createElement("strong", null, "! WATCHER NOT RESPONDING"),
              React.createElement("span", { className: "lm-terminal-badge warn" }, "STALE")),
            React.createElement("p", { className: "lm-terminal-attention-detail" },
              React.createElement("b", null, "Reason: "),
              monitor.stale_reason || "The watcher stopped sending its expected heartbeat."),
            React.createElement("p", { className: "lm-terminal-attention-fix" },
              "→ Fix: Click RESTART WATCHER above. This restarts monitoring without changing your library or saved settings.")),

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

          filingAttentionIncoming.map(item => React.createElement("div", {
            className: "lm-terminal-attention-item warn",
            key: `filing-attention-${item.path}`
          },
            React.createElement("div", { className: "lm-terminal-attention-title" },
              React.createElement("strong", null, `! FILING NEEDS ATTENTION: ${basename(item.path)}`),
              React.createElement("span", { className: "lm-terminal-badge warn" }, "FILING UNRESOLVED")),
            React.createElement("p", { className: "lm-terminal-attention-detail" },
              React.createElement("b", null, "Reason: "),
              item.filing_diagnostic || "The video was added to Stash, but Watchtower could not create a filing proposal."),
            React.createElement("p", { className: "lm-terminal-attention-sub" },
              React.createElement("b", null, "Imported successfully: "), item.detail || item.path),
            React.createElement("p", { className: "lm-terminal-attention-fix" },
              "→ Fix: Correct the destination mapping or scene metadata if needed, then click RETRY FILING."),
            React.createElement("div", { className: "lm-terminal-actions" },
              React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn retry",
                disabled: !!busy,
                onClick: () => handleRetryFiling(item.path)
              }, busy === `retry_filing:${item.path}` ? "⟳ RETRYING…" : "⟳ RETRY FILING")))),

          unavailableRoots.map(path => React.createElement("div", { className: "lm-terminal-attention-item warn", key: path },
            React.createElement("div", { className: "lm-terminal-attention-title" },
              React.createElement("strong", null, `! LIBRARY FOLDER UNAVAILABLE: ${path}`),
              React.createElement("span", { className: "lm-terminal-badge warn" }, "DRIVE OFFLINE")),
            React.createElement("p", { className: "lm-terminal-attention-detail" },
              "Storage volume or network mount is disconnected. Check that the drive is plugged in or mounted."))),

          filingRecoveryProposals.map(prop => React.createElement("div", {
            className: "lm-terminal-attention-item filing-recovery",
            key: `filing-${prop.id}`
          },
            React.createElement("div", { className: "lm-terminal-attention-title" },
              React.createElement("strong", null, `⚠️ INCOMPLETE FILING MOVE: ${basename(prop.source_path)}`),
              React.createElement("span", {
                className: "lm-terminal-badge filing-recovery"
              }, "RECOVERY NEEDED")),
            React.createElement("p", { className: "lm-terminal-attention-detail" },
              prop.last_error || "Companion move failed and video rollback could not be verified."),
            React.createElement("p", { className: "lm-terminal-attention-sub" },
              React.createElement("b", null, "From: "), prop.source_path),
            React.createElement("p", { className: "lm-terminal-attention-sub" },
              React.createElement("b", null, "To: "), prop.proposed_path),
            React.createElement("div", { className: "lm-terminal-actions" },
              React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn filing-recovery",
                disabled: !!busy,
                onClick: () => handleRecoverFiling(prop.id)
              }, busy === `recover_filing_${prop.id}` ? "⟳ RECOVERING…" : "⟳ RECOVER TO SOURCE")
            )
          )),

          groupedReconciliation.map(batch => {
            const members = batch.members || [];
            const readyMembers = members.filter(member => member.state === "ready");
            const verifiedMembers = members.filter(member => member.state === "verified");
            const uncertainMembers = members.filter(member => member.state !== "ready" && member.state !== "verified");
            const isMoveGroup = batch.operation_type === "folder_move" || batch.operation_type === "bulk_move";
            const isResumable = batch.state === "scanning" || batch.state === "verifying";
            const isPartiallyVerified = batch.state === "partially_verified";
            const hasStashCleanGuidance = uncertainMembers.some(member =>
              String(member.reason || "").includes("run Stash Clean")
            );
            const renderGroupedMember = member => React.createElement("div", {
              className: `lm-grouped-member ${member.state === "verified" ? "ready" : "uncertain"}`,
              key: `grouped-${batch.id}-member-${member.id}`
            },
              React.createElement("div", null,
                React.createElement("a", {
                  href: `/scenes/${member.scene_id}`,
                  target: "_blank",
                  rel: "noopener noreferrer",
                  className: "lm-terminal-link"
                }, `Open Scene #${member.scene_id}`),
                ` • File #${member.file_id} • ${member.state.toUpperCase()}`),
              React.createElement("div", { className: "lm-grouped-path" }, `Old: ${member.old_path}`),
              React.createElement("div", { className: "lm-grouped-path" }, `Expected: ${member.expected_path || "No verified destination path"}`),
              React.createElement("div", { className: "lm-grouped-reason" }, member.reason || "No additional detail"));
            const operationLabel = ({
              folder_move: "FOLDER MOVED",
              bulk_move: "FILES MOVED",
              folder_copy: "FOLDER COPIED",
              bulk_copy: "FILES COPIED"
            })[batch.operation_type] || "GROUPED FILE CHANGE";
            return React.createElement("div", {
              className: "lm-terminal-attention-item grouped-reconciliation",
              key: `grouped-${batch.id}`
            },
              React.createElement("div", { className: "lm-terminal-attention-title" },
                React.createElement("strong", null, `📁 ${operationLabel}: ${batch.tracked_count} tracked video${batch.tracked_count === 1 ? "" : "s"}`),
                React.createElement("span", { className: `lm-terminal-badge ${uncertainMembers.length ? "warn" : "imported"}` },
                  isResumable ? "RESUME VERIFICATION" : (uncertainMembers.length ? `${uncertainMembers.length} NEED REVIEW` : "READY FOR REVIEW"))),
              React.createElement("p", { className: "lm-terminal-attention-sub" },
                React.createElement("b", null, "From: "), batch.source_prefix || "Unknown source"),
              React.createElement("p", { className: "lm-terminal-attention-sub" },
                React.createElement("b", null, "To: "), batch.destination_prefix || "Unknown destination"),
              React.createElement("p", { className: "lm-terminal-attention-detail" },
                isPartiallyVerified
                  ? `${verifiedMembers.length} of ${members.length} tracked file records were verified in Stash. ${uncertainMembers.length} ${uncertainMembers.length === 1 ? "record needs" : "records need"} manual review and will not be cleared automatically.`
                  : `${readyMembers.length} path${readyMembers.length === 1 ? "" : "s"} match the inferred folder mapping. ${uncertainMembers.length
                    ? `${uncertainMembers.length} item${uncertainMembers.length === 1 ? " remains" : "s remain"} uncertain and will not be reconciled automatically.`
                    : "All tracked paths are ready for a later verified reconciliation step."}`),
              isPartiallyVerified && React.createElement("div", { className: "lm-grouped-members" },
                React.createElement("strong", null, `Items requiring review (${uncertainMembers.length})`),
                React.createElement("div", { className: "lm-grouped-member-list" }, uncertainMembers.map(renderGroupedMember))),
              isPartiallyVerified && React.createElement("details", { className: "lm-grouped-members" },
                React.createElement("summary", null, `Verified successfully (${verifiedMembers.length})`),
                React.createElement("div", { className: "lm-grouped-member-list" }, verifiedMembers.map(renderGroupedMember))),
              !isPartiallyVerified && React.createElement("details", { className: "lm-grouped-members" },
                React.createElement("summary", null, `Review ${members.length} tracked item${members.length === 1 ? "" : "s"}`),
                React.createElement("div", { className: "lm-grouped-member-list" }, members.map(renderGroupedMember))),
              React.createElement("p", { className: "lm-terminal-attention-fix" },
                isPartiallyVerified
                  ? (hasStashCleanGuidance
                    ? "Recovery order: 1. Open the affected scene and confirm it plays from the new path. 2. In Stash, run Clean to remove the missing old attachment. 3. Return here and click Recheck After Stash Clean. Do not dismiss the group before rechecking."
                    : "Watchtower has not yet established why these records failed verification. Click Diagnose Remaining first. Do not run Stash Clean or dismiss the group yet.")
                  : isMoveGroup
                  ? "Approval asks Stash to scan the destination folder; Watchtower then verifies every original scene and file identity."
                  : "Copy groups are review-only and cannot trigger a Stash scan."),
              React.createElement("div", { className: "lm-terminal-actions" },
                isMoveGroup && React.createElement("button", {
                  type: "button",
                  className: "lm-terminal-btn retry",
                  disabled: !!busy,
                  onClick: () => executeGroupedReconciliation(batch)
                }, busy === `grouped-execute:${batch.id}`
                  ? "VERIFYING…"
                  : (isResumable ? "⟳ RESUME VERIFICATION" : (isPartiallyVerified
                    ? (hasStashCleanGuidance ? "⟳ RECHECK AFTER STASH CLEAN" : `⌕ DIAGNOSE ${uncertainMembers.length} REMAINING`)
                    : "✓ SCAN & VERIFY MOVE"))),
                React.createElement("button", {
                  type: "button",
                  className: "lm-terminal-btn dismiss",
                  disabled: !!busy || isResumable,
                  onClick: () => dismissGroupedReconciliation(batch)
                }, busy === `grouped:${batch.id}` ? "DISMISSING…" : "✕ DISMISS GROUP")));
          }),

          attentionEvents.map((event, idx) => {
            const info = pendingEventInfo(event);
            const deletion = event.event_type === "deleted" || String(event.destination_path || "").toLowerCase().endsWith(".delete");
            const isVideo = /\.(mp4|m4v|avi|mkv|mov|wmv|flv|webm)$/i.test(event.source_path || event.destination_path || "");
            const sourceName = basename(event.source_path);
            const isAmbiguous = event.event_subtype === "ambiguous_duplicate_detected" || (event.duplicate_info && event.duplicate_info.is_ambiguous);
            const isDuplicate = event.event_subtype === "duplicate_detected" || (event.duplicate_info && !event.duplicate_info.is_external_move && !event.duplicate_info.is_ambiguous);
            const isExternalMove = event.event_subtype === "external_move_detected" || (event.duplicate_info && event.duplicate_info.is_external_move && !event.duplicate_info.is_ambiguous);
            const dup = event.duplicate_info;

            if (isAmbiguous && dup) {
              const isPending = dup.checksum_status !== "verified";
              const isUnverified = dup.checksum_status === "unverified";
              return React.createElement("div", {
                className: "lm-terminal-attention-item warn",
                key: `pending-${event.last_seen_at}-${idx}`
              },
                React.createElement("div", { className: "lm-terminal-attention-title" },
                  React.createElement("strong", null, `⚠️ AMBIGUOUS DUPLICATE CANDIDATES: ${sourceName}`),
                  React.createElement("span", { className: "lm-terminal-badge warn" }, "AMBIGUOUS DUPLICATE")),
                React.createElement("p", { className: "lm-terminal-attention-detail" },
                  isUnverified
                    ? `Multiple Stash scenes (${dup.ambiguous_count} candidates) share this file size, but their source checksums are unavailable. Manual review is required.`
                    : (isPending
                        ? `Multiple Stash scenes (${dup.ambiguous_count} candidates) share this file size. Checksum verification is pending.`
                        : `Multiple matching Stash scenes (${dup.ambiguous_count} candidates) share this exact file size/checksum. Manual review is required.`)),
                React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "Candidate Scenes in Stash:")),
                (dup.all_candidates || []).map((c, cIdx) => React.createElement("p", {
                  className: "lm-terminal-attention-sub",
                  key: `cand-${cIdx}`,
                  style: { marginLeft: ".75rem" }
                },
                  React.createElement("a", {
                    href: `/scenes/${c.scene_id}`,
                    target: "_blank",
                    rel: "noopener noreferrer",
                    className: "lm-terminal-link"
                  }, `Scene #${c.scene_id}`),
                  ` (${c.title || basename(c.existing_path)}) — `,
                  React.createElement("span", { style: { color: "#92b89c" } }, c.existing_path)
                )),
                React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "Newly Detected Path: "), event.source_path),
                React.createElement("div", { className: "lm-terminal-actions" },
                  React.createElement("button", {
                    type: "button",
                    className: "lm-terminal-btn retry",
                    disabled: !!busy || isPending,
                    onClick: () => {
                      if (window.confirm("Keep this additional file?\n\nWatchtower will ask Stash to scan it. Stash may add it, associate it with an existing scene, or ignore it according to Stash's duplicate-handling rules.")) {
                        resolvePendingEvent(event, "keep_both");
                      }
                    }
                  }, "KEEP BOTH (ADD TO STASH)"),
                  React.createElement("button", {
                    type: "button",
                    className: "lm-terminal-btn dismiss",
                    disabled: !!busy,
                    onClick: () => resolvePendingEvent(event, "dismiss")
                  }, "✕ DISMISS"))
              );
            }

            if (isDuplicate && dup) {
              const isPending = dup.checksum_status === "pending";
              return React.createElement("div", {
                className: "lm-terminal-attention-item duplicate",
                key: `pending-${event.last_seen_at}-${idx}`
              },
                React.createElement("div", { className: "lm-terminal-attention-title" },
                  React.createElement("strong", null, isPending ? `⚠️ POSSIBLE DUPLICATE (VERIFYING): ${sourceName}` : `⚠️ POSSIBLE DUPLICATE: ${sourceName}`),
                  React.createElement("span", { className: "lm-terminal-badge duplicate" }, isPending ? "VERIFYING CHECKSUM" : "POSSIBLE DUPLICATE")),
                React.createElement("p", { className: "lm-terminal-attention-detail duplicate" },
                  isPending
                    ? `Possible match with Scene #${dup.scene_id}${dup.title ? ` (${dup.title})` : ""}; calculating checksum in background…`
                    : `Identical file already exists in Stash as Scene #${dup.scene_id}${dup.title ? ` (${dup.title})` : ""}.`),
                React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "Existing Stash Scene: "),
                  React.createElement("a", {
                    href: `/scenes/${dup.scene_id}`,
                    target: "_blank",
                    rel: "noopener noreferrer",
                    className: "lm-terminal-link"
                  }, `Scene #${dup.scene_id}${dup.title ? ` — ${dup.title}` : ""}`)),
                React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "Current Stash Path: "), dup.existing_path),
                React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "Newly Detected Path: "), event.source_path),
                React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "Verification: "),
                  isPending
                    ? React.createElement("span", { style: { color: "#ffb52e" } }, "⏳ Verification pending (calculating checksum…)")
                    : React.createElement("span", { style: { color: "#5bf" } }, "✓ Verified Identical (SHA-256 Match)")),
                dup.candidate_companions && dup.candidate_companions.length > 0 && React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "New Companions: "), dup.candidate_companions.join(", ")),
                React.createElement("div", { className: "lm-terminal-actions" },
                  React.createElement("a", {
                    href: `/scenes/${dup.scene_id}`,
                    target: "_blank",
                    rel: "noopener noreferrer",
                    className: "lm-terminal-btn view-scene",
                    style: { textDecoration: "none", display: "inline-flex", alignItems: "center" }
                  }, `👁 VIEW SCENE #${dup.scene_id}`),
                  React.createElement("button", {
                    type: "button",
                    className: "lm-terminal-btn retry",
                    disabled: !!busy || isPending,
                    onClick: () => {
                      if (window.confirm("Keep this additional file?\n\nWatchtower will ask Stash to scan it. Stash may add it, associate it with an existing scene, or ignore it according to Stash's duplicate-handling rules.")) {
                        resolvePendingEvent(event, "keep_both");
                      }
                    }
                  }, "KEEP BOTH (ADD TO STASH)"),
                  React.createElement("button", {
                    type: "button",
                    className: "lm-terminal-btn dismiss",
                    disabled: !!busy,
                    onClick: () => resolvePendingEvent(event, "dismiss")
                  }, "✕ DISMISS"))
              );
            }

            if (isExternalMove && dup) {
              const isPending = dup.checksum_status === "pending";
              const isUnverified = dup.checksum_status === "unverified";
              return React.createElement("div", {
                className: "lm-terminal-attention-item warn",
                key: `pending-${event.last_seen_at}-${idx}`
              },
                React.createElement("div", { className: "lm-terminal-attention-title" },
                  React.createElement("strong", null, isPending ? `⚠️ POSSIBLE EXTERNAL MOVE (VERIFYING): ${sourceName}` : (isUnverified ? `⚠️ UNVERIFIED EXTERNAL MOVE: ${sourceName}` : `⚠️ POSSIBLE EXTERNAL MOVE: ${sourceName}`)),
                  React.createElement("span", { className: "lm-terminal-badge warn" }, isPending ? "VERIFYING MOVE" : (isUnverified ? "UNVERIFIED" : "EXTERNAL MOVE"))),
                React.createElement("p", { className: "lm-terminal-attention-detail" },
                  isPending
                    ? `Possible match for missing Scene #${dup.scene_id}${dup.title ? ` (${dup.title})` : ""}; calculating checksum in background…`
                    : (isUnverified
                        ? `Possible size match for missing Scene #${dup.scene_id}${dup.title ? ` (${dup.title})` : ""}, but Watchtower has no recorded source checksum. The candidate was not hashed automatically.`
                        : `File matches missing Stash Scene #${dup.scene_id}${dup.title ? ` (${dup.title})` : ""}.`)),
                React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "Existing Stash Scene: "),
                  React.createElement("a", {
                    href: `/scenes/${dup.scene_id}`,
                    target: "_blank",
                    rel: "noopener noreferrer",
                    className: "lm-terminal-link"
                  }, `Scene #${dup.scene_id}${dup.title ? ` — ${dup.title}` : ""}`)),
                React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "Old Path (Missing): "), dup.existing_path),
                React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "New Path: "), event.source_path),
                React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "Verification: "),
                  isPending
                    ? React.createElement("span", { style: { color: "#ffb52e" } }, "⏳ Verification pending (calculating checksum…)")
                    : (isUnverified
                        ? React.createElement("span", { style: { color: "#ffb52e" } }, "Source checksum unavailable — manual review required")
                        : React.createElement("span", { style: { color: "#5bf" } }, "✓ Verified Identical (SHA-256 Match)"))),
                dup.candidate_companions && dup.candidate_companions.length > 0 && React.createElement("p", { className: "lm-terminal-attention-sub" },
                  React.createElement("b", null, "New Companions: "), dup.candidate_companions.join(", ")),
                React.createElement("div", { className: "lm-terminal-actions" },
                  React.createElement("a", {
                    href: `/scenes/${dup.scene_id}`,
                    target: "_blank",
                    rel: "noopener noreferrer",
                    className: "lm-terminal-btn view-scene",
                    style: { textDecoration: "none", display: "inline-flex", alignItems: "center" }
                  }, `👁 VIEW SCENE #${dup.scene_id}`),
                  React.createElement("span", { className: "lm-terminal-attention-sub" },
                    "Review only — Watchtower will not scan or relink this file automatically."),
                  React.createElement("button", {
                    type: "button",
                    className: "lm-terminal-btn dismiss",
                    disabled: !!busy,
                    onClick: () => resolvePendingEvent(event, "dismiss")
                  }, "✕ DISMISS"))
              );
            }

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

        pendingFilingProposals.length > 0 && React.createElement("div", { className: "lm-terminal-filing-card" },
          React.createElement("div", { className: "lm-terminal-filing-header" },
            React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: ".65rem" } },
              React.createElement("span", { className: "lm-terminal-filing-tag" }, "📁 FILING PROPOSALS"),
              React.createElement("span", { className: "lm-terminal-filing-count" },
                `${pendingFilingProposals.length} proposal${pendingFilingProposals.length === 1 ? "" : "s"} ready for review`))),
          pendingFilingProposals.map(prop => {
            const opts = filingOptions[prop.id] || {};
            const rawCandidates = prop.candidate_destinations || [];
            const candidates = rawCandidates.map(c => {
              if (typeof c === "string") {
                return { destination_folder: c, label: c, entity_type: prop.organize_by || "performer", entity_name: prop.matched_entity_name };
              }
              return c;
            });
            const hasMultiple = candidates.length > 1;
            const selectedTarget = opts.targetDest || (hasMultiple ? "" : (prop.destination_folder || (candidates[0] ? candidates[0].destination_folder : "")));
            const selectedCandidate = candidates.find(c => c.destination_folder === selectedTarget) || (hasMultiple ? null : candidates[0]);

            const singleDestPath = prop.destination_folder || (candidates[0] ? candidates[0].destination_folder : "");
            const singleLabel = candidates[0]?.label || `[${(candidates[0]?.entity_type || "DEST").toUpperCase()}] ${candidates[0]?.entity_name || prop.matched_entity_name || "Destination"} → ${singleDestPath}`;

            const destDisplay = hasMultiple
              ? React.createElement("div", {
                  className: "lm-filing-candidate-picker multiple",
                  style: { marginTop: "10px", padding: "10px 14px", background: "rgba(0,0,0,0.28)", borderRadius: "6px", border: "1px solid rgba(56, 189, 248, 0.4)" }
                },
                  React.createElement("div", {
                    style: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "8px", fontSize: "0.85rem", fontWeight: "600", color: "#38bdf8" }
                  },
                    React.createElement("span", null, `⚠️ Multiple Destinations Detected (${candidates.length}) — Select Destination:`),
                    React.createElement("span", {
                      className: "lm-terminal-badge",
                      style: {
                        background: selectedTarget ? "rgba(56, 189, 248, 0.15)" : "rgba(245, 158, 11, 0.15)",
                        color: selectedTarget ? "#38bdf8" : "#f59e0b",
                        border: `1px solid ${selectedTarget ? "rgba(56, 189, 248, 0.3)" : "rgba(245, 158, 11, 0.3)"}`,
                        fontSize: "0.72rem",
                        padding: "2px 8px"
                      }
                    }, selectedTarget ? "CHOICE SELECTED" : "SELECTION REQUIRED")
                  ),
                  React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: "6px" } },
                    candidates.map(c => {
                      const isSelected = selectedTarget === c.destination_folder;
                      const displayLabel = c.label || `[${(c.entity_type || "DEST").toUpperCase()}] ${c.entity_name ? `${c.entity_name} → ` : ""}${c.destination_folder}`;
                      return React.createElement("label", {
                        key: c.destination_folder,
                        style: {
                          display: "flex",
                          alignItems: "flex-start",
                          gap: "10px",
                          padding: "8px 12px",
                          background: isSelected ? "rgba(56, 189, 248, 0.15)" : "rgba(255,255,255,0.03)",
                          border: `1px solid ${isSelected ? "#38bdf8" : "rgba(255,255,255,0.08)"}`,
                          borderRadius: "5px",
                          cursor: "pointer",
                          fontSize: "0.83rem",
                          wordBreak: "break-all",
                          overflowWrap: "anywhere"
                        }
                      },
                        React.createElement("input", {
                          type: "radio",
                          name: `dest_choice_${prop.id}`,
                          value: c.destination_folder,
                          checked: isSelected,
                          onChange: () => setFilingOptions(prev => ({ ...prev, [prop.id]: { ...(prev[prop.id] || {}), targetDest: c.destination_folder } })),
                          style: { marginTop: "3px" }
                        }),
                        React.createElement("div", { style: { flex: 1, minWidth: 0 } },
                          React.createElement("span", { style: { fontWeight: "600", color: isSelected ? "#38bdf8" : "#f1f5f9" } }, displayLabel),
                          React.createElement("span", {
                            style: {
                              display: "block",
                              marginTop: "2px",
                              fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                              fontSize: "0.78rem",
                              color: isSelected ? "#bae6fd" : "#94a3b8",
                              wordBreak: "break-all",
                              overflowWrap: "anywhere"
                            }
                          }, c.destination_folder)
                        )
                      );
                    })
                  )
                )
              : React.createElement("div", {
                  className: "lm-filing-candidate-picker",
                  style: { marginTop: "10px", padding: "10px 14px", background: "rgba(0,0,0,0.28)", borderRadius: "6px", border: "1px solid rgba(56, 189, 248, 0.25)" }
                },
                  React.createElement("div", {
                    style: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "8px", fontSize: "0.85rem", fontWeight: "600", color: "#38bdf8" }
                  },
                    React.createElement("span", null, "📁 Destination Folder:"),
                    React.createElement("span", {
                      className: "lm-terminal-badge",
                      style: { background: "rgba(56, 189, 248, 0.15)", color: "#38bdf8", border: "1px solid rgba(56, 189, 248, 0.3)", fontSize: "0.72rem", padding: "2px 8px" }
                    }, "SELECTED DESTINATION")
                  ),
                  React.createElement("div", {
                    style: {
                      display: "flex",
                      alignItems: "flex-start",
                      gap: "10px",
                      padding: "8px 12px",
                      background: "rgba(56, 189, 248, 0.10)",
                      border: "1px solid rgba(56, 189, 248, 0.35)",
                      borderRadius: "5px",
                      fontSize: "0.83rem",
                      wordBreak: "break-all",
                      overflowWrap: "anywhere"
                    }
                  },
                    React.createElement("div", { style: { flex: 1, minWidth: 0 } },
                      React.createElement("span", { style: { fontWeight: "600", color: "#38bdf8" } }, singleLabel),
                      React.createElement("span", {
                        style: {
                          display: "block",
                          marginTop: "2px",
                          fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                          fontSize: "0.78rem",
                          color: "#bae6fd",
                          wordBreak: "break-all",
                          overflowWrap: "anywhere"
                        }
                      }, singleDestPath || "(Destination folder will be resolved upon approval)")
                    )
                  )
                );

            const activeTransfer = activeTransferMap[prop.id];
            const isCurrentTransferring = Boolean(activeTransfer) || busy === `filing_${prop.id}`;
            const isAnotherTransferActive = isAnyTransferActive && !isCurrentTransferring;
            const isApproveDisabled = Boolean(busy) || isAnyTransferActive || (hasMultiple && !selectedTarget);

            let approveButtonLabel = "✓ APPROVE & MOVE";
            if (isCurrentTransferring) {
              approveButtonLabel = "⟳ IN PROGRESS…";
            } else if (isAnotherTransferActive) {
              approveButtonLabel = "⚠️ TRANSFER IN PROGRESS";
            } else if (hasMultiple && !selectedTarget) {
              approveButtonLabel = "⚠️ SELECT DESTINATION FIRST";
            }

            const isDualMatch = Boolean(selectedCandidate?.matched_entities && selectedCandidate.matched_entities.length > 1);
            const currentTagEntity = selectedCandidate?.entity_type || prop.organize_by || "performer";
            const currentEntityName = selectedCandidate?.entity_name || prop.matched_entity_name;

            const progressPanel = isCurrentTransferring ? React.createElement("div", {
              className: "lm-filing-progress-panel"
            },
              React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "6px" } },
                React.createElement("span", { style: { fontWeight: "600", fontSize: "0.85rem", color: "#38bdf8", display: "flex", alignItems: "center", gap: "6px" } },
                  React.createElement("span", { className: "lm-filing-spinner" }, "⟳"),
                  activeTransfer?.stage_label || "Filing in progress…"
                ),
                (activeTransfer?.total_bytes > 0 || prop.file_size > 0) ? React.createElement("span", { style: { fontSize: "0.78rem", color: "#94a3b8" } }, formatBytes(activeTransfer?.total_bytes || prop.file_size)) : null
              ),
              React.createElement("div", { className: "lm-filing-progress-track" },
                React.createElement("div", { className: "lm-filing-progress-bar-indeterminate" })
              ),
              React.createElement("div", { style: { fontSize: "0.78rem", color: "#bae6fd", marginTop: "6px" } },
                activeTransfer?.detail || "Moving video and companion files to destination folder..."
              )
            ) : null;

            return React.createElement("div", {
              className: "lm-terminal-attention-item filing",
              key: `filing-${prop.id}`
            },
              React.createElement("div", {
                className: "lm-terminal-attention-title scene-card",
                "data-scene-id": prop.scene_id || "",
                style: { display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "8px" }
              },
                React.createElement("div", { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
                  React.createElement("strong", null,
                    "📁 FILING PROPOSAL: ",
                    prop.scene_id
                      ? SceneLink(prop.scene_id, basename(prop.source_path), "lm-filing-scene-link")
                      : basename(prop.source_path)
                  ),
                  React.createElement("span", {
                    className: "lm-terminal-badge filing"
                  }, (selectedCandidate?.entity_type || prop.organize_by || "FILING").toUpperCase()),
                  (selectedCandidate?.is_custom_mapped || prop.is_custom_mapped) ? React.createElement("span", {
                    className: "lm-terminal-badge",
                    style: { background: "rgba(99, 102, 241, 0.15)", color: "#818cf8", border: "1px solid rgba(99, 102, 241, 0.3)" }
                  }, "CUSTOM MAPPED") : null
                ),
                React.createElement("div", { className: "lm-filing-action-menu-wrap", style: { position: "relative" } },
                  React.createElement("button", {
                    type: "button",
                    className: "lm-terminal-btn details lm-filing-menu-trigger",
                    style: { padding: "2px 8px", fontSize: "0.78rem", lineHeight: "1.2", height: "auto", minWidth: "26px" },
                    title: "Scene actions (Open in Stash, FastTag, Refresh)",
                    onClick: (e) => {
                      e.stopPropagation();
                      setActiveFilingMenuId(curr => curr === prop.id ? null : prop.id);
                    }
                  }, "⋮ ACTIONS"),
                  activeFilingMenuId === prop.id && React.createElement("div", {
                    className: "lm-filing-dropdown-menu",
                    style: {
                      position: "absolute", right: 0, top: "100%", marginTop: "4px",
                      background: "#0f172a", border: "1px solid rgba(56, 189, 248, 0.4)",
                      borderRadius: "6px", boxShadow: "0 8px 24px rgba(0,0,0,0.6)",
                      zIndex: 100, minWidth: "180px", padding: "4px 0",
                      display: "flex", flexDirection: "column"
                    },
                    onClick: (e) => e.stopPropagation()
                  },
                    React.createElement("button", {
                      type: "button", className: "lm-filing-dropdown-item",
                      style: { background: "transparent", border: "none", color: "#f1f5f9", textAlign: "left", padding: "8px 12px", fontSize: "0.82rem", cursor: "pointer", display: "flex", alignItems: "center", gap: "8px" },
                      onClick: () => {
                        setActiveFilingMenuId(null);
                        if (prop.scene_id) window.open(`/scenes/${prop.scene_id}`, "_blank");
                      }
                    }, "🎬 Open Scene in Stash"),
                    React.createElement("button", {
                      type: "button", className: "lm-filing-dropdown-item",
                      style: { background: "transparent", border: "none", color: "#f1f5f9", textAlign: "left", padding: "8px 12px", fontSize: "0.82rem", cursor: "pointer", display: "flex", alignItems: "center", gap: "8px" },
                      onClick: () => {
                        setActiveFilingMenuId(null);
                        if (prop.scene_id) {
                          const cardEl = document.querySelector(`[data-scene-id="${prop.scene_id}"]`);
                          if (cardEl && window.FastTag) {
                            cardEl.dispatchEvent(new MouseEvent("contextmenu", {
                              bubbles: true, cancelable: true,
                              clientX: window.innerWidth / 2,
                              clientY: window.innerHeight / 3
                            }));
                          } else {
                            window.open(`/scenes/${prop.scene_id}`, "_blank");
                          }
                        }
                      }
                    }, "⚡ Edit Scene with FastTag"),
                    React.createElement("button", {
                      type: "button", className: "lm-filing-dropdown-item",
                      style: { background: "transparent", border: "none", color: "#38bdf8", textAlign: "left", padding: "8px 12px", fontSize: "0.82rem", cursor: "pointer", display: "flex", alignItems: "center", gap: "8px", borderTop: "1px solid rgba(255,255,255,0.06)" },
                      onClick: () => {
                        setActiveFilingMenuId(null);
                        handleRefreshFilingProposal(prop.source_path, prop.id);
                      }
                    }, "⟳ Refresh Filing Choices")
                  )
                )),
              React.createElement("p", { className: "lm-terminal-attention-detail" },
                React.createElement(React.Fragment, null,
                  React.createElement("b", null, "Matched: "),
                  `${currentEntityName}${prop.matched_alias ? ` (via alias "${prop.matched_alias}")` : ""}`,
                  React.createElement("span", { style: { marginLeft: "8px", opacity: 0.8 } }, `(${selectedCandidate?.match_source || prop.match_source})`))),
              React.createElement("p", { className: "lm-terminal-attention-sub", style: { wordBreak: "break-all", overflowWrap: "anywhere" } },
                React.createElement("b", null, "From: "), prop.source_path),
              destDisplay,
              prop.in_nested_folder ? React.createElement("div", {
                className: "lm-filing-torrent-warning",
                style: { marginTop: "6px", fontSize: "0.8rem", color: "#f59e0b", background: "rgba(245, 158, 11, 0.1)", padding: "4px 8px", borderRadius: "4px" }
              }, "⚠️ Nested download folder: moving this file may interrupt torrent seeding.") : null,
              progressPanel,
              React.createElement("div", { style: { marginTop: "8px" } },
                React.createElement("label", {
                  style: { fontSize: "0.85rem", cursor: "pointer", display: "inline-flex", alignItems: "center", gap: "6px" }
                },
                  React.createElement("input", {
                    type: "checkbox",
                    checked: Boolean(opts.updateMetadata),
                    onChange: e => setFilingOptions(prev => ({ ...prev, [prop.id]: { ...(prev[prop.id] || {}), updateMetadata: e.target.checked } }))
                  }),
                  isDualMatch
                    ? `Tag matched entity in Stash scene (Default: Move only)`
                    : `Tag matched ${currentTagEntity} in Stash scene (Default: Move only)`
                ),
                isDualMatch && Boolean(opts.updateMetadata) ? React.createElement("div", {
                  style: { marginTop: "6px", marginLeft: "22px", display: "flex", gap: "12px", fontSize: "0.82rem" }
                },
                  selectedCandidate.matched_entities.map(me => React.createElement("label", {
                    key: `${me.entity_type}:${me.entity_id}`,
                    style: { cursor: "pointer", display: "inline-flex", alignItems: "center", gap: "4px" }
                  },
                    React.createElement("input", {
                      type: "radio",
                      name: `meta_choice_${prop.id}`,
                      value: `${me.entity_type}:${me.entity_id}`,
                      checked: (opts.targetEntityType || selectedCandidate.matched_entities[0].entity_type) === me.entity_type &&
                        String(opts.targetEntityId || selectedCandidate.matched_entities[0].entity_id) === String(me.entity_id),
                      onChange: () => setFilingOptions(prev => ({ ...prev, [prop.id]: { ...(prev[prop.id] || {}), targetEntityType: me.entity_type, targetEntityId: me.entity_id } }))
                    }),
                    `Use ${me.entity_type.charAt(0).toUpperCase() + me.entity_type.slice(1)} (${me.entity_name})`
                  ))
                ) : null
              ),
              React.createElement("div", { className: "lm-terminal-actions", style: { marginTop: "10px" } },
                React.createElement("button", {
                  type: "button",
                  className: "lm-terminal-btn filing-approve",
                  disabled: isApproveDisabled,
                  onClick: () => handleApproveFiling(prop.id)
                }, approveButtonLabel),
                React.createElement("button", {
                  type: "button",
                  className: "lm-terminal-btn retry",
                  disabled: Boolean(busy) || isAnyTransferActive,
                  onClick: () => handleRefreshFilingProposal(prop.source_path, prop.id),
                  title: "Fetch current Stash metadata and recalculate choices using the cached folder list"
                }, busy === `refresh_prop_${prop.source_path}` ? "⟳ REFRESHING…" : "⟳ REFRESH CHOICES"),
                React.createElement("button", {
                  type: "button",
                  className: "lm-terminal-btn dismiss",
                  disabled: Boolean(busy) || isAnyTransferActive,
                  onClick: () => handleIgnoreFiling(prop.id)
                }, "✕ IGNORE")
              )
            );
          })),

        React.createElement("div", { className: "lm-terminal-section" },
          React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: ".55rem", flexWrap: "wrap", gap: ".5rem" } },
            React.createElement("h3", { style: { margin: 0 } }, "HAPPENING NOW"),
            React.createElement("button", {
              type: "button",
              className: "lm-terminal-btn-backlog",
              onClick: () => setShowBacklogModal(true),
              title: "View workflow for organising pre-existing library files"
            }, `📁 ORGANISE EXISTING FILES${(incoming?.baseline_count || 0) > 0 ? ` (${(backlogData?.eligible_count ?? incoming?.backlog_eligible_count ?? 0).toLocaleString()})` : ""}`)
          ),
          (activeFilingTransfers.length || waitingMoves.length || reconnectingMoves.length || deferredMoves.length || transcoderCandidates.length || activeJobs.length || activeIncoming.length) ? React.createElement(React.Fragment, null,
            activeFilingTransfers.map(t => {
              const displayName = basename(t.source_path);
              return React.createElement("div", {
                className: "lm-terminal-line filing_transfer",
                key: `filing-transfer-${t.proposal_id}`
              },
                React.createElement("span", { className: "lm-filing-transfer-badge" }, (t.stage_label || "FILING").toUpperCase()),
                React.createElement("strong", { title: `${t.source_path} → ${t.destination_path}` }, displayName),
                React.createElement("span", { className: "lm-terminal-sub" }, `→ ${t.detail || t.destination_folder}`)
              );
            }),
            waitingMoves.map(event => {
              const displayName = basename(event.destination_path || event.source_path);
              const targetVideo = event.companion_of || "VIDEO";
              return React.createElement("div", {
                className: "lm-terminal-line waiting_video",
                key: `waiting-video-${event.event_key || event.last_seen_at || displayName}`
              },
                React.createElement("span", null, "WAITING FOR VIDEO"),
                React.createElement("strong", { title: event.destination_path || event.source_path }, displayName),
                React.createElement("em", null, `WAITING FOR VIDEO: ${targetVideo}`)
              );
            }),
            reconnectingMoves.map(event => {
              const displayName = basename(event.destination_path || event.source_path);
              const isCompanion = Boolean(event.companion_of);
              const badgeText = isCompanion ? "RECONNECTING (COMPANION)" : "RECONNECTING";
              const detailText = isCompanion
                ? `RECONNECTING WITH VIDEO: ${event.companion_of}`
                : "RECONNECTING IN STASH";
              return React.createElement("div", {
                className: "lm-terminal-line reconnecting",
                key: `reconnecting-${event.event_key || event.last_seen_at || displayName}`
              },
                React.createElement("span", null, badgeText),
                React.createElement("strong", { title: event.destination_path || event.source_path }, displayName),
                React.createElement("em", null, detailText)
              );
            }),
            deferredMoves.map(event => {
              const displayName = basename(event.destination_path || event.source_path);
              const attempts = event.processing_attempts || 1;
              return React.createElement("div", {
                className: "lm-terminal-line waiting_retry",
                key: `deferred-${event.event_key || event.last_seen_at || displayName}`
              },
                React.createElement("span", null, "RETRY WAITING"),
                React.createElement("strong", { title: event.destination_path || event.source_path }, displayName),
                React.createElement("em", null, `WAITING FOR FILE LOCK (ATTEMPT ${attempts}/5)`)
              );
            }),
            transcoderCandidates.map(item => React.createElement("div", {
              className: "lm-terminal-line transcoder_candidate",
              key: `transcoder-${item.candidate_path}`
            },
              React.createElement("span", null, "ENCODE READY"),
              React.createElement("strong", null, basename(item.candidate_path)),
              React.createElement("em", null, `WAITING FOR ORIGINAL TO BE REMOVED: ${basename(item.source_path)}`),
              React.createElement("button", {
                type: "button",
                className: "lm-terminal-btn details",
                disabled: !busy,
                title: "Stop treating this as an encoded replacement and review it as a separate new file",
                onClick: async () => {
                  setBusy(`promote-transcoder:${item.candidate_path}`);
                  setError("");
                  try {
                    await operation("promote_transcoder_candidate", { candidate_path: item.candidate_path });
                    await refresh();
                  } catch (e) {
                    setError(`Could not review encoded file separately: ${e.message}`);
                  } finally {
                    setBusy("");
                  }
                }
              }, "REVIEW AS NEW FILE")
            )),
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
            activeIncoming.map(item => {
              const isDownloading = item.status === "downloading";
              const isScanning = item.status === "scanning";
              const isPendingRename = item.status === "pending_rename";
              const isRenaming = item.status === "renaming";
              const isGeneratingSheet = item.status === "generating_sheet";
              const isImage = /\.(jpg|jpeg|png|webp)$/i.test(item.path);
              const isWaitingVideo = item.status === "waiting" && !isImage;
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
                    React.createElement("strong", { style: { color: "#4ade80", fontSize: "0.85rem" } }, "RENAMING PROPOSAL (PENDING USER CONFIRMATION)"),
                    React.createElement("span", { className: "lm-terminal-badge rename" }, "RENAME")),
                  React.createElement("div", { style: { fontSize: "0.82rem", color: "#94a3b8", marginBottom: "3px" } },
                    React.createElement("span", null, "Current: "),
                    React.createElement("span", { style: { color: "#e2e8f0" } }, currentName)),
                  React.createElement("div", { style: { fontSize: "0.82rem", color: "#94a3b8", marginBottom: "6px" } },
                    React.createElement("span", null, "Proposed: "),
                    React.createElement("span", { style: { color: "#4ade80", fontWeight: "bold" } }, proposedName)),
                  React.createElement("div", { className: "lm-terminal-actions" },
                    React.createElement("button", {
                      type: "button",
                      className: "lm-terminal-btn rename-approve",
                      disabled: !busy,
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
                      disabled: !busy,
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
                : "SETTLING";

              const statusDetail = isRenaming
                ? "APPLYING FILENAME IN STASH"
                : isDownloading
                ? "INCOMING DOWNLOAD (IN PROGRESS)"
                : isScanning
                ? "STASH IS CHECKING IT"
                : isGeneratingSheet
                ? "CREATING CONTACT SHEET (CSM)"
                : `SETTLES IN ${countdown(item)}`;

              const showProcessNow = item.status === "waiting" && !isDownloading && !isScanning;

              return React.createElement("div", { className: `lm-terminal-line ${item.status}`, key: item.path },
                React.createElement("span", null, badgeText),
                React.createElement("div", { style: { overflow: "hidden", minWidth: 0 } },
                  React.createElement("strong", { title: item.path, style: { display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, displayName)
                ),
                React.createElement("em", null,
                  React.createElement("span", null, statusDetail),
                  showProcessNow && React.createElement("button", {
                    type: "button",
                    className: "lm-terminal-inline-btn process-now",
                    disabled: !busy,
                    title: "Bypass settling delay and import this video into Stash immediately",
                    onClick: () => handleProcessIncomingNow(item.path, displayName)
                  }, "▶ PROCESS NOW")
                )
              );
            })
          ) :
            React.createElement("p", { className: "lm-terminal-empty" }, watcherWorking ? "No active operations. Watchtower is listening." : "The watcher is not running.")),

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
                className: `lm-terminal-filter-pill ${terminalFilter === "attention" ? "active" : ""}`,
                onClick: () => setTerminalFilter("attention")
              }, `⚠️ Needs Attention (${totalProblems})`),
              React.createElement("button", {
                type: "button",
                className: `lm-terminal-filter-pill ${terminalFilter === "problems" ? "active" : ""}`,
                onClick: () => setTerminalFilter("problems")
              }, `Warning History (${problemsCount})`),
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
              } else if (row.category === "companion") {
                if (row.action.includes("contact sheet updated") || row.action.includes("sheet updated")) {
                  badgeText = "SHEET UPDATED";
                } else if (row.action.includes("contact sheet") || row.action.includes("sheet")) {
                  badgeText = "SHEET GENERATED";
                } else if (row.action.includes("move") || row.action.includes("rename")) {
                  badgeText = "COMPANION MOVED";
                } else {
                  badgeText = "PAIRED";
                }
              } else if (row.category === "config") {
                badgeClass = "config";
                badgeText = "CONFIG";
              } else if (row.category === "monitor") {
                badgeClass = "monitor";
                badgeText = "WATCHER";
              } else if (row.status === "deleted") {
                badgeText = "DELETED";
              }

              const targetName = (row.category === "config" || row.category === "monitor")
                ? (row.detail || row.action)
                : basename(row.new_path || row.old_path || row.detail || row.action || "Event");

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
            }) : React.createElement("p", { className: "lm-terminal-empty" }, terminalFilter === "attention" ? "No items currently require attention. The watcher is listening." : `No events match filter “${terminalFilter}”.`))),

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
      panel("Automatic Renaming", "Optional. Automatically renames a scene's file when you edit naming metadata in Stash.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "automaticRenaming", label: "Automatic Renaming",
            help: "Rename edited scenes using the stable filename base. Turning this off leaves filenames untouched while filesystem monitoring continues running." }),
          React.createElement("div", { className: "lm-filename-style-grid", style: { marginTop: "12px" } },
            React.createElement(ChoiceField, {
              label: "Metadata Edit Settle Delay",
              help: "Wait this many seconds after metadata edits in Stash before renaming files and refreshing contact sheets. Additional edits reset the timer.",
              value: Number(config.renameSettleSeconds !== undefined ? config.renameSettleSeconds : 30),
              
              choices: [
                [0, "Immediate (No delay)"],
                [15, "15 seconds"],
                [30, "30 seconds (Recommended)"],
                [60, "60 seconds"],
                [120, "2 minutes"]
              ],
              onChange: value => updateSetting("renameSettleSeconds", Number(value))
            })),
          config.testSceneId && React.createElement("p", { className: "lm-help", style: { marginTop: "8px", color: "var(--lm-accent-gold, #f59e0b)" } },
            `Testing limit active: automatic renaming applies only to Scene ${config.testSceneId}. Configure under Advanced Diagnostics.`))),
      panel("How Filenames Look", "Choose a clear style for future filename changes. Saving these choices does not rename your existing library.",
        React.createElement(React.Fragment, null,
          React.createElement("div", { className: "lm-filename-style-grid", style: { gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))" } },
            React.createElement(ChoiceField, { label: "Information Order", help: "Choose what appears first, second and third.", value: config.filenameOrder || "title,studio,performers", choices: filenameOrders, onChange: value => updateSetting("filenameOrder", value) }),
            React.createElement(ChoiceField, { label: "Between the Main Parts", help: "Choose what appears between the title, studio and performer list.", value: config.filenameSectionSeparator || "dash", choices: sectionSeparators, onChange: value => updateSetting("filenameSectionSeparator", value) }),
            React.createElement(ChoiceField, { label: "Between Performer Names", help: "Choose what appears between two or more performer names.", value: config.filenamePerformerSeparator || "comma", choices: performerSeparators, onChange: value => updateSetting("filenamePerformerSeparator", value) }),
            React.createElement(ChoiceField, { label: "Maximum Performers", help: "Limit how many performers are included in filenames.", value: Number(config.maxPerformersInFilename || 0), choices: performerCountLimits, onChange: value => updateSetting("maxPerformersInFilename", Number(value)) }),
            React.createElement(ChoiceField, { label: "Master Title Source", help: "Choose if scene title comes from Stash metadata or original disk filename.", value: config.masterTitleSource || "stash_title", choices: masterTitleSources, onChange: value => updateSetting("masterTitleSource", value) })),
          React.createElement("div", { className: "lm-subpanel", style: { marginTop: "14px", padding: "12px 14px", border: "1px solid rgba(140, 155, 185, 0.2)", borderRadius: "6px", background: "rgba(10, 20, 34, 0.25)" } },
            React.createElement("strong", { style: { display: "block", marginBottom: "8px", fontSize: "0.95rem", color: "var(--lm-accent-green, #39ff64)" } }, "Filename Inclusion & Title Cleaning Rules"),
            React.createElement("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: "10px 18px" } },
              React.createElement(Switch, { setting: "includeStudio", defaultValue: true,
                label: "Include Studio Name in Filename",
                help: "Include the studio name when constructing new filenames. If disabled, studio is omitted from filenames." }),
              React.createElement(Switch, { setting: "includePerformers", defaultValue: true,
                label: "Include Performers in Filename",
                help: "Include tagged performer names when constructing new filenames. If disabled, performers are omitted." }),
              React.createElement(Switch, { setting: "cleanPerformerOnlyTitles", defaultValue: true,
                label: "Deduplicate Performer-Only Titles",
                help: "When the Stash title is only performer names (or joined by 'and', '&', 'feat.', 'vs.'), omit the duplicate title component from the filename." }),
              React.createElement(Switch, { setting: "stripStudioFromTitle", defaultValue: true,
                label: "Strip Studio from Scene Titles",
                help: "Automatically remove embedded studio names from the title component to prevent repeating the studio twice." }),
              React.createElement(Switch, { setting: "stripPerformersFromTitle", defaultValue: true,
                label: "Strip Performers from Scene Titles",
                help: "Automatically remove embedded performer names from the title component to prevent duplicate performer tags." }),
              React.createElement(Switch, { setting: "stripConnectiveWords", defaultValue: true,
                label: "Strip Connective Words & Punctuation",
                help: "Clean leftover conjunctions (and, &, with, feat., vs.) and trailing symbols left behind when metadata is stripped." }),
              React.createElement(Switch, { setting: "collapseMultipleDashes", defaultValue: true,
                label: "Collapse Multiple Separators & Spaces",
                help: "Automatically merge duplicate dashes, spaces, and punctuation generated during title cleanup into single clean separators." }),
              React.createElement("div", { className: "lm-date-setting" },
                React.createElement(Switch, { setting: "includeSceneDate",
                  label: "Include Scene Date in Filename",
                  help: "Include the scene date from Stash using the fixed YYYY-MM-DD format. Missing dates are omitted." }),
                config.includeSceneDate === true && React.createElement("div", {
                  className: "lm-date-position-options",
                  role: "radiogroup",
                  "aria-label": "Scene date position"
                }, datePositions.map(([value, label]) => React.createElement("label", { key: value },
                  React.createElement("input", {
                    type: "radio",
                    name: "librarymanager-date-position",
                    value,
                    checked: (config.filenameDatePosition || "beginning") === value,
                    onChange: () => updateSetting("filenameDatePosition", value)
                  }),
                  label.replace(" (Recommended)", ""))))))),
          React.createElement("div", { className: "lm-filename-example" },
            React.createElement("small", null, "Example filename"),
            React.createElement("strong", null, exampleFilename),
            React.createElement("span", null, "Only future edits are affected. Very long or duplicate filenames are safely blocked.")),
          React.createElement(RealScenePreviewer, { config, data }),
          React.createElement("div", { className: "lm-actions", style: { marginTop: "14px" } },
            React.createElement(TaskButton, { name: readOnlyTasks.filenames, label: "Preview All Filenames (Read Only)", showResults: "filenames", help: "Shows what would change using these choices across your entire library without renaming anything." })))))
    else if (tab === "monitor") content = React.createElement(React.Fragment, null,
      panel("Filesystem Monitor Status", "The background watcher monitors your Stash library folders, detects files moved or renamed outside of Stash, and safely reconnects them.",
        React.createElement(React.Fragment, null,
          React.createElement("div", { className: "lm-status-grid compact" },
            React.createElement(StatusCard, {
              title: "Monitor State",
              value: (monitor.is_stale || monitor.state === "stale") ? "Stale" : (monitor.state === "running" ? "Running" : (monitor.state || "Stopped")),
              detail: (monitor.is_stale || monitor.state === "stale")
                ? (monitor.stale_reason || `Heartbeat lost (${monitor.heartbeat_at || "no timestamp"})`)
                : (monitor.heartbeat_at ? `Heartbeat active: ${monitor.heartbeat_at}` : "Watcher is stopped"),
              tone: (monitor.is_stale || monitor.state === "stale") ? "warn" : (monitor.state === "running" ? "ok" : "")
            }),
            React.createElement(StatusCard, {
              title: "Configured Roots",
              value: (data?.library_roots || []).length,
              detail: `${monitor.unavailable_roots?.length || 0} unavailable`
            }),
            React.createElement(StatusCard, {
              title: "Recorded Events",
              value: monitor.pending_events || 0,
              detail: "Audited in SQLite"
            })),
          React.createElement("div", { className: "lm-actions", style: { marginTop: "14px", display: "flex", gap: "10px", alignItems: "center" } },
            watcherWorking ? React.createElement(React.Fragment, null,
              React.createElement(Button, {
                variant: "secondary",
                disabled: !!busy,
                title: "Restart the running background watcher daemon",
                onClick: async () => {
                  setBusy("restart_monitor");
                  setError("");
                  try {
                    await operation("stop_monitor");
                    await startMonitorAndRemember(operation, updateSetting);
                    await refresh();
                    setNotice("Filesystem watcher restarted successfully.");
                  } catch (err) {
                    setError(`Restart failed: ${err.message}`);
                  } finally {
                    setBusy("");
                  }
                }
              },
                React.createElement(RestartIcon, { size: 16 }),
                "Restart Watcher"),
              React.createElement(Button, {
                className: "lm-btn-stop",
                disabled: !!busy,
                title: "Stop the background watcher daemon",
                onClick: async () => {
                  setBusy("stop_monitor");
                  setError("");
                  try {
                    await updateSetting("autoStartMonitor", false);
                    await operation("stop_monitor");
                    await refresh();
                    setNotice("Filesystem watcher stopped.");
                  } catch (err) {
                    setError(`Stop failed: ${err.message}`);
                  } finally {
                    setBusy("");
                  }
                }
              },
                React.createElement(StopIcon, { size: 14 }),
                "Stop Watcher")
            ) : isMonitorStale ? React.createElement(React.Fragment, null,
              React.createElement(Button, {
                variant: "primary",
                disabled: !!busy,
                title: "Revive and restart the stale background watcher daemon",
                onClick: async () => {
                  setBusy("restart_monitor");
                  setError("");
                  try {
                    await operation("stop_monitor");
                    await startMonitorAndRemember(operation, updateSetting);
                    await refresh();
                    setNotice("Filesystem watcher restarted successfully.");
                  } catch (err) {
                    setError(`Restart failed: ${err.message}`);
                  } finally {
                    setBusy("");
                  }
                }
              },
                React.createElement(RestartIcon, { size: 16 }),
                "RESTART WATCHER"),
              React.createElement(Button, {
                className: "lm-btn-stop",
                disabled: !!busy,
                title: "Stop the stale watcher daemon",
                onClick: async () => {
                  setBusy("stop_monitor");
                  setError("");
                  try {
                    await updateSetting("autoStartMonitor", false);
                    await operation("stop_monitor");
                    await refresh();
                    setNotice("Filesystem watcher stopped.");
                  } catch (err) {
                    setError(`Stop failed: ${err.message}`);
                  } finally {
                    setBusy("");
                  }
                }
              },
                React.createElement(StopIcon, { size: 14 }),
                "Stop Watcher")
            ) : React.createElement(Button, {
              variant: "primary",
              disabled: !!busy,
              title: "Start the background watcher daemon",
              onClick: async () => {
                setBusy("start_monitor");
                setError("");
                try {
                  await startMonitorAndRemember(operation, updateSetting);
                  await refresh();
                  setNotice("Filesystem watcher started successfully.");
                } catch (err) {
                  setError(`Start failed: ${err.message}`);
                } finally {
                  setBusy("");
                }
              }
            },
              React.createElement(StartIcon, { size: 15 }),
              "Start Watcher"),
            React.createElement(TaskButton, { name: readOnlyTasks.events, label: "Reconcile Events (Read Only)", showResults: "reports" })))),
      panel("Watcher Automation & Behavior", "Configure external move reconciliation and system startup options.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "automaticMoveReconciliation", label: "Reconcile Verified External Moves",
            help: "When a file is moved outside of Stash and verified by size/hash, ask Stash to scan the new path and reconnect it." }),
          React.createElement(Switch, { setting: "transcoderReplacementCompatibility", label: "Transcoder Replacement Compatibility",
            help: "Optional compatibility for FileFlows, HandBrake, Tdarr and similar tools. Allows Watchtower to reconnect a newly encoded replacement, such as movie.mp4 → movie encoded.mp4, to the existing Stash scene. Only strong same-folder matches are accepted; ambiguous matches are left for review." }),
          data?.startup?.supported && React.createElement(Switch, {
            setting: "startAtLogin",
            label: `Start Monitoring with ${data.startup.platform_label || "OS"}`,
            help: data.startup.enabled
              ? `The watcher starts with ${data.startup.platform_label || "your OS"} and waits for Stash if necessary.`
              : `Start the background watcher at login and retry once a minute until Stash is available.`
          }))),
      panel("Monitored Library Roots", "All configured Stash scene library folders tracked by the background filesystem monitor.",
        React.createElement("div", { className: "lm-info-list" },
          (data?.library_roots || []).length ? (data.library_roots || []).map(root => {
            const rootPath = typeof root === "string" ? root : (root?.path || "");
            const isUnavailable = (monitor.unavailable_roots || []).includes(rootPath);
            return React.createElement("div", {
              key: rootPath,
              style: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 4px", borderBottom: "1px solid rgba(255,255,255,0.06)" }
            },
              React.createElement("code", { style: { color: isUnavailable ? "#ffb52e" : "#20e64a", fontSize: "0.88rem" } }, rootPath),
              React.createElement("span", { className: `lm-badge ${isUnavailable ? "warn" : "ok"}` }, isUnavailable ? "UNAVAILABLE" : "AVAILABLE")
            );
          })
            : React.createElement("p", { className: "lm-empty" }, "No library roots discovered in Stash configuration."))))
    else if (tab === "incoming") {
      const rawFolders = Array.isArray(config.incomingFolders)
        ? config.incomingFolders
        : (config.incomingFolder ? [config.incomingFolder] : [""]);
      const incomingFoldersList = rawFolders.length > 0 ? rawFolders : [""];
      const multiStatus = data?.incoming_folders || { folders: [], valid_count: 0, total_count: 0, all_valid: false };
      const statusFolders = multiStatus.folders || [];
      const folderValidationPending = incomingFoldersList.some((folderPath, idx) => {
        const isConfigured = Boolean((folderPath || "").trim());
        const folderStatus = statusFolders[idx] || {};
        return isConfigured && folderStatus.reason === "Choose an incoming folder first";
      });

      const handleUpdateFolder = (index, val) => {
        const next = [...incomingFoldersList];
        next[index] = val;
        setConfig({ ...config, incomingFolders: next, incomingFolder: next[0] || "" });
      };

      const handleSaveFolders = (nextFolders) => {
        const cleaned = nextFolders.map(f => (f || "").trim()).filter(Boolean);
        const finalList = cleaned.length > 0 ? cleaned.slice(0, 5) : [];
        setConfig({ ...config, incomingFolders: nextFolders, incomingFolder: nextFolders[0] || "" });
        updateSettings({
          incomingFolders: finalList,
          incomingFolder: finalList[0] || ""
        });
      };

      const handleAddFolder = () => {
        if (incomingFoldersList.length >= 5) return;
        const next = [...incomingFoldersList, ""];
        setConfig({ ...config, incomingFolders: next });
      };

      const handleRemoveFolder = (index) => {
        if (incomingFoldersList.length <= 1) {
          const next = [""];
          setConfig({ ...config, incomingFolders: next, incomingFolder: "" });
          updateSettings({ incomingFolders: [], incomingFolder: "" });
        } else {
          const next = incomingFoldersList.filter((_, idx) => idx !== index);
          const cleaned = next.map(f => (f || "").trim()).filter(Boolean);
          setConfig({ ...config, incomingFolders: next, incomingFolder: next[0] || "" });
          updateSettings({
            incomingFolders: cleaned,
            incomingFolder: cleaned[0] || ""
          });
        }
      };

      const rawDestRoots = Array.isArray(config.autoFilingDestinationRoots)
        ? config.autoFilingDestinationRoots
        : (config.autoFilingDestinationRoot ? [config.autoFilingDestinationRoot] : [""]);
      const destinationRootsList = rawDestRoots.length > 0 ? rawDestRoots : [""];

      const handleAddDestRoot = () => {
        if (destinationRootsList.length >= 5) return;
        const next = [...destinationRootsList, ""];
        setConfig({ ...config, autoFilingDestinationRoots: next });
      };

      const handleUpdateDestRoot = (index, val) => {
        const next = [...destinationRootsList];
        next[index] = val;
        setConfig({ ...config, autoFilingDestinationRoots: next });
      };

      const handleSaveDestRoots = (nextRoots) => {
        const cleaned = nextRoots.map(f => (f || "").trim()).filter(Boolean);
        const finalList = cleaned.length > 0 ? cleaned.slice(0, 5) : [];
        setConfig({ ...config, autoFilingDestinationRoots: nextRoots, autoFilingDestinationRoot: nextRoots[0] || "" });
        updateSettings({
          autoFilingDestinationRoots: finalList,
          autoFilingDestinationRoot: finalList[0] || ""
        }, "Destination roots saved.");
      };

      const handleRemoveDestRoot = (index) => {
        if (destinationRootsList.length <= 1) {
          const next = [""];
          setConfig({ ...config, autoFilingDestinationRoots: next, autoFilingDestinationRoot: "" });
          updateSettings({ autoFilingDestinationRoots: [], autoFilingDestinationRoot: "" }, "Destination root removed.");
        } else {
          const next = destinationRootsList.filter((_, idx) => idx !== index);
          const cleaned = next.map(f => (f || "").trim()).filter(Boolean);
          setConfig({ ...config, autoFilingDestinationRoots: next, autoFilingDestinationRoot: next[0] || "" });
          updateSettings({
            autoFilingDestinationRoots: cleaned,
            autoFilingDestinationRoot: cleaned[0] || ""
          }, "Destination root removed.");
        }
      };

      const handleRefreshFolderCache = async () => {
        try {
          setBusy("refresh_cache");
          await operation("refresh_filing_cache");
          setNotice("Destination folder discovery cache refreshed.");
        } catch (err) {
          setError(`Folder cache refresh failed: ${err?.message || err}`);
        } finally {
          setBusy("");
        }
      };

      const folderMappingsList = data?.filing_folder_mappings || [];


      const handleSaveNewMapping = async () => {
        if (!newMappingName.trim() || !newMappingFolder.trim()) return;
        setBusy("save_mapping");
        try {
          let entityId = "";
          const requestedName = newMappingName.trim();
          if (newMappingType === "performer") {
            const gqlRes = await gql(`query FindP { allPerformers { id name } }`);
            const match = (gqlRes?.allPerformers || []).find(item => item.name.toLowerCase() === requestedName.toLowerCase());
            if (match) entityId = match.id;
          } else if (newMappingType === "studio") {
            const gqlRes = await gql(`query FindS { allStudios { id name } }`);
            const match = (gqlRes?.allStudios || []).find(item => item.name.toLowerCase() === requestedName.toLowerCase());
            if (match) entityId = match.id;
          } else {
            const gqlRes = await gql(`query FindT { allTags { id name } }`);
            const match = (gqlRes?.allTags || []).find(item => item.name.toLowerCase() === requestedName.toLowerCase());
            if (match) entityId = match.id;
          }
          if (!entityId) {
            throw new Error(`No Stash ${newMappingType} named '${requestedName}' was found. Check the name and try again.`);
          }

          const raw = await operation("save_filing_folder_mapping", {
            entity_type: newMappingType,
            entity_id: String(entityId),
            entity_name: requestedName,
            folder_path: newMappingFolder.trim()
          });
          const res = typeof raw === "string" ? JSON.parse(raw) : raw;
          if (res && res.success) {
            setNotice(`Custom folder mapping for ${newMappingType} '${newMappingName.trim()}' saved.`);
            setNewMappingName("");
            setNewMappingFolder("");
            await refresh(true);
          } else {
            setError(`Failed saving mapping: ${res?.message || "Invalid path or destination root"}`);
          }
        } catch (err) {
          setError(err.message || String(err));
        } finally {
          setBusy("");
        }
      };

      const handleDeleteMapping = async (mappingId) => {
        setBusy(`delete_mapping_${mappingId}`);
        try {
          await operation("delete_filing_folder_mapping", { mapping_id: mappingId });
          setNotice("Custom folder mapping removed.");
          await refresh(true);
        } catch (err) {
          setError(`Failed deleting mapping: ${err.message || String(err)}`);
        } finally {
          setBusy("");
        }
      };

      content = React.createElement(React.Fragment, null,
        panel("Incoming Downloads Folders (Auto-Ingest)", "Watch up to 5 staging folders where new downloads arrive before you organize them.",
          React.createElement(React.Fragment, null,
            React.createElement(Switch, { setting: "automaticIncomingScan", label: "Automatically Add Completed Videos",
              help: "Existing files are left alone. Only new or completed videos in your incoming folders are added to Stash." }),
            React.createElement("div", { style: { marginTop: "14px" } },
              React.createElement(ChoiceField, {
                label: "Wait Before Adding a Video",
                help: "The video must stay completely unchanged for this long before Watchtower asks Stash to add it (default: 5 minutes; 0 uses the default).",
                value: Number(config.incomingSettleMinutes || 5),
                choices: [[1, "1 minute"], [5, "5 minutes (recommended)"], [10, "10 minutes"], [15, "15 minutes"], [30, "30 minutes"]],
                onChange: value => updateSetting("incomingSettleMinutes", Number(value))
              })),
            React.createElement("div", { className: "lm-incoming-list-container", style: { marginTop: "18px" } },
              React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" } },
                React.createElement("strong", null, `Watched Incoming Folders (${incomingFoldersList.length}/5)`),
                React.createElement("button", {
                  type: "button",
                  className: "btn btn-secondary btn-sm lm-incoming-add-btn",
                  disabled: incomingFoldersList.length >= 5,
                  onClick: handleAddFolder,
                  title: incomingFoldersList.length >= 5 ? "Maximum 5 folders reached" : "Add another incoming staging folder"
                }, incomingFoldersList.length >= 5 ? "Max 5 Folders Configured" : "+ Add Incoming Folder")
              ),
              React.createElement("small", { style: { display: "block", color: "var(--text-muted, #aab3c5)", marginBottom: "10px" } },
                "Specify up to 5 directories where downloaders (e.g. Torrents, Usenet, JDownloader) save files. Each folder must be inside one of your Stash library folders."),
              incomingFoldersList.map((folderPath, idx) => {
                const folderStatus = statusFolders[idx] || {};
                const isConfigured = Boolean((folderPath || "").trim());
                const isValid = Boolean(folderStatus.valid);
                const statusReason = isConfigured && folderStatus.reason === "Choose an incoming folder first"
                  ? "Finish editing to validate"
                  : (folderStatus.reason || "Outside Library");
                return React.createElement("div", { key: idx, className: "lm-incoming-row" },
                  React.createElement("span", { className: "lm-incoming-row-num" }, `#${idx + 1}`),
                  React.createElement("input", {
                    value: folderPath || "",
                    className: "lm-incoming-row-input",
                    onChange: event => handleUpdateFolder(idx, event.target.value),
                    onBlur: event => {
                      const next = [...incomingFoldersList];
                      next[idx] = event.target.value;
                      handleSaveFolders(next);
                    },
                    placeholder: `/Volumes/Library/Incoming${idx > 0 ? `_${idx + 1}` : ""}`
                  }),
                  isConfigured ? React.createElement("span", {
                    className: `lm-incoming-status-pill ${isValid ? "ok" : "warn"}`
                  }, isValid ? "✓ Inside Library" : statusReason) : null,
                  React.createElement("button", {
                    type: "button",
                    className: "lm-incoming-row-remove",
                    title: incomingFoldersList.length > 1 ? "Remove this incoming folder" : "Clear folder path",
                    onMouseDown: event => event.preventDefault(),
                    onClick: () => handleRemoveFolder(idx)
                  }, "✕")
                );
              })
            ),
            React.createElement("div", { className: `lm-incoming-state ${multiStatus.valid_count > 0 ? "ready" : "warning"}`, style: { marginTop: "16px" } },
              React.createElement("strong", null, multiStatus.valid_count > 0
                ? `${multiStatus.valid_count} incoming folder${multiStatus.valid_count === 1 ? "" : "s"} ready to monitor`
                : (folderValidationPending ? "Folder validation pending" : "Incoming folders need attention")),
              React.createElement("span", null, multiStatus.valid_count > 0
                ? "Watchtower is tracking completed video files across all valid folders."
                : (folderValidationPending
                  ? "Finish editing the folder path and Watchtower will check that it is available and inside a Stash library root."
                  : "Please configure at least one incoming folder located inside a Stash library root.")),
              React.createElement("small", null, `${incoming.downloading ? `${incoming.downloading} downloading, ` : ""}${incoming.waiting || 0} waiting, ${incoming.scanning || 0} being added, ${incoming.imported || 0} added, ${incoming.failed || 0} failed.`)),
            React.createElement("p", { className: "lm-help", style: { marginTop: "10px" } },
              "In-flight downloads (.crdownload, .part, .download, .tmp) are actively tracked in the Live Terminal. When downloading finishes and the file settles, Stash adds it automatically."))),
        panel("Automatic Filing (Phase 2)", "Conservatively propose destination folders for new videos arriving in Incoming folders across multiple roots with custom mappings and optional metadata tagging. All moves require explicit review and approval.",
          React.createElement(React.Fragment, null,
            React.createElement(Switch, { setting: "autoFilingEnabled", defaultValue: false,
              label: "Enable Automatic Filing Proposals",
              help: "Disabled by default. When enabled, Watchtower evaluates newly added videos in incoming folders and proposes safe moves into matching performer, studio, or tag category subdirectories." }),
            React.createElement("div", { style: { marginTop: "14px" } },
              React.createElement(ChoiceField, {
                label: "Organize By",
                help: "Choose Performer or Studio only, or let Watchtower offer matching Performer, Studio, and verified Stash Tag folders for your approval.",
                value: config.autoFilingOrganizeBy || "performer",
                choices: [["performer", "Performer"], ["studio", "Studio"], ["both", "Performer, Studio or Tag (Let me choose)"]],
                onChange: value => updateSetting("autoFilingOrganizeBy", value, `Automatic Filing organization set to ${value === "studio" ? "Studio" : (value === "both" ? "Performer, Studio or Tag (Let me choose)" : "Performer")}.`)
              })),
            React.createElement("div", { style: { marginTop: "14px" } },
              React.createElement(ChoiceField, {
                label: "Match Source Priority",
                help: "Choose whether to prioritize Stash scene metadata (if already tagged) or match strictly from the physical filename.",
                value: config.autoFilingMatchSource || "metadata_first",
                choices: [["metadata_first", "Metadata First (Fallback to Filename)"], ["filename_only", "Filename Only"], ["metadata_only", "Metadata Only"]],
                onChange: value => updateSetting("autoFilingMatchSource", value, "Match source priority updated.")
              })),
            React.createElement("div", { style: { marginTop: "14px" } },
              React.createElement(ChoiceField, {
                label: "When to Suggest Filing",
                help: "Choose whether to evaluate filing proposals immediately upon import, or wait until a performer, studio, or tag is assigned in Stash.",
                value: config.autoFilingTrigger || "import",
                choices: [["import", "Immediately after import"], ["metadata", "After metadata has been added in Stash"]],
                onChange: value => updateSetting("autoFilingTrigger", value, `When to suggest filing set to ${value === "metadata" ? "after metadata has been added" : "immediately after import"}.`)
              })),
            React.createElement(Switch, { setting: "autoFilingPreserveFilename", defaultValue: false,
              label: "Preserve Original Filename",
              help: "Keep the original filename when moving files via Automatic Filing, and protect filed videos from being subsequently renamed by Automatic Renaming." }),
            React.createElement("div", { style: { marginTop: "14px" } },
              React.createElement(ChoiceField, {
                label: "Maximum Folder Discovery Depth",
                help: "Maximum folder depth (1 to 8, default: 4) when automatically searching destination roots for matching performer, studio, or category folders. Custom folder mappings can target any existing depth.",
                value: String(config.autoFilingMaxDiscoveryDepth ?? 4),
                choices: [
                  ["1", "1 (Immediate subfolders only)"],
                  ["2", "2 levels"],
                  ["3", "3 levels"],
                  ["4", "4 levels (Default)"],
                  ["5", "5 levels"],
                  ["6", "6 levels"],
                  ["7", "7 levels"],
                  ["8", "8 levels (Maximum safe)"]
                ],
                onChange: value => updateSetting("autoFilingMaxDiscoveryDepth", parseInt(value, 10) || 4, `Folder discovery depth set to ${value} level${value === "1" ? "" : "s"}.`)
              })),
            React.createElement("div", { className: "lm-incoming-list-container", style: { marginTop: "18px" } },
              React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" } },
                React.createElement("strong", null, `Destination Roots (${destinationRootsList.length}/5)`),
                React.createElement("div", { style: { display: "flex", gap: "6px" } },
                  React.createElement("button", {
                    type: "button",
                    className: "btn btn-secondary btn-sm lm-refresh-folders-btn",
                    disabled: !!busy,
                    onClick: handleRefreshFolderCache,
                    title: "Force an immediate refresh of the destination folder discovery cache"
                  }, "⟳ Refresh Folders"),
                  React.createElement("button", {
                    type: "button",
                    className: "btn btn-secondary btn-sm lm-incoming-add-btn",
                    disabled: destinationRootsList.length >= 5,
                    onClick: handleAddDestRoot,
                    title: destinationRootsList.length >= 5 ? "Maximum 5 roots reached" : "Add another destination root directory"
                  }, destinationRootsList.length >= 5 ? "Max 5 Roots Configured" : "+ Add Destination Root")
                )
              ),
              React.createElement("small", { style: { display: "block", color: "var(--text-muted, #aab3c5)", marginBottom: "10px" } },
                "Configure up to 5 parent directories containing existing performer, studio, or category subfolders (e.g. /Media/Library/Example Folder)."),
              destinationRootsList.map((rootPath, idx) => {
                return React.createElement("div", { className: "lm-incoming-row", key: `dest-root-${idx}` },
                  React.createElement("input", {
                    value: rootPath || "",
                    className: "lm-incoming-row-input",
                    onChange: event => handleUpdateDestRoot(idx, event.target.value),
                    onBlur: event => {
                      const next = [...destinationRootsList];
                      next[idx] = event.target.value;
                      handleSaveDestRoots(next);
                    },
                    placeholder: `/Media/Performers${idx > 0 ? `_${idx + 1}` : ""}`
                  }),
                  React.createElement("button", {
                    type: "button",
                    className: "lm-incoming-row-remove",
                    title: destinationRootsList.length > 1 ? "Remove this destination root" : "Clear root path",
                    onMouseDown: event => event.preventDefault(),
                    onClick: () => handleRemoveDestRoot(idx)
                  }, "✕")
                );
              })
            ),
            React.createElement("div", { className: "lm-custom-mappings-container", style: { marginTop: "20px", borderTop: "1px solid var(--border-color, #2a2f3a)", paddingTop: "16px" } },
              React.createElement("strong", { style: { display: "block", marginBottom: "4px" } }, "Custom Folder Mappings"),
              React.createElement("small", { style: { display: "block", color: "var(--text-muted, #aab3c5)", marginBottom: "10px" } },
                "Associate specific Stash performers, studios, or tags with custom folder locations. Mapped folders must exist and be inside configured destination roots."),
              (folderMappingsList.length > 0) ? React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: "8px", marginBottom: "14px" } },
                folderMappingsList.map(m => React.createElement("div", {
                  key: `mapping-${m.id}`,
                  style: { display: "flex", justifyContent: "space-between", alignItems: "center", background: "rgba(255,255,255,0.03)", padding: "8px 12px", borderRadius: "6px", border: "1px solid rgba(255,255,255,0.07)" }
                },
                  React.createElement("div", null,
                    React.createElement("strong", null, m.entity_name),
                    React.createElement("span", { style: { opacity: 0.6, fontSize: "0.8rem", marginLeft: "6px" } }, `(${m.entity_type} #${m.entity_id})`),
                    React.createElement("div", { style: { fontSize: "0.85rem", color: "var(--text-muted, #aab3c5)", marginTop: "2px" } }, m.folder_path)
                  ),
                  React.createElement("button", {
                    type: "button",
                    className: "btn btn-outline-danger btn-sm",
                    disabled: !!busy,
                    onClick: () => handleDeleteMapping(m.id),
                    title: "Delete custom mapping"
                  }, "✕")
                ))
              ) : React.createElement("p", { style: { fontStyle: "italic", color: "var(--text-muted, #aab3c5)", fontSize: "0.85rem" } }, "No custom folder mappings configured."),
              React.createElement("div", { style: { background: "rgba(0,0,0,0.15)", padding: "12px", borderRadius: "6px", border: "1px solid rgba(255,255,255,0.05)" } },
                React.createElement("span", { style: { fontWeight: "bold", fontSize: "0.85rem", display: "block", marginBottom: "8px" } }, "+ Add Custom Folder Mapping"),
                React.createElement("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 2fr auto", gap: "8px", alignItems: "center" } },
                  React.createElement("select", {
                    className: "form-control form-control-sm",
                    value: newMappingType,
                    onChange: e => setNewMappingType(e.target.value)
                  },
                    React.createElement("option", { value: "performer" }, "Performer"),
                    React.createElement("option", { value: "studio" }, "Studio"),
                    React.createElement("option", { value: "tag" }, "Tag")
                  ),
                  React.createElement("input", {
                    type: "text",
                    className: "form-control form-control-sm",
                    placeholder: "Performer, studio, or tag name",
                    value: newMappingName,
                    onChange: e => setNewMappingName(e.target.value)
                  }),
                  React.createElement("input", {
                    type: "text",
                    className: "form-control form-control-sm",
                    placeholder: "Existing destination folder",
                    value: newMappingFolder,
                    onChange: e => setNewMappingFolder(e.target.value)
                  }),
                  React.createElement("button", {
                    type: "button",
                    className: "btn btn-primary btn-sm",
                    disabled: !!busy || !newMappingName.trim() || !newMappingFolder.trim(),
                    onClick: handleSaveNewMapping
                  }, "+ Save")
                )
              )
            ),
            React.createElement("div", { style: { marginTop: "20px" } },
              React.createElement("button", {
                type: "button",
                className: "btn btn-outline-secondary btn-sm",
                disabled: !!busy,
                onClick: async () => {
                  setBusy("baseline");
                  try {
                    const raw = await operation("establish_filing_baseline");
                    const res = typeof raw === "string" ? JSON.parse(raw) : raw;
                    setNotice(`Baseline snapshot captured ${res?.snapshotted ?? 0} existing incoming file(s).`);
                  } catch (err) {
                    setError(err.message || String(err));
                  } finally {
                    setBusy("");
                  }
                }
              }, "Snapshot Incoming Baseline Now")))))
    }
    else if (tab === "csm") content = React.createElement(React.Fragment, null,
      panel("Contact Sheets (CSM)", "Generate multi-frame visual contact sheet companion images alongside your video files.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "generateContactSheets", defaultValue: false,
            label: "Generate Visual Contact Sheets (CSM)",
            help: "Automatically generate a visual contact sheet companion image for new videos within your configured location scope." }),
          React.createElement(Switch, { setting: "refreshContactSheetsOnRename", defaultValue: true,
            label: "Refresh Contact Sheets on Metadata Edits",
            help: "Re-render contact sheets with updated title and performer banners when scene metadata is edited in Stash. If turned off, existing contact sheets are kept and safely renamed alongside the video." }),
          React.createElement("div", { className: "lm-filename-style-grid", style: { marginTop: "12px" } },
            React.createElement(ChoiceField, {
              label: "Contact Sheet Location Scope",
              help: "Choose whether contact sheets are generated across all library folders or restricted to your incoming folder.",
              value: config.contactSheetScope || "incoming",
              choices: [["incoming", "Incoming Folder Only (Recommended)"], ["all", "All Library Folders"]],
              
              onChange: value => updateSetting("contactSheetScope", value)
            }),
            React.createElement(ChoiceField, {
              label: "Contact Sheet Layout",
              help: "Number of timestamped scene snapshots per contact sheet.",
              value: config.contactSheetGrid || "4x4",
              choices: [["5x4", "5×4 (20 frames) — Widescreen 16:9 (Recommended)"], ["4x4", "4×4 (16 frames) — Standard"], ["4x5", "4×5 (20 frames) — Detailed"], ["5x5", "5×5 (25 frames) — Dense Overview"], ["3x4", "3×4 (12 frames) — Compact"], ["4x6", "4×6 (24 frames) — Extended"]],
              
              onChange: value => updateSetting("contactSheetGrid", value)
            })),
          React.createElement(Switch, { setting: "contactSheetBanner", defaultValue: true,
            label: "Include Metadata Header Banner",
            help: "Adds a top header banner displaying filename, resolution, size, and duration." }),
          React.createElement(Switch, { setting: "contactSheetAdjustVertical", defaultValue: true,
            label: "Auto-adjust for Vertical Videos",
            help: "Automatically optimizes the grid layout for 9:16 vertical videos to fit standard widescreen monitors." }),
          React.createElement(Switch, { setting: "allowCustomContactSheetScript", defaultValue: false,
            label: "Allow a Trusted Custom Script",
            help: "Runs the configured executable with the same access as Stash. Enable only for a local script you trust." }),
          React.createElement("label", { className: "lm-field", style: { marginTop: "12px" } },
            React.createElement("strong", null, "Custom Contact Sheet Script"),
            React.createElement("small", null, config.allowCustomContactSheetScript === true
              ? "Trusted-code access is enabled. Enter the path to a regular executable file."
              : "Disabled. Watchtower will use its built-in generator and will not execute this path."),
            React.createElement("input", {
              value: config.contactSheetScript || "",
              disabled: config.allowCustomContactSheetScript !== true,
              onChange: e => setConfig({ ...config, contactSheetScript: e.target.value }),
              onBlur: e => updateSetting("contactSheetScript", e.target.value.trim()),
              placeholder: "Leave blank to use built-in generator"
            })),
          React.createElement("div", { className: "lm-actions", style: { marginTop: "16px" } },
            React.createElement(TaskButton, {
              name: "Generate Missing Contact Sheets for Incoming Folder",
              label: "🎞️ Generate Missing Contact Sheets for Incoming Folder",
              help: "Safely process existing videos in your incoming folder that are currently missing a contact sheet."
            })))),
      panel("Companion Files & Safety", "Companion images (.jpg) and subtitle files (.srt, .vtt) stay paired with your scenes.",
        React.createElement("div", { className: "lm-info-list" },
          React.createElement("p", null, React.createElement("strong", null, "Artwork companions: "), ".jpg, .jpeg, .png, .webp"),
          React.createElement("p", null, React.createElement("strong", null, "Subtitles & data: "), ".srt, .vtt, .scc, .ttml, .dfxp, .lrc, .txt, .funscript"),
          React.createElement("p", null, "When a video is renamed, matching companion files are automatically renamed with it. If Stash rejects a video rename, companion changes roll back safely."))))
    else if (tab === "activity") content = panel("Activity History", "Search the history of detected moves, filename changes, warnings and failures.",
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
        }) : React.createElement("p", { className: "lm-empty" }, "No matching activity has been recorded yet."))))
    else if (tab === "advanced") content = React.createElement(React.Fragment, null,
      panel("Testing & Safety Limits", "Safe controls for testing renames on a single scene before applying them across your entire library.",
        React.createElement(React.Fragment, null,
          React.createElement("label", { className: "lm-field" },
            React.createElement("strong", null, "Test Scene ID"),
            React.createElement("small", null, "Optional test scene ID. While set, Automatic Renaming is restricted to this scene only, protecting the rest of your library while you test."),
            React.createElement("input", {
              value: config.testSceneId || "",
              onChange: e => { setConfig({ ...config, testSceneId: e.target.value }); setTestRenameResult(null); },
              onBlur: e => updateSetting("testSceneId", e.target.value.trim()),
              placeholder: "Leave blank for all scenes"
            })),
          React.createElement("div", { className: "lm-actions", style: { marginTop: "12px", display: "flex", gap: "8px", alignItems: "center" } },
            React.createElement(Button, {
              variant: "secondary",
              disabled: busy === "test_rename_preview" || busy === "test_rename_apply" || !config.testSceneId,
              onClick: previewTestRename,
              title: "Preview the filename for Test Scene ID without changing any file."
            }, busy === "test_rename_preview" ? "Previewing..." : "Preview Test Rename"),
            React.createElement(Button, {
              variant: "danger",
              disabled: busy === "test_rename_preview" || busy === "test_rename_apply" || !config.testSceneId,
              onClick: applyTestRename,
              title: "Rename Test Scene ID on disk using configured format and tags."
            }, busy === "test_rename_apply" ? "Renaming..." : "Apply Configured Test Rename")),
          testRenameResult && React.createElement("div", {
            className: `lm-correction-preview ${testRenameResult.status || ""}`,
            style: { marginTop: "14px" }
          },
            React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" } },
              React.createElement("strong", null,
                testRenameResult.status === "ready" ? "✓ Ready to Rename" :
                testRenameResult.status === "renamed" ? "✓ Renamed Successfully" :
                testRenameResult.status === "unchanged" ? "✓ Current Filename Matches" :
                `⚠ ${String(testRenameResult.status || "").replace(/_/g, " ").toUpperCase()}`
              ),
              React.createElement("button", {
                type: "button",
                className: "btn btn-sm btn-link text-muted p-0",
                style: { textDecoration: "none", cursor: "pointer" },
                onClick: () => setTestRenameResult(null)
              }, "✕ Dismiss")
            ),
            testRenameResult.current_path && React.createElement("p", { className: "lm-modal-path", style: { margin: "4px 0" } },
              React.createElement("b", null, "Current: "),
              React.createElement("code", null, testRenameResult.current_path.split("/").pop())
            ),
            testRenameResult.proposed_path && React.createElement("p", { className: "lm-modal-path", style: { margin: "4px 0" } },
              React.createElement("b", null, "Proposed: "),
              React.createElement("code", { style: { color: "#58a6ff" } }, testRenameResult.proposed_path.split("/").pop())
            ),
            testRenameResult.reason && React.createElement("p", { style: { margin: "6px 0 0 0", color: "#8b949e", fontSize: "0.85rem" } },
              testRenameResult.reason
            ),
            testRenameResult.associated_files && testRenameResult.associated_files.length > 0 && React.createElement("p", { style: { margin: "6px 0 0 0", color: "#8b949e", fontSize: "0.85rem" } },
              React.createElement("b", null, "Companion files: "),
              testRenameResult.associated_files.map(s => (s.source || s.target || "").split("/").pop()).join(", ")
            )
          ))),
      panel("Advanced Diagnostic Tools", "Under-the-hood diagnostic scanners for library reconciliation and metadata repair. All preview tools are strictly read-only.",
        React.createElement("div", { className: "lm-task-list" },
          [[readOnlyTasks.inventory, "Refresh the SQLite inventory."],
           [readOnlyTasks.filenames, "Preview filename changes across your entire library."],
           [readOnlyTasks.find, "Find safe candidates for missing paths."],
           [readOnlyTasks.plan, "Build a conflict-aware resolution plan."],
           [readOnlyTasks.merge, "Preview metadata that could be recovered."],
           [readOnlyTasks.events, "Review events recorded by the filesystem monitor."],
           [readOnlyTasks.activity, "Refresh the permanent JSON and CSV activity exports."]].map(([name, help]) =>
            React.createElement("div", { className: "lm-task", key: name, title: help },
              React.createElement("span", null,
                React.createElement("strong", null, name.replace(" (Read Only)", "")),
                React.createElement("small", null, help)),
              React.createElement(TaskButton, { name, label: "Run", help, showResults: "reports" }))))),
      panel("Desktop Notifications", "Receive system notifications for important warnings and background failures.",
        React.createElement(React.Fragment, null,
          React.createElement(Switch, { setting: "macNotifications", label: "Important Warnings & Failures",
            help: "Notify for failed renames, unavailable roots and events needing review." }),
          React.createElement(Switch, { setting: "notifySuccessfulRenames", label: "Notify Successful Renames",
            help: "Also show a desktop notification after each completed automatic rename." }))),
      panel("Stash Plugin Hooks", "Plugin integration triggers configured in Stash.",
        React.createElement("div", { className: "lm-field", style: { padding: "10px 14px", background: "rgba(255,255,255,0.03)", borderRadius: "6px", border: "1px solid rgba(255,255,255,0.08)" } },
          React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "8px", marginBottom: "6px" } },
            React.createElement("strong", null, "Hooks: Metadata Synchronization"),
            React.createElement("div", { style: { display: "inline-flex", gap: "6px", flexWrap: "wrap" } },
              React.createElement("span", { className: "lm-badge ok", style: { fontSize: "11px" } }, "Scene.Update.Post"),
              React.createElement("span", { className: "lm-badge ok", style: { fontSize: "11px" } }, "Performer.Update.Post"),
              React.createElement("span", { className: "lm-badge ok", style: { fontSize: "11px" } }, "Studio.Update.Post"))),
          React.createElement("small", null, "Automatically synchronizes video filenames whenever scene, performer, or studio metadata is updated."))),
      panel("Guided Setup & Onboarding", "Re-run the initial 4-step library configuration wizard.",
        React.createElement("div", { className: "lm-field", style: { padding: "12px 14px", background: "rgba(33, 134, 87, 0.08)", borderRadius: "6px", border: "1px solid rgba(47, 166, 109, 0.3)" } },
          React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "10px" } },
            React.createElement("div", null,
              React.createElement("strong", { style: { color: "#39ff64" } }, "Onboarding Setup Wizard"),
              React.createElement("p", { style: { margin: "2px 0 0 0", fontSize: "0.82rem", color: "#aab3c5" } }, "Step through library storage verification, incoming staging configuration, and baseline database indexing.")
            ),
            React.createElement(Button, {
              variant: "primary",
              style: { background: "#218657", borderColor: "#2da76f", color: "#ffffff", fontWeight: 600 },
              onClick: () => setShowOnboardingWizard(true)
            }, "🚀 Launch Setup Wizard")
          )
        )
      ),
      panel("Factory Reset", "Reset all Watchtower configuration switches, naming rules, and diagnostic settings back to safe factory defaults.",
        React.createElement("div", { className: "lm-field", style: { padding: "12px 14px", background: "rgba(239, 68, 68, 0.04)", borderRadius: "6px", border: "1px solid rgba(239, 68, 68, 0.2)" } },
          React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "10px" } },
            React.createElement("div", null,
              React.createElement("strong", { style: { color: "#f87171" } }, "Reset All Watchtower Settings"),
              React.createElement("p", { style: { margin: "2px 0 0 0", fontSize: "0.82rem", color: "#8b949e" } }, "Restores safe default naming rules and turns Desktop Notifications ON. No media files or Stash records are ever deleted.")
            ),
            React.createElement(Button, {
              variant: "danger",
              style: { background: "#dc2626", borderColor: "#ef4444", color: "#ffffff", fontWeight: 600 },
              disabled: !!busy,
              onClick: handleFactoryReset
            }, busy === "reset" ? "Resetting…" : "↺ Reset Settings to Factory Defaults")
          )
        )
      ),
      reports && reports.length ? panel("Diagnostic Results", "Technical diagnostic scan output.",
        React.createElement(React.Fragment, null,
          React.createElement("div", { className: "lm-actions", style: { display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" } },
            React.createElement(Button, { variant: "secondary", onClick: loadReports, disabled: !!busy }, "Refresh Reports"),
            React.createElement(Button, { variant: "secondary", onClick: handleCleanStash, disabled: !!busy }, busy === "clean" ? "Resolving & Cleaning…" : "⚡ Safe Auto-Resolve (Reconcile & Clean)")),
          React.createElement("div", { className: "lm-report-list" },
            reports.map(report =>
              React.createElement("details", { key: report.name, className: "lm-report", open: report.available },
                React.createElement("summary", null,
                  React.createElement("strong", null, report.title),
                  React.createElement("span", { className: `lm-badge ${report.available ? "ok" : ""}` }, report.available ? "READY" : "NOT RUN"),
                  React.createElement("time", null, report.timestamp ? new Date(report.timestamp).toLocaleString() : "Never")),
                report.available ? React.createElement("div", { className: "lm-report-body" },
                  React.createElement("div", { className: "lm-report-summary" }, Object.entries(report.summary || {}).map(([key, value]) =>
                    React.createElement("span", { key }, React.createElement("small", null, readableKey(key)), React.createElement("strong", null, String(value ?? "—"))))),
                  report.rows.length ? React.createElement("div", { className: "lm-report-rows" }, report.rows.map((row, index) => {
                    const isVerified = row.confidence === "verified" && row.candidate_path;
                    const isConflict = row.confidence === "conflict";
                    return React.createElement("details", { key: index },
                      React.createElement("summary", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
                        React.createElement("div", { style: { display: "inline-flex", alignItems: "center", gap: "8px" } },
                          React.createElement("span", { className: `lm-badge ${isVerified ? "ok" : isConflict ? "warn" : ""}` }, row.status === "conflict" ? "Protected" : (row.status || row.confidence || row.action || row.recommendation || `Finding ${index + 1}`)),
                          row.scene_id && React.createElement("span", { className: "scene-card lm-scene-pill-card", onClick: e => e.stopPropagation() }, SceneLink(row.scene_id, `Scene ${row.scene_id}`, "lm-activity-scene-pill")),
                          React.createElement("span", { style: { fontSize: "0.82rem", color: "#8b949e", maxWidth: "450px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
                            row.candidate_path ? row.candidate_path.split("/").pop() : (row.reason || ""))
                        ),
                        isVerified ? React.createElement(Button, {
                          variant: "primary",
                          style: { fontSize: "0.75rem", padding: "3px 10px" },
                          disabled: !!busy,
                          onClick: async (e) => {
                            e.stopPropagation();
                            await handleReconcilePath(row.candidate_path);
                          }
                        }, busy === "reconcile-item" ? "Reconciling…" : "⚡ Reconcile in Stash") : null
                      ),
                      React.createElement("pre", null, JSON.stringify(row, null, 2))
                    );
                  })) :
                    React.createElement("p", { className: "lm-empty" }, "This report contains no individual findings."),
                  React.createElement("small", { className: "lm-report-file" }, `Export file: ${report.filename}`)) :
                  React.createElement("p", { className: "lm-empty" }, report.error || "Run the corresponding tool under Advanced Diagnostic Tools to create this report."))))))
        : null)
    else if (tab === "help") {
      const GUIDE_SECTIONS = [
        {
          id: "architecture",
          icon: "🚀",
          title: "1. Core Architecture & Safety",
          content: [
            React.createElement("h2", { key: "h2" }, "🚀 1. Core Architecture & Safety Principles"),
            React.createElement("p", { key: "p1" }, "Watchtower is an automated filesystem daemon, metadata synchronization engine, and companion asset manager for Stash. It bridges Stash's database with your physical disk storage."),
            React.createElement("h3", { key: "h3_1" }, "Non-Destructive Safety Guarantees"),
            React.createElement("ul", { key: "ul1" },
              React.createElement("li", null, React.createElement("strong", null, "Safe Defaults: "), "Automatic renaming is disabled by default. All initial scans, inventory runs, and filename previews are strictly 100% read-only."),
              React.createElement("li", null, React.createElement("strong", null, "Hard Scope Lock (Test Scene ID): "), "Setting a Test Scene ID locks all automated hooks and tasks exclusively to that single scene ID, protecting the rest of your library while you experiment."),
              React.createElement("li", null, React.createElement("strong", null, "Live Simulation Sandbox: "), "Test any scene with live screenshot artwork and character-aligned before/after diffs in memory without writing anything to disk."),
              React.createElement("li", null, React.createElement("strong", null, "Worker Serialization & Debounce: "), "Metadata edits are debounced (configurable 1–120s settle delay) and queued through an internal SQLite worker lock (Process Rename Queue) so rapid saves never freeze Stash or cause race conditions."),
              React.createElement("li", null, React.createElement("strong", null, "Rollback Protection: "), "If Stash rejects a video rename or the disk is unavailable, companion file changes roll back automatically.")
            ),
            React.createElement("div", { key: "note1", className: "lm-help-note" },
              React.createElement("strong", null, "Recommended Setup: "), "1. Choose your naming rules in Filename Management. 2. Verify with Real Scene sandbox. 3. Set a Test Scene ID in Advanced Diagnostics. 4. Toggle Automatic Renaming on. 5. Clear Test Scene ID when ready for full library management.")
          ]
        },
        {
          id: "filename-management",
          icon: "🏷️",
          title: "2. Filename Management & Knobs",
          content: [
            React.createElement("h2", { key: "h2" }, "🏷️ 2. Filename Management & Granular Knobs"),
            React.createElement("p", { key: "p1" }, "Watchtower provides a granular naming engine with over 220,000 distinct permutations. Here is what every switch and setting controls:"),
            
            React.createElement("h3", { key: "h3_1" }, "Master Title & Base Source (masterTitleSource)"),
            React.createElement("ul", { key: "ul1" },
              React.createElement("li", null, React.createElement("strong", null, "Stash Scene Title: "), "Uses Stash's scraped or edited title. If the title is blank in Stash, gracefully falls back to the original physical filename stem."),
              React.createElement("li", null, React.createElement("strong", null, "Physical Filename: "), "Always uses the original file stem on disk as the immutable base title, ignoring Stash's title field."),
              React.createElement("li", null, React.createElement("strong", null, "Strict Scene Title: "), "Strict mode — skips renaming entirely if the scene does not have an explicit title in Stash.")
            ),

            React.createElement("h3", { key: "h3_2" }, "Metadata Inclusion Switches"),
            React.createElement("ul", { key: "ul2" },
              React.createElement("li", null, React.createElement("strong", null, "Include Studio in Filename (includeStudio): "), "Appends or prepends the scene's studio name based on your chosen information order."),
              React.createElement("li", null, React.createElement("strong", null, "Include Performers in Filename (includePerformers): "), "Appends or prepends tagged performer names according to your chosen ordering and performer separator."),
              React.createElement("li", null, React.createElement("strong", null, "Include Scene Date in Filename (includeSceneDate): "), "Adds the scene date from Stash in the fixed YYYY-MM-DD format. Missing dates are omitted."),
              React.createElement("li", null, React.createElement("strong", null, "Scene Date Position (filenameDatePosition): "), "Places the optional scene date at the beginning or end of the configured filename."),
              React.createElement("li", null, React.createElement("strong", null, "Maximum Performers in Filename (maxPerformersInFilename): "), "Limits the number of performer names included in the filename (e.g. first 2). Set to 0 to include all tagged performers.")
            ),

            React.createElement("h3", { key: "h3_3" }, "Granular Title Cleaning Switches"),
            React.createElement("ul", { key: "ul3" },
              React.createElement("li", null, React.createElement("strong", null, "Clean Performer-Only Titles (cleanPerformerOnlyTitles): "), "When a title consists solely of performer names (e.g. Alex & John), cleans the title so performers are not redundantly duplicated."),
              React.createElement("li", null, React.createElement("strong", null, "Strip Studio from Titles (stripStudioFromTitle): "), "Removes the studio name from the title string if it was scraped or embedded inside it."),
              React.createElement("li", null, React.createElement("strong", null, "Strip Performers from Titles (stripPerformersFromTitle): "), "Removes tagged performer names from the title string."),
              React.createElement("li", null, React.createElement("strong", null, "Strip Connective Words & Punctuation (stripConnectiveWords): "), "Automatically consumes directly attached conjunctions (&, and, with, w/, feat., vs., presents) when stripping performer/studio names. Prevents dangling punctuation like Alex & - Studio."),
              React.createElement("li", null, React.createElement("strong", null, "Collapse Multiple Separators & Spaces (collapseMultipleDashes): "), "Cleans duplicate hyphens (--) and excess spaces.")
            ),

            React.createElement("h3", { key: "h3_4" }, "Ordering, Separators & Delays"),
            React.createElement("ul", { key: "ul4" },
              React.createElement("li", null, React.createElement("strong", null, "Filename Information Order (filenameOrder): "), "6 permutation choices: Title - Studio - Performers, Studio - Title - Performers, Performers - Title - Studio, etc."),
              React.createElement("li", null, React.createElement("strong", null, "Between Title, Studio and Performers (filenameSectionSeparator): "), "Separator between major sections (Dash \" - \", Space \" \", Underscore \"_\", Dot \".\")."),
              React.createElement("li", null, React.createElement("strong", null, "Between Performer Names (filenamePerformerSeparator): "), "Separator between multiple performers (Comma \", \", Space \" \", Ampersand \" & \", Slash \" / \")."),
              React.createElement("li", null, React.createElement("strong", null, "Rename Settle Delay (renameSettleSeconds): "), "Seconds to wait after a metadata edit before executing disk renames and contact sheet re-renders (default: 30s)."),
              React.createElement("li", null, React.createElement("strong", null, "Automatic Renaming (automaticRenaming): "), "The master switch. When enabled, Stash metadata edits trigger background renames on disk.")
            ),

            React.createElement("h3", { key: "h3_5" }, "Real Scene Sandbox (Interactive Tester)"),
            React.createElement("p", { key: "p2" }, "Search for any scene by ID, performer name, or studio, or click \"Pick Recent Scene\". Watchtower displays a live screenshot thumbnail and a 2-column character-aligned diff of Current on disk vs Proposed filename. Changing any dropdown or switch instantly recalculates the preview in <50ms with zero file modifications.")
          ]
        },
        {
          id: "hooks-cascade",
          icon: "⚡",
          title: "3. Automatic Sync & Hooks",
          content: [
            React.createElement("h2", { key: "h2" }, "⚡ 3. Automatic Sync & Cascade Hooks"),
            React.createElement("p", { key: "p1" }, "Watchtower registers native Stash event hooks in librarymanager.yml to keep files synchronized without manual tasks."),
            React.createElement("h3", { key: "h3_1" }, "Active Hooks & Behaviors"),
            React.createElement("ul", { key: "ul1" },
              React.createElement("li", null, React.createElement("strong", null, "Scene.Update.Post: "), "Fires when saving a scene title, studio, performers, date, or tags in the Stash UI, scrapers, or FastTag."),
              React.createElement("li", null, React.createElement("strong", null, "Performer.Update.Post: "), "When you rename a performer entity or fix a typo in the Performers tab (e.g. \"Jane Dowe\" -> \"Jane Doe\"), Watchtower automatically queries all scenes containing that performer and queues them for background renaming."),
              React.createElement("li", null, React.createElement("strong", null, "Studio.Update.Post: "), "When you rename a studio in the Studios tab, Watchtower automatically updates filenames for all associated scenes.")
            ),
            React.createElement("h3", { key: "h3_2" }, "FastTag & Bulk Edit Integration"),
            React.createElement("p", { key: "p2" }, "When FastTag parses AI titles or applies bulk performer tags, Stash fires update hooks. Watchtower receives them, debounces rapid saves, and serializes the physical renames through its worker queue. FastTag handles the metadata inside Stash, and Watchtower instantly translates that metadata onto your hard drive.")
          ]
        },
        {
          id: "filesystem-monitor",
          icon: "👁️",
          title: "4. Filesystem Monitor & Moves",
          content: [
            React.createElement("h2", { key: "h2" }, "👁️ 4. Filesystem Monitor & External Moves"),
            React.createElement("p", { key: "p1" }, "The Filesystem Monitor runs a background daemon process (watchdog) that observes your configured Stash library roots for external file changes."),
            React.createElement("h3", { key: "h3_1" }, "Controls & Status Cards"),
            React.createElement("ul", { key: "ul1" },
              React.createElement("li", null, React.createElement("strong", null, "Monitor State: "), "Displays whether the daemon is Running or Stopped, along with process PID and heartbeat timestamp."),
              React.createElement("li", null, React.createElement("strong", null, "Start / Stop / Reload Buttons: "), "Manually control the daemon process or reload configuration changes."),
              React.createElement("li", null, React.createElement("strong", null, "Auto Start Monitor (autoStartMonitor): "), "Automatically launches the monitor process whenever the Stash web interface loads."),
              React.createElement("li", null, React.createElement("strong", null, "Start Monitoring at Login (startAtLogin): "), "Configures the appropriate login startup entry on macOS, Windows, or Linux."),
              React.createElement("li", null, React.createElement("strong", null, "Library Roots: "), "Displays all discovered Stash library roots and verifies whether each mount/drive is currently available or offline.")
            ),
            React.createElement("h3", { key: "h3_2" }, "Reconcile Verified External Moves (automaticMoveReconciliation)"),
            React.createElement("p", { key: "p2" }, "When enabled, if you move or rename a video in Finder/Explorer outside of Stash:"),
            React.createElement("ol", { key: "ol1" },
              React.createElement("li", null, "Watchtower detects the file move on disk."),
              React.createElement("li", null, "Verifies exact byte size and computes the cryptographic OpenSubtitles hash (oshash) to guarantee a 100% match."),
              React.createElement("li", null, "Triggers a targeted Stash scan to update the scene's path, preserving all metadata, performers, and history."),
              React.createElement("li", null, "Automatically moves companion artwork and subtitle sidecars alongside the video.")
            )
          ]
        },
        {
          id: "incoming-workflow",
          icon: "📥",
          title: "5. Incoming Downloads Workflow",
          content: [
            React.createElement("h2", { key: "h2" }, "📥 5. Incoming Downloads & Automatic Ingest"),
            React.createElement("p", { key: "p1" }, "Automates the ingest of completed video downloads arriving in up to 5 incoming staging folders (e.g., Torrents, Usenet, JDownloader, AirDrop)."),
            React.createElement("h3", { key: "h3_1" }, "Configuration & Settle Delays"),
            React.createElement("ul", { key: "ul1" },
              React.createElement("li", null, React.createElement("strong", null, "Automatically Add Completed Videos (automaticIncomingScan): "), "Master switch to enable incoming download monitoring across all configured staging folders."),
              React.createElement("li", null, React.createElement("strong", null, "Watched Incoming Folders (incomingFolders): "), "Configure up to 5 designated staging directories inside your Stash library roots where new downloads arrive."),
              React.createElement("li", null, React.createElement("strong", null, "Wait Before Adding a Video (incomingSettleMinutes): "), "Minutes a completed video file must remain 100% unchanged before Watchtower asks Stash to scan and import it (default: 5 min; a value of 0 uses the 5-minute default)."),
              React.createElement("li", null, React.createElement("strong", null, "Live Terminal Tracking: "), "Actively tracks in-flight download temporary files (.crdownload, .part, .download, .tmp). When downloading completes and the file settles, Stash adds it automatically.")
            )
          ]
        },
        {
          id: "contact-sheets-csm",
          icon: "🖼️",
          title: "6. Contact Sheets (CSM)",
          content: [
            React.createElement("h2", { key: "h2" }, "🖼️ 6. Contact Sheets (CSM)"),
            React.createElement("p", { key: "p1" }, "Generates multi-frame visual contact sheet index companion images (.mp4.jpg) alongside video files using ffmpeg."),
            React.createElement("h3", { key: "h3_1" }, "Settings & Layouts"),
            React.createElement("ul", { key: "ul1" },
              React.createElement("li", null, React.createElement("strong", null, "Generate Visual Contact Sheets (generateContactSheets): "), "Enables automated generation of contact sheet artwork for new scenes."),
              React.createElement("li", null, React.createElement("strong", null, "Refresh on Metadata Edits (refreshContactSheetsOnRename): "), "When enabled, re-renders contact sheets with updated metadata banners whenever titles or performers change in Stash. If disabled, existing contact sheets are kept and safely renamed."),
              React.createElement("li", null, React.createElement("strong", null, "Contact Sheet Layout (contactSheetGrid): "), "Grid layout: 5x4 (20 frames widescreen), 4x4 (16 frames), or 3x3 (9 frames)."),
              React.createElement("li", null, React.createElement("strong", null, "Include Header Banner (contactSheetBanner): "), "Renders a top banner showing video resolution, duration, file size, codec, and clean filename."),
              React.createElement("li", null, React.createElement("strong", null, "Auto-adjust for Vertical Videos (contactSheetAdjustVertical): "), "Automatically switches to horizontal multi-column grids for 9:16 vertical smartphone videos so sheets fit standard widescreen displays."),
              React.createElement("li", null, React.createElement("strong", null, "Allow Trusted Custom Script (allowCustomContactSheetScript): "), "Explicit permission to run a configured local executable with the same filesystem access as Stash. Leave off unless you trust the script."),
              React.createElement("li", null, React.createElement("strong", null, "Custom Script (contactSheetScript): "), "Optional path to a regular executable file. It is ignored unless trusted-script access is enabled.")
            ),
            React.createElement("h3", { key: "h3_2" }, "Task: Generate Missing Contact Sheets for Incoming Folder"),
            React.createElement("p", { key: "p2" }, "Clicking this task scans your configured incoming folder and generates contact sheet artwork only for videos that currently lack companion images.")
          ]
        },
        {
          id: "activity-history",
          icon: "📜",
          title: "7. Activity History & Audit",
          content: [
            React.createElement("h2", { key: "h2" }, "📜 7. Activity History & Audit Exports"),
            React.createElement("p", { key: "p1" }, "The Activity tab provides a searchable audit history of all filesystem events, renames, contact sheet creations, warnings, and background actions."),
            React.createElement("h3", { key: "h3_1" }, "Features & Tools"),
            React.createElement("ul", { key: "ul1" },
              React.createElement("li", null, React.createElement("strong", null, "Search Filter: "), "Filter events in real time by scene ID, filename, action, path, or outcome."),
              React.createElement("li", null, React.createElement("strong", null, "Download CSV: "), "Exports the full activity history as a spreadsheet-compatible CSV file."),
              React.createElement("li", null, React.createElement("strong", null, "Download JSON: "), "Exports raw activity records formatted in JSON."),
              React.createElement("li", null, React.createElement("strong", null, "Scene Cards: "), "Each log entry displays before/after file paths, outcome status, timestamp, and clickable scene pill links to open the scene directly in Stash.")
            ),
            React.createElement("h3", { key: "h3_2" }, "Audit Categories Explained"),
            React.createElement("ul", { key: "ul2" },
              React.createElement("li", null, React.createElement("strong", null, "rename: "), "Automatic and manual file renames, before/after paths, and debounced worker execution."),
              React.createElement("li", null, React.createElement("strong", null, "companion: "), "Sidecar pairing, companion renames, and contact sheet updates."),
              React.createElement("li", null, React.createElement("strong", null, "monitor: "), "Daemon process lifecycle, filesystem events, and heartbeat reports."),
              React.createElement("li", null, React.createElement("strong", null, "reconciliation: "), "External file move verifications, size/hash checks, and Stash path updates."),
              React.createElement("li", null, React.createElement("strong", null, "incoming: "), "Download tracking, settle delays, and automated Stash import scans.")
            )
          ]
        },
        {
          id: "diagnostic-tools",
          icon: "🛠️",
          title: "8. Advanced Diagnostic Tools",
          content: [
            React.createElement("h2", { key: "h2" }, "🛠️ 8. Advanced Diagnostic Tools & Buttons"),
            React.createElement("p", { key: "p1" }, "Located under Advanced Diagnostics. All preview tools are 100% read-only and never modify files on disk."),
            React.createElement("h3", { key: "h3_1" }, "Single Scene Testing Controls"),
            React.createElement("ul", { key: "ul1" },
              React.createElement("li", null, React.createElement("strong", null, "Test Scene ID: "), "Enter a single numeric scene ID. While populated, Automatic Renaming is strictly restricted to this scene ID only, protecting the rest of your library."),
              React.createElement("li", null, React.createElement("strong", null, "Preview Test Rename: "), "Preflights the test scene and displays a modal showing Current vs Proposed filename, status, and associated companion sidecars without changing files."),
              React.createElement("li", null, React.createElement("strong", null, "Apply Configured Test Rename: "), "Executes the rename on disk only for the configured Test Scene ID after a confirmation prompt.")
            ),
            React.createElement("h3", { key: "h3_2" }, "Every Diagnostic Button Explained"),
            React.createElement("ul", { key: "ul2" },
              React.createElement("li", null, React.createElement("strong", null, "Build Read-Only Inventory: "), "Scans all scene records from Stash into Watchtower's local SQLite database. Automatically prunes records for scenes that have been deleted or cleaned in Stash."),
              React.createElement("li", null, React.createElement("strong", null, "Preview Safe Filenames: "), "Calculates proposed filenames for every file across your entire collection using your active naming rules, showing what would change without touching disk."),
              React.createElement("li", null, React.createElement("strong", null, "Find Renamed Files: "), "Searches folders for missing files to locate safe rename/move candidates by comparing file size and cryptographic hash."),
              React.createElement("li", null, React.createElement("strong", null, "Build Resolution Plan: "), "Compares missing and live metadata to recommend safe reconciliation steps."),
              React.createElement("li", null, React.createElement("strong", null, "Preview Metadata Merge: "), "Previews metadata that could be recovered from stale duplicate records."),
              React.createElement("li", null, React.createElement("strong", null, "Reconcile Filesystem Events: "), "Reviews raw events logged by the monitor daemon."),
              React.createElement("li", null, React.createElement("strong", null, "View / Export Recent Activity: "), "Exports full audit history to readable JSON and CSV files.")
            ),
            React.createElement("h3", { key: "h3_3" }, "Desktop Notifications"),
            React.createElement("ul", { key: "ul3" },
              React.createElement("li", null, React.createElement("strong", null, "Important Warnings & Failures (macNotifications): "), "Sends native OS desktop notifications for failed renames, unavailable drives/roots, and events needing review."),
              React.createElement("li", null, React.createElement("strong", null, "Notify Successful Renames (notifySuccessfulRenames): "), "Also sends a desktop notification after each completed automatic rename."),
              React.createElement("li", null, React.createElement("strong", null, "Factory Reset (Reset to Defaults): "), "Restores all 20+ Watchtower configuration toggles and naming rules back to factory defaults with Desktop Notifications ON. Never deletes media files or Stash records.")
            )
          ]
        },
        {
          id: "auto-resolve-guide",
          icon: "🧹",
          title: "9. Safe Auto-Resolve & Maintenance",
          content: [
            React.createElement("h2", { key: "h2" }, "🧹 9. Safe Auto-Resolve & Maintenance"),
            React.createElement("p", { key: "p1" }, "Explains how to resolve discrepancies, missing files, and duplicate conflicts in Diagnostic Results."),
            React.createElement("h3", { key: "h3_1" }, "The 1-Click Safe Auto-Resolve Sequence"),
            React.createElement("p", { key: "p2" }, "Clicking ", React.createElement("strong", null, "⚡ Safe Auto-Resolve (Reconcile & Clean)"), " executes an automated, metadata-safe multi-step pipeline:"),
            React.createElement("ol", { key: "ol1" },
              React.createElement("li", null, React.createElement("strong", null, "1. Re-links Verified Renames First: "), "Scans the report for any verified moved/renamed files on disk (like Scene 5314) and triggers a targeted scan in Stash first. This locks the live file path to the scene, preserving 100% of your tags, performers, and ratings."),
              React.createElement("li", null, React.createElement("strong", null, "2. Prunes Orphaned Phantom Records: "), "Runs Stash's clean task with dryRun: false to remove dead references for missing files that no longer exist on disk (like phantom references on Scene 4860). No media files on your hard drive are ever deleted."),
              React.createElement("li", null, React.createElement("strong", null, "3. Rebuilds Inventory & Refreshes: "), "Re-runs the inventory scan and clears the diagnostic report to 0 missing.")
            ),
            React.createElement("h3", { key: "h3_2" }, "Understanding Diagnostic Badges"),
            React.createElement("ul", { key: "ul2" },
              React.createElement("li", null, React.createElement("strong", null, "verified: "), "The file was renamed or moved on disk, but matches the scene's original file by exact byte size and cryptographic hash (oshash). Clicking \"⚡ Reconcile in Stash\" attaches the file to the scene."),
              React.createElement("li", null, React.createElement("strong", null, "Protected / Skipped: "), "Watchtower detected that multiple files would share the same target filename in the same folder. Watchtower locks them and refuses to rename automatically to safeguard against accidental overwrites."),
              React.createElement("li", null, React.createElement("strong", null, "conflict: "), "Duplicate file records in Stash. Resolved safely via Safe Auto-Resolve."),
              React.createElement("li", null, React.createElement("strong", null, "skipped: "), "The filename was evaluated and already matches the current naming rules (no disk write needed)."),
              React.createElement("li", null, React.createElement("strong", null, "missing: "), "A file record exists in Stash but is not present on disk at that path."),
              React.createElement("li", null, React.createElement("strong", null, "ambiguous: "), "Multiple potential candidate files were found on disk; manual review recommended.")
            ),
            React.createElement("div", { key: "tip1", className: "lm-help-tip" },
              React.createElement("strong", null, "Safe Operation: "), "Stash Clean and Safe Auto-Resolve only prune phantom database entries; they never delete or alter video files on your hard drive.")
          ]
        }
      ];

      const filteredSections = helpSearch.trim()
        ? GUIDE_SECTIONS.filter(s => {
            const query = helpSearch.toLowerCase().trim();
            return s.title.toLowerCase().includes(query) || s.id.toLowerCase().includes(query);
          })
        : GUIDE_SECTIONS;

      const activeSection = GUIDE_SECTIONS.find(s => s.id === helpSectionId) || GUIDE_SECTIONS[0];

      content = React.createElement("div", { className: "lm-help-container" },
        React.createElement("div", { className: "lm-help-header-bar" },
          React.createElement("div", { style: { display: "flex", alignItems: "center", gap: "10px" } },
            React.createElement("strong", null, "📖 Watchtower Complete Reference Manual"),
            React.createElement("button", {
              type: "button",
              className: "btn btn-sm btn-primary lm-wizard-launch-btn",
              style: { fontSize: "0.78rem", padding: "0.25rem 0.65rem" },
              onClick: () => setShowOnboardingWizard(true)
            }, "🚀 Launch Setup Wizard")
          ),
          React.createElement("input", {
            type: "search",
            className: "lm-help-search-input",
            placeholder: "Search manual topics, switches, buttons…",
            value: helpSearch,
            onChange: e => setHelpSearch(e.target.value)
          })
        ),
        React.createElement("div", { className: "lm-help-workspace" },
          React.createElement("nav", { className: "lm-help-nav-sidebar" },
            filteredSections.map(s => React.createElement("button", {
              key: s.id,
              type: "button",
              className: `lm-help-nav-item ${s.id === activeSection.id ? "active" : ""}`,
              onClick: () => setHelpSectionId(s.id)
            },
              React.createElement("span", null, s.icon),
              React.createElement("span", null, s.title)
            ))
          ),
          React.createElement("div", { className: "lm-help-content-pane" },
            activeSection.content
          )
        )
      );
    }

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
        ))),
      React.createElement(Toast, { notice, error, onClose: () => { setNotice(""); setError(""); } }),
      (data !== null && config?.onboardingCompleted !== true && !(data?.inventory?.status === "complete" && Boolean(data?.inventory?.completed_at) && ((data?.inventory?.present_count || 0) > 0 || (data?.inventory?.stash_file_count || 0) > 0))) ? React.createElement(OnboardingBanner, {
        onStart: () => {
          onboardingClosedForSession.current = false;
          setShowOnboardingWizard(true);
        }
      }) : null,
      React.createElement(OnboardingWizardModal, {
        show: showOnboardingWizard,
        onHide: () => {
          onboardingClosedForSession.current = true;
          setShowOnboardingWizard(false);
        },
        data,
        config,
        updateSetting,
        requestAutomaticRenaming,
        updateSettings,
        operation,
        refresh,
        onNavigateTab: (targetTab) => setTab(targetTab)
      }),
      React.createElement(AutomaticRenamingWarning, {
        show: showAutomaticRenamingWarning,
        onCancel: () => setShowAutomaticRenamingWarning(false),
        onConfirm: confirmAutomaticRenaming
      }),
      showBacklogModal ? (() => {
        const validSelectedPaths = new Set(Array.from(selectedBacklogPaths).filter(p => (backlogData?.items || []).some(i => i.path === p && i.eligible && i.exists_on_disk !== false)));
        const validSelectedCount = validSelectedPaths.size;

        return React.createElement(Modal, {
        show: true,
        size: "xl",
        onHide: () => {
          if (!isBacklogEvaluating) {
            setShowBacklogModal(false);
            setSelectedBacklogPaths(new Set());
          }
        },
        centered: true,
        dialogClassName: "lm-backlog-modal"
      },
        React.createElement(Modal.Header, { closeButton: !isBacklogEvaluating, style: { background: "#0b120c", borderBottom: "1px solid rgba(56, 189, 248, .2)" } },
          React.createElement(Modal.Title, { style: { color: "#38bdf8", fontWeight: "700", fontSize: "1.1rem" } },
            "📁 Organise Existing Files"
          )
        ),
        React.createElement(Modal.Body, { style: { background: "#060a07", color: "#d1fae5", fontSize: ".88rem", lineHeight: "1.5", maxHeight: "75vh", overflowY: "auto" } },
          React.createElement("div", { className: "lm-backlog-overview" },
            React.createElement("p", { className: "lm-backlog-intro" },
              "Review files already in your incoming folders and create filing suggestions. Nothing moves until you approve a proposal."
            ),
            React.createElement("p", { className: "lm-backlog-protection" },
              React.createElement("strong", { style: { color: "#38bdf8" } }, "Baseline Protection Active: "),
              `Watchtower registered ${(backlogData?.baseline_total ?? backlogData?.total_count ?? incoming?.baseline_count ?? 0).toLocaleString()} pre-existing files in your original baseline snapshot. Files remain protected and are never moved automatically.`
            ),
            React.createElement("div", { className: "lm-backlog-stats-bar" },
              React.createElement("section", { className: "lm-backlog-stat-group current" },
                React.createElement("h4", null, "Current work"),
                React.createElement("div", { className: "lm-backlog-stat-cards" },
                  React.createElement("div", { className: "lm-backlog-stat incoming" }, React.createElement("span", { className: "stat-label" }, "In Incoming"), React.createElement("span", { className: "stat-val" }, (backlogData?.remaining_incoming_count ?? 0).toLocaleString()), React.createElement("span", { className: "stat-sub" }, "Files still present")),
                  React.createElement("div", { className: "lm-backlog-stat eligible" }, React.createElement("span", { className: "stat-label" }, "Ready to Evaluate"), React.createElement("span", { className: "stat-val" }, (backlogData?.eligible_count ?? 0).toLocaleString()), React.createElement("span", { className: "stat-sub" }, "Safe to inspect")),
                  React.createElement("div", { className: "lm-backlog-stat companions" }, React.createElement("span", { className: "stat-label" }, "Companions"), React.createElement("span", { className: "stat-val" }, (backlogData?.remaining_companion_count ?? backlogData?.companion_count ?? 0).toLocaleString()), React.createElement("span", { className: "stat-sub" }, "JPG, NFO and sidecars"))
                )
              ),
              React.createElement("section", { className: "lm-backlog-stat-group review" },
                React.createElement("h4", null, "Needs review"),
                React.createElement("div", { className: "lm-backlog-stat-cards" },
                  React.createElement("div", { className: "lm-backlog-stat pending" }, React.createElement("span", { className: "stat-label" }, "Filing Proposals"), React.createElement("span", { className: "stat-val" }, (backlogData?.pending_proposal_count ?? 0).toLocaleString()), React.createElement("span", { className: "stat-sub" }, "Awaiting approval")),
                  React.createElement("div", { className: "lm-backlog-stat duplicate-removed" }, React.createElement("span", { className: "stat-label" }, "Duplicate Review"), React.createElement("span", { className: "stat-val" }, (backlogData?.duplicate_review_count || 0).toLocaleString()), React.createElement("span", { className: "stat-sub" }, "Possible exact copies")),
                  React.createElement("div", { className: "lm-backlog-stat ineligible" }, React.createElement("span", { className: "stat-label" }, "Missing on Disk"), React.createElement("span", { className: "stat-val" }, (backlogData?.missing_count ?? 0).toLocaleString()), React.createElement("span", { className: "stat-sub" }, "Needs explanation"))
                )
              ),
              React.createElement("section", { className: "lm-backlog-stat-group completed" },
                React.createElement("h4", null, "Completed"),
                React.createElement("div", { className: "lm-backlog-stat-cards" },
                  React.createElement("div", { className: "lm-backlog-stat total" }, React.createElement("span", { className: "stat-label" }, "Protected Baseline"), React.createElement("span", { className: "stat-val" }, (backlogData?.baseline_total ?? backlogData?.total_count ?? 0).toLocaleString()), React.createElement("span", { className: "stat-sub" }, "Original snapshot")),
                  React.createElement("div", { className: "lm-backlog-stat moved" }, React.createElement("span", { className: "stat-label" }, "Verified Filed"), React.createElement("span", { className: "stat-val" }, (backlogData?.verified_moved_count ?? (backlogData?.already_filed_count ?? 0)).toLocaleString()), React.createElement("span", { className: "stat-sub" }, "Moved from Incoming")),
                  React.createElement("div", { className: "lm-backlog-stat duplicate-removed" }, React.createElement("span", { className: "stat-label" }, "Duplicates Removed"), React.createElement("span", { className: "stat-val" }, (backlogData?.resolved_duplicate_count || 0).toLocaleString()), React.createElement("span", { className: "stat-sub" }, "Verified incoming copies"))
                )
              )
            )
          ),

          loadingBacklog ? React.createElement("div", { style: { padding: "2rem", textAlign: "center", color: "#94a3b8" } }, "Loading Incoming baseline backlog items…") :
          backlogError ? React.createElement("div", { className: "lm-alert-box error" }, backlogError) :
          
          isBacklogEvaluating ? React.createElement("div", { className: "lm-backlog-eval-progress-card" },
            React.createElement("h4", { style: { color: "#38bdf8", margin: "0 0 10px 0", fontSize: "1rem" } }, "⚙️ Evaluating Selected Backlog Videos…"),
            React.createElement("div", { style: { display: "flex", justifyContent: "space-between", fontSize: ".82rem", color: "#94a3b8", marginBottom: "6px" } },
              React.createElement("span", null, `Progress: ${backlogProgress.current} / ${backlogProgress.total} (${Math.round((backlogProgress.current / (backlogProgress.total || 1)) * 100)}%)`),
              React.createElement("span", { style: { maxWidth: "60%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, backlogProgress.currentFile)
            ),
            React.createElement("div", { className: "lm-progress-bar-bg", style: { height: "8px", background: "rgba(255,255,255,0.1)", borderRadius: "4px", overflow: "hidden", marginBottom: "14px" } },
              React.createElement("div", { style: { width: `${Math.round((backlogProgress.current / (backlogProgress.total || 1)) * 100)}%`, height: "100%", background: "#38bdf8", transition: "width 0.2s" } })
            ),
            React.createElement("div", { className: "lm-backlog-tally-grid" },
              React.createElement("div", { className: "tally-item ready" }, React.createElement("strong", null, backlogProgress.tally?.proposal_ready || 0), " Proposals Ready"),
              React.createElement("div", { className: "tally-item multi" }, React.createElement("strong", null, backlogProgress.tally?.candidate_selection_required || 0), " Multi-Candidate"),
              React.createElement("div", { className: "tally-item no-id" }, React.createElement("strong", null, backlogProgress.tally?.no_identity_found || 0), " No Identity Found"),
              React.createElement("div", { className: "tally-item no-dest" }, React.createElement("strong", null, backlogProgress.tally?.destination_not_found || 0), " No Destination"),
              React.createElement("div", { className: "tally-item multi" }, React.createElement("strong", null, backlogProgress.tally?.duplicate_review || 0), " Duplicate Review"),
              React.createElement("div", { className: "tally-item inelig" }, React.createElement("strong", null, (backlogProgress.tally?.ineligible || 0) + (backlogProgress.tally?.already_filed || 0) + (backlogProgress.tally?.errors || 0)), " Ineligible / Errors")
            ),
            React.createElement("div", { style: { marginTop: "16px", textAlign: "right" } },
              React.createElement(Button, {
                variant: "danger",
                size: "sm",
                onClick: () => { backlogCancelRequested.current = true; },
                style: { background: "#a9434c", borderColor: "#ca5964" }
              }, "⏹ Cancel Evaluation")
            )
          ) :

          backlogCompletedSummary ? React.createElement("div", { className: "lm-backlog-summary-card" },
            React.createElement("h4", { style: { color: backlogCompletedSummary.cancelled ? "#f59e0b" : "#4ade80", margin: "0 0 10px 0", fontSize: "1rem" } },
              backlogCompletedSummary.cancelled ? "⚠️ Backlog Evaluation Cancelled" : "✅ Backlog Evaluation Complete"
            ),
            React.createElement("p", { style: { fontSize: ".85rem", color: "#cbd5e1" } },
              `Evaluated ${backlogCompletedSummary.processed} of ${backlogCompletedSummary.total} selected videos. Filing proposals were created for review. Files were not moved.`
            ),
            React.createElement("div", { className: "lm-backlog-tally-grid", style: { margin: "14px 0" } },
              React.createElement("div", { className: "tally-item ready" }, React.createElement("strong", null, backlogCompletedSummary.tally?.proposal_ready || 0), " Proposals Ready"),
              React.createElement("div", { className: "tally-item multi" }, React.createElement("strong", null, backlogCompletedSummary.tally?.candidate_selection_required || 0), " Multi-Candidate"),
              React.createElement("div", { className: "tally-item no-id" }, React.createElement("strong", null, backlogCompletedSummary.tally?.no_identity_found || 0), " No Identity Found"),
              React.createElement("div", { className: "tally-item no-dest" }, React.createElement("strong", null, backlogCompletedSummary.tally?.destination_not_found || 0), " No Destination"),
              React.createElement("div", { className: "tally-item multi" }, React.createElement("strong", null, backlogCompletedSummary.tally?.duplicate_review || 0), " Duplicate Review"),
              React.createElement("div", { className: "tally-item inelig" }, React.createElement("strong", null, (backlogCompletedSummary.tally?.ineligible || 0) + (backlogCompletedSummary.tally?.already_filed || 0) + (backlogCompletedSummary.tally?.errors || 0)), " Ineligible / Errors")
            ),
            (backlogCompletedSummary.results || []).some(result => !["proposal_ready", "candidate_selection_required"].includes(result.outcome)) &&
              React.createElement("section", { className: "lm-backlog-result-details", "aria-label": "Backlog evaluation details" },
                React.createElement("h5", null, "Files that need explanation"),
                React.createElement("p", { className: "lm-backlog-result-help" },
                  "Expand a category to see every affected filename and Watchtower's exact reason."),
                ["duplicate_review", "no_identity_found", "destination_not_found", "ambiguous_match", "ineligible", "already_filed", "errors"].map(outcome => {
                  const items = (backlogCompletedSummary.results || []).filter(result => result.outcome === outcome);
                  if (items.length === 0) return null;
                  return React.createElement("details", {
                    className: `lm-backlog-result-group ${outcome}`,
                    key: outcome,
                    open: outcome === "duplicate_review" || outcome === "no_identity_found" || outcome === "destination_not_found" || outcome === "errors"
                  },
                    React.createElement("summary", null, `${backlogOutcomeLabel(outcome)} (${items.length})`),
                    React.createElement("div", { className: "lm-backlog-result-list" },
                      items.map((result, index) => React.createElement("article", {
                        className: "lm-backlog-result-item scene-card",
                        key: `${result.path || result.basename || outcome}-${index}`,
                        "data-scene-id": result.scene_id || ""
                      },
                        result.scene_id ? React.createElement("a", {
                          href: `/scenes/${result.scene_id}`,
                          className: "lm-fasttag-scene-context",
                          tabIndex: -1,
                          "aria-hidden": "true"
                        }) : null,
                        React.createElement("strong", null, result.basename || basename(result.path) || "Unknown file"),
                        React.createElement("p", null, backlogResultReason(result)),
                        result.path && React.createElement("code", null, result.path),
                        result.duplicate_info && React.createElement("div", { className: "lm-duplicate-review-evidence" },
                          React.createElement("p", null, React.createElement("b", null, "Stash scene: "), `#${result.duplicate_info.scene_id}`),
                          React.createElement("p", null, React.createElement("b", null, "Organised file retained: "), result.duplicate_info.retained_path),
                          React.createElement("p", null, React.createElement("b", null, "Verification: "), result.duplicate_info.reason),
                          (result.duplicate_info.companions || []).length > 0 && React.createElement("label", { className: "lm-duplicate-companion-choice" },
                            React.createElement("input", {
                              type: "checkbox",
                              checked: duplicateCompanionChoices[result.path] === true,
                              onChange: event => {
                                event.stopPropagation();
                                setDuplicateCompanionChoices(previous => ({ ...previous, [result.path]: event.target.checked }));
                              }
                            }),
                            ` Also delete ${(result.duplicate_info.companions || []).length} exact companion file(s) from the incoming folder`,
                            React.createElement("small", null, (result.duplicate_info.companions || []).map(entry => entry.path).join(" • "))
                          )
                        ),
                        React.createElement("div", { className: "lm-backlog-item-actions" },
                          result.scene_id ? React.createElement("button", {
                            type: "button",
                            className: "lm-terminal-btn details",
                            onClick: event => openBacklogMetadataEditor(event, result.scene_id)
                          }, "🎬 OPEN SCENE TO EDIT") : null,
                          result.scene_id ? React.createElement("button", {
                            type: "button",
                            className: "lm-terminal-btn details",
                            title: window.FastTag ? "Open this scene in FastTag" : "FastTag is unavailable; open the Stash scene editor",
                            onClick: event => openBacklogFastTag(event, result.scene_id)
                          }, "⚡ EDIT WITH FASTTAG") : null,
                          result.path ? React.createElement("button", {
                            type: "button",
                            className: "lm-terminal-btn retry",
                            disabled: !!busy,
                            onClick: event => {
                              event.stopPropagation();
                              handleReevaluateBacklogItem(result);
                            }
                          }, busy === `backlog_refresh:${result.path}` ? "⟳ CHECKING…" : "⟳ RE-EVALUATE") : null
                          , result.duplicate_info ? React.createElement("button", {
                            type: "button",
                            className: `lm-terminal-btn ${result.duplicate_info.checksum_status === "verified" ? "dismiss" : "retry"}`,
                            disabled: !!busy || result.duplicate_info.checksum_status === "mismatch" || result.duplicate_info.status === "ambiguous",
                            onClick: event => {
                              event.stopPropagation();
                              if (result.duplicate_info.checksum_status === "verified") handleDeleteBacklogDuplicate(result);
                              else handleInspectBacklogDuplicate(result, true);
                            }
                          }, busy === `duplicate_verify:${result.path}` ? "VERIFYING…" :
                             busy === `duplicate_delete:${result.path}` ? "DELETING…" :
                             result.duplicate_info.checksum_status === "verified" ? "DELETE EXACT DUPLICATE…" : "VERIFY EXACT DUPLICATE") : null
                        )
                      ))
                    )
                  );
                })
              ),
            React.createElement("div", { style: { display: "flex", gap: "8px", justifyContent: "flex-end", marginTop: "16px" } },
              React.createElement(Button, {
                variant: "secondary",
                size: "sm",
                onClick: () => setBacklogCompletedSummary(null),
                style: { background: "rgba(255,255,255,0.1)", color: "#fff" }
              }, "Back to Backlog List"),
              ((backlogCompletedSummary.tally?.proposal_ready || 0) + (backlogCompletedSummary.tally?.candidate_selection_required || 0)) > 0 ?
                React.createElement(Button, {
                  variant: "success",
                  size: "sm",
                  onClick: () => {
                    setShowBacklogModal(false);
                    setSelectedBacklogPaths(new Set());
                    setBacklogCompletedSummary(null);
                    setTab("overview");
                    refresh(true);
                  },
                  style: { background: "#2fa66d", borderColor: "#3ab87b" }
                }, `Review Proposals (${(backlogCompletedSummary.tally?.proposal_ready || 0) + (backlogCompletedSummary.tally?.candidate_selection_required || 0)})`) : null
            )
          ) :

          showBacklogConfirm ? React.createElement("div", { className: "lm-backlog-confirm-card" },
            React.createElement("h4", { style: { color: "#38bdf8", margin: "0 0 10px 0", fontSize: "1rem" } }, "🛡️ Confirm Backlog Evaluation"),
            React.createElement("p", { style: { fontSize: ".88rem", color: "#e2e8f0" } },
              `You are about to evaluate `,
              React.createElement("strong", { style: { color: "#38bdf8" } }, `${validSelectedCount} selected video(s)`),
              ` for Automatic Filing.`
            ),
            React.createElement("ul", { style: { fontSize: ".82rem", color: "#94a3b8", paddingLeft: "1.2rem", margin: "10px 0" } },
              React.createElement("li", null, "Existing Stash scene IDs and metadata will be preserved without rescanning or reimporting."),
              React.createElement("li", null, "Files are processed in small batches to protect disk I/O and keep the dashboard snappy."),
              React.createElement("li", null, "Proposals will be generated for your individual review. Files are NEVER moved automatically.")
            ),
            React.createElement("div", { style: { display: "flex", gap: "10px", justifyContent: "flex-end", marginTop: "16px" } },
              React.createElement(Button, {
                variant: "secondary",
                size: "sm",
                onClick: () => setShowBacklogConfirm(false),
                style: { background: "rgba(255,255,255,0.1)", color: "#fff" }
              }, "Cancel"),
              React.createElement(Button, {
                variant: "primary",
                size: "sm",
                onClick: startBacklogEvaluation,
                style: { background: "#0284c7", borderColor: "#38bdf8" }
              }, `Confirm & Evaluate ${validSelectedCount} Videos`)
            )
          ) :

          React.createElement("div", null,
            React.createElement("div", { className: "lm-backlog-controls" },
              React.createElement("div", { className: "lm-backlog-tabs" },
                React.createElement("button", { className: backlogTab === "eligible" ? "active" : "", onClick: () => setBacklogTab("eligible") }, `Ready to Evaluate (${backlogData?.eligible_count ?? 0})`),
                React.createElement("button", { className: backlogTab === "companions" ? "active" : "", onClick: () => setBacklogTab("companions") }, `Companions (${backlogData?.remaining_companion_count ?? backlogData?.companion_count ?? 0})`),
                React.createElement("button", { className: backlogTab === "ineligible" ? "active" : "", onClick: () => setBacklogTab("ineligible") }, `Ineligible / Filed (${backlogData?.ineligible_count ?? Math.max(0, (backlogData?.video_count ?? 0) - (backlogData?.eligible_count ?? 0))})`),
                React.createElement("button", { className: backlogTab === "all" ? "active" : "", onClick: () => setBacklogTab("all") }, `All Files (${backlogData?.total_count ?? 0})`)
              ),
              React.createElement("div", { style: { display: "flex", gap: "8px", alignItems: "center" } },
                React.createElement("input", {
                  type: "text",
                  className: "lm-backlog-search",
                  placeholder: "Filter filename...",
                  value: backlogSearch,
                  onChange: (e) => setBacklogSearch(e.target.value)
                }),
                backlogTab === "eligible" || backlogTab === "all" ? React.createElement(React.Fragment, null,
                  React.createElement(Button, {
                    size: "sm",
                    variant: "outline-info",
                    className: "lm-btn-select-all-eligible",
                    onClick: handleSelectAllEligible,
                    style: { fontSize: ".76rem", whiteSpace: "nowrap" }
                  }, `Select All Ready (${backlogData?.eligible_count ?? 0})`),
                  validSelectedCount > 0 ? React.createElement(Button, {
                    size: "sm",
                    variant: "outline-secondary",
                    onClick: handleClearBacklogSelection,
                    style: { fontSize: ".76rem", whiteSpace: "nowrap" }
                  }, "Clear") : null
                ) : null
              )
            ),

            React.createElement("div", { className: "lm-backlog-list" },
              (backlogData?.items || [])
                .filter(item => {
                  if (backlogTab === "eligible") return item.eligible;
                  if (backlogTab === "companions") return item.is_companion;
                  if (backlogTab === "ineligible") return item.is_video && !item.eligible;
                  return true;
                })
                .filter(item => !backlogSearch || item.basename.toLowerCase().includes(backlogSearch.toLowerCase()))
                .map((item, idx) => {
                  const isChecked = validSelectedPaths.has(item.path);
                  const diagnosticParts = String(item.diagnostic || "").split("|").map(part => part.trim()).filter(Boolean);
                  const needsDestination = !item.destination_path && diagnosticParts.some(part => /no destination|destination folder not found/i.test(part));
                  const decisionLabel = item.duplicate_info ? "Duplicate review" :
                    !item.eligible ? (item.status_label || "Not ready") :
                    item.destination_path ? "Destination found" :
                    needsDestination ? "Destination needed" : "Ready to evaluate";
                  const decisionClass = item.duplicate_info ? "duplicate" : item.destination_path ? "destination-found" : needsDestination ? "destination-needed" : "ready";
                  return React.createElement("div", {
                    key: item.path || idx,
                    className: `lm-backlog-item ${item.eligible ? "eligible" : "ineligible"} ${isChecked ? "selected" : ""}`,
                    onClick: () => {
                      if (item.eligible && item.exists_on_disk !== false) toggleBacklogItemSelection(item.path);
                    }
                  },
                    item.eligible ? React.createElement("input", {
                      type: "checkbox",
                      checked: isChecked,
                      onChange: () => {},
                      style: { cursor: "pointer", marginRight: "8px" }
                    }) : React.createElement("span", { style: { width: "18px", display: "inline-block", color: "#64748b" } }, "•"),
                    React.createElement("div", { className: "item-body", style: { flex: 1, minWidth: 0 } },
                      React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px" } },
                        React.createElement("span", { className: "item-name", title: item.path }, item.basename),
                        React.createElement("span", { className: `item-status-pill decision ${decisionClass}` }, decisionLabel)
                      ),
                      React.createElement("div", { className: "item-meta", style: { fontSize: ".75rem", color: "#94a3b8", display: "flex", flexWrap: "wrap", gap: "10px", marginTop: "2px" } },
                        item.scene_id ? React.createElement("span", null, `Scene ${item.scene_id}${item.scene_title ? `: ${item.scene_title}` : ""}`) : null,
                        item.size ? React.createElement("span", null, `${(item.size / (1024 * 1024)).toFixed(1)} MB`) : null
                      ),
                      item.destination_path ? React.createElement("div", {
                        className: "item-destination",
                        style: { fontSize: ".74rem", color: "#38bdf8", marginTop: "3px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
                        title: item.destination_path
                      }, `➔ ${item.destination_path}`) : null,
                      diagnosticParts.length ? React.createElement("details", {
                        className: "lm-backlog-item-details",
                        onClick: event => event.stopPropagation()
                      },
                        React.createElement("summary", null, "Why Watchtower classified this file"),
                        React.createElement("ul", null, diagnosticParts.map((part, partIndex) =>
                          React.createElement("li", { key: `${item.path}-reason-${partIndex}` }, part)
                        ))
                      ) : null,
                      item.duplicate_info ? React.createElement("div", { className: "lm-duplicate-review-evidence" },
                        React.createElement("p", null, React.createElement("b", null, "Incoming duplicate: "), item.path),
                        React.createElement("p", null, React.createElement("b", null, "Organised file retained: "), item.duplicate_info.retained_path),
                        React.createElement("p", null, React.createElement("b", null, "Verification: "), item.duplicate_info.reason),
                        (item.duplicate_info.companions || []).length > 0 && React.createElement("label", { className: "lm-duplicate-companion-choice" },
                          React.createElement("input", {
                            type: "checkbox",
                            checked: duplicateCompanionChoices[item.path] === true,
                            onChange: event => {
                              event.stopPropagation();
                              setDuplicateCompanionChoices(previous => ({ ...previous, [item.path]: event.target.checked }));
                            }
                          }),
                          ` Also delete ${(item.duplicate_info.companions || []).length} exact companion file(s) from the incoming folder`,
                          React.createElement("small", null, (item.duplicate_info.companions || []).map(entry => entry.path).join(" • "))
                        ),
                        React.createElement("div", { className: "lm-backlog-item-actions" },
                          item.scene_id ? React.createElement("a", {
                            href: `/scenes/${item.scene_id}`,
                            target: "_blank",
                            rel: "noopener noreferrer",
                            className: "lm-terminal-btn details",
                            onClick: event => event.stopPropagation()
                          }, `🎬 VIEW SCENE ${item.scene_id}`) : null,
                          React.createElement("button", {
                            type: "button",
                            className: `lm-terminal-btn ${item.duplicate_info.checksum_status === "verified" ? "dismiss" : "retry"}`,
                            disabled: !!busy || item.duplicate_info.checksum_status === "mismatch" || item.duplicate_info.status === "ambiguous",
                            onClick: event => {
                              event.stopPropagation();
                              if (item.duplicate_info.checksum_status === "verified") handleDeleteBacklogDuplicate(item);
                              else handleInspectBacklogDuplicate(item, true);
                            }
                          }, busy === `duplicate_verify:${item.path}` ? "VERIFYING…" :
                             busy === `duplicate_delete:${item.path}` ? "DELETING…" :
                             item.duplicate_info.checksum_status === "verified" ? "DELETE EXACT DUPLICATE…" : "VERIFY EXACT DUPLICATE")
                        )
                      ) : null
                    )
                  );
                })
            ),

            React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "14px", borderTop: "1px solid rgba(56,189,248,0.15)", paddingTop: "12px" } },
              React.createElement("span", { style: { fontSize: ".82rem", color: "#94a3b8" } },
                `Selected: `,
                React.createElement("strong", { style: { color: "#38bdf8" } }, validSelectedCount),
                ` of ${backlogData?.eligible_count ?? 0} ready to evaluate`
              ),
              React.createElement(Button, {
                variant: "primary",
                disabled: validSelectedCount === 0,
                onClick: () => setShowBacklogConfirm(true),
                style: { background: "#0284c7", borderColor: "#38bdf8", fontWeight: "700" }
              }, `🚀 Evaluate Selected (${validSelectedCount})`)
            )
          )
        ),
        React.createElement(Modal.Footer, { style: { background: "#0b120c", borderTop: "1px solid rgba(56, 189, 248, .2)" } },
          React.createElement(Button, {
            variant: "secondary",
            disabled: isBacklogEvaluating,
            onClick: () => {
              setShowBacklogModal(false);
              setSelectedBacklogPaths(new Set());
            },
            style: { background: "rgba(255,255,255,0.1)", border: "1px solid rgba(255,255,255,0.2)", color: "#fff" }
          }, "Close")
        )
      );
    })() : null,
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
    const [health, setHealth] = React.useState({ tone: "checking", title: "Watchtower", status: null });
    const [onboarded, setOnboarded] = React.useState(false);
    const [showHud, setShowHud] = React.useState(false);
    const hudTimer = React.useRef(null);

    const check = React.useCallback(async () => {
      try {
        const [raw, cfg] = await Promise.all([
          operation("monitor_health"),
          getConfig().catch(() => ({}))
        ]);
        const status = typeof raw === "string" ? JSON.parse(raw) : raw;
        const isCompleted = cfg?.onboardingCompleted === true || (status?.inventory?.status === "complete" && Boolean(status?.inventory?.completed_at));
        setOnboarded(isCompleted);

        if (!isCompleted) {
          setHealth({
            tone: "setup",
            title: "Watchtower: Setup required — click to configure",
            status
          });
          return;
        }

        const heartbeatAge = status.heartbeat_at ? Date.now() - Date.parse(status.heartbeat_at) : Infinity;
        const unavailable = status.unavailable_roots?.length || 0;
        const activeMovesCount = status.active_moves?.length || 0;
        const pending = status.pending_events || 0;
        const attentionCount = status.attention_events != null ? status.attention_events : Math.max(0, pending - activeMovesCount);
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
        } else if (attentionCount) {
          tone = "warning";
          title = `Library Manager: listening; ${attentionCount} detected change${attentionCount === 1 ? " needs" : "s need"} review`;
        } else if (activeMovesCount) {
          tone = "healthy";
          title = `Library Manager: watching; reconnecting moved file`;
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
      const timer = window.setInterval(check, 10000);
      const visible = () => { if (!document.hidden) check(); };
      const onHealthEvent = () => check();
      document.addEventListener("visibilitychange", visible);
      window.addEventListener("librarymanager:health-check", onHealthEvent);
      return () => {
        window.clearInterval(timer);
        document.removeEventListener("visibilitychange", visible);
        window.removeEventListener("librarymanager:health-check", onHealthEvent);
      };
    }, [check]);

    return React.createElement("div", {
      className: "nav-utility lm-nav-wrapper",
      style: { position: "relative", display: "inline-flex", alignItems: "center" },
      onMouseEnter: () => {
        window.clearTimeout(hudTimer.current);
        check();
        if (onboarded) setShowHud(true);
      },
      onMouseLeave: () => { hudTimer.current = window.setTimeout(() => setShowHud(false), 250); }
    },
      React.createElement(NavLink, { className: "lm-nav-link", exact: true, to: PATH, title: health.title,
        "aria-label": health.title },
        React.createElement(Button, {
          className: `minimal d-flex align-items-center h-100 lm-nav-button lm-health-${health.tone}`
        }, React.createElement("img", { className: "lm-watchtower-icon",
          src: "/plugin/librarymanager/assets/watchtower-icon.png", alt: "" }))),
      showHud && onboarded && React.createElement("div", {
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
          React.createElement("div", null, React.createElement("span", null, "Unreviewed Changes:"), React.createElement("strong", null, `${health.status?.attention_events != null ? health.status.attention_events : Math.max(0, (health.status?.pending_events || 0) - (health.status?.active_moves?.length || 0))}`)),
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
    version: "1.0.4",
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
    if (config.incomingSettleMinutes === undefined || config.incomingSettleMinutes === 0) {
      saveConfig({ ...config, incomingSettleMinutes: 5 }).catch(() => {});
    }
  }).catch(error => console.error("[LibraryManager] Could not read auto-start setting:", error));
})();
