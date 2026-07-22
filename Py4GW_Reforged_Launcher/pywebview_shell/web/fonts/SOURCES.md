# Bundled font sources

All 4 fonts are Google Fonts, licensed under the SIL Open Font License
1.1 (free to bundle/redistribute, including in closed-source apps, no
attribution required in the UI). Latin-subset `.woff2` files only,
fetched directly from `fonts.gstatic.com` at build time — the app itself
never fetches fonts over the network at runtime.

| File | Family | Weight(s) | Source |
|---|---|---|---|
| `Sora-Variable.woff2` | Sora | 400-800 (variable) | fonts.googleapis.com/css2?family=Sora, v17 |
| `Manrope-Variable.woff2` | Manrope | 400-800 (variable) | fonts.googleapis.com/css2?family=Manrope, v20 |
| `ChakraPetch-400.woff2` | Chakra Petch | 400 | fonts.googleapis.com/css2?family=Chakra+Petch, v13 |
| `ChakraPetch-600.woff2` | Chakra Petch | 600 | fonts.googleapis.com/css2?family=Chakra+Petch, v13 |
| `ChakraPetch-700.woff2` | Chakra Petch | 700 | fonts.googleapis.com/css2?family=Chakra+Petch, v13 |
| `Spectral-400.woff2` | Spectral | 400 | fonts.googleapis.com/css2?family=Spectral, v15 |
| `Spectral-600.woff2` | Spectral | 600 | fonts.googleapis.com/css2?family=Spectral, v15 |
| `Spectral-700.woff2` | Spectral | 700 | fonts.googleapis.com/css2?family=Spectral, v15 |

Sora and Manrope ship as variable fonts (one file spans the whole
400-800 weight range via the `wght` axis); Chakra Petch and Spectral
ship as static per-weight files since Google doesn't serve those two
as variable fonts. Only the weights this app's CSS actually uses
(400/600/700, plus 800 for Sora/Manrope's variable range since it's the
same file regardless) were fetched — not every weight Google offers.

To refresh/re-fetch: `curl -A "<modern browser UA>" "https://fonts.googleapis.com/css2?family=<Name>:wght@<weights>&display=swap"` returns the current CSS with real `.woff2` URLs; a browser UA is required or Google serves older `.ttf`/`.eot` formats instead.
