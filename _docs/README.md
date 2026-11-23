# Visual documentation

These are static, standalone HTML pages. Open the rendered `.html` files in a
browser, or read them on [GitHub Pages](https://sebi06.github.io/napari-virtual-reader/).
GitHub's repository file viewer shows HTML source; it does not run these pages.

- [Plugin workflow](plugin-workflow.html): combined dock, native S navigation,
  release-only dimension dragging, multidimensional canvases, metadata and VZIP.
- [CZI tile assembly](czi-tile-assembly.html): native tile placement and chunk reads,
  plus the separate scene-canvas overview path and bounded cache.

The guides describe version 0.0.4 and the current source checkout.
Matching scenes retain one layer per acquisition channel in both
display modes, including T/Z. Generated overviews share a 64 MiB cache; native
pixels remain lazy. A selected composed canvas defaults to loading on thumb
release, with an opt-in live-drag checkbox. Unsupported or nonmatching geometries
retain separate layers. Historical release notes and older labelled screenshots
describe their original releases rather than the current checkout.

## Editing and rendering

Edit the corresponding `.src.html` files, then regenerate the `.html` files with
the [visual-explainer](https://github.com/nicobailon/visual-explainer) skill.
The source marker `data-ve-kit` is expanded into inline styles and scripts, plus
Google Fonts stylesheet links. Fonts have fallbacks when offline.
The rendered pages include static SVG diagrams, so diagrams need no Mermaid CDN
or GitHub Markdown rendering at viewing time. Interactive steppers use the
embedded script.

With that skill installed locally, run each command with its installation path:

```powershell
node <visual-explainer-directory>\kit\render.mjs _docs\plugin-workflow.src.html --open
node <visual-explainer-directory>\kit\render.mjs _docs\czi-tile-assembly.src.html --open
node <visual-explainer-directory>\kit\render.mjs _docs\index.src.html --open
```

Keep the `.mmd` sources identical to the Mermaid blocks in the corresponding
`.src.html` files. Fix every renderer error and inspect its generated sheets at
desktop and mobile widths. Commit the sources and regenerated HTML together;
preview sheets are local validation output and are not published.

## GitHub Pages

The [Pages workflow](../.github/workflows/docs-pages.yml) uploads only the three
rendered HTML files from this directory. It serves them at the project site root,
not under `/_docs/`. Relative links therefore work both locally and under
`https://sebi06.github.io/napari-virtual-reader/`.

The repository must have **Settings > Pages > Build and deployment > Source**
set to **GitHub Actions**. After committing and pushing these files to `main`,
the workflow deploys them. It can also be started manually on `main`.
The website is not updated merely by editing local files.

There is no Jekyll build: the workflow stages the files explicitly and includes
`.nojekyll`. This avoids Jekyll excluding the underscore-prefixed source folder.
No microscopy images, local file paths, source datasets or Python environment
are required to serve these guides.
