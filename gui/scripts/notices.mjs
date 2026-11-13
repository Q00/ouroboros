// Regenerate public/THIRD_PARTY_NOTICES.txt from the packages that actually
// end up in the bundle:  node scripts/notices.mjs
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "vite";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const bundled = new Set();

await build({
  root,
  logLevel: "silent",
  build: { write: false },
  plugins: [
    {
      name: "collect-bundled-packages",
      generateBundle(_options, bundle) {
        for (const chunk of Object.values(bundle)) {
          for (const id of Object.keys(chunk.modules ?? {})) {
            const match = id.match(/node_modules\/((?:@[^/]+\/)?[^/]+)/g);
            if (match) bundled.add(match[match.length - 1].replace("node_modules/", ""));
          }
        }
      },
    },
  ],
});

// Packages published without a license file, mapped to a sibling package by
// the same author whose license file states the same terms.
const SAME_TERMS_AS = { "react-remove-scroll-bar": "react-remove-scroll" };

function licenseText(name) {
  const dir = join(root, "node_modules", name);
  const file = readdirSync(dir).find((entry) => /^licen[sc]e/i.test(entry));
  return file && existsSync(join(dir, file)) ? readFileSync(join(dir, file), "utf8").trim() : null;
}

const sections = [];
for (const name of [...bundled].sort()) {
  const dir = join(root, "node_modules", name);
  const manifest = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
  const sibling = SAME_TERMS_AS[name];
  const text =
    licenseText(name) ??
    (sibling
      ? `(The package ships no license file. Its manifest declares ${manifest.license}; the text below is from ${sibling}, by the same author.)\n\n${licenseText(sibling)}`
      : `(${manifest.license}; the package ships no license file)`);
  const home = typeof manifest.repository === "string" ? manifest.repository : (manifest.repository?.url ?? manifest.homepage ?? "");
  sections.push(`${name} ${manifest.version}\nLicense: ${manifest.license}\n${home.replace(/^git\+/, "")}\n\n${text}`);
}

const header = `THIRD-PARTY NOTICES — Ouroboros console (ouroboros gui)

The built page in src/ouroboros/gui/static bundles the open-source packages
below. Each is distributed under the license reproduced with it. The console
ships no web fonts and no third-party images; it uses the fonts installed on
the viewer's system.

Build tools (Vite, TypeScript, @vitejs/plugin-react and their dependencies)
are used to produce the bundle and are not part of it.
`;

const rule = `\n\n${"=".repeat(78)}\n\n`;
writeFileSync(join(root, "public", "THIRD_PARTY_NOTICES.txt"), `${header}${rule}${sections.join(rule)}\n`);
console.log(`${bundled.size} bundled packages`);
