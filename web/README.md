# PriceCourt web app

Static page served by GitHub Pages from `docs/` (live link in the main README).
It reads the deployed PriceCourt contract on GenLayer studionet with `genlayer-js`
and can run a full live deal (two throwaway agents funded with free studionet GEN).

Rebuild the bundle after editing `app.src.js`:

```bash
npm i genlayer-js@1.1.8 esbuild
npx esbuild web/app.src.js --bundle --minify --format=esm --target=es2020 --outfile=docs/app.js
```

The contract address is set in `docs/index.html` (`window.PRICECOURT_CONTRACT`).
