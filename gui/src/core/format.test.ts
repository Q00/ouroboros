// Run with `bun test` in gui/ (excluded from the page's type-check and bundle).
import { describe, expect, test } from "bun:test";

import { shortPath } from "./format";

// What a macOS console reports in /api/overview: /tmp and /var are links into /private.
const MAC: [string, string][] = [
  ["/private/tmp", "/tmp"],
  ["/private/var", "/var"],
];

describe("shortPath with the home folder", () => {
  test("a path under the home reads as ~", () => {
    expect(shortPath("/Users/ada/projects/md2csv", { home: "/Users/ada" })).toBe("~/projects/md2csv");
    expect(shortPath("/Users/ada", { home: "/Users/ada" })).toBe("~");
  });

  test("a home outside /Users is abbreviated too (an isolated HOME)", () => {
    expect(shortPath("/srv/demo-home/projects/md2csv", { home: "/srv/demo-home" })).toBe("~/projects/md2csv");
  });

  test("a home given in its /tmp form matches the /private/tmp path the console resolved", () => {
    expect(shortPath("/private/tmp/demo/home/projects/live", { home: "/tmp/demo/home", aliases: MAC })).toBe("~/projects/live");
  });

  test("a home given resolved matches a path in its /tmp form", () => {
    expect(shortPath("/tmp/demo/home/projects/live", { home: "/private/tmp/demo/home", aliases: MAC })).toBe("~/projects/live");
  });

  test("a home trailing slash changes nothing", () => {
    expect(shortPath("/srv/h/p", { home: "/srv/h/" })).toBe("~/p");
  });

  test("only whole path segments match the home", () => {
    expect(shortPath("/Users/adam/x", { home: "/Users/ada" })).toBe("/Users/adam/x");
    expect(shortPath("/srv/h2/p", { home: "/srv/h" })).toBe("/srv/h2/p");
  });

  test("with a known home, another user's folder is not called ~", () => {
    expect(shortPath("/Users/other/x", { home: "/tmp/isolated" })).toBe("/Users/other/x");
  });

  test("a home of / abbreviates nothing", () => {
    expect(shortPath("/etc/hosts", { home: "/" })).toBe("/etc/hosts");
  });
});

describe("shortPath with symlinked roots", () => {
  test("a resolved /private path reads by the link it is reached by", () => {
    expect(shortPath("/private/tmp/x/y", { home: "/Users/ada", aliases: MAC })).toBe("/tmp/x/y");
    expect(shortPath("/private/var/folders/ab/T/run", { home: "/Users/ada", aliases: MAC })).toBe("/var/folders/ab/T/run");
  });

  test("a /private path that is not under an aliased root is left alone", () => {
    expect(shortPath("/private/etc/hosts", { home: "/Users/ada", aliases: MAC })).toBe("/private/etc/hosts");
    expect(shortPath("/private/tmpfoo/x", { home: "/Users/ada", aliases: MAC })).toBe("/private/tmpfoo/x");
  });

  test("without aliases (Linux) /private stays as it is", () => {
    expect(shortPath("/private/tmp/x", { home: "/home/ada" })).toBe("/private/tmp/x");
  });

  test("the longest alias wins, in the order the server sends them", () => {
    const aliases: [string, string][] = [["/Volumes/data/ada", "/Users/ada"], ...MAC];
    expect(shortPath("/Volumes/data/ada/code", { home: "/Users/ada", aliases })).toBe("~/code");
  });
});

describe("shortPath before the home is known", () => {
  test("/Users/<name> and /home/<name> still read as ~", () => {
    expect(shortPath("/Users/ada/code")).toBe("~/code");
    expect(shortPath("/home/ada")).toBe("~");
  });

  test("an empty path is empty", () => {
    expect(shortPath("")).toBe("");
    expect(shortPath(null)).toBe("");
    expect(shortPath(undefined, { home: "/Users/ada" })).toBe("");
  });
});

describe("shortPath for long paths", () => {
  test("the middle of a long path is elided, keeping its ends", () => {
    const long = "/opt/very/long/folder/structure/that/keeps/going/and/going/project";
    expect(shortPath(long, { home: "/Users/ada" })).toBe("/opt/…/going/project");
  });

  test("a long path under the home is short once it starts with ~", () => {
    const home = "/tmp/claude-501/-Users-someone-project/43054418-fa24-434f-8028/scratchpad/home";
    expect(shortPath(`/private${home}/projects/live`, { home, aliases: MAC })).toBe("~/projects/live");
  });
});
