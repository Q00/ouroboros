const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const habitCli = path.resolve(__dirname, "../bin/habit");

function runHabit(cwd, args) {
  return spawnSync(process.execPath, [habitCli, ...args], {
    cwd,
    encoding: "utf8",
  });
}

function makeWorkingDirectory(t) {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "habit-test-"));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  return cwd;
}

test("an unknown habit id fails without changing stored habits", (t) => {
  const cwd = makeWorkingDirectory(t);
  const habits = '[{"id":1,"text":"Drink water","checked":false}]';
  fs.writeFileSync(path.join(cwd, "habits.json"), habits);

  const result = runHabit(cwd, ["check", "999"]);

  assert.equal(result.status, 1);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, "Error: habit not found\n");
  assert.equal(fs.readFileSync(path.join(cwd, "habits.json"), "utf8"), habits);
});

test("an unsupported command fails without changing stored habits", (t) => {
  const cwd = makeWorkingDirectory(t);
  const habits = '[{"id":1,"text":"Drink water","checked":false}]';
  fs.writeFileSync(path.join(cwd, "habits.json"), habits);

  const result = runHabit(cwd, ["delete", "1"]);

  assert.equal(result.status, 1);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, "Error: unsupported command\n");
  assert.equal(fs.readFileSync(path.join(cwd, "habits.json"), "utf8"), habits);
});
