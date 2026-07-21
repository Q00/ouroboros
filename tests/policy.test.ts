import { describe, expect, it } from "vitest";

describe("input policy", () => {
  it("normalizes a safe review request into a bounded immutable payload", async () => {
    const policy = await import("../src/policy.js").catch(() => ({}));

    expect(policy).toHaveProperty("normalizeInput");
    if (!("normalizeInput" in policy) || typeof policy.normalizeInput !== "function") {
      return;
    }

    expect(
      policy.normalizeInput({
        objective: "  Validate the onboarding acceptance criteria.  ",
        candidate: "The delivered flow includes login and a recovery path.",
      }),
    ).toEqual({
      objective: "Validate the onboarding acceptance criteria.",
      candidate: "The delivered flow includes login and a recovery path.",
    });
  });

  it.each([
    ["secret label", "API_KEY=super-secret-value-123456"],
    ["bearer token", "Bearer abcdefghijklmnopqrstuvwxyz123456"],
    ["OpenAI-style token", "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz1234567890"],
    ["GitHub-style token", "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890"],
    ["AWS access key", "AKIAIOSFODNN7EXAMPLE"],
    ["JWT", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturevalue"],
    ["PEM private key", "-----BEGIN PRIVATE KEY-----"],
    ["encrypted PEM private key", "-----BEGIN ENCRYPTED PRIVATE KEY-----"],
    ["email address", "Contact jane@example.com for details"],
    ["international phone", "Call +61 412 345 678 for access"],
    ["Korean phone", "담당자 010-1234-5678"],
    ["Korean resident identifier", "식별번호 900101-1234567"],
    ["payment card number", "Use card 4111 1111 1111 1111"],
    ["URL", "See https://example.com/private/spec"],
    ["host-form URL", "See www.example.com/private/spec"],
    ["fenced code", "```ts\nconst secret = process.env.KEY\n```"],
    ["raw code", "function deploy() { return process.env.TOKEN; }"],
    ["Python code", "def deploy():\n return True"],
    ["Go code", "package main\nfunc main() {}"],
    ["Rust code", "fn main() { println!(\"hello\"); }"],
    ["Java code", "public class Main { static void run() {} }"],
  ])("rejects %s before any upstream call", async (_label, candidate) => {
    const { normalizeInput } = await import("../src/policy.js");

    expect(() =>
      normalizeInput({ objective: "Review delivery", candidate }),
    ).toThrowError(/rejected/i);
  });

  it("rejects content beyond the 64 KiB absolute ceiling", async () => {
    const { normalizeInput } = await import("../src/policy.js");

    expect(() =>
      normalizeInput({ objective: "Review", candidate: "x".repeat(65_537) }),
    ).toThrowError(/64 KiB/i);
  });

  it("rejects content beyond the default 20,000-character analysis bound", async () => {
    const { normalizeInput } = await import("../src/policy.js");

    expect(() =>
      normalizeInput({ objective: "Review", candidate: "x".repeat(20_001) }),
    ).toThrowError(/20,000/i);
  });

  it("returns a new frozen value instead of retaining the caller object", async () => {
    const { normalizeInput } = await import("../src/policy.js");
    const input = { objective: "Review", candidate: "Safe prose." };
    const normalized = normalizeInput(input);

    expect(normalized).not.toBe(input);
    expect(Object.isFrozen(normalized)).toBe(true);
  });
});
