// Stills and short clips of one composition from the current code, with a single bundle:
//   node <scripts>/stills.mjs <Comp> <props.json|-> <outDir> <frame|a-b> [frame|a-b…] [--scale=0.5]
// frame -> <outDir>/f<N>.png (full size); a-b -> <outDir>/f<a>-<b>.mp4 (frames a…b with sound, --scale, default 0.5).
// Prints each written file on its own line.
// public/ (≈ 8 GB) goes into the bundle as symlinks, not copies: the bundle is ~30 MB and builds in < 1 s.
// fs.rmSync removes the symlinks themselves, never what they point to.
// Запускается с cwd = Remotion-проект; лежит в плагине, где нет node_modules, поэтому модули Remotion — из проекта.
import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const req = createRequire(path.resolve("package.json"));
const load = (name) => import(pathToFileURL(req.resolve(name)).href);
const { bundle } = await load("@remotion/bundler");
const { openBrowser, renderMedia, renderStill, selectComposition } = await load("@remotion/renderer");

const args = process.argv.slice(2);
const scale = Number((args.find((a) => a.startsWith("--scale=")) || "--scale=0.5").slice(8));
const [comp, propsFile, outDir, ...items] = args.filter((a) => !a.startsWith("--"));
if (!items.length) {
  console.error("node stills.mjs <Comp> <props.json|-> <outDir> <frame|a-b> [...] [--scale=0.5]");
  process.exit(2);
}
const bad = items.filter((it) => !/^\d+(-\d+)?$/.test(it));
if (bad.length) {
  console.error(`кадры — числа или отрезки a-b, каждый отдельным аргументом; не понял: ${bad.map((b) => JSON.stringify(b)).join(", ")}`);
  process.exit(2);
}
const inputProps = propsFile && propsFile !== "-" ? JSON.parse(fs.readFileSync(propsFile, "utf8")) : {};
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "reel-stills-"));
const pub = path.join(tmp, "public");
fs.mkdirSync(pub);
for (const f of fs.readdirSync("public")) fs.symlinkSync(path.resolve("public", f), path.join(pub, f));
let browser;
try {
  const serveUrl = await bundle({ entryPoint: path.resolve("src/index.ts"), publicDir: pub, outDir: path.join(tmp, "bundle"),
    onSymlinkDetected: () => {} });
  const composition = await selectComposition({ serveUrl, id: comp, inputProps });
  const last = composition.durationInFrames - 1;
  browser = await openBrowser("chrome");
  fs.mkdirSync(outDir, { recursive: true });
  for (const it of items) {
    const m = it.match(/^(\d+)-(\d+)$/);
    if (m) {
      const a = Math.min(+m[1], last), b = Math.min(Math.max(+m[2], a), last);
      const out = path.join(outDir, `f${a}-${b}.mp4`);
      await renderMedia({ composition, serveUrl, inputProps, codec: "h264", crf: 23, scale, frameRange: [a, b], outputLocation: out,
        puppeteerInstance: browser });
      console.log(out);
    } else {
      const f = Math.min(Number(it), last);
      const out = path.join(outDir, `f${f}.png`);
      await renderStill({ composition, serveUrl, inputProps, frame: f, output: out, puppeteerInstance: browser,
        timeoutInMilliseconds: 120000 });
      console.log(out);
    }
  }
} finally {
  await browser?.close({ silent: true });
  fs.rmSync(tmp, { recursive: true, force: true });
}
