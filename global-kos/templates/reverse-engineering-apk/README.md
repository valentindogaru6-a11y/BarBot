# Template: Reverse Engineering APK

Use for APK analysis, decompilation, smali work, obfuscated apps, and runtime tracing.

Favor summaries for:

- workspace layout
- split APKs and build outputs
- manifest and launcher path
- decompilation layout
- runtime or patching baseline

Domain pattern to seed locally:

- start from stable anchors such as manifest entries, resources, routes, layouts, runtime artifacts, and known models before opening arbitrary obfuscated classes
