Task 0.1: complete (commits 227b92f..312c7a4, review found dead code in common/json.py, fix subagent removed unused unmarshal+StringIO; verified import still works; final commit 312c7a4)
Task 0.2: complete (commits afc7260..fc2950f, review approved with minor unused-import findings; fix subagent cleaned up; final commit fc2950f)
Task 0.3: complete (commits 8e88fef..5ec3235, review approved; make unavailable on Windows host, smoke.sh syntax-checked; final commit 5ec3235)
Task 1.1: complete (commits e2f02ac..646bd2e, review found dead forward_request helper; fix inlined + removed; 2/2 tests passing; final commit 646bd2e)
Task 1.2: complete (commits 37090ac..c36b54d, review approved; implementer fixed brief bug in unmarshal_str signature; 2/2 tests passing; final commit c36b54d)
Task 1.3: complete (commits 82cf9c0..fdd8c38, review approved with minor unused-imports findings; 2/2 tests passing; full suite 6/6; final commit fdd8c38)
Task 2.1: complete (commits d2e5a6e..698cdec, review approved; brief bug in gemini adapter fixed by implementer; 4/4 tests passing; full suite 10/10; final commit 698cdec)
Task 2.2: complete (commits 5205c7f..4a76636, review approved; brief bug in register_key param order fixed by implementer; 3/3 tests; final commit 4a76636)
Task 3.1: complete (commits a6624fa..7e3f791, review approved; 2/2 tests; final commit 7e3f791)
Task 3.2: complete (commits a6624fa..4e312e1, 3/3 tests; final commit 4e312e1)
Task 3.3: complete (commits ..., 1/1 tests passing; final commit 62dfdbb)
Task 4.1: complete (commits 62dfdbb..ca9a358, review approved; 4/4 tests passing; implementer fixed brief bugs: Windows reset_db PermissionError, README paths to lowercase controller/model files; final commit ca9a358)
Task 4.2: complete (commits ca9a358..221f3a5, review approved with minor: unused `from typing import Annotated` import in code.py:9; 2/2 tests passing; implementer fixed brief bug: Starlette mount order — `app.mount("/", s09_app)` must come AFTER admin routes, not before; final commit 221f3a5. Minor for final review: remove unused Annotated import.)
Task 4.3: complete (commits 221f3a5..41c6bc4, review approved; 1/1 tests passing; implementer fixed FOUR brief bugs (bcrypt typo, query_params vs JSON body, double-prefix path, JWT vs API key+quota) plus two TestClient/middleware adaptations (`_drain_now`, removed StreamingResponse guard); final commit 41c6bc4)
Task 5.1: complete (commits 41c6bc4..89b89a8, review found Important: README body-replay claim was empirically false — Starlette's BaseHTTPMiddleware caches body in request._body; fix subagent corrected trade-off section (89b89a8→8a1cf15) AND how-it-works key point (8a1cf15→1b392f5); 1/1 tests passing; final commit 1b392f5)
Task 5.2: complete (commits 1b392f5..10430e5, review approved with minor: dead `provider =` assignment in code.py:60; fix subagent removed assignment (10430e5→8ccf1f6); 1/1 tests passing; final commit 8ccf1f6)
Task 6.1: complete (commits 8ccf1f6..02bce9d, review approved; 2/2 tests passing; implementer fixed TWO brief bugs: Starlette ≥0.30 TemplateResponse signature change, and TestClient follows 302 redirects so 302→200 fails assertion — changed to 401; final commit 02bce9d)
Task 6.2: complete (commits 02bce9d..6d79986, review approved; 1/1 tests passing; implementer fixed brief bug: `/healthz` was AFTER `app.mount(...)` in brief, moved to before so the route is reachable; final commit 6d79986)
Task 6.3: complete (commits 6d79986..fa424c5, review found Important: README/code comment taught wrong reason for dual-path middleware match (both paths ARE reachable, just via different handlers), and the counter has no test coverage; fix subagent corrected wording + added 3rd counter-increment test + fixed stale content-type + clarified HELP/TYPE-vs-sample; 3/3 tests passing; final commit fa424c5)
Task 7.1: complete (commits fa424c5..5377909, review approved; 2/2 tests passing; implementer fixed FIVE brief bugs (typed-parameter Depends, issue_token location, top_up signature, create_user signature, _pick key gating) + one Important: Prometheus metric naming collision with s16 — fixed by prefixing s_full metrics with `learn_new_api_s_full_*`; final commit 5377909)

ALL 21 IMPLEMENTATION TASKS COMPLETE.

Task 31 (whole-branch review + fixes): complete. Whole-branch review (opus) found 2 Critical + 9 Important + 11 Minor issues. Single fix subagent addressed all 9 Critical+Important: 05a3209 (s05 test fixture leak), edbfe5f (s13 chat bypass closed with auth+rate+quota), c3d0036 (s01-s08 Chinese translation), 7eb03a1 (s04-s06 broken links fixed), 41410c6 (s07-s16 Previous/Next), 4a4b70e (s05 Principal docstring note), 0e16881 (s11 docstring fix), 3df9137 (unused imports dropped), ff7b00c (s15 docker-compose Redis removed). 37/37 tests passing.

## Minor findings for final review
- s10_channel_management/code.py:9 — unused `from typing import Annotated` import (Task 4.2 review)
- s11_call_logs/code.py:27 — unused `from starlette.responses import StreamingResponse` import (Task 4.3 review)
- s11_call_logs/log_store.py:5 — stale docstring says "0.5s" but code sleeps 0.1s (Task 4.3 review)
- **Important for s_full**: s11 introduces `/v1/v1/chat/completions` double-prefix path because s09 mounts s08 at `/v1` and s08's chat route is already `/v1/chat/completions`. The mount chain s11→s10→s09→s08 produces `/v1/v1/chat/completions` externally. This will likely break s_full integration unless the mount path is fixed in a follow-up. Recommended fix: in s09_user_system/code.py, change `app.mount("/v1", s08_app)` to `app.mount("/", s08_app)` so s08's routes keep their natural `/v1/chat/completions` path. This is a brief bug from earlier chapters (s09 onwards) that compounds through s10, s11, and s_full. (Task 4.3 review)
- **Important regression**: s13 owns the `/v1/chat/completions` route locally, so s11's LogMiddleware never fires for chat calls. Documented in README 取舍. Recommend s_full lifts LogMiddleware to the top of the mount chain so all chat calls are logged. (Task 5.2 review)
