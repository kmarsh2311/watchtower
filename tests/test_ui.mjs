import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const javascript = readFileSync(new URL("../librarymanager.js", import.meta.url), "utf8");
const css = readFileSync(new URL("../librarymanager.css", import.meta.url), "utf8");
const manifest = readFileSync(new URL("../librarymanager.yml", import.meta.url), "utf8");

test("Command Centre presents unresolved changes as actions, not completed work", () => {
  assert.match(javascript, /NEEDS ATTENTION/);
  assert.match(javascript, /Video deletion detected/);
  assert.match(javascript, /DELETED — REVIEW/);
  assert.doesNotMatch(javascript, /RECENTLY COMPLETED/);
  assert.doesNotMatch(javascript, /Show \d+ changes? to resolve/);
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
  assert.match(javascript, /if \(isExiting \|\| indexing\) return/);
  assert.match(javascript, /className: "lm-wizard-close-btn",[\s\S]*disabled: indexing/);
  assert.match(javascript, /if \(!indexing && s\.num < step\) goToStep/);
  assert.match(javascript, /disabled: indexing \|\| \(step === 4 && !inventoryComplete\)/);
  assert.match(javascript, /Indexing your library… Please keep this setup window open/);
  assert.match(javascript, /indexError \? "↻ Try Again"/);
});
