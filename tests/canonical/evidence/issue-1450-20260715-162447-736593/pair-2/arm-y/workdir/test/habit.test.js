const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const habitBin = path.resolve(__dirname, "..", "bin", "habit");

function makeTempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "habit-test-"));
}

function runHabit(cwd, args) {
  return spawnSync(process.execPath, [habitBin, ...args], {
    cwd,
    encoding: "utf8"
  });
}

{
  const cwd = makeTempDir();

  const add = runHabit(cwd, ["add", "Drink water"]);
  assert.strictEqual(add.status, 0);
  assert.strictEqual(add.stdout, "Added habit 1: Drink water\n");
  assert.strictEqual(add.stderr, "");
  assert.strictEqual(
    fs.readFileSync(path.join(cwd, "habits.json"), "utf8"),
    '[{"id":1,"text":"Drink water","checked":false}]'
  );

  const list = runHabit(cwd, ["list"]);
  assert.strictEqual(list.status, 0);
  assert.strictEqual(list.stdout, "[ ] 1. Drink water\n");
  assert.strictEqual(list.stderr, "");

  const check = runHabit(cwd, ["check", "1"]);
  assert.strictEqual(check.status, 0);
  assert.strictEqual(check.stdout, "Checked habit 1: Drink water\n");
  assert.strictEqual(check.stderr, "");
  assert.strictEqual(
    fs.readFileSync(path.join(cwd, "habits.json"), "utf8"),
    '[{"id":1,"text":"Drink water","checked":true}]'
  );
}

{
  const cwd = makeTempDir();
  runHabit(cwd, ["add", "Drink water"]);
  const before = fs.readFileSync(path.join(cwd, "habits.json"), "utf8");

  const result = runHabit(cwd, ["check", "999"]);
  assert.strictEqual(result.status, 1);
  assert.strictEqual(result.stdout, "");
  assert.strictEqual(result.stderr, "Error: habit not found\n");
  assert.strictEqual(fs.readFileSync(path.join(cwd, "habits.json"), "utf8"), before);
}

{
  const cwd = makeTempDir();
  runHabit(cwd, ["add", "Drink water"]);
  const before = fs.readFileSync(path.join(cwd, "habits.json"), "utf8");

  const result = runHabit(cwd, ["delete", "1"]);
  assert.strictEqual(result.status, 1);
  assert.strictEqual(result.stdout, "");
  assert.strictEqual(result.stderr, "Error: unsupported command\n");
  assert.strictEqual(fs.readFileSync(path.join(cwd, "habits.json"), "utf8"), before);
}
