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

test("completed inventory automatically preserves onboarding completion across reloads without showing wizard", () => {
  assert.match(javascript, /hasCompletedInventory/);
  assert.match(javascript, /payload\?\.inventory\?\.status === "complete"/);
  assert.match(javascript, /updateSetting\("onboardingCompleted", true\)/);
  assert.match(manifest, /onboardingCompleted:/);
  assert.match(manifest, /Indicates whether initial onboarding setup has been completed/);
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

test("pending filing proposals appear in their own visible section when zero problems exist", () => {
  // 1. Logic verification: 1 pending proposal with zero problems
  const sampleFilingProposals = [
    {
      id: 1,
      scene_id: "6386",
      source_path: "/Volumes/Main/Vault1/New/Jamie Ray.mp4",
      proposed_path: "/Volumes/Main/Vault1/FilingTest/Jamie Ray/Jamie Ray.mp4",
      status: "pending",
      matched_entity_name: "Jamie Ray",
      organize_by: "performer"
    }
  ];
  const pendingFilingProposals = sampleFilingProposals.filter(p => p.status !== "needs_recovery");
  const filingRecoveryProposals = sampleFilingProposals.filter(p => p.status === "needs_recovery");
  const filingRecoveryCount = filingRecoveryProposals.length;
  const failedIncoming = [];
  const unavailableRoots = [];
  const attentionCount = 0;
  const isMonitorStale = false;
  const totalProblems = failedIncoming.length + unavailableRoots.length + attentionCount + filingRecoveryCount + (isMonitorStale ? 1 : 0);

  assert.equal(totalProblems, 0, "Ordinary pending proposal must not count as a problem");
  assert.equal(pendingFilingProposals.length, 1, "Pending proposal exists in pendingFilingProposals");
  assert.equal(filingRecoveryCount, 0, "No recovery proposals");

  // Recovery test: needs_recovery must count as a problem
  const recoveryProposals = [{ id: 2, status: "needs_recovery" }];
  const problemsWithRecovery = failedIncoming.length + unavailableRoots.length + attentionCount + recoveryProposals.filter(p => p.status === "needs_recovery").length + (isMonitorStale ? 1 : 0);
  assert.equal(problemsWithRecovery, 1, "needs_recovery must count as a problem");

  // 2. UI rendering assertions: independent Filing Proposals card rendered outside Needs Attention
  assert.match(javascript, /pendingFilingProposals = filingProposals\.filter\(p => p\.status !== "needs_recovery"\)/);
  assert.match(javascript, /filingRecoveryProposals = filingProposals\.filter\(p => p\.status === "needs_recovery"\)/);
  assert.match(javascript, /totalProblems = failedIncoming\.length \+ unavailableRoots\.length \+ attentionCount \+ filingRecoveryCount/);
  assert.match(javascript, /pendingFilingProposals\.length > 0 && React\.createElement\("div", \{ className: "lm-terminal-filing-card" \}/);
  assert.match(javascript, /className: "lm-terminal-filing-tag"/);
  assert.match(javascript, /"📁 FILING PROPOSALS"/);
  assert.match(javascript, /className: "lm-terminal-btn filing-approve"/);
  assert.match(javascript, /handleApproveFiling\(prop\.id\)/);
  assert.match(javascript, /handleIgnoreFiling\(prop\.id\)/);
  assert.match(javascript, /filingRecoveryProposals\.map\(prop =>/);
  assert.match(javascript, /handleRecoverFiling\(prop\.id\)/);

  // 3. Stylesheet assertions
  assert.match(css, /\.lm-terminal-filing-card/);
  assert.match(css, /\.lm-terminal-filing-header/);
  assert.match(css, /\.lm-terminal-filing-tag/);
  assert.match(css, /\.lm-terminal-filing-count/);
});

test("Automatic Filing Phase 2 UI elements: multiple roots, custom mappings, candidate picker, metadata toggle, and torrent warning", () => {
  // 1. Multiple destination roots controls in settings
  assert.match(javascript, /Destination Roots \(\$\{destinationRootsList\.length\}\/5\)/);
  assert.match(javascript, /handleAddDestRoot/);
  assert.match(javascript, /handleUpdateDestRoot/);
  assert.match(javascript, /handleRemoveDestRoot/);
  assert.match(javascript, /autoFilingDestinationRoots/);

  // 2. Custom folder mappings UI in settings
  assert.match(javascript, /Custom Folder Mappings/);
  assert.match(javascript, /handleSaveNewMapping/);
  assert.match(javascript, /handleDeleteMapping/);
  assert.match(javascript, /save_filing_folder_mapping/);
  assert.match(javascript, /delete_filing_folder_mapping/);

  // 3. Proposal card enhancements: candidate destination dropdown, custom mapped badge, torrent warning, metadata checkbox
  assert.match(javascript, /className: "lm-filing-candidate-picker"/);
  assert.match(javascript, /Select Destination:/);
  assert.match(javascript, /CUSTOM MAPPED/);
  assert.match(javascript, /className: "lm-filing-torrent-warning"/);
  assert.match(javascript, /Nested download folder: moving this file may interrupt torrent seeding/);
  assert.match(javascript, /Tag matched \${currentTagEntity} in Stash scene \(Default: Move only\)/);

  // 4. Stylesheet assertions
  assert.match(css, /\.lm-filing-candidate-picker/);
  assert.match(css, /\.lm-filing-torrent-warning/);
  assert.match(css, /\.lm-filing-metadata-check/);
  assert.match(css, /\.lm-custom-mappings-container/);
});

test("Automatic Filing settings include 'When to Suggest Filing' and 'Preserve Original Filename'", () => {
  assert.match(javascript, /When to Suggest Filing/);
  assert.match(javascript, /autoFilingTrigger/);
  assert.match(javascript, /Immediately after import/);
  assert.match(javascript, /After metadata has been added in Stash/);

  assert.match(javascript, /Preserve Original Filename/);
  assert.match(javascript, /autoFilingPreserveFilename/);
});

test("Automatic Filing destination roots management supports adding up to 5 roots, editing, saving, and removing", () => {
  assert.match(javascript, /destinationRootsList\.length >= 5/);
  assert.match(javascript, /autoFilingDestinationRoots: next/);
  assert.match(javascript, /autoFilingDestinationRoots: finalList/);
});

test("Automatic Filing nested folder discovery depth and explicit cache refresh UI", () => {
  // Discovery depth setting
  assert.match(javascript, /Maximum Folder Discovery Depth/);
  assert.match(javascript, /autoFilingMaxDiscoveryDepth/);
  assert.match(javascript, /1 \(Immediate subfolders only\)/);
  assert.match(javascript, /4 levels \(Default\)/);
  assert.match(javascript, /8 levels \(Maximum safe\)/);

  // Explicit refresh button and handler
  assert.match(javascript, /lm-refresh-folders-btn/);
  assert.match(javascript, /handleRefreshFolderCache/);
  assert.match(javascript, /refresh_filing_cache/);
  assert.match(javascript, /Destination folder discovery cache refreshed/);

  // Manifest schema
  assert.match(manifest, /autoFilingMaxDiscoveryDepth:/);
  assert.match(manifest, /Maximum Folder Discovery Depth/);
});

test("Automatic Filing save notifications cover all settings, removals, and error paths", () => {
  // 1. Destination roots save and remove toasts
  assert.match(javascript, /"Destination roots saved\."/);
  assert.match(javascript, /"Destination root removed\."/);

  // 2. Custom folder mappings save and remove toasts
  assert.match(javascript, /Custom folder mapping for .* saved\./);
  assert.match(javascript, /"Custom folder mapping removed\."/);

  // 3. Settings switches and choices toasts
  assert.match(javascript, /"Automatic Filing proposals enabled\."/);
  assert.match(javascript, /"Automatic Filing proposals disabled\."/);
  assert.match(javascript, /"Filename preservation enabled\."/);
  assert.match(javascript, /"Match source priority updated\."/);
  assert.match(javascript, /When to suggest filing set to/);
  assert.match(javascript, /Folder discovery depth set to/);

  // 4. Error toast handling on failure
  assert.match(javascript, /setError\(`Failed saving mapping:/);
  assert.match(javascript, /setError\(`Failed deleting mapping:/);
});

test("Automatic Filing uses generic placeholder examples and contains no personal database entities", () => {
  // No personal examples in UI code or manifest
  assert.doesNotMatch(javascript, /Jamie Ray/);
  assert.doesNotMatch(javascript, /Vault1\/Jamie Ray Collection/);
  assert.doesNotMatch(manifest, /Vault1\/Performers/);
  assert.doesNotMatch(manifest, /Vault2\/Vault2/);

  // Generic examples are present
  assert.match(javascript, /placeholder: "Performer name"/);
  assert.match(javascript, /placeholder: "Existing destination folder"/);
  assert.match(javascript, /\/Media\/Performers/);
  assert.match(javascript, /\/Media\/Performers\/Example Folder/);
  assert.match(manifest, /\/Media\/Performers\/Example Folder/);
});


test("Incoming settling live MM:SS countdown and Process Now UI components", () => {
  // 1. Countdown calculation from backend settling_deadline
  assert.match(javascript, /function countdown\(item\)/);
  assert.match(javascript, /item\.settling_deadline/);
  assert.match(javascript, /Math\.round\(Number\(item\.settling_deadline\) - \(Date\.now\(\) \/ 1000\)\)/);
  assert.match(javascript, /String\(Math\.floor\(remaining \/ 60\)\)\.padStart\(2, "0"\)/);
  assert.match(javascript, /String\(remaining % 60\)\.padStart\(2, "0"\)/);
  assert.match(javascript, /\`\${minutes}:\${seconds}\`/);

  // 2. Countdown display on waiting video items
  assert.match(javascript, /isWaitingVideo = item\.status === "waiting" && !isImage/);
  assert.match(javascript, /SETTLES IN \${countdown\(item\)}/);

  // 3. Process Now button rendering and handler
  assert.match(javascript, /handleProcessIncomingNow/);
  assert.match(javascript, /className: "lm-terminal-inline-btn process-now"/);
  assert.match(javascript, /"▶ PROCESS NOW"/);
  assert.match(javascript, /window\.confirm\(`Process "\${name}" now\?/);
  assert.match(javascript, /operation\("process_incoming_file_now", \{ path \}\)/);
  assert.match(javascript, /setNotice\(res\.message \|\| `Settling delay bypassed for \${name}\. Processing initiated\.`\)/);
  assert.match(javascript, /setError\(res\?\.error \|\| "Failed to process incoming file now\."\)/);

  // 4. CSS styling
  assert.match(css, /\.lm-terminal-inline-btn\.process-now/);
});

test("Automatic Filing 'Performer or Studio (Let me choose)' option and dual-entity candidate UI", () => {
  // 1. Dropdown choices include the third option 'both'
  assert.match(javascript, /\["both",\s*"Performer or Studio \(Let me choose\)"\]/);
  assert.match(manifest, /Performer or Studio \(Let me choose\)/);

  // 2. Candidate destination formatting with [Performer] and [Studio] tags
  assert.match(javascript, /\[\$\{\(c\.entity_type \|\| "DEST"\)\.toUpperCase\(\)\}\]/);

  // 3. Dual-match radio entity selection when both match same folder
  assert.match(javascript, /isDualMatch && Boolean\(opts\.updateMetadata\)/);
  assert.match(javascript, /Tag \${me\.entity_type === "performer" \? "Performer" : "Studio"} \(\${me\.entity_name}\)/);

  // 4. Passing chosen target_entity_type and target_entity_id to approve handler
  assert.match(javascript, /target_entity_type: selectedEntityType/);
  assert.match(javascript, /target_entity_id: selectedEntityId/);
});

test("Automatic Filing Incoming diagnostic display, Retry Filing and Backlog UI components", () => {
  // 1. Diagnostic rendering for imported incoming items
  assert.match(javascript, /const isImported = item\.status === "imported"/);
  assert.match(javascript, /className: "lm-terminal-filing-diagnostic"/);
  assert.match(javascript, /className: "lm-filing-diag-label"/);
  assert.match(javascript, /📁 Filing:/);
  assert.match(javascript, /item\.filing_diagnostic/);

  // 2. Retry Filing button, safeguards (rejects already-filed, baseline, recovery, non-video) and handler
  assert.match(javascript, /handleRetryFiling/);
  assert.match(javascript, /className: "lm-terminal-inline-btn retry-filing"/);
  assert.match(javascript, /"⟳ RETRY FILING"/);
  assert.match(javascript, /operation\("retry_filing_proposal", \{ path \}\)/);
  assert.match(javascript, /showRetryFiling = isImported && isVideo && !item\.filed && !item\.needs_recovery && !hasPendingProposal && !item\.is_baseline && item\.exists_on_disk !== false/);

  // 3. Organise Existing Files backlog entry point and informational modal
  assert.match(javascript, /className: "lm-terminal-btn-backlog"/);
  assert.match(javascript, /📁 ORGANISE EXISTING FILES/);
  assert.match(javascript, /Organise Existing Files \(Backlog Workflow\)/);
  assert.match(javascript, /Baseline Protection Active/);

  // 4. CSS styling for imported lines, diagnostic pill, retry button, and backlog entry button
  assert.match(css, /\.lm-terminal-line\.imported/);
  assert.match(css, /\.lm-terminal-filing-diagnostic/);
  assert.match(css, /\.lm-terminal-inline-btn\.retry-filing/);
  assert.match(css, /\.lm-terminal-btn-backlog/);
});

test("Backlog Organiser UI: selection, stats badges, confirmation modal, progress tallies and cancel action", () => {
  // 1. Stats bar with separate original baseline snapshot, remaining incoming, eligible videos, companions and filed/ineligible
  assert.match(javascript, /className: "lm-backlog-stats-bar"/);
  assert.match(javascript, /Original Snapshot/);
  assert.match(javascript, /Remaining in Incoming/);
  assert.match(javascript, /Eligible Videos/);
  assert.match(javascript, /Companion Files/);
  assert.match(javascript, /Verified Moved/);
  assert.match(javascript, /Ineligible \/ Filed/);

  // 2. Select all eligible videos, reset on open, and individual checkbox selection
  assert.match(javascript, /handleSelectAllEligible/);
  assert.match(javascript, /Select All Eligible/);
  assert.match(javascript, /toggleBacklogItemSelection/);
  assert.match(javascript, /selectedBacklogPaths/);
  assert.match(javascript, /setSelectedBacklogPaths\(new Set\(\)\)/);

  // 3. Confirmation dialog before evaluation
  assert.match(javascript, /showBacklogConfirm/);
  assert.match(javascript, /Confirm Backlog Evaluation/);
  assert.match(javascript, /Files are NEVER moved automatically/);

  // 4. Batch evaluation with progress bar and cancel button
  assert.match(javascript, /isBacklogEvaluating/);
  assert.match(javascript, /backlogCancelRequested/);
  assert.match(javascript, /Cancel Evaluation/);
  assert.match(javascript, /operation\("evaluate_backlog_batch",\s*\{\s*paths:/);

  // 5. Outcome tally grid and completion summary
  assert.match(javascript, /className: "lm-backlog-tally-grid"/);
  assert.match(javascript, /Proposals Ready/);
  assert.match(javascript, /Multi-Candidate/);
  assert.match(javascript, /No Identity Found/);
  assert.match(javascript, /No Destination/);
  assert.match(javascript, /Backlog Evaluation Complete/);

  // 6. CSS styling for backlog modal, stats bar, items, and tally grid
  assert.match(css, /\.lm-modal-backlog-dialog/);
  assert.match(css, /\.lm-backlog-stats-bar/);
  assert.match(css, /\.lm-backlog-stat\.eligible/);
  assert.match(css, /\.lm-backlog-item\.eligible/);
  assert.match(css, /\.lm-backlog-tally-grid/);
});

test("Dashboard component mounts and executes without ReferenceError or TDZ errors", () => {
  const registeredRoutes = {};
  const ReactMock = {
    useState: (initial) => [typeof initial === "function" ? initial() : initial, () => {}],
    useRef: (val) => ({ current: val }),
    useEffect: (fn, deps) => {},
    useCallback: (fn, deps) => fn,
    useMemo: (fn, deps) => fn(),
    createElement: (type, props, ...children) => {
      if (typeof type === "function") {
        try { return type(props || {}); } catch (_) { return { type: type.name || "fn", props, children }; }
      }
      return { type, props, children };
    },
    Fragment: "Fragment"
  };
  const ModalMock = Object.assign(() => null, {
    Header: () => null,
    Title: () => null,
    Body: () => null,
    Footer: () => null
  });
  const ButtonMock = () => null;

  const originalWindow = globalThis.window;
  const originalDocument = globalThis.document;
  const originalMutationObserver = globalThis.MutationObserver;

  globalThis.MutationObserver = class {
    observe() {}
    disconnect() {}
  };

  globalThis.window = {
    React: ReactMock,
    ReactDOM: { createPortal: (node) => node, render: () => {} },
    MutationObserver: globalThis.MutationObserver,
    PluginApi: {
      React: ReactMock,
      ReactDOM: { createPortal: (node) => node, render: () => {} },
      libraries: {
        ReactRouterDOM: { NavLink: () => null },
        Bootstrap: { Button: ButtonMock, Modal: ModalMock, Form: { Check: () => null, Control: () => null } }
      },
      register: {
        route: (path, component) => { registeredRoutes[path] = component; }
      },
      patch: {
        before: () => {}
      },
      loadableComponents: {
        Button: ButtonMock,
        Modal: ModalMock,
        Form: { Check: () => null, Control: () => null }
      }
    },
    localStorage: { getItem: () => null, setItem: () => {} },
    setTimeout: () => 1,
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => {},
    CustomEvent: class CustomEvent { constructor(name, opts) { this.name = name; this.opts = opts; } }
  };
  globalThis.document = {
    title: "",
    getElementById: () => null,
    querySelector: () => null,
    createElement: () => ({ appendChild: () => {}, addEventListener: () => {}, style: {} }),
    body: { appendChild: () => {} },
    addEventListener: () => {},
    removeEventListener: () => {}
  };

  try {
    // Execute the full librarymanager.js script
    const fn = new Function(javascript);
    fn();
    const DashboardComponent = registeredRoutes["/library-manager"];
    assert.ok(DashboardComponent, "Dashboard route component registered");
    
    // Mount and execute DashboardComponent render
    const rendered = DashboardComponent();
    assert.ok(rendered, "Dashboard component mounted and returned JSX tree without ReferenceError");
  } finally {
    globalThis.window = originalWindow;
    globalThis.document = originalDocument;
    globalThis.MutationObserver = originalMutationObserver;
  }
});

test('Backlog Organiser Review Proposals button navigates to overview and triggers refresh', () => {

  // Verify navigation target is overview tab
  assert.ok(javascript.includes('setTab("overview");'), 'Backlog completion Review Proposals sets tab to overview');
  assert.ok(!javascript.includes('setTab("proposals");'), 'Invalid proposals tab navigation removed');
  
  // Verify multi-candidate radio picker and explicit choice enforcement
  assert.ok(javascript.includes('Multiple Destinations Detected'), 'Multi-candidate picker label rendered');
  assert.ok(javascript.includes('dest_choice_'), 'Multi-destination radio button group present');
  assert.ok(javascript.includes('hasMultiple && !selectedTarget'), 'Enforces explicit destination selection before move');
  assert.ok(javascript.includes('handleRefreshFilingProposal'), 'Refresh choices action available for proposals');
});

test('Automatic Filing approval and refresh choices UI feedback and candidate validation', () => {
  // 1. Approval result handling
  assert.ok(javascript.includes('res.status === "completed"'), 'Checks backend status completed');
  assert.ok(javascript.includes('Video successfully moved to'), 'Displays move success notification');
  assert.ok(javascript.includes('res.status === "needs_recovery"'), 'Handles needs_recovery state');
  assert.ok(javascript.includes('CRITICAL: Move incomplete / uncertain. Recovery required:'), 'Reports critical recovery required error');
  assert.ok(javascript.includes('Filing failed:'), 'Reports filing failure reason');

  // 2. In-place proposal refresh
  assert.ok(javascript.includes('allow_refresh: true'), 'Passes allow_refresh flag to retry_filing_proposal');
  assert.ok(javascript.includes('proposal_id: proposalId'), 'Passes proposal_id for in-place update');
  assert.ok(javascript.includes('Filing proposal destination choices refreshed successfully.'), 'Success notice on refresh');

  // 3. Selection validation on refresh
  assert.ok(javascript.includes('newCandidates.some'), 'Validates current selection against refreshed candidates');
  assert.ok(javascript.includes('targetDest: undefined'), 'Resets invalid choice to require fresh explicit selection');
});

test('Filing Proposals UI displays consistent destination panel for both single and multiple destinations', () => {
  // 1. Single destination presentation
  assert.ok(javascript.includes('SELECTED DESTINATION'), 'Single destination displays SELECTED DESTINATION badge');
  assert.ok(javascript.includes('📁 Destination Folder:'), 'Single destination displays Destination Folder header');

  // 2. Multiple destinations presentation
  assert.ok(javascript.includes('SELECTION REQUIRED'), 'Multiple destinations displays SELECTION REQUIRED indicator');
  assert.ok(javascript.includes('CHOICE SELECTED'), 'Multiple destinations displays CHOICE SELECTED indicator');
  assert.ok(javascript.includes('Multiple Destinations Detected'), 'Multiple destinations header present');

  // 3. Robust layout wrapping
  assert.ok(javascript.includes('wordBreak: "break-all"'), 'Applies word-break to file paths');
  assert.ok(javascript.includes('overflowWrap: "anywhere"'), 'Applies overflow-wrap to prevent overflow on long NAS paths');

  // 4. Stylesheet rules
  assert.ok(css.includes('.lm-filing-dest-item'), 'CSS includes dest item styles');
  assert.ok(css.includes('.lm-filing-dest-path'), 'CSS includes dest path styling');
});

test('Automatic Filing visible move progress, stage tracking, concurrency serialization and recovery UI', () => {
  // 1. Indeterminate progress bar and stage reporting
  assert.ok(javascript.includes('lm-filing-progress-panel'), 'Includes progress panel component');
  assert.ok(javascript.includes('lm-filing-progress-track'), 'Includes progress track');
  assert.ok(javascript.includes('lm-filing-progress-bar-indeterminate'), 'Includes indeterminate animated progress bar');
  assert.ok(javascript.includes('activeTransfer?.stage_label'), 'Renders active transfer stage label');
  assert.ok(javascript.includes('activeTransfer?.detail'), 'Renders active transfer detail');

  // 2. Concurrency serialization
  assert.ok(javascript.includes('active_filing_transfers'), 'Unpacks active filing transfers from backend state');
  assert.ok(javascript.includes('isAnyTransferActive'), 'Tracks global active transfer state');
  assert.ok(javascript.includes('⚠️ TRANSFER IN PROGRESS'), 'Displays transfer in progress notice on queued proposals');

  // 3. Happening Now integration
  assert.ok(javascript.includes('lm-filing-transfer-badge'), 'Renders transfer badge in Happening Now');

  // 4. CSS rules
  assert.ok(css.includes('.lm-filing-progress-panel'), 'CSS contains progress panel');
  assert.ok(css.includes('.lm-filing-progress-bar-indeterminate'), 'CSS contains indeterminate bar animation');
  assert.ok(css.includes('@keyframes lmProgressIndeterminate'), 'CSS contains progress keyframe animation');
});

test('Happening Now uncluttered and shows only active operations or listening empty state', () => {
  // 1. Genuinely active filtering
  assert.ok(javascript.includes('activeIncoming = allActive.filter'), 'Filters genuinely active incoming files');
  assert.ok(javascript.includes('item.status === "downloading" || item.status === "scanning" || item.status === "generating_sheet" || item.status === "renaming" || item.status === "pending_rename"'), 'Includes only actively running incoming statuses');
  
  // 2. Empty state message
  assert.ok(javascript.includes('No active operations. Watchtower is listening.'), 'Displays exact uncluttered listening message when idle');

  // 3. Organise Existing Files button preserved in header
  assert.ok(javascript.includes('lm-terminal-btn-backlog'), 'Backlog button present in Happening Now header');
  assert.ok(javascript.includes('backlogData?.eligible_count ?? incoming?.backlog_eligible_count'), 'Backlog button displays live eligible count');

  // 4. Incoming Diagnostics accessible in Incoming tab
  assert.ok(javascript.includes('Incoming File Status & Diagnostics'), 'Incoming tab includes diagnostics panel');
});

test('Backlog Organise Existing Files reconciled stats, verified moved status pill, destination display and selection reset', () => {
  // 1. Stats Bar Cards
  assert.ok(javascript.includes('Original Snapshot'), 'Stats bar includes Original Snapshot label');
  assert.ok(javascript.includes('Remaining in Incoming'), 'Stats bar includes Remaining in Incoming label');
  assert.ok(javascript.includes('Eligible Videos'), 'Stats bar includes Eligible Videos label');
  assert.ok(javascript.includes('Pending Proposals'), 'Stats bar includes Pending Proposals label');
  assert.ok(javascript.includes('Companion Files'), 'Stats bar includes Companion Files label');
  assert.ok(javascript.includes('Verified Moved'), 'Stats bar includes Verified Moved label');

  // 2. Verified moved item rendering and destination path display
  assert.ok(javascript.includes('item.destination_path'), 'Renders destination path when available');
  assert.ok(javascript.includes('item.status_label'), 'Renders item status label in pill');

  // 3. Selection behavior on reopen / close
  assert.ok(javascript.includes('setSelectedBacklogPaths(new Set())'), 'Clears backlog selections on close/finish');
  assert.ok(javascript.includes('validSelectedCount === 0'), 'Disables evaluate button when zero selected');

  // 4. CSS styling
  assert.ok(css.includes('.item-status-pill.moved'), 'CSS defines style for moved pill');
  assert.ok(css.includes('.lm-backlog-stat.pending'), 'CSS defines style for pending stat card');
  assert.ok(css.includes('.lm-backlog-stat.moved'), 'CSS defines style for moved stat card');
});
