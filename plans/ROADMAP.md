# MealBot Implementation Roadmap

## Dependency Graph & Execution Order

```
Phase 1: Wire the Skeleton
  #1  Foundation (schema, models, config)
   ├──> #2  Tracer wire (stubs + CLI)          <- demoable after this
   └──> #3  Prompt templates

Phase 2: Replace Stubs (always demoable)
  #4  Recipe store        <- depends on #1
  #5  Pantry manager      <- depends on #1, #3
  #6  Meal parser         <- depends on #3, #4
  #7  Consolidator        <- depends on #3, #6
  #8  CLI hardening       <- depends on #4-#7   <- full pipeline works from terminal

Phase 3: User Interfaces
  #9  Web: layout + inventory   <- depends on #4, #5
  #10 Web: remaining pages      <- depends on #9, #7
  #11 Discord bot               <- depends on #4-#7
```

## Parallelism Windows

| After completing | Can start in parallel |
|------------------|-----------------------|
| #1 (foundation)  | #2, #3, #4            |
| #1 + #3          | #5                    |
| #3 + #4          | #6                    |
| #6               | #7                    |
| #4 + #5          | #9                    |
| #4-#7            | #8, #11              |
| #9 + #7          | #10                   |

## Tracer-Code Properties

- **After #2**: the system runs end-to-end with hardcoded data
- **#4-#7**: each replaces one stub -- the system never breaks between issues
- **#8**: proves the full real pipeline works before building any UI
- **#9-#11**: UI layers on top of proven backend, can be parallelized

## Issue Index

| # | File | Title | Phase | Priority |
|---|------|-------|-------|----------|
| 1 | `001-foundation-schema-models-config.json` | Foundation: schema, models, config | 0-foundation | P0 |
| 2 | `002-tracer-wire-stubs-cli.json` | Tracer wire: stubs + CLI pipeline | 1-tracer | P0 |
| 3 | `003-prompt-templates.json` | Prompt templates: Claude .txt files | 1-tracer | P0 |
| 4 | `004-recipe-store.json` | Recipe store: SQLite DAL + fuzzy matching | 2-core | P0 |
| 5 | `005-pantry-manager.json` | Pantry manager: inventory + restock queue | 2-core | P0 |
| 6 | `006-meal-parser.json` | Meal parser: Claude-powered decomposition | 2-core | P0 |
| 7 | `007-consolidator.json` | Consolidator: ingredient merging | 2-core | P0 |
| 8 | `008-cli-hardening.json` | CLI hardening: real modules wired | 2-core | P1 |
| 9 | `009-web-dashboard-layout-inventory.json` | Web: Flask + layout + inventory page | 3-ui | P1 |
| 10 | `010-web-recipes-shopping-remaining.json` | Web: recipes, shopping, pantry, brands | 3-ui | P1 |
| 11 | `011-discord-bot.json` | Discord bot: slash commands + NL parsing | 3-ui | P2 |
