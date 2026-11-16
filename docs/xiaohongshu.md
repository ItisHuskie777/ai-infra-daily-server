# Xiaohongshu (RedNote) source

Adapter: `app/sources/xiaohongshu.py` (class `XiaohongshuAdapter`). Kind `xiaohongshu`, modes
`following` and `user`. Shared helpers (login-wall detection, count parsing) live in
`app/sources/_social.py`.

Xiaohongshu has no public API and the owner follows 700+ accounts, so the adapter never
enumerates accounts. It renders the owner's own logged-in feed in a headless Chromium driven by
Playwright (`app/adapters/browser.py`), reads the note cards, then opens the first 20 notes to read
the body and the top comments. It is a best-effort source: a failure here never aborts the digest
(FR1.5, QR2).

The browser is only started when a cookie is configured. Install it once on the server with
`uv run playwright install chromium` (and `uv run playwright install-deps chromium` for the system
libraries, which needs root).

## What it fetches

| Mode        | Page rendered                                                    | Notes                                  |
| ----------- | ---------------------------------------------------------------- | -------------------------------------- |
| `following` | `https://www.xiaohongshu.com/explore?channel_id=homefeed.follow` | The "关注" channel of the home feed    |
| `user`      | `https://www.xiaohongshu.com/user/profile/{user_id}`             | Requires `user_id` in the source config |

For each run:

1. The feed page is rendered with the session cookies and scrolled three times. `fetch`
   returns `not_configured` before rendering when there is no browser, no cookie, or mode
   `user` without a `user_id`.
2. An extract script (a JavaScript expression evaluated in the page) reads note cards from
   `window.__INITIAL_STATE__` (`feed.feeds` or `user.notes`), falling back to the rendered
   `section.note-item` cards. The returned list is validated with a Pydantic `TypeAdapter`.
3. The first 20 notes are opened (two at a time) via their feed link, which carries the
   `xsec_token` the site demands. The extract script reads
   `__INITIAL_STATE__.note.noteDetailMap[noteId]` (body, author, time, like and comment counts,
   comment list), falling back to `#detail-title`, `#detail-desc` and `.comment-item`.
4. Each note becomes a `SourceItem` of kind `commentary`. The canonical URL is
   `https://www.xiaohongshu.com/explore/{noteId}` with the `xsec_*` parameters stripped, and the
   item id is its sha-256. Comments are sorted by likes and cut to `comment_limit`. Language is
   detected from the title and body with `zh` as the hint when detection is inconclusive.
5. A note page that fails to render or looks like a login wall still produces an item from the
   feed card alone (no body, no comments).

## Obtaining the cookie

The adapter needs the raw `Cookie` request header of a logged-in browser session, stored as
`XIAOHONGSHU_COOKIE` in `.env` and handed to `XiaohongshuAdapter` when `app/sources/registry.py`
builds it. An empty value means the source is off.

1. Log in at `https://www.xiaohongshu.com` in a desktop browser (scan the QR code with the app).
2. Open the developer tools, Network tab, and reload the page.
3. Select any request to `www.xiaohongshu.com` or `edith.xiaohongshu.com`, open Request Headers
   and copy the whole value of the `Cookie` header. The useful entries are `a1`, `webId`,
   `web_session`, `xsecappid` and `websectiga`; copying everything is fine.
4. Store it: set `XIAOHONGSHU_COOKIE=<value>` in `.env` on the server (the value contains `;` and
   `=`, so keep it on one line without quotes) and restart the service.

Use a dedicated account if possible; an automated session can be logged out or rate-limited.

## Expected breakage modes

| Symptom                                                    | Result                                    | Likely cause                                                                                                          |
| ---------------------------------------------------------- | ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `not_configured`                                           | Source skipped                            | No browser (both cookies empty), `XIAOHONGSHU_COOKIE` empty, or mode `user` without `user_id`                         |
| `blocked`                                                  | Source skipped, reported in Source health | Session expired (QR login page), captcha or "安全验证" slider, redirect to `/login`                                   |
| `fetch_failed`                                             | Source skipped                            | Page timeout (30 s), navigation failure, Chromium not installed                                                       |
| `parse_failed`                                             | Source skipped                            | The feed extract script returned something that is not a list of cards: the DOM or `__INITIAL_STATE__` layout changed |
| Items without text or comments                             | Digest still ships                        | Note pages blocked or re-laid out while the feed still works                                                          |
| Items from the recommend feed instead of followed accounts | Wrong content                             | The `channel_id` for the following tab changed                                                                        |

The site changes its markup often. Every selector, URL and state path lives in the `SELECTORS`
mapping at the top of the adapter; fix it there. The extraction scripts are built from that
mapping, so nothing else needs to change.

## Refreshing the cookie

`web_session` typically lasts a few weeks. When the digest footer reports `blocked` for this
source on two consecutive days, repeat the steps above, update `XIAOHONGSHU_COOKIE` in `.env` and
restart the service. Rotating the cookie also helps when the feed suddenly comes back empty, which
is how a silently logged-out session looks.

## Limits

- At most 20 note pages per run, two in flight at a time, one feed render: around 21 page loads
  a day, far below any platform limit (QR1).
- Content is read for the owner only and never republished.
