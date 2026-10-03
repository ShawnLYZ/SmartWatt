import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const BUILD = resolve(__dirname, "../../server/static");
const SRC = resolve(__dirname, "../src");

/**
 * Hosts a page may reference. Everything else is a build failure.
 *
 * Each entry tolerates a PROTOCOL-RELATIVE form as well as an explicit
 * `http:`/`https:` one, because `EXTERNAL` below now matches both and an
 * entry anchored at `^https?:` would stop recognising its own host the
 * moment the reference lost its scheme.
 */
const ALLOWED = [
  /^(https?:)?\/\/(www\.)?w3\.org/,
  /^(https?:)?\/\/127\.0\.0\.1/,
  // React 19's production bundle builds this string in its minified error
  // handler ("https://react.dev/errors/" + code) to print a decoder link
  // for a human reading the console. It is never fetched by the app.
  /^(https?:)?\/\/react\.dev\/errors\//,
  // Tailwind CSS v4 stamps its MIT-license banner, `/*! tailwindcss v4.x
  // | MIT License | https://tailwindcss.com */`, at the top of the
  // generated CSS. An inert attribution comment, never fetched.
  /^(https?:)?\/\/tailwindcss\.com\/?$/,
  // Ruling AE: `prop-types` (bundled transitively via recharts ->
  // react-smooth / react-transition-group) builds this URL into the
  // literal text of a misuse-guard Error, thrown only if application code
  // calls a PropTypes validator directly with the wrong internal secret --
  // nothing here does. Dead text in a minified bundle, never fetched,
  // never a resource load. Anchored to the full path rather than the
  // host: unlike react.dev/tailwindcss.com (first-party, purpose-built
  // domains), fb.me is a generic URL shortener, so a host-only entry
  // would wave through any future shortened link.
  /^(https?:)?\/\/fb\.me\/use-check-prop-types$/,
];

/**
 * An external reference, with or without a scheme.
 *
 * `//fonts.googleapis.com/css?family=...` is a PROTOCOL-RELATIVE URL: the
 * browser fetches it over the page's own scheme, so it is every bit the
 * CDN load a `https://` one is -- and the old `https?:\/\/` scan walked
 * straight past it. One such link is all it takes to turn venue Wi-Fi
 * failure into a broken demonstration, which is the whole reason this
 * check is a test rather than a code-review convention.
 *
 * Two alternatives rather than an optional scheme, on purpose:
 *
 *  - the SCHEMED branch is character-for-character the original scan, so
 *    nothing it used to catch is quietly dropped -- including a dotless
 *    host like `http://intranet/font.woff2`, which a "host must contain a
 *    dot" rule would wave through;
 *  - the SCHEME-LESS branch does require a dotted host, because `//` on
 *    its own opens every line comment in the scanned source, and
 *    `// like this` is not a finding.
 */
const EXTERNAL =
  /https?:\/\/[^\s"'`)\\]+|\/\/[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+(?::\d+)?(?:[/?#][^\s"'`)\\]*)?/g;

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) out.push(...walk(path));
    else out.push(path);
  }
  return out;
}

function externalRefs(text: string): string[] {
  // `EXTERNAL` is a module-level /g regex, so `.match` (not `.exec`) is
  // used deliberately: it ignores and does not advance `lastIndex`, which
  // a shared /g regex would otherwise carry between files.
  const matches = text.match(EXTERNAL) ?? [];
  return matches.filter((url) => !ALLOWED.some((ok) => ok.test(url)));
}

describe("no external assets", () => {
  it("counts a protocol-relative URL as external", () => {
    // The exact form the previous scan walked past. A browser fetches
    // this over the page's own scheme; it is a CDN load like any other.
    expect(
      externalRefs('<link rel="stylesheet" href="//fonts.googleapis.com/css?family=X">'),
    ).toEqual(["//fonts.googleapis.com/css?family=X"]);
    expect(externalRefs("@import url(//cdn.example.com/x.css);")).toEqual([
      "//cdn.example.com/x.css",
    ]);
    // The explicit form still lands, unchanged -- including a host with
    // no dot in it, which the scheme-less branch deliberately does not
    // try to recognise.
    expect(externalRefs('src="https://cdn.example.com/a.js"')).toEqual([
      "https://cdn.example.com/a.js",
    ]);
    expect(externalRefs('src="http://intranet/font.woff2"')).toEqual([
      "http://intranet/font.woff2",
    ]);
    // Allowlisted hosts stay allowed with or without a scheme.
    expect(externalRefs("//react.dev/errors/418")).toEqual([]);
    expect(externalRefs("https://react.dev/errors/418")).toEqual([]);
    // And an ordinary line comment is not a finding: the host part must
    // look like a host, so `// text` and `//` alone never match.
    expect(
      externalRefs("// Ruling D: see api.py:95-114\n  // a plain comment\n"),
    ).toEqual([]);
    expect(externalRefs("`${scheme}://${window.location.host}/api/ws`")).toEqual(
      [],
    );
  });

  it("source contains no external URL", () => {
    const offenders: string[] = [];
    for (const file of walk(SRC)) {
      if (!/\.(tsx?|css|html)$/.test(file)) continue;
      const found = externalRefs(readFileSync(file, "utf8"));
      if (found.length) offenders.push(`${file}: ${found.join(", ")}`);
    }
    expect(offenders).toEqual([]);
  });

  it("build output contains no external URL", (ctx) => {
    let files: string[];
    try {
      files = walk(BUILD);
    } catch {
      // Nothing built yet. Skip rather than return: a silent pass here would
      // report the no-CDN guarantee as verified when nothing was scanned.
      ctx.skip();
      return;
    }
    const offenders: string[] = [];
    for (const file of files) {
      if (!/\.(js|css|html)$/.test(file)) continue;
      const found = externalRefs(readFileSync(file, "utf8"));
      if (found.length) offenders.push(`${file}: ${found.join(", ")}`);
    }
    expect(offenders).toEqual([]);
  });

  it("index.html links no external stylesheet or script", () => {
    const html = readFileSync(resolve(__dirname, "../index.html"), "utf8");
    expect(html).not.toMatch(/fonts\.googleapis|fonts\.gstatic|cdn\./);
  });
});
