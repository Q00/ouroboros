/**
 * Ambient type declaration for the OpenCode plugin SDK.
 *
 * OpenCode loads plugins at runtime from its own SDK; the package is not
 * published to npm.  This declaration provides the {@link Plugin} type so
 * `bunx tsc --noEmit` passes without a real `@opencode-ai/plugin`
 * dependency.
 */
declare module "@opencode-ai/plugin" {
  /** A hook callback receives arbitrary args from the OpenCode runtime. */
  type HookFn = (...args: any[]) => any | Promise<any>

  /** Map of lifecycle hook names → callbacks returned by the plugin factory. */
  type HookMap = Record<string, HookFn>

  /** Plugin factory — called once at load time, returns hook map. */
  export type Plugin = (ctx: any) => HookMap | Promise<HookMap>
}
