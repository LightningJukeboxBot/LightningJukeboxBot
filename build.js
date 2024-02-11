#!/usr/bin/env bun

await Bun.build({
  entrypoints: ["./web/src/popup.ts"],
  outdir: "web/",
  target: "browser",
});
