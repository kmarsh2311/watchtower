import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const javascript = readFileSync(new URL("../librarymanager.js", import.meta.url), "utf8");
const css = readFileSync(new URL("../librarymanager.css", import.meta.url), "utf8");
const manifest = readFileSync(new URL("../librarymanager.yml", import.meta.url), "utf8");

test("transcoder candidates are neutral work in progress with an independent-file action", () => {
  assert.match(javascript, /data\?\.transcoder_candidates \|\| \[\]/);
  assert.match(javascript, /ENCODE READY/);
  assert.match(javascript, /WAITING FOR ORIGINAL TO BE REMOVED/);
  assert.match(javascript, /promote_transcoder_candidate/);
  assert.match(javascript, /REVIEW AS NEW FILE/);
});

function loadNamedFunction(name) {
  const marker = `  function ${name}`;
  const start = javascript.indexOf(marker);
  assert.notEqual(start, -1, `${name} is present`);
  let brace = javascript.indexOf("{", start);
  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let index = brace; index < javascript.length; index += 1) {
    const character = javascript[index];
    if (escaped) { escaped = false; continue; }
    if (character === "\\") { escaped = true; continue; }
    if (quote) { if (character === quote) quote = null; continue; }
    if (character === '"' || character === "'" || character === "`") { quote = character; continue; }
    if (character === "{") depth += 1;
    if (character === "}") {
      depth -= 1;
      if (depth === 0) {
        const source = javascript.slice(start + 2, index + 1);
        return Function(`${source}; return ${name};`)();
      }
    }
  }
  throw new Error(`Could not parse ${name}`);
}

test("Command Centre presents unresolved changes as actions, not completed work", () => {
  assert.match(javascript, /NEEDS ATTENTION/);
  assert.match(javascript, /Video deletion detected/);
  assert.match(javascript, /DELETED — REVIEW/);
  assert.doesNotMatch(javascript, /RECENTLY COMPLETED/);
  assert.doesNotMatch(javascript, /Show \d+ changes? to resolve/);
});

test("a stale watcher explains the problem inside the terminal alert", () => {
  assert.match(javascript, /! WATCHER NOT RESPONDING/);
  assert.match(javascript, /monitor\.stale_reason \|\| "The watcher stopped sending its expected heartbeat\."/);
  assert.match(javascript, /Click RESTART WATCHER above/);
});

test("review controls expose only context-appropriate actions", () => {
  assert.match(javascript, /deletion && isVideo && event\.scene_id[\s\S]*REMOVE STASH SCENE/);
  assert.match(javascript, /!deletion && isVideo[\s\S]*scan_destination/);
  assert.match(javascript, /resolvePendingEvent\(event, "dismiss"\)/);
});

test("bulk review can only send an explicit dismissal", () => {
  assert.match(javascript, /async function resolveAllPendingEvents\(\)/);
  assert.match(javascript, /resolve_all_filesystem_events", \{ resolution: "dismiss" \}/);
  assert.doesNotMatch(javascript, /resolveAllPendingEvents\(resolution/);
});

test("long activity names wrap to two lines instead of using ellipses", () => {
  const terminalNameRule = css.match(/\.lm-terminal-stream-name\s*\{[^}]+\}/)?.[0] || "";
  assert.match(terminalNameRule, /white-space:\s*normal/);
  assert.match(terminalNameRule, /line-clamp:\s*2/);
  assert.match(terminalNameRule, /overflow-wrap:\s*anywhere/);
  assert.doesNotMatch(terminalNameRule, /text-overflow:\s*ellipsis/);

  const recentActivityRules = [...css.matchAll(/\.lm-overview-event summary strong\s*\{[^}]+\}/g)];
  const recentActivityRule = recentActivityRules.at(-1)?.[0] || "";
  assert.match(recentActivityRule, /white-space:\s*normal/);
  assert.match(recentActivityRule, /line-clamp:\s*2/);
});

test("manifest wording reflects current cross-platform and multi-folder behaviour", () => {
  assert.match(manifest, /displayName: Start Monitoring at Login/);
  assert.match(manifest, /macOS, Windows or Linux/);
  assert.match(manifest, /Up to five folders inside your Stash libraries/);
  assert.doesNotMatch(manifest, /Allow One Test Rename/);
});

test("an edited incoming-folder path is described as pending validation", () => {
  assert.match(javascript, /Finish editing to validate/);
  assert.match(javascript, /Folder validation pending/);
  assert.match(javascript, /Finish editing the folder path and Watchtower will check/);
});

test("custom contact-sheet executables require an explicit trust switch", () => {
  assert.match(manifest, /allowCustomContactSheetScript:/);
  assert.match(manifest, /same access as Stash/);
  assert.match(javascript, /setting: "allowCustomContactSheetScript"/);
  assert.match(javascript, /disabled: config\.allowCustomContactSheetScript !== true/);
});

test("automatic renaming requires beta acknowledgement only when enabling", () => {
  assert.match(javascript, /function requestAutomaticRenaming\(enabled\)/);
  assert.match(javascript, /if \(enabled !== true\) \{[\s\S]*updateSetting\("automaticRenaming", false\)/);
  assert.match(javascript, /if \(config\.automaticRenaming === true\) return;/);
  assert.match(javascript, /setShowAutomaticRenamingWarning\(true\)/);
  assert.match(javascript, /function AutomaticRenamingWarning\(\{ show, onCancel, onConfirm \}\)/);
  assert.match(javascript, /ReactDOM\.createPortal\(warning, document\.body\)/);
  assert.match(css, /\.lm-confirm-backdrop\{[^}]*z-index:1000001/);
  assert.match(javascript, /requestAutomaticRenaming\(!\(config\.automaticRenaming === true\)\)/);
  assert.match(javascript, /setting === "automaticRenaming"[\s\S]*requestAutomaticRenaming\(e\.target\.checked\)/);
  assert.match(javascript, /Beta Feature 🧪/);
  assert.match(javascript, /Automatic Renaming is still in beta and changes filenames on disk\./);
  assert.match(javascript, /Missing metadata, long names, uncommon symbols or unusual metadata combinations may produce unexpected filenames\./);
  assert.match(javascript, /confirm that video and companion files are renamed as expected\./);
  assert.match(javascript, /cannot anticipate every filename and filesystem combination\./);
  assert.match(javascript, /onConfirm: confirmAutomaticRenaming/);
  assert.match(javascript, /onClick: onConfirm \}, "I understand"/);
  assert.match(javascript, /async function confirmAutomaticRenaming\(\) \{[\s\S]*updateSetting\("automaticRenaming", true\)/);
});

test("scene date naming is optional, ISO-only, and positioned at either edge", () => {
  assert.match(manifest, /includeSceneDate:[\s\S]*fixed, sortable YYYY-MM-DD format/);
  assert.match(manifest, /filenameDatePosition:[\s\S]*beginning or end/);
  assert.match(javascript, /setting: "includeSceneDate"/);
  assert.match(javascript, /const datePositions = \[\["beginning", "Beginning \(Recommended\)"\], \["end", "End"\]\]/);
  assert.match(javascript, /className: "lm-date-position-options"/);
  assert.match(javascript, /role: "radiogroup"/);
  assert.match(javascript, /name: "librarymanager-date-position"/);
  assert.match(javascript, /type: "radio"/);
  assert.match(javascript, /config\.includeSceneDate === true && React\.createElement/);
  assert.doesNotMatch(javascript, /label: "Scene Date Position"/);
  assert.match(css, /\.lm-date-position-options\{[^}]*display:flex/);
  assert.match(css, /\.lm-date-setting\{[^}]*border-bottom:/);
  assert.match(css, /\.lm-date-setting>\.lm-switch-row\{border-bottom:0\}/);
  assert.ok(javascript.indexOf('setting: "collapseMultipleDashes"') < javascript.indexOf('className: "lm-date-setting"'),
    "scene date setting follows all primary title-cleaning controls");
  assert.match(javascript, /config\.filenameDatePosition === "end" \? exampleMainParts\.push\(exampleDate\) : exampleMainParts\.unshift\(exampleDate\)/);
  assert.doesNotMatch(javascript, /Date Format/);
});

test("onboarding cannot be permanently dismissed or completed before required setup", () => {
  assert.doesNotMatch(javascript, /onboardingBannerDismissed/);
  assert.match(javascript, /Required before Watchtower can operate/);
  assert.match(javascript, /const setupReady = availableRoots\.length > 0 && inventoryComplete/);
  assert.match(javascript, /inventory\?\.stash_scene_count \?\? inventory\?\.total_scenes/);
  assert.match(javascript, /inventory\?\.stash_file_count \?\? inventory\?\.total_files/);
  assert.match(javascript, /if \(!setupReady\)[\s\S]*goToStep\(4\)/);
  assert.match(javascript, /disabled: !setupReady/);
  assert.match(javascript, /onboardingClosedForSession\.current/);
  assert.match(javascript, /role: "dialog"/);
  assert.match(javascript, /"aria-modal": "true"/);
});

test("onboarding accurately describes ingest and local inventory", () => {
  assert.match(javascript, /asks Stash to scan and add it from its existing folder/);
  assert.match(javascript, /librarymanager\.sqlite3 database/);
  assert.doesNotMatch(javascript, /automatically moves them into your Stash collection/);
  assert.doesNotMatch(javascript, /local SQLite database \(watchtower\.db\)/);
  assert.doesNotMatch(javascript, /Incoming files are automatically moved/);
  assert.doesNotMatch(javascript, /into watchtower\.db/);
});

test("onboarding locks navigation while indexing and requires the baseline before continuing", () => {
  assert.match(javascript, /if \(isExiting \|\| !onboardingControls\.canClose\) return/);
  assert.match(javascript, /className: "lm-wizard-close-btn",[\s\S]*disabled: !onboardingControls\.canClose/);
  assert.match(javascript, /disabled: !onboardingControls\.canGoNext/);
  assert.match(javascript, /Indexing your library…/);
  assert.match(javascript, /indexError \? "↻ Try Again"/);
  assert.match(javascript, /operation\("inventory_progress"\)/);
  assert.match(javascript, /formatInventoryProgress\(indexProgress\)/);
  assert.match(javascript, /className: "lm-wizard-index-progress"/);
});

test("help describes login startup as cross-platform", () => {
  assert.match(javascript, /Start Monitoring at Login \(startAtLogin\)/);
  assert.match(javascript, /macOS, Windows, or Linux/);
  assert.doesNotMatch(javascript, /Start with macOS \(startAtLogin\)/);
});

test("onboarding controls enforce indexing and baseline transitions", () => {
  const controls = loadNamedFunction("onboardingControlState");
  assert.deepEqual(controls(4, true, false), {
    canClose: false, canGoBack: false, canUseCompletedSteps: false, canGoNext: false
  });
  assert.equal(controls(4, false, false).canGoNext, false);
  assert.equal(controls(4, false, true).canGoNext, true);
  assert.equal(controls(5, false, true).canGoBack, true);
});

test("inventory progress formatter reports preparation and bounded percentage", () => {
  const format = loadNamedFunction("formatInventoryProgress");
  assert.match(format({ status: "preparing", detail: "Reading scenes from Stash" }), /Reading scenes from Stash/);
  assert.match(format({ status: "running", processed: 50, total: 200 }), /50 of 200 files checked \(25%\)/);
  assert.match(format({ status: "running", processed: 250, total: 200 }), /\(100%\)/);
});

test("terminal filter bar displays Needs Attention and Warning History separately", () => {
  assert.match(javascript, /Needs Attention \(\$\{totalProblems\}\)/);
  assert.match(javascript, /Warning History \(\$\{problemsCount\}\)/);
});

test("active moves appear as Reconnecting in HAPPENING NOW and are excluded from Needs Attention", () => {
  assert.match(javascript, /reconnectingMoves = unresolved\.filter\(e => e\.processing_state === "reconnecting" \|\| e\.processing_state === "queued"\)/);
  assert.match(javascript, /deferredMoves = unresolved\.filter\(e => e\.processing_state === "deferred"\)/);
  assert.match(javascript, /attentionEvents = unresolved\.filter\(e => !e\.processing_state\)/);
  assert.match(javascript, /badgeText = isCompanion \? "RECONNECTING \(COMPANION\)" : "RECONNECTING"/);
  assert.match(javascript, /RECONNECTING IN STASH/);
  assert.match(javascript, /RETRY WAITING/);
  assert.match(javascript, /WAITING FOR FILE LOCK \(ATTEMPT \$\{attempts\}\/5\)/);
  assert.match(css, /\.lm-terminal-line\.reconnecting/);
  assert.match(css, /\.lm-terminal-line\.waiting_retry/);
});

test("early companion JPGs appear as WAITING FOR VIDEO and ambiguous standalone JPGs appear under Needs Attention", () => {
  assert.match(javascript, /waitingMoves = unresolved\.filter\(e => e\.processing_state === "waiting_video"\)/);
  assert.match(javascript, /"WAITING FOR VIDEO"/);
  assert.match(javascript, /`WAITING FOR VIDEO: \${targetVideo}`/);
  assert.match(javascript, /attentionCount = attentionEvents\.length/);
  assert.match(css, /\.lm-terminal-line\.waiting_video/);
});
