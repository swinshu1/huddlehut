import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const markerPath = "/tmp/muse-s4-state-6174.txt";

let actual = "MISSING";
try {
  actual = readFileSync(markerPath, "utf8").trim();
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}

test("disposable state", () => {
  assert.equal(actual, "S4_READY_6174");
});
