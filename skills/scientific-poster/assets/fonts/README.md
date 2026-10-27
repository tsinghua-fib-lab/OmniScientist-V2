# Mathematical font resources

STIX Two Math 2.13 (`v2.13b171`) comes from the [official STIX Fonts repository](https://github.com/stipub/stixfonts). See [OFL.txt](OFL.txt) for its license.

- `STIXTwoMath-Regular.ttf`: upstream `fonts/static_ttf/STIXTwoMath-Regular.ttf`, preserving its original names, glyphs and MATH table.
- `STIXTwoMath-Regular.eot`: converted from that TTF with `ttf2eot` 3.1.0, retaining the complete font data for native document embedding.

TTF SHA-256: `49b22767985171bce579307b21d98f4d35012fa39e316acc4ffcab8644dfb69b`

EOT SHA-256: `746dc978f33d8b72815a87e723351d4377fe9e0dbab5ce77965c53fd67c41219`

`posterlib.math_fonts` provides the shared font name, resource paths and cached offline CSS. HTML templates retain the font name; `embed_assets` adds font styles to rendered documents containing MathML, and `tokenize_embedded_images` removes those styles at the authoring boundary. The font ships with the skill and works offline.
