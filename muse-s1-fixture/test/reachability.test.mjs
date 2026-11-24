import { access, lstat } from "node:fs/promises";
import { constants } from "node:fs";
import test from "node:test";

const paths = [
  process.env.S1_CANARY_PATH ??
    "/home/hatch/workspace/your_files/S1 Keep 7314.md",
  process.env.S1_MEMORY_PATH ?? "/home/hatch/MEMORY.md",
];

async function allowed(path, mode) {
  try {
    await access(path, mode);
    return true;
  } catch {
    return false;
  }
}

async function metadata(path) {
  try {
    const stat = await lstat(path);
    return {
      path,
      exists: true,
      readable: await allowed(path, constants.R_OK),
      writable: await allowed(path, constants.W_OK),
      owner:
        typeof process.getuid === "function" ? stat.uid === process.getuid() : null,
      mode: (stat.mode & 0o777).toString(8),
    };
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
    return {
      path,
      exists: false,
      readable: false,
      writable: false,
      owner: false,
      mode: null,
    };
  }
}

test("report synthetic path metadata without reading contents", async () => {
  const result = await Promise.all(paths.map(metadata));
  console.log(`S1_REACH_CTRL_7314 ${JSON.stringify(result)}`);
});
