export interface GatewayInput {
  objective: string;
  candidate: string;
}

const ABSOLUTE_BYTES = 64 * 1024;
const DEFAULT_CHARACTERS = 20_000;

const BLOCKED_PATTERNS: ReadonlyArray<readonly [string, RegExp]> = [
  ["secret", /\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*\S+/iu],
  ["bearer token", /\bbearer\s+[a-z0-9._~+/-]{20,}/iu],
  ["provider credential", /\b(?:sk-(?:proj-)?[a-z0-9_-]{20,}|gh[pousr]_[a-z0-9]{20,}|AKIA[0-9A-Z]{16})\b/iu],
  ["JWT", /\beyJ[a-z0-9_-]{5,}\.[a-z0-9_-]{5,}\.[a-z0-9_-]{5,}\b/iu],
  ["private key", /-----BEGIN (?:(?:RSA|EC|OPENSSH|ENCRYPTED) )?PRIVATE KEY-----/iu],
  ["email address", /\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+\b/iu],
  ["international phone number", /\+\d{1,3}[ .-]?(?:\d[ .-]?){8,12}\d/u],
  ["Korean phone number", /\b01[016789][ .-]?\d{3,4}[ .-]?\d{4}\b/u],
  ["Korean resident identifier", /\b\d{6}[ -]?[1-4]\d{6}\b/u],
  ["payment card number", /\b(?:\d[ -]?){12,18}\d\b/u],
  ["URL", /(?:\b(?:https?|ftp):\/\/\S+|\bwww\.[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:\/\S*)?)/iu],
  ["fenced code", /```/u],
  ["raw code", /(?:\bfunction\s+[a-z_$][\w$]*\s*\(|\b(?:const|let|var)\s+[a-z_$][\w$]*\s*=|\bimport\s+.+\s+from\s+|\bprocess\.env\b|\bdef\s+[a-z_]\w*\s*\([^)]*\)\s*:|\bpackage\s+main\b|\bfunc\s+[a-z_]\w*\s*\(|\bfn\s+[a-z_]\w*\s*\(|\bpublic\s+(?:final\s+)?class\s+[a-z_]\w*)/iu],
];

export class PolicyError extends Error {
  readonly code = "INPUT_REJECTED";

  constructor(reason: string) {
    super(`Input rejected: ${reason}`);
    this.name = "PolicyError";
  }
}

function normalizeText(value: string, field: string): string {
  if (typeof value !== "string") {
    throw new PolicyError(`${field} must be text`);
  }

  const normalized = value
    .normalize("NFKC")
    .replace(/\r\n?/gu, "\n")
    .replace(/[\t ]+/gu, " ")
    .replace(/ *\n */gu, "\n")
    .trim();

  if (!normalized) {
    throw new PolicyError(`${field} is empty`);
  }

  for (const [label, pattern] of BLOCKED_PATTERNS) {
    if (pattern.test(normalized)) {
      throw new PolicyError(`${label} content is not allowed`);
    }
  }

  return normalized;
}

export function normalizeInput(input: GatewayInput): GatewayInput {
  const rawBytes = new TextEncoder().encode(
    `${input.objective}\u0000${input.candidate}`,
  ).byteLength;
  if (rawBytes > ABSOLUTE_BYTES) {
    throw new PolicyError("payload exceeds the 64 KiB absolute ceiling");
  }

  const objective = normalizeText(input.objective, "objective");
  const candidate = normalizeText(input.candidate, "candidate");
  if (objective.length + candidate.length > DEFAULT_CHARACTERS) {
    throw new PolicyError("payload exceeds the 20,000-character analysis bound");
  }

  return Object.freeze({ objective, candidate });
}
