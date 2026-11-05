# Xiaohongshu (RedNote) source

Adapter: `src/sources/adapters/xiaohongshu.ts`. Kind `xiaohongshu`, modes `following` and `user`.

Xiaohongshu has no public API and the owner follows 700+ accounts, so the adapter never
enumerates accounts. It renders the owner's own logged-in feed in Cloudflare Browser Rendering,
reads the note cards, then opens the first 20 notes to read the body and the top comments. It is a
best-effort source: a failure here never aborts the digest (FR1.5, QR2).

## What it fetches

| Mode        | Page rendered                                                    | Notes                                  |
| ----------- | ---------------------------------------------------------------- | -------------------------------------- |
| `following` | `https://www.xiaohongshu.com/explore?channel_id=homefeed.follow` | The "关注" channel of the home feed    |
| `user`      | `https://www.xiaohongshu.com/user/profile/{userId}`              | Requires `userId` in the source config |

For each run:

1. The feed page is rendered with the session cookies and scrolled three times.
2. An extract script reads note cards from `window.__INITIAL_STATE__` (`feed.feeds` or
   `user.notes`), falling back to the rendered `section.note-item` cards.
3. The first 20 notes are opened (two at a time) via their feed link, which carries the
   `xsec_token` the site demands. The extract script reads
   `__INITIAL_STATE__.note.noteDetailMap[noteId]` (body, author, time, like and comment counts,
   comment list), falling back to `#detail-title`, `#detail-desc` and `.comment-item`.
4. Each note becomes a `SourceItem` of kind `commentary`. The canonical URL is
   `https://www.xiaohongshu.com/explore/{noteId}` with the `xsec_*` parameters stripped, and the
   item id is its sha-256. Comments are sorted by likes and cut to `commentLimit`. Language is
   `zh` when the text contains Han characters.
5. A note page that fails to render or looks like a login wall still produces an item from the
   feed card alone (no body, no comments).

## Obtaining the cookie

The adapter needs the raw `Cookie` request header of a logged-in browser session, stored as the
`XIAOHONGSHU_COOKIE` secret and exposed to the code as `ctx.secrets.xiaohongshuCookie`.

1. Log in at `https://www.xiaohongshu.com` in a desktop browser (scan the QR code with the app).
2. Open the developer tools, Network tab, and reload the page.
3. Select any request to `www.xiaohongshu.com` or `edith.xiaohongshu.com`, open Request Headers
   and copy the whole value of the `Cookie` header. The useful entries are `a1`, `webId`,
   `web_session`, `xsecappid` and `websectiga`; copying everything is fine.
4. Store it: `npx wrangler secret put XIAOHONGSHU_COOKIE` and paste the value.

Use a dedicated account if possible; an automated session can be logged out or rate-limited.

## Expected breakage modes

| Symptom                                                    | Result                                    | Likely cause                                                                                                          |
| ---------------------------------------------------------- | ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `not_configured`                                           | Source skipped                            | Browser Rendering binding absent, cookie secret empty, or mode `user` without `userId`                                |
| `blocked`                                                  | Source skipped, reported in Source health | Session expired (QR login page), captcha or "安全验证" slider, redirect to `/login`                                   |
| `fetch_failed`                                             | Source skipped                            | Page timeout (30 s), navigation failure, Browser Rendering limits                                                     |
| `parse_failed`                                             | Source skipped                            | The feed extract script returned something that is not a list of cards: the DOM or `__INITIAL_STATE__` layout changed |
| Items without text or comments                             | Digest still ships                        | Note pages blocked or re-laid out while the feed still works                                                          |
| Items from the recommend feed instead of followed accounts | Wrong content                             | The `channel_id` for the following tab changed                                                                        |

The site changes its markup often. Every selector, URL and state path lives in the `SELECTORS`
object at the top of the adapter; fix it there and the unit tests still pass because they feed
canned extraction results through the fake browser.

## Refreshing the cookie

`web_session` typically lasts a few weeks. When the digest footer reports `blocked` for this
source on two consecutive days, repeat the steps above and run
`npx wrangler secret put XIAOHONGSHU_COOKIE` again; no deploy is needed. Rotating the cookie also
helps when the feed suddenly comes back empty, which is how a silently logged-out session looks.

## Limits

- At most 20 note pages per run, two in flight at a time, one feed render: around 21 page loads
  a day, far below any platform limit (QR1).
- Content is read for the owner only and never republished.
