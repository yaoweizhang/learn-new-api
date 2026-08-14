# Architecture Overview

The tutorial builds up a single FastAPI app across 16 chapters. End state:

```
Client → [/v1/chat/completions]
         │
         ▼
   ┌──────────────┐
   │ Auth (s05)   │  ← API key → user_id
   │ Rate (s08)   │  ← token bucket
   └──────┬───────┘
          ▼
   ┌──────────────┐
   │ Token count  │  (s06)
   │ Pre-consume  │  (s07)
   └──────┬───────┘
          ▼
   ┌──────────────┐
   │ Adapter      │  (s04)  pick provider (s10), retry (s13), cache (s12)
   └──────┬───────┘
          ▼
   Upstream (OpenAI / Claude / Gemini)
          │
          ▼
   Stream back + log (s11) + metrics (s16)
```

Each chapter adds one box. The boxes never move.