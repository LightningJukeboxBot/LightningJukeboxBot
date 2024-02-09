#!/usr/bin/env bun

await Bun.build({
  entrypoints: ["./web/src/index.ts"],
  outdir: "web/",
  target: "browser",
});
