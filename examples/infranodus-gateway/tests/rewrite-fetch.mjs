const nativeFetch = globalThis.fetch;
const testBase = process.env.OIG_TEST_BASE;

if (!testBase) {
  throw new Error("OIG_TEST_BASE is required by the test-only fetch preload");
}

globalThis.fetch = (input, init) => {
  const original = new URL(
    typeof input === "string" || input instanceof URL ? input : input.url,
  );
  const rewritten = new URL(`${original.pathname}${original.search}`, testBase);
  return nativeFetch(rewritten, init);
};
